@echo off
chcp 65001 >nul
title NOZZA PrintFlow 8.0
:: Запуск нативного приложения как у 1С / Photoshop
:: Если pywebview установлен — откроется отдельное окно, иначе — браузер
py --version >nul 2>&1
if %errorlevel%==0 (set PY=py) else (set PY=python)
echo  Запускаю NOZZA PrintFlow 8.0...
echo  Панель: http://localhost:8080/
echo  Закройте это окно чтобы остановить.
echo.
%PY% pf.py app
if %errorlevel% neq 0 (
  echo  Пробую обычный запуск...
  %PY% pf.py
)
pause
