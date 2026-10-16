@echo off
setlocal

cd /d "%~dp0"

rem garminconnect needs Python 3.12+; prefer the py launcher, fall back to python on PATH.
set "PY="
for %%v in (3.14 3.13 3.12) do (
    if not defined PY (
        py -%%v -c "import sys" >nul 2>nul && set "PY=py -%%v"
    )
)
if not defined PY (
    python -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python 3.12 or newer is required ^(garminconnect needs it^) but was not found.
    exit /b 1
)

if not exist ".venv" (
    echo Creating virtual environment in .venv ...
    %PY% -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r src\requirements.txt

echo Done. Run run.bat to start the app.
