@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title NOZZA PrintFlow

rem ---- find a working Python (py -3, python, python3) ------------------
rem Each candidate is verified with "-c import sys": "where" alone is not
rem enough because Windows ships a fake python.exe that opens the Store.
set "PY="

where py >nul 2>&1
if not errorlevel 1 (
  py -3 -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=python"
  )
)
if not defined PY (
  where python3 >nul 2>&1
  if not errorlevel 1 (
    python3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=python3"
  )
)

if not defined PY (
  echo.
  echo  Python not found.
  echo  Install Python 3.10+ from https://www.python.org/downloads/
  echo  Enable the checkbox: Add python.exe to PATH
  echo.
  pause
  exit /b 1
)

if not exist "pf.py" (
  echo.
  echo  pf.py not found in this folder:
  echo  %CD%
  echo  Put this BAT next to pf.py
  echo.
  pause
  exit /b 1
)

echo.
echo  Starting NOZZA PrintFlow...
echo  Panel:  http://127.0.0.1:8080/
echo  Close this window to stop.
echo.

%PY% "pf.py" app
if errorlevel 1 (
  echo.
  echo  Native window failed, starting usual mode...
  %PY% "pf.py"
)

echo.
pause
