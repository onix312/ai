@echo off
rem NOZZA: отдельный запуск панели помощника двойным кликом (Windows).
rem Не запускает вторую копию PrintFlow, если сервер уже работает.
setlocal EnableExtensions
cd /d "%~dp0"
title NOZZA - Помощник
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
  where py >nul 2>&1
  if not errorlevel 1 (
    py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
  )
)
if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 (
    python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
    if not errorlevel 1 set "PY=python"
  )
)
if not defined PY goto :no_python
if not exist "pf.py" goto :no_pf

echo Запускаю отдельную панель помощника...
echo Ollama запускается отдельно: ollama serve
echo.
%PY% "pf.py" assistant --local
if errorlevel 1 goto :failed
exit /b 0

:no_python
echo Python 3.10+ не найден. Установите его с python.org и включите Add to PATH.
goto :failed
:no_pf
echo Рядом с батником нет pf.py. Положите его в корень PrintFlow.
goto :failed
:failed
echo.
echo Проверьте: python pf.py doctor
echo Если Ollama отвечает, но моделей нет: ollama pull qwen2.5:3b
pause
exit /b 1
