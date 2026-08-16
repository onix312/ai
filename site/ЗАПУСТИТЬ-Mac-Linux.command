#!/bin/bash
cd "$(dirname "$0")"
echo ""
echo " ================================================"
echo "  3D-ПЕЧАТЬ: РУКОВОДСТВО И КАЛЬКУЛЯТОРЫ"
echo " ================================================"
echo ""
echo " Открываем сайт в браузере..."
echo " НЕ ЗАКРЫВАЙТЕ это окно, пока пользуетесь сайтом."
echo " Чтобы закрыть — нажмите Ctrl+C."
echo ""
(sleep 1.5; (open http://localhost:8080 2>/dev/null || xdg-open http://localhost:8080 2>/dev/null)) &
python3 -m http.server 8080 2>/dev/null || python -m http.server 8080
