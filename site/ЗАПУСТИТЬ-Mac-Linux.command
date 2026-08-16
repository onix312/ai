#!/bin/bash
set -e
cd "$(dirname "$0")/.."
PF_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/printflow"
PF_VENV="$PF_HOME/venv"

echo ""
echo " =================================================="
echo "  PrintFlow 2.0: Bambu Lab + AMS"
echo " =================================================="
echo ""
command -v python3 >/dev/null || { echo "Нужен Python 3"; read -r; exit 1; }
mkdir -p "$PF_HOME"
[ -x "$PF_VENV/bin/python" ] || python3 -m venv "$PF_VENV"
"$PF_VENV/bin/python" -m pip install --disable-pip-version-check -q -r connector/requirements.txt

echo "Сайт:   http://localhost:8080"
echo "Данные: ${XDG_CONFIG_HOME:-$HOME/.config}/printflow (база printflow.sqlite3)"
echo ""
echo "Не закрывайте это окно: без коннектора интерфейс не сохраняет данные."
echo "Для остановки нажмите Ctrl+C."
"$PF_VENV/bin/python" connector/printflow_connector.py
