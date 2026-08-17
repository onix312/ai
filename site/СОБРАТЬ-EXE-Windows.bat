@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title PrintFlow - сборка EXE
cd /d "%~dp0.."

echo.
echo  ==================================================
echo   СБОРКА PrintFlow.exe — Windows-бинарь без Python
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
  pause
  exit /b 1
)

set "PF_HOME=%LOCALAPPDATA%\PrintFlow"
set "PF_BVENV=%PF_HOME%\build-venv"
if not exist "%PF_HOME%" mkdir "%PF_HOME%"

echo  Шаг 1/4. Окружение сборки: %PF_BVENV%
if not exist "%PF_BVENV%\Scripts\python.exe" (
  echo         Создаём (первый раз занимает пару минут)...
  %PYTHON_CMD% -m venv "%PF_BVENV%"
  if errorlevel 1 goto :error
)

echo  Шаг 2/4. Ставим PyInstaller и зависимости приложения...
"%PF_BVENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
"%PF_BVENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q pyinstaller
"%PF_BVENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r "connector\requirements.txt"
if errorlevel 1 goto :error

echo  Шаг 3/4. Собираем бинарь (5-10 минут, в окне будут сообщения)...
"%PF_BVENV%\Scripts\pyinstaller.exe" connector\pyinstaller.spec --noconfirm --distpath dist --workpath build
if errorlevel 1 goto :error

echo  Шаг 4/4. Проверяем результат...
if not exist "dist\PrintFlow\PrintFlow.exe" (
  echo  [ОШИБКА] dist\PrintFlow\PrintFlow.exe не создался. Смотрите сообщения выше.
  pause
  exit /b 1
)

echo.
echo  ==================================================
echo   ГОТОВО!
echo.
echo   Папка с программой:  %cd%\dist\PrintFlow\
echo   Запуск:              dist\PrintFlow\PrintFlow.exe
echo.
echo   Эту папку можно целиком скопировать на другой ПК
echo   (Windows 10/11 x64) и запускать двойным кликом —
echo   Python там не нужен. Данные хранятся в
echo   %APPDATA%\PrintFlow и при переносе не теряются.
echo.
echo   Открыть папку с готовым EXE? (Y/N)
set /p OPEN_DIST=
if /i "%OPEN_DIST%"=="Y" explorer "dist\PrintFlow"
echo  ==================================================
echo.
pause
exit /b 0

:error
echo.
echo  [ОШИБКА] Сборка не удалась.
echo  1. Проверьте доступ к интернету (нужны PyInstaller и paho-mqtt).
echo  2. Закройте антивирус, если он блокирует PyInstaller.
echo  3. Посмотрите текст ошибки выше и пришлите его, если сами не разобрались.
echo.
pause
exit /b 1
