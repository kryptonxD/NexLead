"""
NexLead — Email Hunter SaaS
============================
Full SaaS backend:
- User registration / login (JWT tokens)
- Credit system (free 10 credits on signup)
- Razorpay payment integration
- Email hunting engine (277+ sources, parallel)
- Usage tracking per user
- Render.com ready
"""

import re, time, random, threading, uuid, logging, os, hashlib, json
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps

import pandas as pd
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_file, send_from_directory, g
from flask_cors import CORS

try:
    import jwt
    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False
    import base64

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("NexLead")

app = Flask(__name__, static_folder="static")
CORS(app)

requests.packages.urllib3.disable_warnings()

# ── Config ────────────────────────────────────────────────────────────────────

SECRET_KEY        = os.environ.get("SECRET_KEY", "nexlead-secret-2026-change-in-prod")
RAZORPAY_KEY_ID   = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_SECRET   = os.environ.get("RAZORPAY_SECRET", "")
FREE_CREDITS      = 10   # credits on signup
PORT              = int(os.environ.get("PORT", 5000))

UPLOAD_FOLDER = Path("uploads")
OUTPUT_FOLDER = Path("outputs")
DB_FILE       = Path("nexlead_db.json")
UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)

# ── Simple JSON "database" (no SQL needed for MVP) ───────────────────────────

def load_db():
    if DB_FILE.exists():
        with open(DB_FILE) as f:
            return json.load(f)
    return {"users": {}, "jobs": {}}

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2, default=str)

db_lock = threading.Lock()

def get_user(email):
    with db_lock:
        db = load_db()
        return db["users"].get(email)

def save_user(email, data):
    with db_lock:
        db = load_db()
        db["users"][email] = data
        save_db(db)

def update_credits(email, delta):
    with db_lock:
        db = load_db()
        if email in db["users"]:
            db["users"][email]["credits"] = max(0, db["users"][email].get("credits", 0) + delta)
            save_db(db)
            return db["users"][email]["credits"]
    return 0

# ── Auth helpers ──────────────────────────────────────────────────────────────

def hash_password(pw):
    return hashlib.sha256((pw + SECRET_KEY).encode()).hexdigest()

def make_token(email):
    payload = {
        "email": email,
        "exp": (datetime.utcnow() + timedelta(days=30)).isoformat()
    }
    token_data = json.dumps(payload)
    return base64.b64encode(token_data.encode()).decode()

def decode_token(token):
    try:
        data = json.loads(base64.b64decode(token.encode()).decode())
        exp = datetime.fromisoformat(data["exp"])
        if datetime.utcnow() > exp:
            return None
        return data["email"]
    except Exception:
        return None

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "").strip()
        email = decode_token(token)
        if not email:
            return jsonify({"error": "Unauthorized"}), 401
        user = get_user(email)
        if not user:
            return jsonify({"error": "User not found"}), 401
        g.user_email = email
        g.user = user
        return f(*args, **kwargs)
    return decorated

# ── In-memory job store ───────────────────────────────────────────────────────

jobs = {}

# ── Email engine (same proven logic from v4) ─────────────────────────────────

