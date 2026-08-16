@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title PrintFlow 2.0 - Bambu Lab Connector
cd /d "%~dp0.."

echo.
echo  ==================================================
echo   PrintFlow 2.0: управление 3D-производством
echo   Bambu Lab + AMS - Симферополь
echo  ==================================================
echo.

REM --- Поиск Python ---
set "PYTHON_CMD="
where py >nul 2>nul && set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (where python >nul 2>nul && set "PYTHON_CMD=python")
if not defined PYTHON_CMD (where python3 >nul 2>nul && set "PYTHON_CMD=python3")
if not defined PYTHON_CMD (
  echo  [ОШИБКА] Python 3 не найден в PATH.
  echo  Установите Python: https://python.org/downloads/
  echo  При установке включите "Add Python to PATH".
  echo.
  echo  Ваши IPv4 адреса:
  ipconfig | findstr /R /C:"IPv4"
  echo.
  pause
  exit /b 1
)

set "PF_HOME=%LOCALAPPDATA%\PrintFlow"
set "PF_VENV=%PF_HOME%\venv"
set "PF_PORT=8080"
if not "%~1"=="" set "PF_PORT=%~1"

if not exist "%PF_HOME%" mkdir "%PF_HOME%"

if not exist "%PF_VENV%\Scripts\python.exe" (
  echo  Первый запуск: создаём окружение в %PF_VENV%
  %PYTHON_CMD% -m venv "%PF_VENV%"
  if errorlevel 1 goto :error
)

echo  Проверяем зависимости (paho-mqtt)...
"%PF_VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r "connector\requirements.txt"

echo.
echo  --------------------------------------------------
echo   СЕТЕВЫЕ АДРЕСА ДЛЯ ПОДКЛЮЧЕНИЯ  [порт %PF_PORT%]
echo  --------------------------------------------------
echo.
echo   Локально на этом ПК:
echo     http://localhost:%PF_PORT%/
echo     http://127.0.0.1:%PF_PORT%/
echo.
echo   В локальной сети Wi-Fi/LAN (для телефона, планшета):
echo   --------------------------------------------------

REM Попытка через PowerShell (Windows 10/11) - самый чистый вывод
powershell -NoProfile -Command "$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -ne 'WellKnown' }; if($ips){ $ips | ForEach-Object { Write-Host (\"     http://\" + $_.IPAddress + \":%PF_PORT%/  [\" + $_.InterfaceAlias + \"]\") } } else { Write-Host '     (PowerShell не нашел IP, смотрим ipconfig ниже)' }" 2>nul

echo.
echo   Дополнительно (ipconfig):
for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /c:"IPv4"') do (
  set "IP=%%A"
  set "IP=!IP: =!"
  if not "!IP!"=="" (
    if not "!IP:~0,7!"=="169.254" echo     http://!IP!:%PF_PORT%/
  )
)

echo.
echo  --------------------------------------------------
echo   Данные:   %APPDATA%\PrintFlow
echo   Порт:     %PF_PORT%  (другой порт: ЗАПУСТИТЬ-Windows.bat 9000)
echo   Город:    Симферополь
echo  --------------------------------------------------
echo   Брандмауэр Windows может спросить разрешение
echo   для Python - нажмите "Разрешить доступ".
echo   --------------------------------------------------
echo   Запуск с --host 0.0.0.0 (доступен в локалке)
echo   Для режима только этот ПК: --host 127.0.0.1
echo   Не закрывайте окно, работает коннектор!
echo   Для остановки - Ctrl+C
echo  --------------------------------------------------
echo.

"%PF_VENV%\Scripts\python.exe" "connector\printflow_connector.py" --host 0.0.0.0 --port %PF_PORT%
if errorlevel 1 goto :error
exit /b 0

:error
echo.
echo  [ОШИБКА] Не удалось запустить PrintFlow
echo  1. Интернет для первого запуска
echo  2. Порт %PF_PORT% занят - попробуйте: ЗАПУСТИТЬ-Windows.bat 9000
echo  3. Проверьте антивирус/брандмауэр
echo.
pause
exit /b 1
