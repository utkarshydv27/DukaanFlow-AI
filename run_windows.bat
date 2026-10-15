@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title DukaanFlow AI - Local Dashboard

echo ==========================================
echo       DukaanFlow AI - Dashboard Setup
echo ==========================================
echo.

where py >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python Launcher 'py' was not found.
  echo Install Python 3.11 or newer from python.org and enable the launcher.
  goto :fail
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating virtual environment...
  py -3.13 -m venv .venv >nul 2>&1
  if errorlevel 1 py -m venv .venv
  if errorlevel 1 (
    echo ERROR: Could not create the virtual environment.
    goto :fail
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Virtual environment Python was not created.
  goto :fail
)

echo [2/3] Installing required packages. Internet connection is needed on first run...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
  echo ERROR: Could not update pip. Check internet/proxy settings and retry.
  goto :fail
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo ERROR: Package installation failed. Read the error above, then retry.
  goto :fail
)

if not exist ".env" (
  if exist ".env.example" (
    copy ".env.example" ".env" >nul
  ) else (
    echo WARNING: .env.example was not found; using default local demo settings.
  )
)

echo [3/3] Starting the dashboard...
echo Keep this black window open while using DukaanFlow AI.
echo The browser will open when the local server responds.
echo If it does not open, visit http://127.0.0.1:5000 manually.
echo Press Ctrl+C to stop the server.
echo.

REM Wait for the local health endpoint, then open the dashboard in the default browser.
start "DukaanFlow AI browser opener" /min powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0open_dashboard.ps1"
".venv\Scripts\python.exe" app.py

echo.
echo DukaanFlow AI has stopped. If there was an error above, copy it for troubleshooting.

:fail
echo.
echo Setup did not complete or the server stopped.
pause
endlocal