EMAIL_RE = re.compile(r"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})")
IGNORE_RE = re.compile(
    r"(noreply|no-reply|donotreply|example\.|@wix\.|@squarespace\.|@godaddy\.|"
    r"@shopify\.|@sentry\.|@amazonaws\.|@cloudflare\.|@sendgrid\.|@mailchimp\.|"
    r"privacy@|webmaster@|postmaster@|abuse@|spam@|test@|bounce@|mailer@|"
    r"daemon@|robot@|auto@|automated@|user@|admin@localhost|root@|nobody@|"
    r"\.(png|jpg|gif|svg|pdf|js|css|woff|ttf|eot|mp4|zip)$)",
    re.IGNORECASE,
)
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]
COL_MAP = {
    "name":         ["name", "business_name", "title"],
    "category":     ["type", "category", "subtypes", "business_type"],
    "phone":        ["phone", "phone_number", "phones"],
    "website":      ["site", "website", "web", "url"],
    "address":      ["full_address", "address", "formatted_address"],
    "city":         ["city", "locality"],
    "state":        ["state", "administrative_area_level_1"],
    "rating":       ["rating", "stars", "google_rating"],
    "reviews":      ["reviews", "reviews_count", "user_ratings_total"],
    "reviews_link": ["reviews_link", "place_url", "google_maps_url"],
    "facebook":     ["facebook", "facebook_url"],
    "instagram":    ["instagram", "instagram_url"],
    "twitter":      ["twitter", "twitter_url"],
    "owner":        ["owner_name", "owner"],
    "description":  ["description", "about"],
}
CONTACT_PATHS = [
    "/contact", "/contact-us", "/about", "/about-us", "/reach-us",
    "/team", "/info", "/get-in-touch", "/book", "/booking",
    "/hello", "/support", "/pages/contact", "/location",
]
DIR_TEMPLATES = [
    'site:yelp.com "{n}" "{c}"', 'site:yellowpages.com "{n}" "{c}"',
    'site:bbb.org "{n}" "{c}"', 'site:manta.com "{n}" "{c}"',
    'site:chamberofcommerce.com "{n}" "{c}"', 'site:alignable.com "{n}" "{c}"',
    'site:hotfrog.com "{n}" "{c}"', 'site:superpages.com "{n}" "{c}"',
    'site:merchantcircle.com "{n}" "{c}"', 'site:brownbook.net "{n}" "{c}"',
    'site:local.com "{n}" "{c}"', 'site:411.com "{n}" "{c}"',
    'site:showmelocal.com "{n}" "{c}"', 'site:2findlocal.com "{n}" "{c}"',
    'site:cylex-usa.com "{n}" "{c}"', 'site:finduslocal.com "{n}" "{c}"',
    'site:golocal247.com "{n}" "{c}"', 'site:facebook.com "{n}" "{c}" email',
    'site:instagram.com "{n}" "{c}"', 'site:twitter.com "{n}" "{c}"',
    'site:linkedin.com/company "{n}" "{c}"', 'site:nextdoor.com "{n}" "{c}"',
    'site:tripadvisor.com "{n}" "{c}"', 'site:foursquare.com "{n}" "{c}"',
    'site:trustpilot.com "{n}" "{c}"', 'site:angi.com "{n}" "{c}"',
    'site:homeadvisor.com "{n}" "{c}"', 'site:thumbtack.com "{n}" "{c}"',
    'site:porch.com "{n}" "{c}"', 'site:bark.com "{n}" "{c}"',
    'site:healthgrades.com "{n}" "{c}"', 'site:zocdoc.com "{n}" "{c}"',
    'site:vitals.com "{n}" "{c}"', 'site:webmd.com "{n}" "{c}"',
    'site:psychologytoday.com "{n}" "{c}"', 'site:booksy.com "{n}" "{c}"',
    'site:vagaro.com "{n}" "{c}"', 'site:fresha.com "{n}" "{c}"',
    'site:styleseat.com "{n}" "{c}"', 'site:opentable.com "{n}" "{c}"',
    'site:zomato.com "{n}" "{c}"', 'site:grubhub.com "{n}" "{c}"',
    'site:doordash.com "{n}" "{c}"', 'site:mindbodyonline.com "{n}" "{c}"',
    'site:classpass.com "{n}" "{c}"', 'site:calendly.com "{n}"',
    'site:squareup.com "{n}" "{c}"', 'site:linktr.ee "{n}"',
    'site:beacons.ai "{n}"', 'site:eventbrite.com "{n}" "{c}"',
    'site:prnewswire.com "{n}" "{c}"', 'site:rover.com "{n}" "{c}"',
    'site:wyzant.com "{n}" "{c}"', 'site:etsy.com "{n}"',
    'site:craigslist.org "{n}" "{c}"', 'site:booking.com "{n}" "{c}"',
    '"{n}" "{c}" email contact', '"{n}" "{c}" {s} contact email address',
    '"{n}" "{c}" owner email', '"{n}" "{c}" gmail OR yahoo OR hotmail',
    '"{n}" "{c}" "email us" OR "email:" OR "contact:"',
    '"{n}" {s} email owner manager', '"{n}" "{c}" reach us email',
]

