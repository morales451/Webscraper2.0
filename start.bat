@echo off
REM Prospector launcher for Windows: double-click this file.
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)

if not exist ".venv\Scripts\python.exe" (
  echo Setting up Prospector for the first time. This takes a few minutes...
  %PY% -m venv .venv || goto :nopython
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -e . || goto :fail
  ".venv\Scripts\python.exe" -m playwright install chromium || goto :fail
)

".venv\Scripts\python.exe" -m prospector serve
pause
exit /b

:nopython
echo.
echo Python 3.10 or newer is required. Install it from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" during install), then run this file again.
pause
exit /b 1

:fail
echo.
echo Setup failed. Scroll up for the error, fix it, delete the .venv folder and try again.
pause
exit /b 1
