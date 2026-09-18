"""Шлюз Bambu Studio: PrintFlow выглядит как принтер Bambu P1S в локальной сети.

Зачем. Владелец режет модель в Bambu Studio или OrcaSlicer и нажимает
«Печать». Studio при этом разговаривает не с очередью PrintFlow, а с
«принтером»: находит его по SSDP, спрашивает личность на :3000/:3002,
заливает файл по implicit FTPS :990 и шлёт ``project_file`` по MQTT/TLS
:8883. Шлюз отвечает на все четыре канала ровно так, как отвечает станок, и
кладёт файл в библиотеку и очередь PrintFlow (estimate, preflight, AMS-map).
На физический принтер уходит уже проверенное задание. Это не прозрачный
прокси и не виртуальный принтер из ``virtual.py``.

Порядок рукопожатия — именно тот, в котором его делает Studio, и именно тот,
в котором чаще всего ломается:

1. **SSDP.** Станок шлёт ``NOTIFY * HTTP/1.1`` с ``NT:
   urn:bambulab-com:device:3dprinter:1`` на :2021 (loopback, свой адрес,
   направленный broadcast, общий broadcast) и в группу 239.255.255.250 на
   1900/1990/2021. На ``M-SEARCH`` отвечает ``200 OK`` с ``Location`` и
   полями ``DevModel/DevName/DevConnect/DevBind/Devseclink/DevVersion/DevCap``.
   UDP :2021 шлюз **не слушает**: там сидит сетевой плагин самой Studio.
2. **Проба личности :3000 (TCP) и :3002 (TLS).** Кадр ``A5A5`` + длина
   (u16 LE, считается по всему кадру) + JSON + ``A7A7``. На
   ``{"login":{"command":"detect","sequence_id":"…"}}`` станок отвечает
   серийником, моделью, ``bind: free``, ``connect: lan``, ``dev_cap: 1`` и
   версией прошивки, **эхом повторяя sequence_id числом**. На ``login`` —
   ``login_report`` со ``status: SUCCESS``. Нет ответа — Studio пишет
   «Сбой подключения, код=-1», не дойдя ни до Access Code, ни до MQTT.
3. **MQTT/TLS :8883.** Пользователь ``bblp``, пароль — Access Code (8
   символов). ``CONNACK`` 0 (вход принят) или 4 (код не подошёл). Отчёты
   уходят publish-ом в ``device/<серийник>/report``: ``get_version``,
   ``push_status`` на ``pushall``, результат ``project_file``.
4. **Implicit FTPS :990.** ``USER bblp`` → 331, ``PASS <код>`` → 230,
   ``PASV`` → 227 с адресом из ``host_pinned``, ``STOR`` → 150, после
   загрузки → 226, файл уходит в очередь PrintFlow.

Почему раньше получалось «код=-1», хотя руками всё пробивалось. На машине
поднят VPN: появляется ``tun0`` с адресом ``10.0.0.1/30``, и ядро отвечает
на «какой у меня адрес» именно им. Этот адрес уезжал в ``Location`` SSDP и в
``PASV`` — Studio честно шла на 10.0.0.1, где принтера нет. Три защиты:

* :func:`printflow.config.get_local_ips` перечисляет интерфейсы и отбрасывает
  туннели (``tun*``/``tap*``/``utun*``/WireGuard/Tailscale/PPP, мосты
  Docker/Hyper-V/VirtualBox) и любые префиксы /30 и уже — это точка-точка,
  а не сегмент LAN;
* ``studio_gateway_host`` (host_pinned) имеет безусловный приоритет, а
  исходящие объявления привязываются к LAN-адресу явно (bind к нему и, на
  Linux, ``IP_PKTINFO`` в ``sendmsg``), поэтому ответ на ``M-SEARCH`` уходит
  c 192.168.0.108, а не с 10.0.0.1 — даже с поднятым ``tun0``;
* если VPN всё-таки перехватывает LAN, в журнал и в ``/api/studio/status``
  пишется внятный совет, а не «код=-1».

Слушают MQTT/FTPS/BIND на ``0.0.0.0`` (иначе Studio с другого компьютера не
достучится), а в ``Location`` и ``PASV`` отдаётся закреплённый адрес.

Автостарт печати — только при ``studio_gateway_mode=autostart`` **и**
``studio_gateway_autostart`` **и** ``unattended_dangerous_actions``.

Тесты передают ``bind=False``: сокеты не открываются, а хуки identity /
ssdp_notify / ingest_bytes / mqtt_handle_packet / ftp_command работают без
сети.
"""
from __future__ import annotations

import errno
import json
import re
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
from pathlib import Path

# get_local_ips импортируется ПОЗДНО, внутри _local_ips(): тесты и
# диагностика подменяют connector.printflow.config.get_local_ips, а
# привязанное при импорте имя подмену бы не увидело.
from .config import UPLOAD_DIR, now_iso
from .studio_mqtt import (
    CONNECT,
    DISCONNECT,
    PINGREQ,
    PUBLISH,
    SUBSCRIBE,
    UNSUBSCRIBE,
    decode_connect,
    decode_publish,
    decode_subscribe,
    decode_unsubscribe,
    encode_connack,
    encode_pingresp,
    encode_puback,
    encode_publish,
    encode_suback,
    encode_unsuback,
    parse_fixed_header,
    read_packet,
)

# --------------------------------------------------------------------- SSDP
SSDP_NT = "urn:bambulab-com:device:3dprinter:1"
SSDP_GROUP = "239.255.255.250"
# Настоящие принтеры шлют NOTIFY широковещательно на 255.255.255.255:2021,
# а сетевой плагин Studio слушает UDP :2021. Дополнительно рассылаем в
# группу 239.255.255.250 — так шлюз видят и старые клиенты (1990/1900).
SSDP_BROADCAST = "255.255.255.255"
SSDP_PORTS = (2021, 1990, 1900)
# Порты, которые шлюз СЛУШАЕТ. UDP 2021 шлюз не занимает и занимать не должен:
# на нём слушает сам сетевой плагин Bambu Studio. Две розетки на одном порту с
# SO_REUSEADDR в Windows делят входящий трафик непредсказуемо — шлюз,
# поднявшийся раньше Studio, отбирал у неё единственный источник адреса
# принтера, Studio получала пустой dev_ip и отдавала «код=-1» до MQTT.
SSDP_LISTEN_PORTS = (1900,)
SSDP_NOTIFY_PERIOD = 5.0  # принтеры анонсируют себя раз в ~5 секунд
SSDP_NOTIFY_LOOPBACK = "127.0.0.1"  # адрес, по которому Studio видит шлюз всегда
SSDP_REPLY_SIZE = 4096
# IP_PKTINFO — управляющее сообщение, которым в Linux задаётся адрес-источник
# дейтаграммы. Python экспортирует эту константу не во всех сборках, поэтому
# берём значение из ABI платформы: без него ответ на M-SEARCH ушёл бы с того
# адреса, который выбрала таблица маршрутизации, — то есть с адреса
# VPN-туннеля, и Studio отбросила бы ответ (ровно тот «код=-1»).
IP_PKTINFO = getattr(socket, "IP_PKTINFO",
                     8 if sys.platform.startswith("linux") else 0)

# --------------------------------------------------------------- порты/имена
MQTT_PORT = 8883
FTP_PORT = 990
# Порт, на котором Studio спрашивает личность принтера ДО MQTT
# (bambu_network_bind_detect): обычный TCP, один кадр login/detect.
# Нет ответа — Studio не добавляет принтер и пишет «Сбой подключения, код=-1».
BIND_PORT_PLAIN = 3000
BIND_PORT_TLS = 3002
BIND_MAGIC_HEAD = b"\xa5\xa5"
BIND_MAGIC_TAIL = b"\xa7\xa7"
BIND_FRAME_MIN = 6
BIND_FRAME_MAX = 0xFFFF
BIND_TIMEOUT = 15
# Прошивка, которую шлюз называет Studio (SSDP DevVersion и login/detect).
FIRMWARE_VERSION = "01.07.00.00"
MQTT_USER = "bblp"
MQTT_KEEPALIVE = 90
FTP_BANNER = "220 PrintFlow Studio Gateway"
# Сколько байт входящего файла держим в памяти до записи в библиотеку.
INCOMING_LIMIT = 32

DEV_MODELS = {
    "P1S": "C12",
    "P1P": "C11",
    "X1C": "BL-P001",
    "X1 Carbon": "BL-P001",
    "X1": "BL-P002",
    "X1E": "C13",
    "A1": "N2S",
    "A1 mini": "N1",
    "A1 Mini": "N1",
}

_PLATE_RE = re.compile(r"plate[_\-]?(\d+)", re.I)
_DIGITS_RE = re.compile(r"-?\d+")


class MqttSession:
    """Состояние **одного** MQTT/TLS-соединения.

    Почему не общий флаг. Настоящий станок авторизует каждое соединение
    отдельно. Если держать одну переменную на весь шлюз, то чужой клиент с
    неверным Access Code (или просто DISCONNECT) сбрасывает авторизацию и у
    уже работающей Studio: её PUBLISH начинают молча выбрасываться, отчёты в
    ``device/<серийник>/report`` прекращаются, а плагин показывает «подключён,
    но данных нет». У PrintFlow к тому же есть собственное соединение с
    принтером — оно не должно влиять на шлюз и наоборот.

    Экземпляр живёт ровно столько, сколько живёт соединение, и передаётся в
    :meth:`StudioGateway.mqtt_handle_packet` явно.
    """

    __slots__ = ("authed", "client_id", "peer", "connected_at")

    def __init__(self, client_id: str = "", peer: str = ""):
        self.authed = False
        self.client_id = client_id
        self.peer = peer
        self.connected_at = time.time()


def is_loopback(host: object) -> bool:
    """Адрес из loopback: 127.0.0.0/8, localhost, ::1."""
    text = str(host or "").strip().lower()
    return text.startswith("127.") or text in ("localhost", "::1", "[::1]")


def directed_broadcast(ip: str) -> str:
    """Направленный широковещательный адрес для /24: 192.168.1.50 → .255.

    Пакет на такой адрес доходит до соседей по подсети на домашних роутерах
    надёжнее, чем на «общий» 255.255.255.255: последний часть оборудования
    режет. Если адрес не IPv4 — пустая строка (рассылать некуда).
    """
    parts = str(ip or "").strip().split(".")
    if len(parts) != 4:
        return ""
    for part in parts:
        if not part.isdigit() or not 0 <= int(part) <= 255:
            return ""
    return ".".join(parts[:3] + ["255"])


def _dev_model(model: str) -> str:
    """Имя модели → код устройства, который ждёт плагин Studio (P1S → C12)."""
    name = (model or "").strip()
    if name in DEV_MODELS:
        return DEV_MODELS[name]
    upper = name.upper()
    for key, value in DEV_MODELS.items():
        if key.upper() == upper:
            return value
    return "C12"


def _new_serial() -> str:
    """Серийник в формате Bambu: префикс P1S + 9 hex-символов, всего 15."""
    return "01P00A" + secrets.token_hex(5)[:9].upper()


