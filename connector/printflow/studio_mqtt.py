"""Минимальный MQTT 3.1.1 — кодек пакетов для шлюза Bambu Studio.

Bambu Studio ходит на принтер по MQTT/TLS :8883 (логин ``bblp`` + Access Code).
PrintFlow отвечает тем же протоколом, без сторонних брокеров и pip-пакетов.
"""
from __future__ import annotations

CONNECT = 1
CONNACK = 2
PUBLISH = 3
PUBACK = 4
SUBSCRIBE = 8
SUBACK = 9
UNSUBSCRIBE = 10
UNSUBACK = 11
PINGREQ = 12
PINGRESP = 13
DISCONNECT = 14

PACKET_NAMES = {
    CONNECT: "CONNECT",
    CONNACK: "CONNACK",
    PUBLISH: "PUBLISH",
    PUBACK: "PUBACK",
    SUBSCRIBE: "SUBSCRIBE",
    SUBACK: "SUBACK",
    UNSUBSCRIBE: "UNSUBSCRIBE",
    UNSUBACK: "UNSUBACK",
    PINGREQ: "PINGREQ",
    PINGRESP: "PINGRESP",
    DISCONNECT: "DISCONNECT",
}


class MqttError(ValueError):
    """Пакет MQTT разобрать нельзя."""


def encode_remaining_length(n: int) -> bytes:
    """Variable Byte Integer из MQTT 3.1.1."""
    if n < 0 or n > 268_435_455:
        raise MqttError("remaining length вне диапазона")
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        if n:
            byte |= 0x80
        out.append(byte)
        if not n:
            break
    return bytes(out)


