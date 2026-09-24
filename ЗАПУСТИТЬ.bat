@echo off
rem ─────────────────────────────────────────────────────────────────────────
rem  NOZZA · PrintFlow — запуск одним двойным кликом (18.12.1).
rem
rem  Этот файл ничего не делает сам: он находит рабочий Python и передаёт
rem  управление лаунчеру pf.py. Все адреса, QR-код, проверки и подсказки
rem  печатает pf.py — держать их здесь вторым списком нельзя, иначе порт и
rem  версия в .bat разъедутся с кодом (так и было до 17.0.27: в батнике
rem  печаталось 8080, а касса искала 8765).
rem
rem  Что делает сам батник и почему:
rem    1. UTF-8 в консоли: chcp 65001 + PYTHONUTF8=1. Без этого русский текст
rem       и вывод pf.py превращаются в кракозябры (кодовая страница cmd по
rem       умолчанию — OEM 866, а Python пишет UTF-8).
rem    2. Рабочая папка = папка файла: двойной клик из ярлыка, с рабочего
rem       стола или из «Отправить» не должен ломать поиск pf.py.
rem    3. Поиск Python: проверяется не «где лежит», а «запускается ли» —
rem       Windows кладёт в PATH заглушку python.exe, которая открывает
rem       Microsoft Store и возвращает успех. Свое окружение (.venv) имеет
rem       приоритет: в нём уже стоят нужные библиотеки.
rem    4. Одна точка выхода (:end): при любой ошибке окно остаётся открытым,
rem       показывает причину и команды диагностики (doctor, net, logs,
rem       help). Раньше окно
rem       закрывалось вместе с текстом ошибки, и владелец видел только
rem       «мелькнуло и пропало».
rem ─────────────────────────────────────────────────────────────────────────
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title NOZZA PrintFlow

rem ---- консоль и вывод на русском -----------------------------------------
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY="
set "CODE=0"

rem ---- 0. своё окружение рядом с pf.py (.venv) ----------------------------
rem Если владелец поставил библиотеки в виртуальное окружение, берём его:
rem системный Python этих пакетов не видит и запуск падает на импорте.
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

rem ---- 1. лаунчер Windows: py -3 -----------------------------------------
if not defined PY (
  where py >nul 2>&1
  if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
  )
)

rem ---- 2. python из PATH --------------------------------------------------
rem Проверка «запускается и печатает версию» отсекает заглушку Microsoft
rem Store: она отвечает на -c "import sys", но на --version молча открывает
rem магазин и ничего не запускает.
if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 (
      python --version >nul 2>&1
      if not errorlevel 1 set "PY=python"
    )
  )
)

rem ---- 3. python3 из PATH -------------------------------------------------
if not defined PY (
  where python3 >nul 2>&1
  if not errorlevel 1 (
    python3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=python3"
  )
)

if not defined PY goto :no_python
if not exist "pf.py" goto :no_pf

rem ---- версия Python: нужна 3.10 или новее -------------------------------
rem Старший номер сравнивается числом, поэтому Python 3.10+ проходит, а 3.9
rem и старше — нет: на них не работает синтаксис коннектора, и падение
rem выглядит как «чёрное окно на секунду».
set "PYVER="
for /f "tokens=2 delims= " %%V in ('%PY% --version 2^>^&1') do if not defined PYVER set "PYVER=%%V"
set "PYMAJOR=0"
set "PYMINOR=0"
for /f "tokens=1,2 delims=." %%A in ("!PYVER!") do (
  set "PYMAJOR=%%A"
  set "PYMINOR=%%B"
)
if not "!PYMAJOR!"=="3" goto :old_python
if !PYMINOR! LSS 10 goto :old_python

echo.
echo  NOZZA · PrintFlow
echo  Python:  !PYVER!  ^(!PY!^)
echo  Папка:   %CD%
echo.

rem ---- библиотеки безопасности -------------------------------------------------
rem cryptography и OpenSSL ставит сам pf.py (ensure_crypto_prerequisites).
rem Второй список в батнике уже ронял запуск: скобка в echo внутри блока if
rem закрывает блок раньше времени, и cmd пишет «... was unexpected at this time»
rem ещё до вызова pf.py. Поэтому здесь нет echo со скобками и нет своего pip.

rem ---- запуск -------------------------------------------------------------
rem Если PrintFlow уже запущен на любом порту — pf.py покажет адреса для
rem телефона и откроет панель, вместо ошибки "порт занят".
%PY% "pf.py"
set "CODE=%ERRORLEVEL%"
if not "!CODE!"=="0" goto :failed
goto :end

rem ================================================================ ошибки
:no_python
echo.
echo  Python не найден.
echo  Поставьте Python 3.10 или новее: https://www.python.org/downloads/
echo  В установщике включите галочку "Add python.exe to PATH".
echo.
echo  Проверено: py -3, python, python3 и .venv\Scripts\python.exe
echo  Папка: %CD%
echo.
goto :end

:old_python
echo.
echo  Найден Python !PYVER!, а нужен 3.10 или новее.
echo  Поставьте свежий Python рядом: https://www.python.org/downloads/
echo  В установщике включите галочку "Add python.exe to PATH".
echo.
goto :end

:no_pf
echo.
echo  Рядом с этим файлом нет pf.py.
echo  Текущая папка: %CD%
echo  Положите ЗАПУСТИТЬ.bat в папку с pf.py (корень репозитория PrintFlow).
echo.
goto :end

:failed
echo.
echo  PrintFlow завершился с кодом !CODE!.
echo.
echo  Что делать дальше:
echo    Диагностика:      %PY% pf.py doctor
echo    Что с телефоном:  %PY% pf.py net
echo    Журнал:           %PY% pf.py logs
echo    Все команды:      %PY% pf.py help
echo.
echo  Чаще всего причина одна из двух: занят порт (лечится pf.py doctor)
echo  или сервер уже работает — тогда панель откроется по адресу, который
echo  напечатает pf.py net.
echo.

:end
rem Окно закрывается только при успешном запуске: текст ошибки владелец
rem должен успеть прочитать или сфотографировать.
if not "!CODE!"=="0" pause
endlocal
