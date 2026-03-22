# NexLead — Deployment Guide
## Deploy to Render.com (Free) in 10 minutes

---

### STEP 1 — Push to GitHub

1. Go to github.com → New repository → name it `nexlead`
2. Download GitHub Desktop from desktop.github.com
3. Clone your new repo to your laptop
4. Copy ALL NexLead files into that folder:
   - app.py
   - requirements.txt
   - render.yaml
   - static/ (folder with index.html inside)
5. In GitHub Desktop → Commit → Push

---

### STEP 2 — Deploy on Render.com

1. Go to render.com → Sign up free
2. Click "New +" → "Web Service"
3. Connect your GitHub account
4. Select your `nexlead` repo
5. Render auto-detects render.yaml — click Deploy

Your site will be live at: https://nexlead.onrender.com

---

### STEP 3 — Add Razorpay (to accept payments)

1. Sign up at razorpay.com (free)
2. Go to Settings → API Keys → Generate Test Key
3. In Render dashboard → Your service → Environment
4. Add these variables:
   - RAZORPAY_KEY_ID = your key id
   - RAZORPAY_SECRET = your secret key
5. Redeploy

---

### STEP 4 — Go live with real payments

1. In Razorpay → complete KYC
2. Switch from Test keys to Live keys
3. Update environment variables in Render
4. Done — you can now accept real money

---

### PRICING YOU CAN CHARGE

| Plan | Credits | Your Price | Your Cost |
|------|---------|-----------|-----------|
| Starter | 200 | ₹999 | ₹0 |
| Pro | 1,000 | ₹2,999 | ₹0 |
| Agency | 5,000 | ₹6,999 | ₹0 |

**100% profit margin.** Your only cost is hosting (free on Render).

---

### HOW TO SELL IT

**Fiverr:**
Title: "I'll find 100 verified emails for local businesses in any US city"
Price: $49 basic | $149 standard | $249 premium
Category: Data → Data Scraping

**Upwork:**
Title: "Expert email finder for local businesses — 200+ sources searched"
Hourly: $25/hr or fixed price

**LinkedIn:**
Target: Marketing agencies, sales teams, cold email agencies
Message: "I built a tool that searches 200+ sources to find local business emails. Happy to give you a free demo batch of 20 leads."

**Reddit:**
- r/Entrepreneur
- r/digital_marketing
- r/smallbusiness
- r/agency

---

### ENVIRONMENT VARIABLES (Render)

| Variable | Value | Required |
|----------|-------|----------|
| SECRET_KEY | auto-generated | Yes |
| RAZORPAY_KEY_ID | from razorpay.com | For payments |
| RAZORPAY_SECRET | from razorpay.com | For payments |
| PORT | auto-set by Render | Auto |

---

### DEMO MODE

If Razorpay keys are NOT set, the app runs in demo mode:
- Users can still sign up and get 10 free credits
- Clicking "Buy" adds credits without real payment
- Good for testing before going live

---

Built by Ritik Barnwal | SolvAI Labs | solvailabs.com
