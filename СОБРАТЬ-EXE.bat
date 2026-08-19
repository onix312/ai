@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title NOZZA PrintFlow - build EXE

rem ---- find a working Python (py -3, python, python3) ------------------
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
echo  [1/2] Python:
%PY% --version
echo.
echo  [2/2] Building EXE. This can take 5-10 minutes.
echo        Need internet on the first build.
echo        Command: %PY% pf.py build
echo.

%PY% "pf.py" build
if errorlevel 1 (
  echo.
  echo  Build failed. Check internet and antivirus.
  echo.
  pause
  exit /b 1
)

echo.
echo  Done: dist\PrintFlow\PrintFlow.exe
echo  Copy the whole folder dist\PrintFlow to another PC.
echo  Python is not needed there.
echo.
pause
