"""PrintFlow local production server.

Пакет содержит ядро системы:
    config      — пути, настройки и секреты вне репозитория;
    db          — SQLite-хранилище (источник правды);
    repo        — доступ к данным;
    accounting  — автоматический учёт материала, времени и денег;
    bambu       — MQTT/TLS-мост к принтерам Bambu Lab;
    camera      — локальный JPEG-поток;
    ftps        — файлы принтера и запуск печати;
    manager     — парк принтеров и очередь заданий;
    api         — HTTP API и раздача сайта.
"""

APP_VERSION = "8.1.0"