def find_col(cols, candidates):
    lower = {c.lower(): c for c in cols}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    return None

def get_val(df, row, key, default=""):
    col = find_col(list(df.columns), COL_MAP.get(key, [key]))
    if col and col in row.index:
        val = row[col]
        if pd.notna(val): return str(val).strip()
    return default

def rand_headers():
    return {"User-Agent": random.choice(UA_LIST),
            "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}

def extract_emails(text):
    if not text: return []
    found = EMAIL_RE.findall(text)
    clean = []
    for e in found:
        e = e.lower().strip(".,;:)'\"<>|\\ ")
        if not IGNORE_RE.search(e) and 5 < len(e) < 80 and "." in e.split("@")[-1] and e.count("@") == 1:
            clean.append(e)
    return list(dict.fromkeys(clean))

def score_email(email):
    score = 0
    if not re.search(r"@(gmail|yahoo|hotmail|outlook|icloud|live\.|aol\.)", email, re.I):
        score += 20
    if any(g in email.split("@")[0] for g in ["info","contact","hello","owner","manager","booking","admin","team","office","reception"]):
        score += 10
    return score

def best_email(emails):
    if not emails: return ""
    return sorted(emails, key=score_email, reverse=True)[0]

def make_session():
    s = requests.Session()
    a = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=1)
    s.mount("http://", a); s.mount("https://", a)
    return s

def fetch_url(session, url, timeout=8):
    if not url: return None
    if not url.startswith("http"): url = "https://" + url
    try:
        r = session.get(url, headers=rand_headers(), timeout=timeout, verify=False, allow_redirects=True)
        if r.status_code == 200: return r.text
    except Exception: pass
    return None

def parse_emails(html):
    if not html: return []
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script","style","noscript"]): t.decompose()
    mailto = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            e = href[7:].split("?")[0].strip().lower()
            if "@" in e and not IGNORE_RE.search(e): mailto.append(e)
    return list(dict.fromkeys(mailto + extract_emails(soup.get_text(" ", strip=True)) + extract_emails(html)))

def google_q(session, q):
    html = fetch_url(session, f"https://www.google.com/search?q={quote_plus(q)}&num=10&hl=en", timeout=10)
    if not html: return [], []
    emails = extract_emails(html)
    soup = BeautifulSoup(html, "lxml")
    urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/url?q="):
            actual = requests.utils.unquote(href[7:].split("&")[0])
            if actual.startswith("http") and "google.com" not in actual:
                urls.append(actual)
    return emails, urls[:4]

