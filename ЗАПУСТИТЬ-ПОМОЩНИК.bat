@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

title LUMA - Automatic Installer and Launcher

chcp 65001 >nul 2>&1

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

set "LUMA_VENV=%CD%\.luma-venv"
set "PYTHON311="
set "PY=%LUMA_VENV%\Scripts\python.exe"

echo.
echo ============================================================
echo                    LUMA LAUNCHER
echo ============================================================
echo.
echo Этот запускатор автоматически:
echo   - найдёт Python 3.11
echo   - при необходимости установит Python 3.11
echo   - создаст отдельное окружение Luma
echo   - установит зависимости
echo   - запустит Luma
echo.
echo ============================================================
echo.


rem ============================================================
rem 0. Проверяем проект
rem ============================================================

if not exist "luma.py" (
    echo [ОШИБКА] Не найден luma.py
    echo.
    echo BAT должен лежать в корне проекта:
    echo %CD%
    echo.
    pause
    exit /b 1
)

if not exist "agent\requirements.txt" (
    echo [ОШИБКА] Не найден agent\requirements.txt
    echo.
    pause
    exit /b 1
)


rem ============================================================
rem 1. Ищем Python 3.11
rem ============================================================

echo [1/7] Ищу Python 3.11...
echo.

call :FIND_PYTHON311

if defined PYTHON311 (
    echo [OK] Python 3.11 найден:
    echo      %PYTHON311%
    "%PYTHON311%" --version
    goto python_ready
)

echo Python 3.11 не найден.
echo.
echo Попробую установить автоматически...
echo.


rem ============================================================
rem 2. Сначала пробуем Winget
rem ============================================================

echo [2/7] Устанавливаю Python 3.11...
echo.

where winget >nul 2>&1

if errorlevel 1 (
    echo Winget не найден.
    echo Перехожу к загрузке установщика с python.org...
    goto install_python_direct
)

echo Найден Winget.
echo Устанавливаю Python 3.11 x64...
echo.

winget install ^
    --id Python.Python.3.11 ^
    -e ^
    --source winget ^
    --scope user ^
    --accept-package-agreements ^
    --accept-source-agreements ^
    --silent

if errorlevel 1 (
    echo.
    echo Winget не смог установить Python.
    echo Перехожу к прямой установке с python.org...
    echo.
    goto install_python_direct
)

echo.
echo Winget завершил установку.
echo.

call :FIND_PYTHON311

if defined PYTHON311 goto python_ready


rem ============================================================
rem 3. Fallback: официальный Python.org
rem ============================================================

:install_python_direct

echo.
echo ------------------------------------------------------------
echo Загружаю официальный Python 3.11.9 x64...
echo ------------------------------------------------------------
echo.

set "PY_INSTALLER=%TEMP%\python-3.11.9-amd64.exe"
set "PY_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"

if exist "%PY_INSTALLER%" del /f /q "%PY_INSTALLER%" >nul 2>&1

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ProgressPreference='SilentlyContinue';" ^
    "Write-Host 'Downloading Python 3.11.9...';" ^
    "Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_INSTALLER%'"

if errorlevel 1 (
    echo.
    echo [ОШИБКА] Не удалось скачать Python.
    echo.
    echo Проверь интернет и попробуй снова.
    goto fatal
)

if not exist "%PY_INSTALLER%" (
    echo.
    echo [ОШИБКА] Установщик Python не скачался.
    goto fatal
)

echo.
echo Устанавливаю Python 3.11...
echo.

"%PY_INSTALLER%" /quiet ^
    InstallAllUsers=0 ^
    PrependPath=1 ^
    Include_launcher=1 ^
    Include_pip=1 ^
    Include_test=0 ^
    Shortcuts=0

set "INSTALL_RESULT=%ERRORLEVEL%"

if not "%INSTALL_RESULT%"=="0" (
    echo.
    echo [ОШИБКА] Установщик Python вернул код %INSTALL_RESULT%.
    echo.
    goto fatal
)

del /f /q "%PY_INSTALLER%" >nul 2>&1

echo.
echo Python установлен.
echo.

call :FIND_PYTHON311

if not defined PYTHON311 (
    echo.
    echo [ОШИБКА] Python установился, но его executable не найден.
    echo.
    echo Попробуй перезагрузить Windows.
    goto fatal
)


rem ============================================================
rem Python найден / установлен
rem ============================================================

:python_ready

echo.
echo ============================================================
echo [OK] Python готов
echo ============================================================
echo.

"%PYTHON311%" --version

rem Проверяем именно 3.11

"%PYTHON311%" -c "import sys; exit(0 if sys.version_info[:2] == (3,11) else 1)"

if errorlevel 1 (
    echo.
    echo [ОШИБКА] Найден не Python 3.11.
    goto fatal
)


rem ============================================================
rem 4. Создаём окружение Luma
rem ============================================================

echo.
echo [3/7] Проверяю окружение Luma...
echo.

if exist "%PY%" (
    "%PY%" -c "import sys; exit(0 if sys.version_info[:2] == (3,11) else 1)" >nul 2>&1

    if errorlevel 1 (
        echo Старое .luma-venv использует другую версию Python.
        echo Удаляю и создаю заново...
        echo.

        rmdir /s /q "%LUMA_VENV%"
    )
)

if not exist "%PY%" (
    echo Создаю:
    echo %LUMA_VENV%
    echo.

    "%PYTHON311%" -m venv "%LUMA_VENV%"

    if errorlevel 1 (
        echo.
        echo [ОШИБКА] Не удалось создать virtual environment.
        goto fatal
    )
)

echo [OK] Окружение готово.
"%PY%" --version


rem ============================================================
rem 5. Pip
rem ============================================================

