@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto run_venv
python --version >nul 2>&1
if not errorlevel 1 goto run_python
py -3 --version >nul 2>&1
if not errorlevel 1 goto run_py
echo Python 3 was not found. Install Python 3 and enable Add Python to PATH.
pause
exit /b 1

:run_python
python pc_server.py --open %*
goto finish

:run_py
py -3 pc_server.py --open %*
goto finish

:run_venv
".venv\Scripts\python.exe" pc_server.py --open %*

:finish
if errorlevel 1 (
    echo.
    echo Controller startup failed. See the error above.
    pause
    exit /b 1
)
exit /b 0