def sequence_id_int(value: object) -> int:
    """Эхо ``sequence_id`` числом — так отвечает прошивка станка.

    Studio шлёт sequence_id строкой (``"20000"``), а ждёт в ответе число:
    плагин сравнивает ``int(response.sequence_id) == int(request.sequence_id)``
    и на несовпадении молча отбрасывает ответ, показывая «код=-1». Раньше шлюз
    отвечал фиксированным 3021 — совпадало только с одним конкретным клиентом.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value)
    text = str(value if value is not None else "").strip()
    if not text:
        return 0
    match = _DIGITS_RE.search(text)
    if not match:
        return 0
    try:
        return int(match.group(0))
    except ValueError:
        return 0


def encode_bind_frame(payload: dict) -> bytes:
    """Кадр порта 3000/3002: A5A5 + длина (u16 LE) + JSON + A7A7.

    Длина считается по всему кадру вместе с обеими магиями и полем длины —
    так же, как её кладёт прошивка принтера (и как читает плагин Studio).
    """
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    total = len(BIND_MAGIC_HEAD) + 2 + len(body) + len(BIND_MAGIC_TAIL)
    if total > BIND_FRAME_MAX:
        raise ValueError("кадр шлюза слишком большой для поля длины")
    return BIND_MAGIC_HEAD + struct.pack("<H", total) + body + BIND_MAGIC_TAIL


def decode_bind_frame(chunk: bytes) -> tuple[dict | None, bytes]:
    """Разобрать первый кадр из буфера: (payload, остаток).

    Возвращает ``(None, rest)``, если кадра нет целиком или он битый —
    вызывающий копит буфер дальше. Это тот же разбор, что делает
    Bambu-плагин (ищет магию, читает длину, проверяет хвост).
    """
    data = bytes(chunk or b"")
    start = data.find(BIND_MAGIC_HEAD)
    if start < 0:
        return None, b""
    if start:
        data = data[start:]
    if len(data) < BIND_FRAME_MIN:
        return None, data
    total = struct.unpack_from("<H", data, 2)[0]
    if total < BIND_FRAME_MIN or len(data) < total:
        return None, data
    if data[total - 2:total] != BIND_MAGIC_TAIL:
        return None, data[2:]
    try:
        payload = json.loads(data[4:total - 2].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, data[total:]
    if not isinstance(payload, dict):
        return None, data[total:]
    return payload, data[total:]


def _filename_from_url(url: str) -> str:
    """Имя файла из ``ftp:///…/model.gcode.3mf`` (или '' — если не разобрать)."""
    text = str(url or "").strip()
    if not text:
        return ""
    text = text.split("?", 1)[0]
    return Path(text.replace("\\", "/").rstrip("/")).name


