@echo off
rem Launch the AI Usage Command Center (Electron UI + Python engine).
cd /d "%~dp0app"

where npm >nul 2>&1
if not %errorlevel%==0 (
  echo Node.js is required. Install it from https://nodejs.org and run this again.
  pause
  exit /b 1
)

if not exist "node_modules" (
  echo First run: installing Electron...
  call npm install --no-audit --no-fund
  if not %errorlevel%==0 (
    echo npm install failed.
    pause
    exit /b 1
  )
)

start "" /b npm start