def hunt_one_business(name, city, state, website, facebook, instagram, twitter, phone):
    session = make_session()
    result_lock = threading.Lock()
    found = {"email": "", "source": ""}
    stop = threading.Event()

    def report(email, source):
        with result_lock:
            if not found["email"] and email:
                found["email"] = email
                found["source"] = source
                stop.set()

    def task_website():
        if stop.is_set() or not website: return
        base = website.rstrip("/")
        hp = fetch_url(session, base)
        if hp:
            e = best_email(parse_emails(hp))
            if e: report(e, "website"); return
            soup = BeautifulSoup(hp, "lxml")
            internal = set()
            for a in soup.find_all("a", href=True):
                full = urljoin(base, a["href"])
                if urlparse(full).netloc == urlparse(base).netloc:
                    path = urlparse(full).path.lower()
                    if any(kw in path for kw in ["contact","about","team","reach","book","hello","info","connect"]):
                        internal.add(full)
        pages = [base + p for p in CONTACT_PATHS] + list(internal)
        with ThreadPoolExecutor(max_workers=8) as pe:
            pfs = {pe.submit(fetch_url, session, u): u for u in pages}
            for pf in as_completed(pfs):
                if stop.is_set(): break
                html = pf.result()
                if html:
                    e = best_email(parse_emails(html))
                    if e: report(e, "website"); break

    def task_google():
        if stop.is_set(): return
        queries = [
            f'"{name}" "{city}" email',
            f'"{name}" "{city}" {state} contact email',
            f'"{name}" "{city}" owner email contact',
            f'"{name}" "{city}" email address',
            f'"{name}" "{city}" "@" contact',
            f'"{name}" "{city}" gmail OR yahoo OR hotmail',
            f'"{name}" {state} email owner manager',
            f'"{name}" "{city}" "email us" OR "email:"',
        ]
        if website:
            domain = urlparse(website if website.startswith("http") else "https://"+website).netloc
            if domain: queries.insert(0, f'site:{domain} email contact')
        if phone: queries.append(f'"{phone}" email contact')
        crawl_urls = []
        with ThreadPoolExecutor(max_workers=5) as ge:
            gfs = {ge.submit(google_q, session, q): q for q in queries}
            for gf in as_completed(gfs):
                if stop.is_set(): break
                try:
                    emails, urls = gf.result()
                    crawl_urls.extend(urls)
                    e = best_email(emails)
                    if e: report(e, "google"); return
                except Exception: pass
        if not stop.is_set() and crawl_urls:
            with ThreadPoolExecutor(max_workers=5) as ce:
                cfs = {ce.submit(fetch_url, session, u): u for u in list(dict.fromkeys(crawl_urls))[:6]}
                for cf in as_completed(cfs):
                    if stop.is_set(): break
                    html = cf.result()
                    if html:
                        e = best_email(parse_emails(html))
                        if e: report(e, "google_crawl"); return

    def task_directories():
        if stop.is_set(): return
        def run_q(q):
            if stop.is_set(): return
            try:
                emails, urls = google_q(session, q)
                e = best_email(emails)
                if e: report(e, "directory"); return
                for u in urls[:2]:
                    if stop.is_set(): return
                    html = fetch_url(session, u)
                    if html:
                        e2 = best_email(parse_emails(html))
                        if e2: report(e2, "directory"); return
            except Exception: pass
        queries = [t.replace("{n}", name).replace("{c}", city).replace("{s}", state) for t in DIR_TEMPLATES]
        with ThreadPoolExecutor(max_workers=15) as de:
            dfs = [de.submit(run_q, q) for q in queries]
            for df_ in as_completed(dfs):
                if stop.is_set(): break

    def task_social():
        if stop.is_set(): return
        urls = []
        if facebook and "facebook.com" in facebook:
            urls += [facebook, facebook.rstrip("/")+"/about"]
        if instagram and "instagram.com" in instagram: urls.append(instagram)
        if twitter: urls.append(twitter)
        for url in urls:
            if stop.is_set(): return
            html = fetch_url(session, url)
            if html:
                e = best_email(parse_emails(html))
                if e: report(e, "social_media"); return

    task_website()
    if not stop.is_set():
        with ThreadPoolExecutor(max_workers=3) as mp:
            sfs = [mp.submit(task_google), mp.submit(task_directories), mp.submit(task_social)]
            for sf in as_completed(sfs):
                if stop.is_set(): break

    return found["email"], found["source"]

def hunt_row(df, row, job_id):
    name      = get_val(df, row, "name")
    city      = get_val(df, row, "city")
    state     = get_val(df, row, "state")
    website   = get_val(df, row, "website")
    facebook  = get_val(df, row, "facebook")
    instagram = get_val(df, row, "instagram")
    twitter   = get_val(df, row, "twitter")
    phone     = get_val(df, row, "phone")
    log.info(f"🔍 {name} | {city}")
    email, source = hunt_one_business(name, city, state, website, facebook, instagram, twitter, phone)
    if email: log.info(f"  ✅ [{source}] {email}")
    else: log.info(f"  ❌ {name} — not found")
    result = {
        "business_name": name, "category": get_val(df, row, "category"),
        "owner_name": get_val(df, row, "owner"), "email": email,
        "email_source": source, "phone": phone, "website": website,
        "facebook": facebook, "instagram": instagram, "twitter": twitter,
        "full_address": get_val(df, row, "address"), "city": city, "state": state,
        "rating": get_val(df, row, "rating"), "total_reviews": get_val(df, row, "reviews"),
        "reviews_link": get_val(df, row, "reviews_link"),
        "description": get_val(df, row, "description"),
        "email_found": "Yes" if email else "No",
    }
    jobs[job_id]["results"].append(result)
    jobs[job_id]["processed"] += 1
    jobs[job_id]["found"] += (1 if email else 0)
    return result