class StudioGateway:
    """Эмулятор принтера Bambu для Studio → очередь PrintFlow."""

    def __init__(self, db, manager, bus=None, bind: bool = True):
        self.db = db
        self.manager = manager
        self.bus = bus
        self.bind = bool(bind)
        self.last_error = ""
        # Диагностика: сбой каждого сервиса отдельно (SSDP/MQTT/FTPS/BIND/TLS/
        # хост/VPN), счётчики подключений и неудачных авторизаций, адрес
        # последнего клиента. Видны в /api/studio/status и в журнале коннектора.
        self._errors: dict[str, str] = {
            "ssdp": "", "mqtt": "", "ftps": "", "bind": "", "tls": "",
            "host": "", "vpn": "", "cert": "",
        }
        self._counters: dict[str, int] = {
            "bind_requests": 0,
            "bind_detects": 0,
            "bind_logins": 0,
            "mqtt_connections": 0,
            "mqtt_auth_failures": 0,
            "mqtt_publishes": 0,
            "ftp_connections": 0,
            "ftp_auth_failures": 0,
            "ftp_uploads": 0,
            "ssdp_notify": 0,
            "ssdp_searches": 0,
            "dropped_connections": 0,
        }
        self._last_client = ""
        self._last_auth_fail_at = ""
        self._tls_ctx = None
        self._cert_cn = ""
        self._cert_san = ""
        self._cert_expires = ""      # до какого числа действует сертификат
        self._bind_socks: list = []
        self._data_conn = None
        self._data_ready = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._incoming: dict[str, bytes] = {}
        # Состояние MQTT-соединения «по умолчанию» — для вызовов
        # mqtt_handle_packet() без явной сессии (тесты, диагностика, скрипты
        # проверки). Живые клиенты получают собственную MqttSession.
        self._mqtt_session = MqttSession()
        self._ftp_user = ""
        self._ftp_authed = False
        self._ftp_cwd = "/"
        self._stor_name = ""
        self._ssdp_sock = None
        self._ssdp_tx = None          # исходящий сокет, привязанный к LAN-адресу
        self._ssdp_tx_lo = None       # отдельный сокет для отправки в loopback
        self._ssdp_source = ""        # адрес-источник объявлений (для статуса)
        self._ssdp_bound_port = 0
        self._ssdp_note = ""
        self._mqtt_sock = None
        self._ftp_sock = None
        self._threads: list[threading.Thread] = []
        self._last_notify = 0.0
        self._host_cache = ""
        self._ips_cache: list[str] = []
        self._ips_cache_at = 0.0
        self._vpn_cache: dict = {}
        self._vpn_cache_at = 0.0
        self._vpn_advice = ""         # уже сказанный совет (не спамим журналом)
        self._pending_confirm: dict[str, dict] = {}

    @property
    def _mqtt_authed(self) -> bool:
        """Авторизована ли сессия «по умолчанию» (см. :class:`MqttSession`).

        Свойство оставлено ради совместимости: старые вызовы и диагностика
        обращались к флагу напрямую. Живые соединения имеют свою сессию и
        через это свойство друг на друга не влияют.
        """
        return self._mqtt_session.authed

    @_mqtt_authed.setter
    def _mqtt_authed(self, value: bool) -> None:
        self._mqtt_session.authed = bool(value)

    # ----------------------------------------------------------- настройки
    def _enabled(self) -> bool:
        try:
            return bool(self.db.setting("studio_gateway_enabled", False))
        except Exception:
            return False

    def _access_code(self) -> str:
        try:
            return str(self.db.setting("studio_gateway_access_code", "") or "")
        except Exception:
            return ""

    def _autostart_allowed(self) -> bool:
        """Автозапуск печати: три независимых подтверждения владельца."""
        try:
            mode = str(self.db.setting("studio_gateway_mode", "confirm") or "confirm").strip().lower()
            return (
                mode == "autostart"
                and bool(self.db.setting("studio_gateway_autostart", False))
                and bool(self.db.setting("unattended_dangerous_actions", False))
            )
        except Exception:
            return False

    def _mode(self) -> str:
        try:
            return str(self.db.setting("studio_gateway_mode", "confirm") or "confirm").strip().lower()
        except Exception:
            return "confirm"

    def _name(self) -> str:
        try:
            name = str(self.db.setting("studio_gateway_name", "NOZZA-PrintFlow")
                       or "NOZZA-PrintFlow").strip()
        except Exception:
            name = ""
        return name or "NOZZA-PrintFlow"

    def _ensure_identity(self) -> None:
        """Создать серийник и Access Code, если их ещё нет.

        Обязательно под блокировкой. Без неё два потока (``start()`` и поток
        SSDP, или два клиента на :3000 одновременно) оба видели пустой
        серийник и записывали каждый свой: объявление уходило с одним USN, а
        в базе оставался другой. Для Studio это выглядит как принтер, который
        меняет личность, — устройство приходится удалять и добавлять заново.
        """
        with self._lock:
            try:
                serial = str(
                    self.db.setting("studio_gateway_serial", "") or "").strip()
            except Exception:
                return
            patch: dict[str, object] = {}
            if not serial:
                patch["studio_gateway_serial"] = _new_serial()
            try:
                need_code = not self._access_code()
            except Exception:
                need_code = False
            if need_code:
                # Ровно 8 hex-символов: столько Bambu Studio показывает в поле
                # «Код доступа» и столько же помещается в её внутренний буфер.
                patch["studio_gateway_access_code"] = secrets.token_hex(4)
            if patch:
                try:
                    self.db.set_settings(patch)
                except Exception:
                    pass

    # ------------------------------------------------------ адрес и VPN
    def _pinned_host(self) -> str:
        """Адрес из настройки studio_gateway_host (пусто = авто).

        Владелец закрепляет адрес, когда на компьютере есть VPN / VirtualBox /
        Hyper-V / Docker: их виртуальные адреса оказываются первыми в
        get_local_ips(), Studio получает их в SSDP и подключается не туда.
        Закреплённый адрес имеет безусловный приоритет. Непохожее на IPv4
        значение игнорируется, но попадает в диагностику.
        """
        try:
            raw = str(self.db.setting("studio_gateway_host", "") or "").strip()
        except Exception:
            return ""
        if not raw:
            # Пустая настройка — сбрасываем прошлую ошибку хоста. Ячейку не
            # удаляем: диагностика (и аудит) ждут, что у каждой службы есть
            # своё место для ошибки, даже если сейчас она пуста.
            self._errors["host"] = ""
            return ""
        parts = raw.split(".")
        ok = len(parts) == 4
        if ok:
            for part in parts:
                if not part.isdigit() or not 0 <= int(part) <= 255:
                    ok = False
                    break
        if not ok:
            message = f"настройка studio_gateway_host не похожа на IPv4: {raw!r}"
            if self._errors.get("host") != message:
                self._errors["host"] = message
                self.last_error = f"host: {message}"
                try:
                    from .logging_setup import log
                    log().warning("Шлюз Bambu Studio: %s", message)
                except Exception:
                    pass
            return ""
        self._errors["host"] = ""
        return raw

    def _host_ip(self) -> str:
        """Адрес, который шлюз называет Studio (SSDP Location, PASV).

        Слушаем при этом на ``0.0.0.0``: иначе Studio с другого компьютера не
        достучится вовсе. Разделение «на чём слушаем» и «что объявляем» —
        главный обход VPN-перехвата.
        """
        pinned = self._pinned_host()
        if pinned:
            return pinned
        if not self.bind:
            return "127.0.0.1"
        if self._host_cache:
            return self._host_cache
        try:
            ips = self._local_ips()
            if ips:
                self._host_cache = ips[0]
                return self._host_cache
        except Exception:
            pass
        return "127.0.0.1"

    def _local_ips(self) -> list[str]:
        """LAN-адреса этого ПК (VPN-туннели отфильтрованы в config.get_local_ips).

        Список кэшируется: перечисление интерфейсов в Windows — это вызов
        PowerShell, а адреса спрашивают и рассылка, и карточка настроек.
        """
        now = time.time()
        if self._ips_cache and now - self._ips_cache_at < 30.0:
            return self._ips_cache
        try:
            # Поздний импорт: тесты подменяют connector.printflow.config.
            from .config import get_local_ips as lookup
            ips = [ip for ip in lookup() if ip and not ip.startswith("127.")]
        except Exception:
            ips = []
        self._ips_cache = ips
        self._ips_cache_at = now
        return ips

    def _lan_source_ip(self) -> str:
        """Адрес-источник для исходящих объявлений ('' — привязывать не к чему).

        Именно он пишется в ``bind()`` исходящего UDP-сокета и в ``IP_PKTINFO``,
        поэтому ответ на M-SEARCH уходит с LAN-адреса (192.168.0.108), а не с
        адреса туннеля (10.0.0.1), даже когда ``tun0`` поднят и держит
        маршрут по умолчанию. Loopback не привязываем: для него это бессмысленно
        и сломало бы доставку на 127.0.0.1:2021.
        """
        pinned = self._pinned_host()
        ips = self._local_ips()
        if pinned and pinned in ips:
            return pinned
        if ips:
            return ips[0]
        # Адрес закреплён, но на интерфейсах его нет (VPN забрал сеть или
        # опечатка). Пробуем привязаться к нему: если не выйдет, шлюз честно
        # напишет об этом в диагностику и продолжит слать без привязки.
        if pinned and not pinned.startswith("127."):
            return pinned
        return ""

    def _vpn_report(self, force: bool = False) -> dict:
        """Перехватывает ли VPN локальную сеть (кэш 30 с)."""
        now = time.time()
        if not force and self._vpn_cache and now - self._vpn_cache_at < 30.0:
            return self._vpn_cache
        try:
            from .config import vpn_interception
            report = vpn_interception(self._host_ip())
        except Exception:
            report = {"active": False, "intercepting": False, "tunnels": [],
                      "culprits": [], "names": "", "lan": [], "advice": "",
                      "detail": "", "default_interface": "",
                      "default_is_vpn": False, "subnet": "", "host": "",
                      "host_missing": False}
        self._vpn_cache = report
        self._vpn_cache_at = now
        return report

    def _warn_vpn(self) -> str:
        """Совет про VPN — в журнал и в статус. Возвращает текст ('' — тихо).

        Пишем один раз на изменение состояния: рассылка идёт каждые 5 секунд,
        и повтор одного и того же совета засорил бы журнал коннектора.
        """
        report = self._vpn_report()
        advice = str(report.get("advice") or "")
        if not report.get("intercepting"):
            if self._errors.get("vpn"):
                self._errors["vpn"] = ""
            self._vpn_advice = ""
            return ""
        if not advice:
            advice = ("VPN перехватывает LAN, отключите туннельный адаптер "
                      "или добавьте подсеть принтера в split-tunnel")
        self._errors["vpn"] = advice
        self.last_error = f"VPN: {advice}"
        if advice != self._vpn_advice:
            self._vpn_advice = advice
            try:
                from .logging_setup import log
                log().warning("Шлюз Bambu Studio: %s", advice)
                if report.get("detail"):
                    log().warning("Шлюз Bambu Studio: пропущены адреса: %s",
                                  report["detail"])
            except Exception:
                pass
        return advice

    # ---------------------------------------------------------- личность
    def _bound_printer(self):
        """Принтер, к которому привязан шлюз (для AMS и команд pause/stop)."""
        if not self.manager or not hasattr(self.manager, "get"):
            return None
        try:
            pid = str(self.db.setting("studio_gateway_printer_id", "") or "").strip()
        except Exception:
            pid = ""
        if pid:
            return self.manager.get(pid)
        try:
            from .virtual import VIRTUAL_ID
        except Exception:
            VIRTUAL_ID = ""
        printers = getattr(self.manager, "printers", {}) or {}
        for printer in printers.values():
            record = getattr(printer, "record", {}) or {}
            if str(record.get("id") or "") == VIRTUAL_ID:
                continue
            return printer
        return self.manager.get()

    def _printer_model(self) -> str:
        printer = self._bound_printer()
        if printer:
            record = getattr(printer, "record", {}) or {}
            model = str(record.get("model") or "").strip()
            if model:
                return model
        return "P1S"

    def identity(self) -> dict:
        """Публичная личность шлюза. Access Code сюда не входит."""
        try:
            self._ensure_identity()
        except Exception:
            pass
        try:
            model = self._printer_model()
        except Exception:
            model = "P1S"
        try:
            serial = str(self.db.setting("studio_gateway_serial", "") or "").strip()
        except Exception:
            # БД закрыта (завершение процесса / тест) — не роняем SSDP-поток.
            # Серийник остаётся прежним; если его не знаем — временный, без записи в БД.
            serial = ""
        name = self._name()
        if not serial:
            # В нормальном старте сюда не попадаем: _ensure_identity уже создала серийник.
            # При закрытой БД возвращаем временный, чтобы объявление не было пустым.
            serial = _new_serial()
        try:
            host = self._host_ip()
        except Exception:
            host = "127.0.0.1"
        return {
            "serial": serial,
            "name": name,
            "model": model,
            "dev_model": _dev_model(model),
            "host": host,
            "mqtt_port": MQTT_PORT,
            "ftp_port": FTP_PORT,
            "bind_port": BIND_PORT_PLAIN,
            "bind_tls_port": BIND_PORT_TLS,
            "version": FIRMWARE_VERSION,
        }

    # ---------------------------------------------------- диагностика
    def _note_auth_failure(self, channel: str) -> None:
        """Засчитать отказ авторизации (MQTT или FTP) и показать в журнале.

        Studio, которой ввели неверный Access Code, молча стучится снова и
        снова — без счётчика владелец видит только «код=-1» без причины.
        """
        key = f"{channel}_auth_failures"
        with self._lock:
            self._counters[key] = int(self._counters.get(key, 0)) + 1
            self._last_auth_fail_at = now_iso()
            total = self._counters[key]
        try:
            from .logging_setup import log
            log().warning(
                "Шлюз Bambu Studio: %s: клиент не прошёл Access Code (раз: %d)",
                "MQTT" if channel == "mqtt" else "FTPS", total)
        except Exception:
            pass

    def _bump(self, key: str, delta: int = 1) -> int:
        """Увеличить счётчик и вернуть новое значение."""
        with self._lock:
            self._counters[key] = int(self._counters.get(key, 0)) + int(delta)
            return self._counters[key]

    def _record_error(self, service: str, exc: Exception | str) -> None:
        """Сохранить сбой сервиса шлюза и продублировать его в журнал.

        Раньше last_error перезаписывался следующим сервисом: при занятых
        8883 и 990 владелец видел только «FTPS: …» и не знал про MQTT.
        """
        text = str(exc).strip() or "неизвестная ошибка"
        key = service.lower()
        self._errors[key] = text
        self.last_error = f"{service}: {text}"
        try:
            from .logging_setup import log
            log().warning("Шлюз Bambu Studio: %s: %s", service, text)
        except Exception:
            pass

    def _record_note(self, service: str, text: str) -> None:
        """Предупреждение в диагностику: служба работает, но есть риск.

        От :meth:`_record_error` отличается тем, что не трогает ``last_error``:
        владелец ищет по нему причину отказа, а предупреждение про сертификат
        её затрёт.
        """
        message = str(text or "").strip()
        if not message or self._errors.get(service.lower()) == message:
            return
        self._errors[service.lower()] = message
        try:
            from .logging_setup import log
            log().warning("Шлюз Bambu Studio: %s: %s", service, message)
        except Exception:
            pass

    def _note_dropped_connection(self, exc: Exception | str) -> None:
        """Соединение отброшено (не TLS, обрыв, сбой рукопожатия) — не сбой службы."""
        self._bump("dropped_connections")
        try:
            from .logging_setup import log
            log().debug("Шлюз Bambu Studio: соединение отброшено: %s", exc)
        except Exception:
            pass

    def status(self) -> dict:
        """Состояние шлюза для /api/studio/status и карточки в настройках."""
        # identity может упасть на закрытой БД — статус всё равно отдаём, без падения карточки.
        try:
            ident = self.identity()
        except Exception:
            ident = {
                "serial": "", "name": "NOZZA-PrintFlow", "model": "P1S", "dev_model": "C12",
                "host": "127.0.0.1", "mqtt_port": MQTT_PORT, "ftp_port": FTP_PORT,
                "bind_port": BIND_PORT_PLAIN, "bind_tls_port": BIND_PORT_TLS,
                "version": FIRMWARE_VERSION,
            }
        try:
            pinned_raw = str(self.db.setting("studio_gateway_host", "") or "").strip()
        except Exception:
            pinned_raw = ""
        with self._lock:
            counters = dict(self._counters)
            last_client = self._last_client
            last_auth_fail_at = self._last_auth_fail_at
            ssdp_source = self._ssdp_source
            cert_cn, cert_san = self._cert_cn, self._cert_san
            cert_expires = self._cert_expires
        # настройки — каждая с защитой от закрытой БД
        mode = self._mode()
        try:
            autostart = bool(self.db.setting("studio_gateway_autostart", False))
        except Exception:
            autostart = False
        try:
            printer_id = str(self.db.setting("studio_gateway_printer_id", "") or "")
        except Exception:
            printer_id = ""
        try:
            ssdp_targets = [f"{host}:{port}" for host, port in self._notify_targets()]
        except Exception:
            ssdp_targets = [f"{SSDP_NOTIFY_LOOPBACK}:{SSDP_PORTS[0]}"]
        try:
            has_code = bool(self._access_code())
        except Exception:
            has_code = False
        try:
            lan_ips = list(self._local_ips())
        except Exception:
            lan_ips = []
        vpn = self._vpn_report()
        errors = {key: value for key, value in self._errors.items() if value}
        out = {
            "enabled": self._enabled(),
            "running": bool(self._mqtt_sock or self._ftp_sock or self._ssdp_sock
                            or self._bind_socks),
            "mqtt_running": bool(self._mqtt_sock),
            "ftp_running": bool(self._ftp_sock),
            "ssdp_running": bool(self._ssdp_sock),
            "bind": self.bind,
            "mode": mode,
            "autostart": autostart,
            "autostart_allowed": self._autostart_allowed(),
            "printer_id": printer_id,
            "mqtt_port": MQTT_PORT,
            "mqtt_user": MQTT_USER,
            "ftp_port": FTP_PORT,
            "bind_port": BIND_PORT_PLAIN,
            "bind_tls_port": BIND_PORT_TLS,
            "bind_ports": [BIND_PORT_PLAIN, BIND_PORT_TLS],
            "bind_running": bool(self._bind_socks),
            "bind_requests": int(counters.get("bind_requests", 0)),
            "bind_detects": int(counters.get("bind_detects", 0)),
            "bind_logins": int(counters.get("bind_logins", 0)),
            "dropped_connections": int(counters.get("dropped_connections", 0)),
            "ssdp_ports": list(SSDP_PORTS),
            "ssdp_listen_ports": list(SSDP_LISTEN_PORTS),
            "ssdp_bound_port": int(self._ssdp_bound_port),
            "ssdp_note": self._ssdp_note,
            "ssdp_targets": ssdp_targets,
            "ssdp_notify": int(counters.get("ssdp_notify", 0)),
            "ssdp_searches": int(counters.get("ssdp_searches", 0)),
            # Адрес, с которого реально уходят объявления. Если он не совпал
            # с host — Studio получит ответ с чужого адреса и не поверит.
            "ssdp_source": ssdp_source,
            "last_error": self.last_error,
            "errors": errors,
            "host_pinned": bool(pinned_raw),
            "host_local": bool(ident["host"] in lan_ips) or ident["host"].startswith("127."),
            "lan_ips": lan_ips,
            "vpn_active": bool(vpn.get("active")),
            "vpn_intercepting": bool(vpn.get("intercepting")),
            "vpn_names": str(vpn.get("names") or ""),
            "vpn_advice": str(vpn.get("advice") or ""),
            "vpn_tunnels": [
                {"name": item.get("name"), "ip": item.get("ip"),
                 "prefix": item.get("prefix"), "reason": item.get("reason")}
                for item in (vpn.get("tunnels") or [])
            ],
            "mqtt_connections": int(counters.get("mqtt_connections", 0)),
            "mqtt_auth_failures": int(counters.get("mqtt_auth_failures", 0)),
            "mqtt_publishes": int(counters.get("mqtt_publishes", 0)),
            "ftp_connections": int(counters.get("ftp_connections", 0)),
            "ftp_auth_failures": int(counters.get("ftp_auth_failures", 0)),
            "ftp_uploads": int(counters.get("ftp_uploads", 0)),
            "last_client": last_client,
            "last_auth_fail_at": last_auth_fail_at,
            "has_access_code": has_code,
            "cert_cn": cert_cn,
            "cert_san": cert_san,
            "cert_expires": cert_expires,
            "urn": SSDP_NT,
        }
        out.update(ident)
        # Access Code в статус не попадает принципиально: /api/studio/status
        # читает браузер, а секрет живёт только в БД и в MQTT/FTPS-входе.
        out.pop("access_code", None)
        return out

    # ----------------------------------------------------------- SSDP
    def _ssdp_headers(self) -> str:
        """Поля объявления, которые разбирает сетевой плагин Studio.

        DevVersion / DevCap / Devseclink / DevInf не косметика: плагин
        собирает из них JSON для DeviceManager и падает на отсутствующих
        ключах, а по DevVersion Studio решает, как общаться с устройством.
        """
        ident = self.identity()
        return (
            f"DevModel.bambu.com: {ident['dev_model']}\r\n"
            f"DevName.bambu.com: {ident['name']}\r\n"
            "DevSignal.bambu.com: -44\r\n"
            "DevConnect.bambu.com: lan\r\n"
            "DevBind.bambu.com: free\r\n"
            "Devseclink.bambu.com: secure\r\n"
            "DevInf.bambu.com: eth0\r\n"
            f"DevVersion.bambu.com: {ident['version']}\r\n"
            "DevCap.bambu.com: 1\r\n"
        )

    def ssdp_notify(self) -> str:
        """Объявление ``ssdp:alive`` — то, что станок шлёт каждые 5 секунд."""
        ident = self.identity()
        return (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_GROUP}:1900\r\n"
            "Server: Buildroot/2018.02-rc3 UPnP/1.0 ssdpd/1.8\r\n"
            f"Location: {ident['host']}\r\n"
            f"NT: {SSDP_NT}\r\n"
            "NTS: ssdp:alive\r\n"
            f"USN: {ident['serial']}\r\n"
            "Cache-Control: max-age=1800\r\n"
            + self._ssdp_headers()
            + "\r\n"
        )

    def ssdp_search_response(self) -> str:
        """Ответ ``200 OK`` на M-SEARCH: те же поля, что и в NOTIFY."""
        ident = self.identity()
        return (
            "HTTP/1.1 200 OK\r\n"
            f"ST: {SSDP_NT}\r\n"
            f"USN: {ident['serial']}\r\n"
            f"Location: {ident['host']}\r\n"
            "Cache-Control: max-age=1800\r\n"
            "Server: Buildroot/2018.02-rc3 UPnP/1.0 ssdpd/1.8\r\n"
            + self._ssdp_headers()
            + "\r\n"
        )

    def _notify_targets(self) -> list[tuple[str, int]]:
        """Куда шлём NOTIFY, чтобы Studio увидел шлюз на любой машине.

        Порядок — от самого надёжного канала к самому капризному:

        1. **loopback 127.0.0.1:2021** — сюда смотрит сетевой плагин Studio,
           когда Studio стоит на том же компьютере, что и PrintFlow. Loopback
           не фильтруется брандмауэром Windows вообще, поэтому объявление
           доходит при любых правилах;
        2. **закреплённый/свой адрес :2021** — та же доставка через интерфейс
           LAN (Windows считает трафик на собственный адрес локальным);
        3. **направленный broadcast x.y.z.255:2021** и **255.255.255.255:2021**
           — как шлёт настоящий станок, чтобы Studio увидел шлюз и с другого
           компьютера в сети;
        4. **группа 239.255.255.250** (2021/1990/1900) — для старых клиентов
           и сторонних искалок (OrcaSlicer, Home Assistant).

        Если адрес закреплён (``studio_gateway_host``), другие LAN-адреса в
        список НЕ добавляются: объявлять принтера сразу с нескольких адресов —
        значит плодить в Studio дубли и уводить её на виртуальный адаптер.
        """
        host = self._host_ip()
        targets: list[tuple[str, int]] = [(SSDP_NOTIFY_LOOPBACK, SSDP_PORTS[0])]
        if host and not host.startswith("127."):
            targets.append((host, SSDP_PORTS[0]))
        if not self._pinned_host():
            for ip in self._local_ips():
                if ip and not ip.startswith("127."):
                    targets.append((ip, SSDP_PORTS[0]))
        directed = directed_broadcast(host)
        if directed and directed != host:
            targets.append((directed, SSDP_PORTS[0]))
        targets.append((SSDP_BROADCAST, SSDP_PORTS[0]))
        targets.extend((SSDP_GROUP, port) for port in SSDP_PORTS)
        unique: list[tuple[str, int]] = []
        for target in targets:
            if target not in unique:
                unique.append(target)
        return unique

    def _make_notify_socket(self):
        """UDP-сокет для объявлений, привязанный к LAN-адресу шлюза.

        Привязка и решает задачу обхода VPN: без неё ядро выбирает адрес
        источника по таблице маршрутизации, и с поднятым ``tun0`` ответ на
        M-SEARCH уходит с 10.0.0.1 — Studio такой ответ отбрасывает, потому
        что он пришёл не с того адреса, который объявлен в Location.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        source = ""
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        candidate = self._lan_source_ip()
        if candidate:
            try:
                sock.bind((candidate, 0))
                source = candidate
            except OSError as exc:
                # Адреса на интерфейсах нет (VPN забрал сеть, опечатка в
                # настройке). Не молчим: причина видна в статусе и журнале.
                self._record_error(
                    "ssdp", f"не удалось привязать объявления к {candidate}: {exc}")
        # Linux: IP_PKTINFO даёт указать источник и для unicast-ответа.
        if IP_PKTINFO:
            try:
                sock.setsockopt(socket.IPPROTO_IP, IP_PKTINFO, 1)
            except OSError:
                pass
        return sock, source

    def _socket_for(self, host: str) -> tuple[object, bool]:
        """Сокет для отправки на конкретный адрес: ``(сокет, закрыть-после)``.

        Почему их два. Ядро не отправит пакет на 127.0.0.1 с источника
        192.168.0.108 — и наоборот. А объявления в loopback нужны именно там,
        где стоит сетевой плагин Studio на этом же компьютере (этот путь не
        фильтруется брандмауэром Windows вообще). Поэтому как только у машины
        появлялся обычный LAN-адрес, единственный привязанный к нему сокет
        начинал молча терять и ``NOTIFY`` на 127.0.0.1:2021, и ответ на
        ``M-SEARCH`` от локальной Studio: ``sendto`` возвращал EINVAL.

        Второй сокет создаётся лениво и живёт до :meth:`stop`.
        """
        if is_loopback(host):
            sock = self._ssdp_tx_lo
            if sock is not None:
                return sock, False
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                                     socket.IPPROTO_UDP)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                except OSError:
                    pass
                sock.bind((SSDP_NOTIFY_LOOPBACK, 0))
            except OSError as exc:
                self._record_error(
                    "ssdp", f"нет сокета для объявлений в loopback: {exc}")
                return None, False
            self._ssdp_tx_lo = sock
            return sock, False
        sock = self._ssdp_tx
        if sock is not None:
            return sock, False
        # Рассылку попросили до старта SSDP (тест, ручной прогрев).
        try:
            sock, _source = self._make_notify_socket()
        except OSError as exc:
            self._record_error("ssdp", f"нет сокета для объявлений: {exc}")
            return None, False
        return sock, True

    def _sendto_lan(self, sock, payload: bytes, addr: tuple[str, int]) -> bool:
        """Отправить дейтаграмму с LAN-адресом в качестве источника.

        На Linux источник задаётся ещё и управляющим сообщением ``IP_PKTINFO``
        (``ipi_spec_dst``): это работает даже тогда, когда сокет привязать не
        удалось, а маршрут по умолчанию уходит в туннель. В остальных системах
        достаточно привязки сокета.
        """
        # В loopback LAN-адресом в качестве источника не отправишь: пакет
        # на 127.0.0.1 обязан идти с 127.0.0.1. Подменяем источник только
        # для адресов реальной сети — там это и есть обход VPN.
        source = "" if is_loopback(addr[0]) else self._lan_source_ip()
        if source and IP_PKTINFO and hasattr(sock, "sendmsg"):
            try:
                packed = socket.inet_aton(source)
                # struct in_pktinfo: индекс интерфейса, ipi_spec_dst (источник,
                # который мы требуем), ipi_addr.
                info = struct.pack("=I4s4s", 0, packed, packed)
                sock.sendmsg([payload], [(socket.IPPROTO_IP, IP_PKTINFO, info)],
                             0, addr)
                return True
            except (OSError, AttributeError, ValueError, struct.error):
                pass   # ядро не поддержало — уходим на обычный sendto
        try:
            sock.sendto(payload, addr)
            return True
        except OSError:
            return False

    def _broadcast_notify(self) -> None:
        """Один цикл рассылки объявления (не чаще SSDP_NOTIFY_PERIOD)."""
        now = time.time()
        if now - self._last_notify < SSDP_NOTIFY_PERIOD:
            return
        self._last_notify = now
        try:
            payload = self.ssdp_notify().encode("utf-8")
        except Exception:
            # БД закрыта или другая ошибка — пропустим этот цикл, но не роняем SSDP
            return
        sent = 0
        temp: list = []
        try:
            for host, port in self._notify_targets():
                # Один недостижимый адрес (например, направленный broadcast в
                # чужой подсети) не должен отменять остальные.
                sock, own = self._socket_for(host)
                if sock is None:
                    continue
                if own:
                    temp.append(sock)
                if self._sendto_lan(sock, payload, (host, port)):
                    sent += 1
        finally:
            for sock in temp:
                try:
                    sock.close()
                except OSError:
                    pass
        if sent:
            self._bump("ssdp_notify", sent)
        self._warn_vpn()

    def _ssdp_reply(self, addr: tuple) -> None:
        """Ответить на M-SEARCH — с LAN-адреса, а не с адреса туннеля."""
        try:
            payload = self.ssdp_search_response().encode("utf-8")
        except Exception:
            return
        sock, own = self._socket_for(addr[0])
        if sock is None:
            # Совсем нет исходящего сокета — отвечаем с розетки приёма: лучше
            # ответ не с того адреса, чем никакого ответа.
            sock, own = self._ssdp_sock, False
        if sock is None:
            return
        try:
            if self._sendto_lan(sock, payload, addr):
                self._bump("ssdp_searches")
        finally:
            if own:
                try:
                    sock.close()
                except OSError:
                    pass

    # ----------------------------------------------------------- ingest
    def ingest_bytes(self, filename: str, data: bytes, command: dict | None = None) -> dict:
        """Принять байты от Studio: библиотека + очередь. Без сокета."""
        if not data:
            raise ValueError("Пустой файл")
        name = Path(str(filename or "project.gcode.3mf").replace("\\", "/")).name
        from .library import FileLibrary
        from .estimate import auto_ams_map, estimate_file
        from .sd_browser import can_print

        lib = FileLibrary(self.db)
        rec = lib.put(name, data, source="studio-gateway")
        estimate: dict = {}
        try:
            local = UPLOAD_DIR / rec["upload_name"]
            if local.is_file():
                estimate = estimate_file(local) or {}
        except Exception:
            estimate = {}
        cmd = command or {}
        print_cmd = cmd.get("print") if isinstance(cmd.get("print"), dict) else cmd
        if not isinstance(print_cmd, dict):
            print_cmd = {}
        plate = 1
        param = str(print_cmd.get("param") or "")
        match = _PLATE_RE.search(param)
        if match:
            plate = max(1, int(match.group(1)))
        mapping = print_cmd.get("ams_mapping")
        if not isinstance(mapping, list):
            mapping = []
            filaments = estimate.get("filaments") or []
            printer = self._bound_printer()
            try:
                ams_auto = bool(self.db.setting("ams_auto_map", True))
            except Exception:
                ams_auto = True
            if filaments and printer and ams_auto:
                try:
                    snap = printer.snapshot()
                    trays = ((snap.get("ams") or {}).get("trays")) or []
                    mapping = auto_ams_map(filaments, trays)
                except Exception:
                    mapping = []
        printer = self._bound_printer()
        printer_id = ""
        if printer:
            printer_id = str((getattr(printer, "record", {}) or {}).get("id") or "")
        try:
            setting_pid = str(self.db.setting("studio_gateway_printer_id", "") or "").strip()
        except Exception:
            setting_pid = ""
        if setting_pid:
            printer_id = setting_pid
        mode = self._mode()
        autostart = self._autostart_allowed()
        job = None
        error = ""
        payload = {
            "file": rec["upload_name"],
            "name": Path(name).stem,
            "source": "studio-gateway",
            "printer_id": printer_id,
            "plate": plate,
            "use_ams": bool(print_cmd.get("use_ams", True)),
            "bed_level": bool(print_cmd.get("bed_leveling", True)),
            "flow_cali": bool(print_cmd.get("flow_cali", False)),
            "timelapse": bool(print_cmd.get("timelapse", False)),
            "ams_mapping": mapping,
            "est_grams": estimate.get("total_grams") or estimate.get("grams") or 0,
            "est_minutes": estimate.get("total_minutes") or estimate.get("minutes") or 0,
            "no_auto": 0 if autostart else 1,
            "allow_auto_start": False,
        }
        # В режиме queue или autostart сразу ставим в очередь;
        # в режиме confirm ждём решения оператора в модальном окне.
        if mode in ("queue", "autostart") and self.manager and hasattr(self.manager, "enqueue") and can_print(name):
            try:
                job = self.manager.enqueue(payload)
            except Exception as exc:
                error = str(exc)
                job = None
            if (autostart and job and not error
                    and hasattr(self.manager, "start_job")):
                try:
                    check = {}
                    if hasattr(self.manager, "preflight"):
                        check = self.manager.preflight(
                            printer_id, rec["upload_name"], plate, mapping) or {}
                    if not check.get("blocks"):
                        self.manager.start_job(job.get("id") or "", printer_id)
                        job = dict(job)
                        job["autostart"] = True
                except Exception as exc:
                    error = str(exc)
        # Сохранение входящего проекта для окна подтверждения оператора
        pending_id = f"st_{int(time.time()*1000)}"
        pending_item = {
            "id": pending_id,
            "filename": name,
            "upload_name": rec["upload_name"],
            "library_id": rec.get("id"),
            "job_id": (job or {}).get("id"),
            "printer_id": printer_id,
            "plate": plate,
            "payload": payload,
            "ams_mapping": mapping,
            "est_grams": estimate.get("total_grams") or estimate.get("grams") or 0,
            "est_minutes": estimate.get("total_minutes") or estimate.get("minutes") or 0,
            "filaments": estimate.get("filaments") or [],
            "thumbnails": estimate.get("thumbnails") or {},
            "at": now_iso(),
            "mode": mode,
            "autostart": autostart,
            "error": error,
        }
        with self._lock:
            self._pending_confirm[pending_id] = pending_item
            if len(self._pending_confirm) > 30:
                oldest = sorted(self._pending_confirm.keys())[:10]
                for k in oldest:
                    self._pending_confirm.pop(k, None)

        try:
            self.db.add_event(
                "studio", "Файл из Bambu Studio",
                name, printer_id,
                {"library_id": rec.get("id"), "job_id": (job or {}).get("id"),
                 "pending_id": pending_id, "autostart": autostart},
            )
        except Exception:
            pass
        if self.bus:
            try:
                self.bus.publish("studio", {
                    "file": name, "library_id": rec.get("id"),
                    "job_id": (job or {}).get("id"),
                    "pending_id": pending_id,
                    "pending": pending_item,
                })
            except Exception:
                pass
        return {
            "library": rec,
            "job": job,
            "estimate": estimate,
            "autostart": autostart,
            "pending_id": pending_id,
            "pending": pending_item,
            "error": error,
        }

    def pending_list(self) -> list[dict]:
        with self._lock:
            return sorted(self._pending_confirm.values(),
                          key=lambda x: str(x.get("at") or ""), reverse=True)

    def pending_get(self, pending_id: str) -> dict | None:
        with self._lock:
            return self._pending_confirm.get(pending_id)

    def pending_dismiss(self, pending_id: str) -> bool:
        with self._lock:
            return bool(self._pending_confirm.pop(pending_id, None))

    def _file_bytes(self, filename: str) -> bytes | None:
        """Найти байты файла, залитого Studio (память → библиотека → uploads)."""
        name = Path(str(filename or "").replace("\\", "/")).name
        if not name:
            return None
        with self._lock:
            if name in self._incoming:
                return self._incoming[name]
            for key, value in self._incoming.items():
                if Path(key).name == name:
                    return value
        try:
            from .library import FileLibrary
            for row in FileLibrary(self.db).list(q=name, limit=8):
                if row.get("name") == name or row.get("upload_name") == name:
                    path = Path(row.get("path") or "")
                    if path.is_file():
                        return path.read_bytes()
        except Exception:
            pass
        upload = UPLOAD_DIR / name
        if upload.is_file():
            return upload.read_bytes()
        return None

    # ------------------------------------------------- порт 3000/3002: detect
    def bind_reply(self, payload: dict) -> dict | None:
        """Ответ шлюза на кадр порта 3000/3002 (проба личности от Studio).

        Studio спрашивает принтер ДО MQTT и ДО FTPS: ``bind_detect``
        открывает обычный TCP на :3000, шлёт ``login/detect`` и ждёт
        серийник, модель и состояние привязки. Раньше на :3000 nobody не
        слушал — соединение сбрасывалось, плагин возвращал
        «socket connect failed» и Studio показывала «код=-1».
        """
        if not isinstance(payload, dict):
            return None
        login = payload.get("login")
        if not isinstance(login, dict):
            return None
        command = str(login.get("command") or "").strip().lower()
        # Эхо sequence_id числом: плагин сверяет его с отправленным и при
        # несовпадении ответ отбрасывает. Фиксированное 3021 подходило только
        # одному клиенту и роняло остальных в «код=-1».
        sequence = sequence_id_int(login.get("sequence_id"))
        ident = self.identity()
        if command == "detect":
            self._bump("bind_requests")
            self._bump("bind_detects")
            return {"login": {
                "bind": "free",
                "command": "detect",
                "connect": "lan",
                "dev_cap": 1,
                "id": ident["serial"],
                "model": ident["dev_model"],
                "name": ident["name"],
                "sequence_id": sequence,
                "version": ident["version"],
            }}
        if command == "login":
            # Привязка к аккаунту — облачная история (тикет + подтверждение в
            # Bambu Cloud), её шлюз не изображает. Локальное подключение по
            # Access Code этим не пользуется, поэтому честно отвечаем успехом
            # LAN-входа: Studio не висит в ожидании ответа до таймаута.
            self._bump("bind_requests")
            self._bump("bind_logins")
            return {"login": {
                "command": "login_report",
                "sequence_id": sequence,
                "session_id": 0,
                "status": "SUCCESS",
            }}
        return None

    def bind_handle_bytes(self, chunk: bytes) -> bytes:
        """Кадр порта 3000/3002 без сети → готовый кадр с ответом (или b'')."""
        payload, _rest = decode_bind_frame(chunk)
        if payload is None:
            return b""
        reply = self.bind_reply(payload)
        if not reply:
            return b""
        return encode_bind_frame(reply)

    def _bind_client(self, conn) -> None:
        """Один клиент порта 3000/3002: обмен кадрами, как у станка.

        Studio может прислать ``detect`` и ``login`` одним соединением, а
        может двумя — поэтому читаем кадры, пока клиент не закроет соединение
        или не выйдет время ожидания, и отвечаем на каждый.
        """
        try:
            conn.settimeout(BIND_TIMEOUT)
            try:
                peer = conn.getpeername()[0]
            except OSError:
                peer = ""
            if peer:
                with self._lock:
                    self._last_client = peer
            buf = b""
            deadline = time.time() + BIND_TIMEOUT
            while time.time() < deadline and len(buf) < 65536:
                try:
                    piece = conn.recv(4096)
                except (socket.timeout, TimeoutError):
                    continue
                except OSError:
                    return
                if not piece:
                    return
                buf += piece
                while True:
                    payload, buf = decode_bind_frame(buf)
                    if payload is None:
                        break
                    reply = self.bind_reply(payload)
                    if not reply:
                        continue
                    try:
                        conn.sendall(encode_bind_frame(reply))
                    except OSError:
                        return
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _listen_tcp(self, port: int, backlog: int = 8, tls: bool = False,
                    cert=None, key=None):
        """Открыть слушающий TCP-порт шлюза, закрыв розетку при сбое.

        Слушаем на ``0.0.0.0``: Studio может стоять на другом компьютере, а
        объявляемый адрес (Location/PASV) при этом берётся из host_pinned.
        Без закрытия розетки занятый порт (второй PrintFlow, Bambu Connect,
        Orca) оставлял висящий сокет — в журнале владелец видел
        ResourceWarning вместо причины, а шлюз считал себя поднятым.
        """
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            raw.bind(("0.0.0.0", port))
            raw.listen(backlog)
            raw.settimeout(1.0)
            if tls:
                ctx = self._tls_ctx or self._tls_context(cert, key)
                return ctx.wrap_socket(raw, server_side=True)
            return raw
        except BaseException:
            try:
                raw.close()
            except OSError:
                pass
            raise

    def _start_bind(self, cert, key) -> None:
        """Поднять :3000 (TCP) и :3002 (TLS) — пробу личности Studio."""
        for port in (BIND_PORT_PLAIN, BIND_PORT_TLS):
            try:
                sock = self._listen_tcp(port, 8, tls=(port == BIND_PORT_TLS),
                                        cert=cert, key=key)
                self._bind_socks.append(sock)
                self._spawn(f"pf-studio-bind-{port}", self._accept_loop,
                            sock, self._bind_client)
            except Exception as exc:
                self._record_error(
                    "bind", f"порт {port}: {exc}" if isinstance(exc, OSError) else exc)

    # ----------------------------------------------------------- MQTT
    def handle_mqtt_request(self, payload: dict, topic: str = "") -> list[dict]:
        """JSON-команда Studio → список отчётов на device/{serial}/report."""
        if not isinstance(payload, dict):
            return []
        reports: list[dict] = []
        print_obj = payload.get("print")
        if isinstance(print_obj, dict):
            reports.extend(self._handle_print_cmd(print_obj))
        pushing = payload.get("pushing")
        if isinstance(pushing, dict) and str(pushing.get("command") or "") in (
                "pushall", "start", "pushsta"):
            reports.append(self._push_status(str(pushing.get("sequence_id") or "0")))
        info = payload.get("info")
        if isinstance(info, dict) and info.get("command") == "get_version":
            reports.append(self._version_report(str(info.get("sequence_id") or "0")))
        system = payload.get("system")
        if isinstance(system, dict) and system.get("command"):
            command = str(system.get("command") or "")
            reply = {
                "system": {
                    "command": command,
                    "sequence_id": str(system.get("sequence_id") or "0"),
                    "result": "success",
                }
            }
            # Сетевой плагин спрашивает код сразу после подключения — без
            # поля access_code Studio считает ответ пустым и не показывает
            # принтер как доступный.
            if command == "get_access_code":
                reply["system"]["access_code"] = self._access_code()
            reports.append(reply)
        return reports

    def mqtt_handle(self, payload: dict, topic: str = "") -> list[dict]:
        """Прежнее имя обработчика (оставлено для совместимости)."""
        return self.handle_mqtt_request(payload, topic)

    def _handle_print_cmd(self, cmd: dict) -> list[dict]:
        name = str(cmd.get("command") or "")
        seq = str(cmd.get("sequence_id") or "0")
        if name in ("push_status", "pushall"):
            return [self._push_status(seq)]
        if name == "project_file":
            return self._handle_project_file(cmd, seq)
        if name in ("pause", "resume", "stop"):
            printer = self._bound_printer()
            if printer and hasattr(printer, "command"):
                try:
                    printer.command(name)
                except Exception:
                    pass
            return [{"print": {"command": name, "sequence_id": seq, "result": "success"}}]
        if name == "gcode_file":
            fake = dict(cmd)
            fake["url"] = str(cmd.get("param") or cmd.get("url") or "")
            return self._handle_project_file(fake, seq)
        return [{"print": {"command": name or "unknown", "sequence_id": seq, "result": "success"}}]

    def _handle_project_file(self, cmd: dict, seq: str) -> list[dict]:
        """Studio залила файл и просит напечатать: берём байты и кладем в очередь."""
        filename = _filename_from_url(str(cmd.get("url") or ""))
        if not filename:
            filename = Path(str(cmd.get("param") or "project.gcode.3mf")).name
        data = self._file_bytes(filename)
        result = "fail"
        reason = "file not found"
        if data:
            try:
                ingested = self.ingest_bytes(filename, data, {"print": cmd})
                if ingested.get("error") and not ingested.get("job"):
                    reason = ingested["error"]
                else:
                    result = "success"
                    reason = ""
            except Exception as exc:
                reason = str(exc)
        report = {
            "print": {
                "command": "project_file",
                "sequence_id": seq,
                "result": result,
                **({"reason": reason} if reason else {}),
            }
        }
        # Studio получила success — дальше очередь, шлюз снова IDLE.
        idle = self._push_status(seq)
        idle["print"]["gcode_state"] = "IDLE"
        return [report, idle]

    def _ams_status(self) -> dict:
        """Раскладка AMS для Studio: лотки, типы, цвета, остатки.

        Возвращает поля верхнего уровня снимка состояния (``ams``,
        ``ams_exist_bits``, ``tray_now``) — у станка они лежат прямо в
        ``print``, а не вложенным объектом.
        """
        printer = self._bound_printer()
        if printer:
            try:
                snap = printer.snapshot()
                trays = (snap.get("ams") or {}).get("trays") or []
                if trays:
                    from .materials import bambu_filament_preset
                    tray_list = []
                    exist_bits = 0
                    bbl_bits = 0
                    for t in trays:
                        slot_n = int(t.get("slot") or 0)
                        if t.get("present"):
                            exist_bits |= (1 << slot_n)
                        if t.get("bambulab"):
                            bbl_bits |= (1 << slot_n)
                        preset = bambu_filament_preset(str(t.get("type") or "PLA"), "")
                        raw_color = str(t.get("color") or "FFFFFFFF").lstrip("#").upper()
                        if len(raw_color) == 6:
                            raw_color += "FF"
                        tray_list.append({
                            "id": slot_n,
                            "tray_type": preset["tray_type"],
                            "tray_color": raw_color,
                            "tray_sub_brands": "",
                            "tray_weight": 0,
                            "nozzle_temp_min": int(t.get("nozzle_min") or preset["nozzle_temp_min"]),
                            "nozzle_temp_max": int(t.get("nozzle_max") or preset["nozzle_temp_max"]),
                            "tray_info_idx": str(t.get("tray_info_idx") or preset["tray_info_idx"]),
                            "remain": int(t.get("remain") if t.get("remain") is not None else 100),
                            "k": 0.02,
                            "n": 1.0,
                            "tray_uuid": str(t.get("uuid") or ""),
                        })
                    return {
                        "ams": [{"id": 0, "humidity": "3", "temp": "24.0",
                                 "tray": tray_list}],
                        "ams_exist_bits": "1",
                        "ams_raw_bits": "1",
                        "tray_exist_bits": str(exist_bits),
                        "tray_is_bbl_bits": str(bbl_bits),
                        "tray_now": "255",
                        "tray_read_done": True,
                        "tray_reading": False,
                    }
            except Exception:
                pass
        return {"ams": [], "ams_exist_bits": "0", "ams_raw_bits": "0",
                "tray_now": "255", "tray_read_done": True, "tray_reading": False}

    def _push_status(self, seq: str = "0") -> dict:
        """Полный снимок состояния принтера (``msg: 0``), как шлёт станок.

        Studio пересобирает карточку принтера по этому JSON целиком. Плагин
        не прощает отсутствующих ключей: DeviceManager берёт их без проверки
        и на недостающем поле бросает исключение — принтер в списке гаснет.
        """
        ident = self.identity()
        report: dict = {"print": {
            "command": "push_status",
            "sequence_id": str(seq),
            # msg: 0 — полный снимок состояния (не дифференциальное
            # обновление): Studio по нему целиком пересобирает карточку.
            "msg": 0,
            "gcode_state": "IDLE",
            "gcode_file": "",
            "gcode_file_prepare_percent": 0,
            "gcode_start_time": "0",
            "print_percentage": 0,
            "mc_percent": 0,
            "mc_remaining_time": 0,
            "mc_print_stage": "1",
            "mc_print_sub_stage": 0,
            "mc_print_error_code": "0",
            "print_error": 0,
            "print_type": "local",
            "print_real_action": "resume",
            "print_gcode_action": "resume",
            "fail_reason": "",
            "job_id": "",
            "task_id": "",
            "project_id": "",
            "profile_id": "",
            "subtask_name": "",
            "s_obj": {"subtask_name": "", "total_layer_num": 0},
            "stg": [],
            "stg_cur": -1,
            "layer_num": 0,
            "total_layer_num": 0,
            "lifecycle": "idle",
            "wifi_signal": "-44dBm",
            "bed_temper": 0.0,
            "bed_target_temper": 0.0,
            "nozzle_temper": 0.0,
            "nozzle_target_temper": 0.0,
            "nozzle_diameter": "0.4",
            "nozzle_type": "hardened_steel",
            "chamber_temper": 0,
            "cooling_fan_speed": "0",
            "big_fan1_speed": "0",
            "big_fan2_speed": "0",
            "auxiliary_fans": "0",
            "fan_gear": 0,
            "spd_mag": 100,
            "spd_lvl": 2,
            "hw_switch_state": 0,
            "home_flag": 0,
            "door_open": False,
            "sdcard": True,
            "force_upgrade": False,
            "maintain": 0,
            "clean_percent": 0,
            "mess_production_state": "active",
            "ctt": 0,
            "queue_est": 0,
            "queue_number": 0,
            "queue_sts": 0,
            "queue_total": 0,
            "net": {"conf": 16, "info": [{"ip": 15, "mask": 4}]},
            "online": {"ahb": False, "ext": False, "rfid": False,
                       "version": 7, "wifi": True},
            "ipcam": {"ipcam_dev": "0", "ipcam_record": "disable",
                      "mode_bits": 2, "resolution": "1080p"},
            "lights_report": [{"node": "chamber_light", "mode": "off"}],
            "upgrade_state": {"idx": 4, "message": "", "module": "",
                              "new_version_state": "IDLE", "sn": "",
                              "status": "IDLE", "url": ""},
            "vt_tray": {"bed_temp": 0, "chamber_temp": 0, "nozzle_temp_1": 0,
                        "nozzle_temp_2": 0, "nozzle_temp_3": 0,
                        "nozzle_temp_4": 0, "nozzle_temp_5": 0,
                        "nozzle_temp_6": 0, "nozzle_temp_7": 0,
                        "nozzle_temp_8": 0, "nozzle_temp_9": 0,
                        "nozzle_temp_10": 0, "nozzle_temp_11": 0,
                        "nozzle_temp_12": 0, "nozzle_temp_13": 0,
                        "nozzle_temp_14": 0, "nozzle_temp_15": 0,
                        "nozzle_temp_16": 0},
            "xcam": {"allow_skip_parts": False, "buildplate_marker_detector": True,
                     "first_layer_inspector": True, "halt_print_sensitivity": 3,
                     "off_frame_detector": False, "pet_detector": False,
                     "spaghetti_detector": True},
            "xcam_status": "off",
            "device": {"mode": ident["dev_model"]},
        }}
        # ams / ams_exist_bits / tray_now лежат прямо в print, как у станка.
        report["print"].update(self._ams_status())
        return report

    def _version_report(self, seq: str) -> dict:
        """Ответ на ``get_version``: модули ota и esp32 с серийником."""
        serial = self.identity()["serial"]
        return {"info": {
            "command": "get_version",
            "sequence_id": str(seq),
            "result": "success",
            "module": [
                {"name": "ota", "sw_ver": FIRMWARE_VERSION, "hw_ver": "AP05", "sn": serial},
                {"name": "esp32", "sw_ver": "00.00.00.00", "hw_ver": "AP05", "sn": serial},
            ],
        }}

    def mqtt_handle_packet(self, packet: bytes,
                           session: MqttSession | None = None) -> list[bytes]:
        """Разобрать один MQTT-пакет, вернуть список ответных пакетов.

        ``session`` — состояние конкретного соединения (авторизация своя у
        каждого клиента). Без него берётся сессия шлюза «по умолчанию»: так
        работают тесты и диагностика, которые гоняют пакеты без сокета.
        """
        sess = session if session is not None else self._mqtt_session
        try:
            ptype, flags, payload = parse_fixed_header(packet)
        except Exception:
            return []
        ident = self.identity()
        report_topic = f"device/{ident['serial']}/report"
        if ptype == CONNECT:
            try:
                info = decode_connect(payload)
            except Exception:
                sess.authed = False
                return [encode_connack(5)]
            user_ok = info.get("username") == MQTT_USER
            expected = self._access_code()
            pass_ok = bool(expected) and info.get("password") == expected
            rc = 0 if user_ok and pass_ok else 4
            # Только ЭТО соединение: чужой неудачный вход не должен гасить
            # отчёты уже работающей Studio.
            sess.authed = rc == 0
            sess.client_id = str(info.get("client_id") or "")
            if rc == 0:
                self._bump("mqtt_connections")
            else:
                self._note_auth_failure("mqtt")
            return [encode_connack(rc)]
        if ptype == PINGREQ:
            return [encode_pingresp()]
        if ptype == DISCONNECT:
            sess.authed = False
            return []
        if ptype == SUBSCRIBE:
            if not sess.authed and self.bind:
                return []
            try:
                sub = decode_subscribe(payload)
            except Exception:
                return []
            qos = [0] * len(sub.get("filters") or [0])
            return [encode_suback(sub["packet_id"], qos or [0])]
        if ptype == UNSUBSCRIBE:
            try:
                unsub = decode_unsubscribe(payload)
            except Exception:
                return []
            return [encode_unsuback(unsub["packet_id"])]
        if ptype == PUBLISH:
            if not sess.authed and self.bind:
                return []
            try:
                pub = decode_publish(flags, payload)
            except Exception:
                return []
            replies: list[bytes] = []
            if pub.get("qos"):
                replies.append(encode_puback(pub["packet_id"]))
            raw = pub.get("payload") or b""
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            try:
                body = json.loads(text.strip("\x00") or "{}")
            except json.JSONDecodeError:
                body = {}
            if isinstance(body, dict):
                for report in self.handle_mqtt_request(body, pub.get("topic") or ""):
                    replies.append(encode_publish(
                        report_topic,
                        json.dumps(report, ensure_ascii=False, separators=(",", ":")),
                    ))
                if replies:
                    self._bump("mqtt_publishes", len(replies))
            return replies
        return []

    # ----------------------------------------------------------- FTP
    def ftp_command(self, line: str) -> str:
        """Одна FTP-команда → ответ. Без сокета."""
        text = (line or "").strip()
        if not text:
            return "500 Empty"
        parts = text.split(" ", 1)
        cmd = parts[0].upper()
        arg = parts[1] if len(parts) > 1 else ""
        if cmd == "USER":
            self._ftp_user = arg.strip()
            self._ftp_authed = False
            self._bump("ftp_connections")
            return "331 Password required"
        if cmd == "PASS":
            expected = self._access_code()
            user_ok = self._ftp_user == MQTT_USER
            pass_ok = bool(expected) and arg == expected
            self._ftp_authed = user_ok and pass_ok
            if self._ftp_authed:
                return "230 Login successful"
            self._note_auth_failure("ftp")
            return "530 Login incorrect"
        if cmd == "QUIT":
            self._ftp_authed = False
            return "221 Goodbye"
        if cmd in ("AUTH", "PBSZ", "PROT"):
            return "234 OK" if cmd == "AUTH" else "200 OK"
        if cmd == "FEAT":
            return "211-Features\r\n UTF8\r\n PASV\r\n SIZE\r\n211 End"
        if cmd == "SYST":
            return "215 UNIX Type: L8"
        if cmd == "TYPE":
            return "200 Type set"
        if cmd == "NOOP":
            return "200 OK"
        if cmd in ("OPTS", "MODE", "STRU", "ALLO", "PORT"):
            return "200 OK"
        if cmd in ("PWD", "XPWD"):
            return '257 "/"'
        if cmd in ("CWD", "XCWD"):
            self._ftp_cwd = "/" + arg.strip("/ ")
            return "250 Directory changed"
        if cmd == "PASV":
            return self._pasv_reply(20)
        if cmd == "EPSV":
            return "229 Entering Extended Passive Mode (|||20|)"
        if cmd in ("LIST", "NLST"):
            return "226 Transfer complete"
        if cmd == "SIZE":
            data = self._file_bytes(arg)
            if data is None:
                return "550 File not found"
            return f"213 {len(data)}"
        if cmd in ("STOR", "APPE", "STOU"):
            if not self._ftp_authed and self.bind:
                return "530 Please login"
            self._stor_name = Path(arg.replace("\\", "/")).name or "upload.bin"
            return "150 Ok to send data"
        if cmd == "DELE":
            return "250 Deleted"
        if cmd in ("MKD", "XMKD"):
            return '257 "/"'
        if cmd == "RNFR":
            return "350 Ready"
        if cmd == "RNTO":
            return "250 Renamed"
        return "502 Command not implemented"

    def ftp_apply(self, filename: str, data: bytes) -> dict:
        """STOR без сети: сохранить во входящие и отправить в очередь."""
        name = Path(str(filename or "upload.bin").replace("\\", "/")).name
        with self._lock:
            self._incoming[name] = data
            if len(self._incoming) > INCOMING_LIMIT:
                extra = list(self._incoming)[:-24]
                for key in extra:
                    self._incoming.pop(key, None)
        return self.ingest_bytes(name, data)

    def _pasv_reply(self, port: int) -> str:
        """Ответ на PASV: адрес из host_pinned (или авто) и порт.

        Если объявить в PASV адрес туннеля, Studio зальёт файл не туда —
        поэтому здесь тот же источник адреса, что и в SSDP Location.
        """
        host = self._host_ip().split(".")
        if len(host) != 4:
            host = ["127", "0", "0", "1"]
        p1, p2 = divmod(int(port) % 65536, 256)
        return (
            f"227 Entering Passive Mode "
            f"({host[0]},{host[1]},{host[2]},{host[3]},{p1},{p2})"
        )

    # ----------------------------------------------------------- жизненный цикл
    def _tls_context(self, cert, key):
        """Серверный TLS-контекст: минимум 1.2, наборы с AES-GCM."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        try:
            # Studio приходит с набором, где есть AES-GCM; на сборках OpenSSL
            # с жёстким DEFAULT (и на Windows) без явного перечисления
            # рукопожатие может не состояться — тогда Studio молчит «код=-1».
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.set_ciphers("DEFAULT:AES-GCM")
        except (ssl.SSLError, ValueError):
            try:
                ctx.set_ciphers("DEFAULT")
            except (ssl.SSLError, ValueError):
                pass
        return ctx

    def start(self) -> None:
        """Поднять все четыре канала: SSDP, :3000/:3002, MQTT, FTPS."""
        if not self.bind or not self._enabled():
            return
        if self._mqtt_sock or self._ftp_sock or self._ssdp_sock or self._bind_socks:
            return
        self._stop.clear()
        self._ensure_identity()
        self._errors = {"ssdp": "", "mqtt": "", "ftps": "", "bind": "", "tls": "",
                        "host": "", "vpn": "", "cert": ""}
        self.last_error = ""
        self._ssdp_note = ""
        self._ssdp_bound_port = 0
        self._ssdp_source = ""
        self._cert_cn = self._cert_san = self._cert_expires = ""
        self._host_cache = ""
        self._ips_cache = []
        self._ips_cache_at = 0.0
        self._vpn_cache = {}
        self._vpn_cache_at = 0.0
        # Адреса перечисляем ДО подъёма сокетов: от них зависит, с какого
        # интерфейса пойдут объявления.
        self._warn_vpn()
        try:
            self._start_ssdp()
        except Exception as exc:
            self._record_error("SSDP", exc)
        cert = key = None
        try:
            from .studio_tls import cert_not_after, ensure_certificate
            # Сертификат — на серийный номер, и обязательно с SAN
            # (DNS:<серийник>, IP:127.0.0.1): без SAN Studio Beta режет
            # рукопожатие, даже если CN совпал.
            serial = self.identity()["serial"]
            cert, key = ensure_certificate(serial)
            self._cert_cn = serial
            self._cert_san = self._certificate_san(cert)
            # Срок действия показываем в карточке: просроченный сертификат
            # Studio отвергает так же молча, как и сертификат без SAN.
            try:
                expiry = cert_not_after(cert)
                self._cert_expires = expiry.date().isoformat() if expiry else ""
            except Exception:
                self._cert_expires = ""
            if self._cert_san is not None and not self._cert_san:
                # Это не отказ службы (TLS поднимется), но Studio Beta
                # рукопожатие срежет — пишем в диагностику, не в last_error.
                self._record_note(
                    "cert", "сертификат без subjectAltName — Studio Beta его "
                            "отвергнет; удалите "
                            f"{Path(cert).parent} и перезапустите коннектор")
        except Exception as exc:
            self._record_error("TLS", exc)
            return
        self._tls_ctx = self._tls_context(cert, key)
        try:
            self._start_bind(cert, key)
        except Exception as exc:
            self._record_error("bind", exc)
        try:
            self._start_mqtt(cert, key)
        except Exception as exc:
            self._record_error("MQTT", exc)
        try:
            self._start_ftp(cert, key)
        except Exception as exc:
            self._record_error("FTPS", exc)

    @staticmethod
    def _certificate_san(cert) -> list[str] | None:
        """SAN сертификата (None — разобрать нечем, [] — SAN действительно нет)."""
        try:
            from .studio_tls import certificate_san
            return certificate_san(cert)
        except Exception:
            return None

    def stop(self) -> None:
        self._stop.set()
        for sock in (self._ssdp_sock, self._ssdp_tx, self._ssdp_tx_lo,
                     self._mqtt_sock, self._ftp_sock, *self._bind_socks):
            if sock is None:
                continue
            try:
                sock.close()
            except Exception:
                pass
        self._ssdp_sock = self._ssdp_tx = self._ssdp_tx_lo = None
        self._mqtt_sock = self._ftp_sock = None
        self._ssdp_bound_port = 0
        self._ssdp_source = ""
        self._bind_socks = []
        self._threads = []

    def reload(self) -> None:
        """Перезапустить шлюз после смены настроек (адрес, имя, режим)."""
        self.stop()
        if self._enabled() and self.bind:
            self.start()

    def _spawn(self, name: str, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _start_ssdp(self) -> None:
        """Слушать M-SEARCH на :1900 и завести исходящий сокет объявлений."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Слушаем только 1900 (стандартный порт SSDP, туда Studio и прочие
        # искалки шлют M-SEARCH). UDP 2021 не занимаем никогда: там слушает
        # сетевой плагин Studio, и вторая розетка на том же порту отбирает у
        # него входящие объявления — Studio остаётся без адреса принтера и
        # отдаёт «код=-1».
        #
        # SO_REUSEPORT намеренно НЕ ставим, пока порт свободен. С ним ядро
        # делит входящие дейтаграммы между всеми сокетами на порту: зависший
        # после перезапуска процесс или чужая служба SSDP начинают перехватывать
        # часть M-SEARCH, и Studio не находит принтер примерно через раз —
        # симптом плавающий и очень похожий на «код=-1». Если порт всё-таки
        # занят (Windows-служба SSDP Discovery, второй экземпляр), делим его:
        # запасной канал поиска лучше никакого, а объявления — главный канал —
        # работают независимо от этого сокета.
        bound_port = 0
        for port in SSDP_LISTEN_PORTS:
            try:
                sock.bind(("", port))
                bound_port = port
                break
            except OSError:
                pass
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                sock.bind(("", port))
            except (AttributeError, OSError):
                continue
            bound_port = port
            self._ssdp_note = (
                f"порт {port} занят другой программой — слушаем его совместно "
                "(SO_REUSEPORT): часть M-SEARCH может доставаться не нам, "
                "объявления NOTIFY при этом уходят как обычно")
            break
        if not bound_port:
            sock.bind(("", 0))
            bound_port = int(sock.getsockname()[1])
            # Это не сбой: объявления (главный канал) уходят как обычно,
            # теряется только ответ на чужой поиск M-SEARCH.
            self._ssdp_note = (
                "порт 1900 занят другой программой — M-SEARCH слушаем "
                f"на :{bound_port}, объявления Studio всё равно получает")
        self._ssdp_bound_port = bound_port
        try:
            mreq = struct.pack("4s4s", socket.inet_aton(SSDP_GROUP),
                               socket.inet_aton("0.0.0.0"))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass
        sock.settimeout(1.0)
        self._ssdp_sock = sock
        # Исходящий сокет отдельно и с привязкой к LAN-адресу: ответ на
        # M-SEARCH обязан уйти с 192.168.0.108, а не с адреса VPN-туннеля.
        try:
            tx, source = self._make_notify_socket()
            self._ssdp_tx = tx
            self._ssdp_source = source
        except OSError as exc:
            self._record_error("ssdp", f"исходящий сокет не создан: {exc}")
        self._spawn("pf-studio-ssdp", self._ssdp_loop, sock)

    def _ssdp_loop(self, sock) -> None:
        """Цикл SSDP: рассылка объявлений + ответы на чужой M-SEARCH."""
        while not self._stop.is_set():
            # Рассылка объявления — не должна ронять цикл при ошибке БД/кодирования.
            try:
                self._broadcast_notify()
            except Exception:
                # _broadcast_notify уже логирует, но падение потока недопустимо
                pass
            try:
                data, addr = sock.recvfrom(SSDP_REPLY_SIZE)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception:
                continue
            text = data.decode("utf-8", "replace")
            if "M-SEARCH" not in text:
                continue
            if SSDP_NT not in text and "ssdp:all" not in text.lower():
                continue
            try:
                self._ssdp_reply(addr)
            except Exception:
                continue

    def _start_mqtt(self, cert, key) -> None:
        sock = self._listen_tcp(MQTT_PORT, 16, tls=True, cert=cert, key=key)
        self._mqtt_sock = sock
        self._spawn("pf-studio-mqtt", self._accept_loop, sock, self._mqtt_client)

    def _start_ftp(self, cert, key) -> None:
        sock = self._listen_tcp(FTP_PORT, 16, tls=True, cert=cert, key=key)
        self._ftp_sock = sock
        self._spawn("pf-studio-ftp", self._accept_loop, sock, self._ftp_client)

    def _accept_loop(self, sock, handler) -> None:
        """Приём соединений: сбой одного клиента не должен гасить службу.

        accept() на TLS-розетке сам выполняет рукопожатие. Клиент, пришедший
        без TLS (проверка «открыт ли порт», сканер сети, антивирус, оборванное
        соединение Studio), раньше убивал цикл целиком: порт оставался в
        состоянии LISTEN, но новых клиентов уже не принимал — Studio висела по
        таймауту и показывала «код=-1» на исправном шлюзе. Теперь такое
        соединение просто отбрасывается, а счётчик виден в статусе.
        """
        fatal_errno = {errno.EBADF, errno.EINVAL, errno.ENOTSOCK, errno.EOPNOTSUPP}
        service = "bind" if handler == self._bind_client else (
            "ftps" if handler == self._ftp_client else "mqtt")
        consecutive = 0
        while not self._stop.is_set():
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                consecutive = 0
                continue
            except (ssl.SSLError, ConnectionError) as exc:
                # Почти всегда: клиент пришёл без TLS или оборвал рукопожатие.
                self._note_dropped_connection(exc)
                consecutive += 1
                if consecutive >= 100:
                    self._record_error(service, f"приём соединений срывается: {exc}")
                    break
                continue
            except OSError as exc:
                if self._stop.is_set() or getattr(exc, "errno", None) in fatal_errno:
                    break
                self._note_dropped_connection(exc)
                consecutive += 1
                if consecutive >= 100:
                    break
                continue
            consecutive = 0
            self._spawn("pf-studio-conn", handler, conn)

    def _mqtt_client(self, conn) -> None:
        """Одно MQTT/TLS-соединение: CONNECT → SUBSCRIBE → PUBLISH → отчёты."""
        peer = ""
        try:
            peer = conn.getpeername()[0]
        except Exception:
            pass
        session = MqttSession(peer=peer)
        try:
            with self._lock:
                self._last_client = peer or self._last_client
            conn.settimeout(MQTT_KEEPALIVE)
            while not self._stop.is_set():
                try:
                    ptype, flags, payload = read_packet(conn.recv)
                except (socket.timeout, TimeoutError):
                    continue
                except (ConnectionError, OSError, ssl.SSLError):
                    break
                from .studio_mqtt import encode_remaining_length
                header = bytes([((ptype & 0x0F) << 4) | (flags & 0x0F)])
                packet = header + encode_remaining_length(len(payload)) + payload
                for reply in self.mqtt_handle_packet(packet, session):
                    try:
                        conn.sendall(reply)
                    except OSError:
                        return
                if ptype == DISCONNECT:
                    break
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _ftp_readline(self, conn) -> str:
        buf = bytearray()
        while len(buf) < 4096:
            chunk = conn.recv(1)
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b"\n"):
                break
        return buf.decode("utf-8", "replace").strip()

    def _data_channel(self, conn, timeout: int = 30, peek_timeout: float = 2.0):
        """Канал данных PASV: TLS, если клиент начинает рукопожатие.

        Клиенты Bambu расходятся: одни поднимают TLS на канале данных сразу
        после connect, другие (например P2S) только дожидаются ``150`` и
        начинают рукопожатие потом. Поэтому тип канала определяем по первому
        байту, не съедая его (MSG_PEEK): начинается с 0x16 — оборачиваем в
        TLS, иначе читаем как есть. Ожидание первого байта короткое: клиент
        подключается к каналу данных только когда готов слать, но ждать его
        секундами нельзя — иначе заливка встанет на таймауте.
        """
        try:
            conn.settimeout(timeout)
        except OSError:
            return conn
        if self._tls_ctx is None:
            return conn
        try:
            conn.settimeout(peek_timeout)
            first = conn.recv(1, socket.MSG_PEEK)
            conn.settimeout(timeout)
        except (socket.timeout, TimeoutError, OSError):
            return conn
        if not first or first[0] != 0x16:
            return conn
        try:
            return self._tls_ctx.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError):
            return conn

    def _accept_data(self, sock) -> None:
        """Принять канал данных PASV в фоне (клиент может прийти до STOR)."""
        try:
            conn, _addr = sock.accept()
        except (socket.timeout, TimeoutError, OSError):
            return
        conn = self._data_channel(conn)
        with self._lock:
            self._data_conn = conn
        self._data_ready.set()

    def _ftp_client(self, conn) -> None:
        """Одно implicit-FTPS-соединение: USER/PASS/PASV/STOR → очередь."""
        self._ftp_user = ""
        self._ftp_authed = False
        pasv_sock = None
        try:
            conn.settimeout(MQTT_KEEPALIVE)
            conn.sendall((FTP_BANNER + "\r\n").encode("utf-8"))
            stor_name = ""
            while not self._stop.is_set():
                try:
                    line = self._ftp_readline(conn)
                except (socket.timeout, TimeoutError):
                    continue
                except (ConnectionError, OSError, ssl.SSLError):
                    break
                if not line:
                    break
                reply = self.ftp_command(line)
                cmd = line.split(" ", 1)[0].upper()
                arg = line.split(" ", 1)[1] if " " in line else ""
                if cmd == "PASV":
                    if pasv_sock:
                        try:
                            pasv_sock.close()
                        except Exception:
                            pass
                    self._data_conn = None
                    self._data_ready.clear()
                    pasv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    pasv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    # Канал данных слушаем на 0.0.0.0, а адрес в ответе PASV —
                    # закреплённый (host_pinned): Studio соединяется туда,
                    # куда мы её послали, а не на адрес VPN-туннеля.
                    pasv_sock.bind(("0.0.0.0", 0))
                    pasv_sock.listen(1)
                    pasv_sock.settimeout(30)
                    port = pasv_sock.getsockname()[1]
                    reply = self._pasv_reply(port)
                    # Клиент может подключиться к каналу данных и до STOR —
                    # приём ждёт в отдельном потоке, иначе рукопожатие
                    # раннего клиента подвиснет до нашего accept().
                    self._spawn("pf-studio-pasv", self._accept_data, pasv_sock)
                if cmd in ("STOR", "APPE"):
                    stor_name = Path(arg.replace("\\", "/")).name or "upload.bin"
                try:
                    conn.sendall((reply + "\r\n").encode("utf-8"))
                except OSError:
                    break
                if cmd in ("STOR", "APPE") and pasv_sock and reply.startswith("150"):
                    data_conn = None
                    try:
                        self._data_ready.wait(30)
                        data_conn = self._data_conn
                        self._data_conn = None
                        if data_conn is None:
                            raise ConnectionError("клиент не открыл канал данных")
                        chunks = []
                        while True:
                            chunk = data_conn.recv(65536)
                            if not chunk:
                                break
                            chunks.append(chunk)
                        blob = b"".join(chunks)
                        self.ftp_apply(stor_name, blob)
                        self._bump("ftp_uploads")
                        conn.sendall(b"226 Transfer complete\r\n")
                    except Exception:
                        try:
                            conn.sendall(b"426 Transfer aborted\r\n")
                        except OSError:
                            break
                    finally:
                        if data_conn:
                            try:
                                data_conn.close()
                            except Exception:
                                pass
                if cmd == "QUIT":
                    break
        except Exception:
            pass
        finally:
            if self._data_conn:
                try:
                    self._data_conn.close()
                except Exception:
                    pass
            self._data_conn = None
            if pasv_sock:
                try:
                    pasv_sock.close()
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass
