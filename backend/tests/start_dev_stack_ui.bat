@echo off
setlocal
cd /d "%~dp0\..\.."

REM Prefer pythonw so no extra console stays open behind the UI.
where pythonw >nul 2>&1
if %ERRORLEVEL%==0 (
  start "" pythonw "backend\tests\dev_stack_ui.py"
  exit /b 0
)

where python >nul 2>&1
if %ERRORLEVEL%==0 (
  start "" python "backend\tests\dev_stack_ui.py"
  exit /b 0
)

echo Python not found on PATH. Install Python or add it to PATH.
pause
exit /b 1
