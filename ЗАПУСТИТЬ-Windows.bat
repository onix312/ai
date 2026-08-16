@echo off
chcp 65001 >nul
title 3D-pechat - rukovodstvo
echo.
echo  ================================================
echo   3D-PECHAT: RUKOVODSTVO I KALKULYATORY
echo  ================================================
echo.
echo  Otkryvaem sayt v brauzere...
echo.
echo  NE ZAKRYVAYTE ETO OKNO poka polzuetes saytom.
echo  Chtoby zakryt - nazhmite Ctrl+C ili prosto zakroyte okno.
echo.
start "" http://localhost:8080
python -m http.server 8080 2>nul || py -m http.server 8080 2>nul || (
  echo.
  echo  ОШИБКА: ne nayden Python.
  echo  Prosto otkroyte fayl index.html dvoynym klikom - sayt rabotaet i tak.
  echo.
  pause
)
