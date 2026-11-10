@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" pc_server.py --simulate --open %*
) else (
    python pc_server.py --simulate --open %*
)
if errorlevel 1 pause
