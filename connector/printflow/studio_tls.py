"""Самоподписанный TLS-сертификат шлюза Bambu Studio.

Studio ждёт MQTT/TLS :8883 и implicit FTPS :990. Сертификат хранится в
``DATA_DIR/studio-gateway`` — не в репозитории. Сначала пробуем openssl
(есть на Linux/macOS и в Git for Windows), затем cryptography, если она
уже стоит. Новых pip-зависимостей нет: без обоих инструментов шлюз
пишет last_error и не падает.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DATA_DIR

CERT_DIR = DATA_DIR / "studio-gateway"
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"


class CertError(RuntimeError):
    """Сертификат шлюза не удалось создать."""


def cert_paths() -> tuple[Path, Path]:
    return CERT_FILE, KEY_FILE


def certificate_ready() -> bool:
    return CERT_FILE.is_file() and KEY_FILE.is_file() and CERT_FILE.stat().st_size > 0


def ensure_certificate(cn: str = "NOZZA-PrintFlow") -> tuple[Path, Path]:
    """Вернуть пути к cert.pem/key.pem, создав их при необходимости."""
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    if certificate_ready():
        return CERT_FILE, KEY_FILE
    errors: list[str] = []
    try:
        _via_openssl(cn)
        if certificate_ready():
            return CERT_FILE, KEY_FILE
    except Exception as exc:
        errors.append(f"openssl: {exc}")
    try:
        _via_cryptography(cn)
        if certificate_ready():
            return CERT_FILE, KEY_FILE
    except Exception as exc:
        errors.append(f"cryptography: {exc}")
    raise CertError(
        "Не удалось создать TLS-сертификат шлюза. Установите openssl "
        "или пакет cryptography. " + "; ".join(errors)
    )


def _via_openssl(cn: str) -> None:
    openssl = shutil.which("openssl")
    if not openssl:
        raise CertError("openssl не найден в PATH")
    # -nodes: ключ без пароля (шлюз читает его сам).
    cmd = [
        openssl, "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
        "-days", "3650", "-nodes",
        "-subj", f"/CN={cn}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:400]
        raise CertError(err or f"код {proc.returncode}")


def _via_cryptography(cn: str) -> None:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise CertError("пакет cryptography не установлен") from exc
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    KEY_FILE.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
