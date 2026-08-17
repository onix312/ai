#!/bin/bash
# Сборка автономного бинаря PrintFlow для macOS / Linux.
# Результат: dist/PrintFlow/PrintFlow (папку можно переносить целиком).
set -e
cd "$(dirname "$0")/.."

echo ""
echo " =================================================="
echo "  СБОРКА PrintFlow — автономный бинарь (PyInstaller)"
echo " =================================================="
echo ""

command -v python3 >/dev/null || { echo "Нужен Python 3"; read -r; exit 1; }

PF_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/printflow"
PF_BVENV="$PF_HOME/build-venv"
mkdir -p "$PF_HOME"

echo " Шаг 1/4. Окружение сборки: $PF_BVENV"
[ -x "$PF_BVENV/bin/python" ] || python3 -m venv "$PF_BVENV"

echo " Шаг 2/4. PyInstaller и зависимости приложения..."
"$PF_BVENV/bin/python" -m pip install --disable-pip-version-check -q --upgrade pip
"$PF_BVENV/bin/python" -m pip install --disable-pip-version-check -q pyinstaller
"$PF_BVENV/bin/python" -m pip install --disable-pip-version-check -q -r connector/requirements.txt

echo " Шаг 3/4. Собираем (несколько минут)..."
"$PF_BVENV/bin/pyinstaller" connector/pyinstaller.spec --noconfirm --distpath dist --workpath build

echo " Шаг 4/4. Проверяем результат..."
[ -x "dist/PrintFlow/PrintFlow" ] || { echo "[ОШИБКА] dist/PrintFlow/PrintFlow не создался."; read -r; exit 1; }

echo ""
echo " =================================================="
echo "  ГОТОВО!"
echo ""
echo "  Папка с программой:  $(pwd)/dist/PrintFlow/"
echo "  Запуск:              ./dist/PrintFlow/PrintFlow"
echo ""
echo "  Папку можно переносить целиком на другой компьютер"
echo "  той же ОС и архитектуры — Python там не нужен."
echo " =================================================="
echo ""
read -r -p "Нажмите Enter, чтобы закрыть окно. "
