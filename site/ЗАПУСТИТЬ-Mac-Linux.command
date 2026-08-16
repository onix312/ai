#!/bin/bash
set -e
cd "$(dirname "$0")/.."
PF_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/printflow"
PF_VENV="$PF_HOME/venv"
PF_PORT="${1:-8080}"

echo ""
echo " =================================================="
echo "  PrintFlow 2.0: Bambu Lab + AMS — Симферополь"
echo " =================================================="
echo ""

command -v python3 >/dev/null || { echo "Нужен Python 3"; read -r; exit 1; }
mkdir -p "$PF_HOME"
[ -x "$PF_VENV/bin/python" ] || python3 -m venv "$PF_VENV"
"$PF_VENV/bin/python" -m pip install --disable-pip-version-check -q -r connector/requirements.txt

echo "--------------------------------------------------"
echo " СЕТЕВЫЕ АДРЕСА [порт $PF_PORT]"
echo "--------------------------------------------------"
echo " Локально:"
echo "   http://localhost:$PF_PORT/"
echo "   http://127.0.0.1:$PF_PORT/"
echo ""
echo " В локальной сети Wi-Fi / LAN:"

IPS=""
if command -v ip >/dev/null 2>&1; then
  IPS=$(ip -4 addr show scope global 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' || true)
fi
if [ -z "$IPS" ]; then
  if command -v ifconfig >/dev/null 2>&1; then
    IPS=$(ifconfig 2>/dev/null | grep -oE 'inet (addr:)?([0-9]*\.){3}[0-9]*' | grep -oE '([0-9]*\.){3}[0-9]*' | grep -v '^127\.' | grep -v '^169\.254\.' || true)
  fi
fi
if [ -z "$IPS" ] && command -v hostname >/dev/null 2>&1; then
  HIPS=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^127\.' | grep -v '^169\.254\.' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true)
  if [ -n "$HIPS" ]; then IPS="$HIPS"; fi
fi
if [ -z "$IPS" ]; then
  IPS=$("$PF_VENV/bin/python" -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(0.8); try: s.connect(('8.8.8.8',80)); print(s.getsockname()[0]);\nexcept: pass" 2>/dev/null || true)
fi

if [ -n "$IPS" ]; then
  for ip in $IPS; do
    echo "   http://$ip:$PF_PORT/"
  done
else
  echo "   (не определился, проверьте Wi-Fi)"
fi

echo "--------------------------------------------------"
echo " Данные: ${XDG_CONFIG_HOME:-$HOME/.config}/printflow (база printflow.sqlite3)"
echo " Город: Симферополь"
echo " Запуск с --host 0.0.0.0 (доступен в локалке)"
echo ""
echo "Не закрывайте это окно: без коннектора интерфейс не сохраняет данные."
echo "Для остановки нажмите Ctrl+C."
echo "--------------------------------------------------"
echo ""

exec "$PF_VENV/bin/python" connector/printflow_connector.py --host 0.0.0.0 --port "$PF_PORT"
