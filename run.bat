@echo off
REM Quick start for Windows: creates a virtual environment (first run only),
REM installs dependencies, and starts the server.
cd /d "%~dp0backend"

if not exist venv (
  echo Creating Python virtual environment...
  python -m venv venv
)

call venv\Scripts\activate.bat
pip install -q -r requirements.txt

echo.
echo Nava is running at http://localhost:5000  (press Ctrl+C to stop)
echo.
python app.py
