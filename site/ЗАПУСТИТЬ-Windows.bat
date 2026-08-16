@echo off
chcp 65001 >nul
setlocal
title PrintFlow 2.0 - Bambu Lab Connector
cd /d "%~dp0.."

echo.
echo  ==================================================
echo   PrintFlow 2.0: управление 3D-производством
echo   Bambu Lab + AMS - локальное подключение
echo  ==================================================
echo.

set "PYTHON_CMD="
where py >nul 2>nul && set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (where python >nul 2>nul && set "PYTHON_CMD=python")
if not defined PYTHON_CMD (
  echo  ОШИБКА: Python 3 не найден.
  echo  Установите Python с https://python.org/downloads/
  echo  При установке включите галочку "Add Python to PATH".
  echo.
  pause
  exit /b 1
)

set "PF_HOME=%LOCALAPPDATA%\PrintFlow"
set "PF_VENV=%PF_HOME%\venv"
if not exist "%PF_HOME%" mkdir "%PF_HOME%"
if not exist "%PF_VENV%\Scripts\python.exe" (
  echo  Первый запуск: создаём локальное окружение...
  %PYTHON_CMD% -m venv "%PF_VENV%"
  if errorlevel 1 goto :error
)

echo  Проверяем компонент связи с принтером...
"%PF_VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r "connector\requirements.txt"
if errorlevel 1 goto :error

echo.
echo  Сайт:   http://localhost:8080
echo  Данные: %APPDATA%\PrintFlow  (база printflow.sqlite3)
echo.
echo  Не закрывайте это окно, пока работаете с PrintFlow:
echo  без него интерфейс не сохраняет данные и не видит принтер.
echo  Для остановки нажмите Ctrl+C.
echo.
"%PF_VENV%\Scripts\python.exe" "connector\printflow_connector.py"
exit /b 0

:error
echo.
echo  Не удалось подготовить PrintFlow Connector.
echo  Проверьте интернет для первого запуска и повторите попытку.
echo.
pause
exit /b 1
