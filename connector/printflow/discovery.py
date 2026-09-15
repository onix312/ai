"""Автопоиск сервера телефоном: UDP-маяк и ответ на «кто здесь?».

Зачем это вообще нужно. Адрес сервера в локальной сети — единственное, что в
мобильной кассе ломается само по себе: роутер выдал компьютеру новый IP, и
телефон «не видит кассу», хотя обе железки в одной сети. Обход своей /24
помогает, но он медленный и промахивается, когда у телефона два интерфейса
(Wi-Fi и мобильная сеть) — обход уходит не в ту подсеть.

Поэтому сервер сам говорит о себе: короткая UDP-датаграмма на широковещательный
адрес каждые две секунды плюс ответ на запрос «кто здесь?». Приложение кассы
шлёт запрос и получает адрес, порт и версию за десятые доли секунды — без
сканирования подсети. Протокол намеренно простой и версионированный (`proto`):
незнакомый ответ телефон игнорирует, а не пытается понять.

Порт маяка — 8765 (UDP), тот же, что в подсказке экрана выбора сервера в кассе.
Это не конфликт с HTTP-панелью: TCP и UDP — разные пространства портов, а
вещательная датаграмма не занимает TCP-порт сервера.

Всё здесь — «лучший возможный», а не «обязательный» сервис: если широковещание
запрещено (AP isolation, гостевой Wi-Fi, Windows-фаервол на исходящие UDP),
модуль молча ничего не делает, и телефон находит сервер обычным перебором."""

from __future__ import annotations

import json
import select
import socket
import threading
import time
from typing import Any, Iterable

from . import APP_VERSION
from .config import get_local_ips

#: UDP-порт маяка. Совпадает с подсказкой в приложении кассы (8765).
BEACON_PORT = 8765
#: Версия протокола: телефон обязан сверить её и при расхождении идти перебором.
PROTO = 1
#: Имя приложения в датаграмме — не доверяем «просто JSON в сети».
APP = "printflow"
#: Строка-запрос «кто здесь?». Легко набрать руками в отладке:
#: ``echo -n 'PRINTFLOW?' | nc -u -w1 192.168.1.50 8765``.
CALL = "PRINTFLOW?"
#: Как часто сервер о себе напоминает (секунды).
BEACON_INTERVAL = 2.0
#: Ограничение на длину датаграммы: всё, что больше, — не наш протокол.
MAX_DATAGRAM = 1024
#: Пути, о которых сообщаем телефону: касса сразу открывает свой, пульт — свой.
PATHS = {"panel": "/", "kassa": "/cashier.html", "pult": "/pult"}


