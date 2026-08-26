@echo off
cd /d "%~dp0"
where pythonw >nul 2>&1
if %errorlevel%==0 (
  start "" pythonw "%~dp0token_hud.py"
  exit /b 0
)
where py >nul 2>&1
if %errorlevel%==0 (
  start "" pyw -3 "%~dp0token_hud.py"
  exit /b 0
)
start "" python "%~dp0token_hud.py"
