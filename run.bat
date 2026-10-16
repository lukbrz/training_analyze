@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv" (
    echo .venv not found. Run install.bat first.
    exit /b 1
)

call .venv\Scripts\activate.bat
python src\app.py