def payload_json(port: int, *, version: str | None = None, name: str | None = None,
                 lan: Iterable[str] | None = None) -> bytes:
    """Датаграмма «я тут»: приложение, протокол, HTTP-порт, версия и адреса."""
    addresses = [ip for ip in (lan if lan is not None else get_local_ips())
                 if _usable(ip)]
    data = {
        "app": APP,
        "proto": PROTO,
        "port": int(port),
        "version": str(version or APP_VERSION),
        "name": str(name or socket.gethostname() or ""),
        # Только адреса, по которым телефон реально достучится: loopback ему
        # недоступен, а 169.254.x (APIPA) означает сломанный DHCP — такой
        # «найденный» адрес только уводит кассу в таймаут.
        "lan": addresses[:4],
        "paths": dict(PATHS),
    }
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse(data: bytes) -> dict[str, Any] | None:
    """Разобрать датаграмму. None — не наш протокол, версия или мусор.

    Проверяются все три признака: имя приложения, версия протокола и числовой
    HTTP-порт. Отвечать (и тем более переключаться) по любому JSON в сети нельзя:
    в гостевой сети кто угодно может «представиться» кассой.
    """
    if not data or len(data) > MAX_DATAGRAM:
        return None
    try:
        raw = json.loads(data.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("app") != APP:
        return None
    try:
        if int(raw.get("proto", 0)) != PROTO:
            return None
        port = int(raw.get("port", 0))
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    lan = raw.get("lan")
    paths = raw.get("paths")
    return {
        "app": APP,
        "proto": PROTO,
        "port": port,
        "version": str(raw.get("version") or ""),
        "name": str(raw.get("name") or ""),
        "lan": [str(ip) for ip in lan if isinstance(ip, str)][:4] if isinstance(lan, list) else [],
        "paths": {str(k): str(v) for k, v in paths.items()} if isinstance(paths, dict) else {},
    }


def is_call(data: bytes) -> bool:
    """Датаграмма-запрос от телефона: строка CALL или JSON с ``want: "?"``."""
    if not data or len(data) > MAX_DATAGRAM:
        return False
    try:
        text = data.decode("utf-8", "strict").strip()
    except UnicodeDecodeError:
        return False
    if text == CALL:
        return True
    try:
        raw = json.loads(text)
    except ValueError:
        return False
    return isinstance(raw, dict) and raw.get("app") == APP and raw.get("want") == "?"


def _usable(ip: str) -> bool:
    """Годится ли адрес для телефона: без loopback и без APIPA 169.254/16."""
    text = str(ip).strip()
    if not text or text.count(".") != 3:
        return False
    return not (text.startswith("127.") or text.startswith("169.254."))


def broadcast_targets(ips: Iterable[str] | None = None) -> list[str]:
    """Куда вещать: широковещательный адрес вообще и по каждому интерфейсу.

    Глобальный ``255.255.255.255`` проходит в большинстве домашних сетей, но
    некоторые роутеры/фаерволы его режут, а адрес подсети (``192.168.1.255``) —
    нет. Поэтому шлём оба, с ограничением на /24: считать маску без зависимостей
    для произвольной длины нечем, а нетипичная сеть телефон всё равно добирает
    перебором.
    """
    targets = ["255.255.255.255"]
    for ip in (ips if ips is not None else get_local_ips()):
        parts = str(ip).split(".")
        if len(parts) == 4 and all(part.isdigit() for part in parts):
            guest = ".".join(parts[:3]) + ".255"
            if guest not in targets:
                targets.append(guest)
    return targets


def _open_socket(udp_port: int) -> socket.socket | None:
    """UDP-сокет для маяка: широковещание включено, порт — если получится занять."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except OSError:
        return None
    try:
        sock.bind(("", int(udp_port)))
    except OSError:
        # Порт занят (второй экземпляр PrintFlow, чужая программа): не беда —
        # вещать всё равно можем, просто не услышим запросы и ответим только
        # тем, кто слушает широковещание.
        try:
            sock.bind(("", 0))
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
            return None
    try:
        sock.setblocking(False)
    except OSError:
        pass
    return sock


class Beacon(threading.Thread):
    """Фоновый поток: раз в интервал вещает о сервере и отвечает на запросы."""

    def __init__(self, port: int, *, udp_port: int = BEACON_PORT,
                 version: str | None = None, interval: float = BEACON_INTERVAL) -> None:
        super().__init__(name="pf-beacon", daemon=True)
        self.http_port = int(port)
        self.udp_port = int(udp_port)
        self.version = str(version or APP_VERSION)
        self.interval = max(0.3, float(interval))
        self.sock = _open_socket(self.udp_port)
        self.bound_port = 0
        if self.sock is not None:
            try:
                self.bound_port = int(self.sock.getsockname()[1])
            except OSError:
                self.bound_port = 0
        self.sent = 0
        self.answered = 0
        # Имя `_halt`, а не `_stop`: у threading.Thread есть свой внутренний
        # `_stop()`, и перекрывать его атрибутом нельзя — поток падает при join.
        self._halt = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ поиск
    def payload(self) -> bytes:
        return payload_json(self.http_port, version=self.version)

    def announce(self) -> int:
        """Разослать «я тут». Возвращает число адресов, куда отправилось."""
        sock = self.sock          # сокет может исчезнуть в stop() — держим ссылку
        if sock is None:
            return 0
        sent = 0
        data = self.payload()
        for target in broadcast_targets():
            try:
                sock.sendto(data, (target, self.udp_port))
                sent += 1
            except OSError:
                continue
        with self._lock:
            self.sent += sent
        return sent

    def reply(self, addr: tuple[str, int] | None = None) -> int:
        """Ответить на запрос. Без адреса — разобрать то, что пришло в сокет."""
        sock = self.sock
        if sock is None:
            return 0
        if addr is None:
            try:
                data, peer = sock.recvfrom(MAX_DATAGRAM)
            except (BlockingIOError, OSError):
                return 0
            if not is_call(data):
                return 0
            addr = peer
        try:
            sock.sendto(self.payload(), addr)
        except OSError:
            return 0
        with self._lock:
            self.answered += 1
        return 1

    # ------------------------------------------------------------------ жизнь
    def run(self) -> None:
        # Вещание с задержкой по времени, а не «раз в оборот цикла»: ядро
        # возвращает копию широковещательной датаграммы и отправителю, поэтому
        # наивный цикл «вещай — прочитай — вещай» превращался в шквал пакетов
        # (замер: 6851 датаграмма за полторы секунды вместо трёх).
        next_announce = time.monotonic()
        while not self._halt.is_set():
            sock = self.sock
            if sock is None:
                return  # без сокета молчим: молчание лучше, чем исключение в потоке
            if time.monotonic() >= next_announce:
                self.announce()
                next_announce = time.monotonic() + self.interval
            wait = max(0.05, min(self.interval, next_announce - time.monotonic()))
            try:
                ready, _, _ = select.select([sock], [], [], wait)
            except (OSError, ValueError):
                ready = []
            if self._halt.is_set():
                return
            if ready:
                self.reply()

    def stop(self, timeout: float = 1.0) -> None:
        self._halt.set()
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=timeout)

    def running(self) -> bool:
        return self.is_alive() and not self._halt.is_set()

    def info(self) -> dict[str, Any]:
        return {"running": self.running(), "http_port": self.http_port,
                "udp_port": self.bound_port or self.udp_port,
                "version": self.version, "sent": self.sent,
                "answered": self.answered}


def start(port: int, host: str = "0.0.0.0", *, version: str | None = None,
          udp_port: int = BEACON_PORT, interval: float = BEACON_INTERVAL) -> Beacon | None:
    """Поднять маяк, если сервер виден по сети. None — нечего объявлять.

    При ``--local`` (сервер слушает только 127.0.0.1) маяк бессмысленен и даже
    вреден: телефон нашёл бы адрес, по которому его никто не ждёт.
    """
    if str(host) not in ("0.0.0.0", "::", ""):
        return None
    try:
        beacon = Beacon(port, udp_port=udp_port, version=version, interval=interval)
        beacon.start()
        return beacon
    except Exception:  # сеть — не повод не запустить кассу
        return None


def probe(*, port: int = BEACON_PORT, timeout: float = 0.9,
          targets: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Спросить «кто здесь?» и собрать ответы. Проверка автопоиска на самом ПК.

    Так `pf.py net` и `pf.py doctor` показывают владельцу ровно то, что увидит
    телефон: если ответов нет — автопоиск в этой сети не работает и нужно
    вводить адрес руками (а не гадать, почему «касса не находит сервер»).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    found: dict[str, dict[str, Any]] = {}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 0))
        sock.settimeout(max(0.05, timeout))
        data = CALL.encode("ascii")
        for target in (list(targets) if targets is not None
                       else ["127.0.0.1", *broadcast_targets()]):
            try:
                sock.sendto(data, (target, int(port)))
            except OSError:
                continue
        deadline = time.monotonic() + max(0.05, timeout)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                sock.settimeout(left)
                raw, peer = sock.recvfrom(MAX_DATAGRAM)
            except (socket.timeout, TimeoutError):
                break
            except OSError:
                break
            info = parse(raw)
            if info is None:
                continue
            lan = info.get("lan") or []
            base_ip = lan[0] if lan else peer[0]
            entry = {
                "base": f"http://{base_ip}:{info['port']}",
                "addr": peer[0],
                "port": info["port"],
                "version": info["version"],
                "name": info["name"],
                "lan": lan,
            }
            found[entry["base"]] = entry
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return sorted(found.values(), key=lambda item: item["base"])


def reachable(*, port: int = BEACON_PORT, timeout: float = 0.9) -> bool:
    """Короткий ответ на вопрос «телефон найдёт сервер сам?» — для диагностики."""
    return bool(probe(port=port, timeout=timeout))
