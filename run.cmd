@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 exit /b 1
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt --disable-pip-version-check --quiet
if errorlevel 1 exit /b 1

".venv\Scripts\python.exe" -m warehouse_ai
exit /b %errorlevel%
