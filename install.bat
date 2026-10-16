@echo off
setlocal

cd /d "%~dp0"

where py -3.12 >nul 2>nul
if errorlevel 1 (
    echo python3.12 is required (garminconnect needs Python 3.12+) but was not found.
    exit /b 1
)

if not exist ".venv" (
    echo Creating virtual environment in .venv ...
    py -3.12 -m venv .venv
)

call .venv\Scripts\activate.bat
pip install --upgrade pip
pip install -r src\requirements.txt

echo Done. Run run.bat to start the app.
