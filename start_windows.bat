@echo off
setlocal
cd /d "%~dp0"
if not defined INQUISITOR_DEMO_ACCOUNTS set INQUISITOR_DEMO_ACCOUNTS=1
if not defined INQUISITOR_JUDGE_BACKEND set INQUISITOR_JUDGE_BACKEND=local
if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  py -m venv .venv
)
call .venv\Scripts\activate.bat
python -c "import flask, tzdata, reportlab, qrcode, xlsxwriter" >nul 2>nul
if errorlevel 1 (
  echo Installing/updating Inquisitor v1.0.7 requirements. Internet access may be needed once...
  python -m pip install -r requirements.txt
  if errorlevel 1 goto :fail
)
echo Starting Inquisitor v1.0.7 in local development mode...
python app.py
goto :eof
:fail
echo.
echo Setup failed. Run: python -m pip install -r requirements.txt
pause
