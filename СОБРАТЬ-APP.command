#!/bin/bash
# NOZZA PrintFlow 8.0 — сборка приложения (macOS / Linux)
cd "$(dirname "$0")"
echo "╔══════════════════════════════════════════╗"
echo "║  NOZZA · PrintFlow 8.0 — сборка APP     ║"
echo "╚══════════════════════════════════════════╝"
echo ""
if ! command -v python3 &> /dev/null; then echo "✗ python3 не найден"; exit 1; fi
echo "[1/2] Проверяю зависимости..."
python3 pf.py deps
echo "[2/2] Собираю..."
python3 pf.py build
echo ""
echo "✓ Готово: dist/PrintFlow/PrintFlow"
echo "  Папку dist/PrintFlow можно перенести на другой ПК той же системы"
