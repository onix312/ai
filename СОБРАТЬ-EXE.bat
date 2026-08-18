@echo off
chcp 65001 >nul
title NOZZA PrintFlow — сборка EXE
echo.
echo  ╔══════════════════════════════════════════╗
echo  ║  NOZZA ^· PrintFlow 8.0 — сборка EXE     ║
echo  ║  Нативное приложение как 1С / Photoshop  ║
echo  ╚══════════════════════════════════════════╝
echo.
echo  [1/3] Проверяю Python...
py --version >nul 2>&1
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% --version
if %errorlevel% neq 0 (
  echo  ✗ Python не найден. Установите Python 3.10+ с python.org
  echo    Обязательно поставьте галочку "Add to PATH"
  pause
  exit /b 1
)
echo  [2/3] Собираю EXE (5-10 минут, нужен интернет)...
echo        Команда: %PY% pf.py build
echo.
%PY% pf.py build
if %errorlevel% neq 0 (
  echo.
  echo  ✗ Сборка не удалась. Проверьте интернет и антивирус.
  pause
  exit /b 1
)
echo.
echo  ──────────────────────────────────────────
echo  ✓ Готово: dist\PrintFlow\PrintFlow.exe
echo    Папку dist\PrintFlow можно целиком перенести
echo    на другой ПК Windows — Python там не нужен.
echo    Двойной клик по PrintFlow.exe — и панель откроется
echo    как приложение (без браузера).
echo  ──────────────────────────────────────────
echo.
pause
