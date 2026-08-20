"""Шифрование секретов в базе SQLite (роадмап 10.10).

Токен Telegram и токен/uid Bambu Cloud — ключи ко всему аккаунту. Раньше они
лежали в settings открытым текстом и попадали в бэкап-копии базы. Теперь в
базе хранится только зашифрованное значение, а ключ — в отдельном файле
``DATA_DIR/.printflow.key`` с правами 600 (читается только владельцем).

Ограничение честное: это защита «на диске» — от случайной утечки бэкапа,
git или экрана. От атакующего, у которого есть полный доступ к компьютеру
(а значит, и к файлу ключа), файловое шифрование не спасает — это не HSM.

Шифрование — только стандартная библиотека (требование репозитория):
потоковый шифр SHA-256 в режиме счётчика + HMAC-SHA256 для проверки
целостности (encrypt-then-MAC). Если установлен пакет ``cryptography``,
используется Fernet (промышленный стандарт) — он проверяется первым.

Формат хранения:
    enc:v1:<base64(nonce)>:<base64(ciphertext)>:<base64(mac)>
Значения без префикса ``enc:v1:`` считаются старым открытым текстом и
читаются как есть — старые установки мигрируют прозрачно при следующей
записи секрета.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from .config import DATA_DIR

PREFIX = "enc:v1:"
_KEY_BYTES = 32
_NONCE_BYTES = 16

try:  # промышленный стандарт, если доступен
    from cryptography.fernet import Fernet  # type: ignore

    _HAVE_FERNET = True
except Exception:  # pragma: no cover - зависит от окружения
    _HAVE_FERNET = False

_fernet = None
_fernet_key = b""


def key_path() -> Path:
    return DATA_DIR / ".printflow.key"


def _ensure_dirs() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _load_key() -> bytes:
    """Ключ из файла или новый (chmod 600)."""
    _ensure_dirs()
    path = key_path()
    try:
        data = path.read_bytes()
        if len(data) == _KEY_BYTES:
            return data
    except OSError:
        pass
    key = secrets.token_bytes(_KEY_BYTES)
    try:
        with open(path, "wb") as handle:
            handle.write(key)
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _fernet_encrypt(key: bytes, plaintext: bytes) -> bytes:
    global _fernet, _fernet_key
    if _fernet is None or _fernet_key != key:
        import base64 as _b64
        _fernet = Fernet(_b64.urlsafe_b64encode(key))
        _fernet_key = key
    return _fernet.encrypt(plaintext)


def _fernet_decrypt(key: bytes, token: bytes) -> bytes:
    global _fernet, _fernet_key
    if _fernet is None or _fernet_key != key:
        import base64 as _b64
        _fernet = Fernet(_b64.urlsafe_b64encode(key))
        _fernet_key = key
    return _fernet.decrypt(token)


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """SHA-256 в режиме счётчика: блоки HMAC(key, nonce || counter)."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hashlib.sha256(nonce + counter.to_bytes(4, "big") + key).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt(plaintext: str) -> str:
    """Зашифровать строку в токен ``enc:v1:…``. Пустая строка остаётся пустой."""
    if not plaintext:
        return ""
    key = _load_key()
    raw = plaintext.encode("utf-8")
    if _HAVE_FERNET:
        token = _fernet_encrypt(key, raw)
        return PREFIX + base64.urlsafe_b64encode(token).decode("ascii")
    nonce = secrets.token_bytes(_NONCE_BYTES)
    ct = bytes(a ^ b for a, b in zip(raw, _keystream(key, nonce, len(raw))))
    mac = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    return PREFIX + ":".join(
        base64.urlsafe_b64encode(x).decode("ascii") for x in (nonce, ct, mac))


def decrypt(token: str) -> str:
    """Расшифровать ``enc:v1:…``; значение без префикса — как есть (старый текст)."""
    if not token:
        return ""
    if not token.startswith(PREFIX):
        return token  # старый открытый текст — читаем без изменений
    key = _load_key()
    payload = token[len(PREFIX):]
    try:
        parts = payload.split(":")
        if len(parts) == 1:  # Fernet: один base64-токен
            raw = base64.urlsafe_b64decode(parts[0].encode("ascii"))
            if _HAVE_FERNET:
                return _fernet_decrypt(key, raw).decode("utf-8")
            raise ValueError("Значение зашифровано Fernet, пакет недоступен")
        nonce = base64.urlsafe_b64decode(parts[0].encode("ascii"))
        ct = base64.urlsafe_b64decode(parts[1].encode("ascii"))
        mac = base64.urlsafe_b64decode(parts[2].encode("ascii"))
    except Exception:
        return ""  # повреждённое значение — не роняем систему
    if not hmac.compare_digest(mac, hmac.new(key, nonce + ct, hashlib.sha256).digest()):
        return ""  # целостность нарушена
    raw = bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct))))
    return raw.decode("utf-8", "replace")


def is_encrypted(token: str) -> bool:
    return bool(token) and token.startswith(PREFIX)