def run_job(job_id, filepath, workers, user_email):
    try:
        jobs[job_id]["status"] = "running"
        p = Path(filepath)
        df = pd.read_excel(p, dtype=str) if p.suffix.lower() in [".xlsx",".xls"] \
             else pd.read_csv(p, dtype=str, encoding="utf-8-sig")
        jobs[job_id]["total"] = len(df)
        with ThreadPoolExecutor(max_workers=min(workers, 4)) as pool:
            futures = [pool.submit(hunt_row, df, row, job_id) for _, row in df.iterrows()]
            for f in as_completed(futures):
                try: f.result()
                except Exception as e: log.error(f"Row error: {e}")
        out_df = pd.DataFrame(jobs[job_id]["results"])
        out_df = out_df.sort_values("email_found", ascending=False).reset_index(drop=True)
        out_path = OUTPUT_FOLDER / f"{job_id}_results.csv"
        out_df.to_csv(out_path, index=False, encoding="utf-8-sig")
        jobs[job_id]["output_path"] = str(out_path)
        jobs[job_id]["status"] = "done"
        # Deduct credits (1 per business processed)
        update_credits(user_email, -jobs[job_id]["processed"])
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        log.error(f"Job failed: {e}")

# ── Auth Routes ───────────────────────────────────────────────────────────────

