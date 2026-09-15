@echo off
rem ─────────────────────────────────────────────────────────────────────────
rem  NOZZA · PrintFlow — запуск одним двойным кликом.
rem
rem  Этот файл ничего не делает сам: он находит рабочий Python и передаёт
rem  управление лаунчеру pf.py. Все адреса, QR-код, проверки и подсказки
rem  печатает pf.py — держать их здесь вторым списком нельзя, иначе порт и
rem  версия в .bat разъедутся с кодом (так и было до 17.0.27: в батнике
rem  печаталось 8080, а касса искала 8765).
rem ─────────────────────────────────────────────────────────────────────────
setlocal EnableExtensions
cd /d "%~dp0"
title NOZZA PrintFlow

rem Русские сообщения в cmd: без этой кодовой страницы текст превращается
rem в кракозябры, поэтому раньше здесь были английские строки.
chcp 65001 >nul 2>&1

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
  echo  Python не найден.
  echo  Поставьте Python 3.10 или новее: https://www.python.org/downloads/
  echo  В установщике включите галочку "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

if not exist "pf.py" (
  echo.
  echo  Рядом с этим файлом нет pf.py.
  echo  Текущая папка: %CD%
  echo  Положите ЗАПУСТИТЬ.bat в папку с pf.py.
  echo.
  pause
  exit /b 1
)

rem Если PrintFlow уже запущен на любом порту — pf.py покажет адреса для
rem телефона и откроет панель, вместо ошибки "порт занят".
%PY% "pf.py"
set "CODE=%ERRORLEVEL%"

rem Ошибочный выход можно и не заметить: окно просто закроется. Поэтому при
rem ошибке показываем, куда смотреть (журнал) и что запускать (диагностика).
if not "%CODE%"=="0" (
  echo.
  echo  PrintFlow завершился с кодом %CODE%.
  echo  Диагностика:      %PY% pf.py doctor
  echo  Что с телефоном:  %PY% pf.py net
  echo  Журнал:           %PY% pf.py logs
  echo.
  pause
)
endlocal