echo.
echo [4/7] Обновляю pip...
echo.

"%PY%" -m ensurepip --upgrade >nul 2>&1
"%PY%" -m pip install --upgrade pip setuptools wheel

if errorlevel 1 (
    echo.
    echo [ОШИБКА] Не удалось обновить pip.
    goto fatal
)


rem ============================================================
rem 6. Сначала ставим критические зависимости
rem ============================================================

echo.
echo [5/7] Устанавливаю основные зависимости Luma...
echo.

"%PY%" -m pip install ^
    "PySide6>=6.7,<7" ^
    "vosk==0.3.45" ^
    "sounddevice==0.4.7" ^
    "mss==9.0.1" ^
    "pillow==10.4.0" ^
    "pywinauto==0.6.9" ^
    "websocket-client>=1.8,<2"

if errorlevel 1 (
    echo.
    echo [ОШИБКА] Не удалось установить основные зависимости.
    goto fatal
)


rem ============================================================
rem 7. OCR отдельно
rem ============================================================

echo.
echo [6/7] Устанавливаю OCR...
echo.

"%PY%" -c "import rapidocr_onnxruntime" >nul 2>&1

if not errorlevel 1 (
    echo [OK] RapidOCR уже установлен.
    goto verify
)

echo Устанавливаю rapidocr-onnxruntime 1.3.24...
echo.

"%PY%" -m pip install "rapidocr-onnxruntime==1.3.24"

if errorlevel 1 (
    echo.
    echo ============================================================
    echo [ВНИМАНИЕ]
    echo RapidOCR установить не удалось.
    echo.
    echo Это НЕ блокирует запуск Luma.
    echo OCR экрана временно будет недоступен.
    echo ============================================================
    echo.
) else (
    echo.
    echo [OK] RapidOCR установлен.
)


rem ============================================================
rem Проверка
rem ============================================================

:verify

echo.
echo Проверяю установку...
echo.

"%PY%" -c "from PySide6.QtWidgets import QApplication; print('[OK] PySide6')"

if errorlevel 1 goto dependency_error

"%PY%" -c "import vosk; print('[OK] Vosk')"

if errorlevel 1 goto dependency_error

"%PY%" -c "import sounddevice; print('[OK] SoundDevice')"

if errorlevel 1 goto dependency_error

"%PY%" -c "import mss; print('[OK] MSS')"

if errorlevel 1 goto dependency_error

"%PY%" -c "from PIL import Image; print('[OK] Pillow')"

if errorlevel 1 goto dependency_error

"%PY%" -c "import pywinauto; print('[OK] PyWinAuto')"

if errorlevel 1 goto dependency_error

"%PY%" -c "import websocket; print('[OK] WebSocket')"

if errorlevel 1 goto dependency_error

"%PY%" -c "import rapidocr_onnxruntime; print('[OK] RapidOCR')" >nul 2>&1

if errorlevel 1 (
    echo [OPTIONAL] RapidOCR недоступен
) else (
    echo [OK] RapidOCR
)


rem ============================================================
rem Запуск Luma
rem ============================================================

echo.
echo ============================================================
echo [7/7] ЗАПУСКАЮ LUMA
echo ============================================================
echo.
echo Python:
"%PY%" --version
echo.
echo Окружение:
echo %LUMA_VENV%
echo.
echo Если Luma упадёт, окно НЕ закроется.
echo Ошибка останется здесь.
echo.
echo ============================================================
echo.

"%PY%" -X faulthandler -u "%CD%\luma.py"

set "LUMA_EXIT=%ERRORLEVEL%"

echo.
echo ============================================================

if "%LUMA_EXIT%"=="0" (
    echo Luma завершила работу нормально.
) else (
    echo.
    echo [ОШИБКА] Luma завершилась с кодом %LUMA_EXIT%.
    echo.
    echo Скопируй ошибку выше и отправь её мне.
)

echo ============================================================
echo.
pause
exit /b %LUMA_EXIT%


rem ============================================================
rem Поиск Python
rem ============================================================

:FIND_PYTHON311

set "PYTHON311="

rem --- py launcher

where py >nul 2>&1

if not errorlevel 1 (
    for /f "delims=" %%P in ('py -3.11 -c "import sys; print(sys.executable)" 2^>nul') do (
        set "PYTHON311=%%P"
    )

    if defined PYTHON311 exit /b 0
)

rem --- стандартная пользовательская установка

if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set "PYTHON311=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    exit /b 0
)

rem --- Program Files

if exist "%ProgramFiles%\Python311\python.exe" (
    set "PYTHON311=%ProgramFiles%\Python311\python.exe"
    exit /b 0
)

if exist "%ProgramFiles%\Python 3.11\python.exe" (
    set "PYTHON311=%ProgramFiles%\Python 3.11\python.exe"
    exit /b 0
)

rem --- Program Files x86

if defined ProgramFiles(x86) (
    if exist "%ProgramFiles(x86)%\Python311\python.exe" (
        set "PYTHON311=%ProgramFiles(x86)%\Python311\python.exe"
        exit /b 0
    )
)

exit /b 0


rem ============================================================
rem Ошибки
rem ============================================================

:dependency_error

echo.
echo ============================================================
echo [ОШИБКА] Одна из обязательных зависимостей Luma не работает.
echo ============================================================
echo.
echo Python:
"%PY%" --version
echo.
echo Pip:
"%PY%" -m pip --version
echo.
echo Установленные пакеты:
"%PY%" -m pip list
echo.
pause
exit /b 1


:fatal

echo.
echo ============================================================
echo                УСТАНОВКА LUMA ПРЕРВАНА
echo ============================================================
echo.
echo Скопируй последние строки ошибки из этого окна и пришли мне.
echo.
pause
exit /b 1
