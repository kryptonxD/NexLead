@echo off
echo.
echo  =========================================
echo    NexLead - Email Hunter SaaS
echo  =========================================
echo.
echo  Installing dependencies...
pip install flask flask-cors requests beautifulsoup4 lxml pandas openpyxl razorpay gunicorn --quiet
echo.
echo  Starting NexLead...
echo  Open your browser: http://localhost:5000
echo.
python app.py
pause