def decode_remaining_length(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Вернуть (длина, новый offset после поля длины)."""
    multiplier = 1
    value = 0
    pos = offset
    for _ in range(4):
        if pos >= len(data):
            raise MqttError("обрезан remaining length")
        byte = data[pos]
        pos += 1
        value += (byte & 0x7F) * multiplier
        if not (byte & 0x80):
            return value, pos
        multiplier *= 128
    raise MqttError("remaining length длиннее 4 байт")


def encode_utf8(text: str) -> bytes:
    raw = (text or "").encode("utf-8")
    if len(raw) > 65535:
        raise MqttError("строка MQTT длиннее 65535")
    return len(raw).to_bytes(2, "big") + raw


def decode_utf8(data: bytes, offset: int) -> tuple[str, int]:
    if offset + 2 > len(data):
        raise MqttError("обрезана MQTT-строка")
    size = int.from_bytes(data[offset:offset + 2], "big")
    start = offset + 2
    end = start + size
    if end > len(data):
        raise MqttError("обрезана MQTT-строка")
    return data[start:end].decode("utf-8", "replace"), end


def wrap_packet(packet_type: int, payload: bytes = b"", flags: int = 0) -> bytes:
    header = bytes([((packet_type & 0x0F) << 4) | (flags & 0x0F)])
    return header + encode_remaining_length(len(payload)) + payload


def parse_fixed_header(data: bytes) -> tuple[int, int, bytes]:
    """Разобрать полный пакет из буфера: (type, flags, payload)."""
    if not data:
        raise MqttError("пустой пакет")
    packet_type = data[0] >> 4
    flags = data[0] & 0x0F
    remaining, offset = decode_remaining_length(data, 1)
    end = offset + remaining
    if end > len(data):
        raise MqttError("пакет обрезан")
    return packet_type, flags, data[offset:end]


def encode_connect(client_id: str = "studio", username: str = "bblp",
                   password: str = "", keepalive: int = 60,
                   clean_session: bool = True) -> bytes:
    """CONNECT MQTT 3.1.1 — как шлёт Bambu Studio."""
    flags = 0
    if clean_session:
        flags |= 0x02
    payload = encode_utf8(client_id)
    if username:
        flags |= 0x80
        payload += encode_utf8(username)
        if password is not None:
            flags |= 0x40
            payload += encode_utf8(password)
    variable = (
        encode_utf8("MQTT")
        + bytes([4, flags])
        + int(keepalive).to_bytes(2, "big")
    )
    return wrap_packet(CONNECT, variable + payload)


def decode_connect(payload: bytes) -> dict:
    """Разобрать тело CONNECT (без фиксированного заголовка)."""
    proto, pos = decode_utf8(payload, 0)
    if pos + 4 > len(payload):
        raise MqttError("CONNECT слишком короткий")
    level = payload[pos]
    flags = payload[pos + 1]
    keepalive = int.from_bytes(payload[pos + 2:pos + 4], "big")
    pos += 4
    client_id, pos = decode_utf8(payload, pos)
    username = password = ""
    if flags & 0x04:  # will
        _, pos = decode_utf8(payload, pos)
        _, pos = decode_utf8(payload, pos)
    if flags & 0x80:
        username, pos = decode_utf8(payload, pos)
    if flags & 0x40:
        password, pos = decode_utf8(payload, pos)
    return {
        "protocol": proto,
        "level": level,
        "flags": flags,
        "keepalive": keepalive,
        "client_id": client_id,
        "username": username,
        "password": password,
        "clean_session": bool(flags & 0x02),
    }


def encode_connack(return_code: int = 0, session_present: bool = False) -> bytes:
    return wrap_packet(CONNACK, bytes([1 if session_present else 0, return_code & 0xFF]))


def encode_publish(topic: str, payload: bytes | str = b"", qos: int = 0,
                   packet_id: int = 0, retain: bool = False, dup: bool = False) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    body = encode_utf8(topic)
    flags = 0
    if retain:
        flags |= 0x01
    if qos:
        flags |= (qos & 0x03) << 1
        body += int(packet_id).to_bytes(2, "big")
    if dup:
        flags |= 0x08
    return wrap_packet(PUBLISH, body + payload, flags)


def decode_publish(flags: int, payload: bytes) -> dict:
    topic, pos = decode_utf8(payload, 0)
    qos = (flags >> 1) & 0x03
    packet_id = 0
    if qos:
        if pos + 2 > len(payload):
            raise MqttError("PUBLISH без packet id")
        packet_id = int.from_bytes(payload[pos:pos + 2], "big")
        pos += 2
    return {
        "topic": topic,
        "payload": payload[pos:],
        "qos": qos,
        "packet_id": packet_id,
        "retain": bool(flags & 0x01),
        "dup": bool(flags & 0x08),
    }


def encode_puback(packet_id: int) -> bytes:
    return wrap_packet(PUBACK, int(packet_id).to_bytes(2, "big"))


def decode_subscribe(payload: bytes) -> dict:
    if len(payload) < 2:
        raise MqttError("SUBSCRIBE слишком короткий")
    packet_id = int.from_bytes(payload[:2], "big")
    pos = 2
    filters: list[tuple[str, int]] = []
    while pos < len(payload):
        topic, pos = decode_utf8(payload, pos)
        if pos >= len(payload):
            raise MqttError("SUBSCRIBE без QoS")
        qos = payload[pos] & 0x03
        pos += 1
        filters.append((topic, qos))
    return {"packet_id": packet_id, "filters": filters}


def encode_suback(packet_id: int, qos_list: list[int] | None = None) -> bytes:
    codes = bytes((q & 0x03) for q in (qos_list or [0]))
    return wrap_packet(SUBACK, int(packet_id).to_bytes(2, "big") + codes)


def decode_unsubscribe(payload: bytes) -> dict:
    if len(payload) < 2:
        raise MqttError("UNSUBSCRIBE слишком короткий")
    packet_id = int.from_bytes(payload[:2], "big")
    pos = 2
    topics: list[str] = []
    while pos < len(payload):
        topic, pos = decode_utf8(payload, pos)
        topics.append(topic)
    return {"packet_id": packet_id, "topics": topics}


def encode_unsuback(packet_id: int) -> bytes:
    return wrap_packet(UNSUBACK, int(packet_id).to_bytes(2, "big"))


def encode_pingresp() -> bytes:
    return wrap_packet(PINGRESP)


def encode_disconnect() -> bytes:
    return wrap_packet(DISCONNECT)


def read_packet(recv, timeout: float | None = None) -> tuple[int, int, bytes]:
    """Прочитать один MQTT-пакет с сокета или file-like ``recv(n) -> bytes``.

    ``recv`` — callable как ``socket.recv``. Тесты передают BytesIO.read.
    """
    first = recv(1)
    if not first:
        raise ConnectionError("MQTT: соединение закрыто")
    remaining = 0
    multiplier = 1
    for _ in range(4):
        byte = recv(1)
        if not byte:
            raise ConnectionError("MQTT: обрезан remaining length")
        remaining += (byte[0] & 0x7F) * multiplier
        if not (byte[0] & 0x80):
            break
        multiplier *= 128
    else:
        raise MqttError("remaining length длиннее 4 байт")
    payload = b""
    while len(payload) < remaining:
        chunk = recv(remaining - len(payload))
        if not chunk:
            raise ConnectionError("MQTT: обрезан payload")
        payload += chunk
    return first[0] >> 4, first[0] & 0x0F, payload
