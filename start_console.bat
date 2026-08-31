@echo off
setlocal
cd /d "%~dp0"

set "APP=start_console.py"

if exist "backend\.venv\Scripts\pythonw.exe" (
  "backend\.venv\Scripts\python.exe" -c "import tkinter" >nul 2>&1
  if not errorlevel 1 (
    start "" "backend\.venv\Scripts\pythonw.exe" "%APP%"
    exit /b 0
  )
)

where pyw >nul 2>&1
if not errorlevel 1 (
  start "" pyw "%APP%"
  exit /b 0
)

where pythonw >nul 2>&1
if not errorlevel 1 (
  start "" pythonw "%APP%"
  exit /b 0
)

where py >nul 2>&1
if not errorlevel 1 (
  py "%APP%"
  exit /b %errorlevel%
)

where python >nul 2>&1
if not errorlevel 1 (
  python "%APP%"
  exit /b %errorlevel%
)

echo [ERROR] Python was not found.
echo Install Python 3.10 or newer, then run this file again.
pause
exit /b 1