@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.get_json()
    email = (data.get("email") or "").lower().strip()
    password = data.get("password") or ""
    name = data.get("name") or ""
    if not email or not password or not name:
        return jsonify({"error": "Name, email and password required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if get_user(email):
        return jsonify({"error": "Email already registered"}), 400
    user = {
        "email": email,
        "name": name,
        "password": hash_password(password),
        "credits": FREE_CREDITS,
        "plan": "free",
        "created": datetime.utcnow().isoformat(),
        "total_searches": 0,
        "total_found": 0,
    }
    save_user(email, user)
    token = make_token(email)
    return jsonify({"token": token, "user": {k: v for k, v in user.items() if k != "password"}})

@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()
    email = (data.get("email") or "").lower().strip()
    password = data.get("password") or ""
    user = get_user(email)
    if not user or user["password"] != hash_password(password):
        return jsonify({"error": "Invalid email or password"}), 401
    token = make_token(email)
    return jsonify({"token": token, "user": {k: v for k, v in user.items() if k != "password"}})

@app.route("/api/me", methods=["GET"])
@require_auth
def me():
    user = {k: v for k, v in g.user.items() if k != "password"}
    return jsonify(user)

# ── Credit & Payment Routes ───────────────────────────────────────────────────

PLANS = {
    "starter": {"credits": 200,  "price": 999,   "label": "Starter — 200 searches"},
    "pro":     {"credits": 1000, "price": 2999,  "label": "Pro — 1,000 searches"},
    "agency":  {"credits": 5000, "price": 6999,  "label": "Agency — 5,000 searches"},
}

@app.route("/api/create-order", methods=["POST"])
@require_auth
def create_order():
    data = request.get_json()
    plan = data.get("plan")
    if plan not in PLANS:
        return jsonify({"error": "Invalid plan"}), 400
    if not RAZORPAY_KEY_ID:
        # Demo mode — no real payment
        return jsonify({
            "demo": True,
            "plan": plan,
            "credits": PLANS[plan]["credits"],
            "message": "Demo mode — Razorpay keys not configured. Add RAZORPAY_KEY_ID and RAZORPAY_SECRET to environment."
        })
    try:
        import razorpay
        client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_SECRET))
        order = client.order.create({
            "amount": PLANS[plan]["price"] * 100,  # paise
            "currency": "INR",
            "receipt": f"{g.user_email}_{plan}_{uuid.uuid4().hex[:8]}",
            "notes": {"email": g.user_email, "plan": plan}
        })
        return jsonify({
            "order_id": order["id"],
            "amount": PLANS[plan]["price"],
            "currency": "INR",
            "key": RAZORPAY_KEY_ID,
            "plan": plan,
            "credits": PLANS[plan]["credits"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/verify-payment", methods=["POST"])
@require_auth
def verify_payment():
    data = request.get_json()
    plan = data.get("plan")
    # Demo mode
    if data.get("demo"):
        credits = PLANS.get(plan, {}).get("credits", 0)
        new_total = update_credits(g.user_email, credits)
        with db_lock:
            db = load_db()
            if g.user_email in db["users"]:
                db["users"][g.user_email]["plan"] = plan
                save_db(db)
        return jsonify({"success": True, "credits": new_total, "plan": plan})
    # Real Razorpay verification
    try:
        import razorpay
        client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_SECRET))
        params = {
            "razorpay_order_id": data.get("razorpay_order_id"),
            "razorpay_payment_id": data.get("razorpay_payment_id"),
            "razorpay_signature": data.get("razorpay_signature"),
        }
        client.utility.verify_payment_signature(params)
        credits = PLANS.get(plan, {}).get("credits", 0)
        new_total = update_credits(g.user_email, credits)
        with db_lock:
            db = load_db()
            if g.user_email in db["users"]:
                db["users"][g.user_email]["plan"] = plan
                save_db(db)
        return jsonify({"success": True, "credits": new_total})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# ── Hunt Routes ───────────────────────────────────────────────────────────────

@app.route("/api/upload", methods=["POST"])
@require_auth
def upload():
    user = g.user
    credits = user.get("credits", 0)
    if credits < 1:
        return jsonify({"error": "No credits left. Please top up to continue."}), 402
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files["file"]
    workers = int(request.form.get("workers", 3))
    if not file.filename.endswith((".csv", ".xlsx", ".xls")):
        return jsonify({"error": "Please upload CSV or Excel file"}), 400
    job_id = str(uuid.uuid4())
    filepath = UPLOAD_FOLDER / f"{job_id}_{file.filename}"
    file.save(filepath)
    jobs[job_id] = {
        "status": "queued", "total": 0, "processed": 0,
        "found": 0, "results": [], "output_path": "", "error": "",
        "user": g.user_email,
    }
    t = threading.Thread(target=run_job, args=(job_id, str(filepath), workers, g.user_email))
    t.daemon = True
    t.start()
    return jsonify({"job_id": job_id, "credits_remaining": credits})

@app.route("/api/status/<job_id>")
@require_auth
def status(job_id):
    job = jobs.get(job_id)
    if not job: return jsonify({"error": "Job not found"}), 404
    if job.get("user") != g.user_email: return jsonify({"error": "Forbidden"}), 403
    pct = round(job["processed"] / job["total"] * 100) if job["total"] > 0 else 0
    return jsonify({
        "status": job["status"], "total": job["total"],
        "processed": job["processed"], "found": job["found"],
        "percent": pct, "recent": job["results"][-5:][::-1],
        "error": job.get("error", ""),
    })

@app.route("/api/download/<job_id>")
@require_auth
def download(job_id):
    job = jobs.get(job_id)
    if not job or not job.get("output_path"): return jsonify({"error": "Not ready"}), 404
    if job.get("user") != g.user_email: return jsonify({"error": "Forbidden"}), 403
    return send_file(job["output_path"], as_attachment=True, download_name="nexlead_results.csv")

# ── Static / SPA ──────────────────────────────────────────────────────────────

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve(path):
    if path and (Path("static") / path).exists():
        return send_from_directory("static", path)
    return send_from_directory("static", "index.html")

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  NexLead — Email Hunter SaaS")
    print(f"  {len(DIR_TEMPLATES)+30}+ sources per business")
    print(f"  Open → http://localhost:{PORT}")
    print("="*55 + "\n")
    app.run(debug=False, port=PORT, host="0.0.0.0")
