"""Самоподписанный TLS-сертификат шлюза Bambu Studio.

Studio ждёт MQTT/TLS :8883, TLS-пробу личности :3002 и implicit FTPS :990.
Сертификат хранится в ``DATA_DIR/studio-gateway`` — не в репозитории.

Три вещи, без которых Bambu Studio Beta (сборка 22710816 и новее) режет
рукопожатие и показывает «код=-1» ещё до ввода Access Code:

1. **CN = серийный номер шлюза.** Станок Bambu выписывает leaf-сертификат на
   свой серийник; плагин Studio сверяет имя узла с ``id`` из ``login/detect``.
2. **SAN обязателен.** Современные OpenSSL/Chromium (а сетевой плагин Studio
   живёт на WebView2) игнорируют CN и смотрят только ``subjectAltName``.
   Сертификат без SAN отбрасывается как ``ERR_CERT_COMMON_NAME_INVALID``,
   поэтому в SAN всегда есть ``DNS:<серийник>`` и ``IP:127.0.0.1`` — второй
   адрес нужен, когда Studio и PrintFlow стоят на одном компьютере и плагин
   стучится на loopback.
3. **Это leaf, а не CA.** ``basicConstraints=critical,CA:FALSE`` +
   ``extendedKeyUsage=serverAuth``: сертификат, выданный самому себе как CA,
   часть сборок Studio считает неподходящим для сервера.

Сначала пробуем openssl (есть на Linux/macOS и в Git for Windows), затем
cryptography, если она уже стоит. Новых обязательных зависимостей нет: без
обоих инструментов шлюз пишет last_error и не падает.
"""
from __future__ import annotations

import ipaddress
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DATA_DIR

CERT_DIR = DATA_DIR / "studio-gateway"
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"
# Рядом с сертификатом лежат метки, на которые он выписан: имя (CN) и список
# альтернативных имён (SAN). По ним шлюз понимает, что сертификат устарел
# (сменился серийник или выпуск был ещё без SAN) и его надо перевыпустить.
CN_FILE = CERT_DIR / "cert.pem.cn"
SAN_FILE = CERT_DIR / "cert.pem.san"

# Адрес, который всегда входит в SAN: Studio на том же компьютере приходит
# на 127.0.0.1, а сверяет она именно SAN, не CN.
SAN_LOOPBACK = "127.0.0.1"
DEFAULT_CN = "NOZZA-PrintFlow"
CERT_DAYS = 3650
# За сколько дней до истечения сертификат перевыпускается сам: просроченный
# leaf Studio отвергает молча, и владелец видит только «код=-1».
RENEW_BEFORE_DAYS = 30


class CertError(RuntimeError):
    """Сертификат шлюза не удалось создать."""


def cert_paths() -> tuple[Path, Path]:
    return CERT_FILE, KEY_FILE


def san_for(cn: str) -> str:
    """Строка SAN для сертификата шлюза: ``DNS:<cn>,IP:127.0.0.1``.

    CN может оказаться именем с пробелами (``NOZZA-PrintFlow`` — без, но
    владелец вправе задать своё). В DNS-имя пробелы не попадают: OpenSSL и
    cryptography на таком значении падают, поэтому проблемные символы
    заменяются дефисом — серийник Bambu (``01P00A…``) от этого не меняется.
    """
    name = str(cn or DEFAULT_CN).strip() or DEFAULT_CN
    dns = "".join(ch if (ch.isalnum() or ch in "-._") else "-" for ch in name)
    return f"DNS:{dns},IP:{SAN_LOOPBACK}"


def certificate_ready() -> bool:
    return CERT_FILE.is_file() and KEY_FILE.is_file() and CERT_FILE.stat().st_size > 0


def stored_cn() -> str:
    """CN, на который выписан лежащий на диске сертификат ('' — неизвестно)."""
    try:
        return CN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def stored_san() -> str:
    """SAN лежащего на диске сертификата ('' — выпуск был ещё без SAN)."""
    try:
        return SAN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _remember(cn: str, san: str) -> None:
    try:
        CN_FILE.write_text(cn, encoding="utf-8")
        SAN_FILE.write_text(san, encoding="utf-8")
    except OSError:
        pass


def cert_not_after(cert_file: Path | None = None) -> datetime | None:
    """Срок действия сертификата (None — не разобрать)."""
    path = Path(cert_file or CERT_FILE)
    if not path.is_file():
        return None
    openssl = shutil.which("openssl")
    if openssl:
        try:
            proc = subprocess.run(
                [openssl, "x509", "-in", str(path), "-noout", "-enddate"],
                capture_output=True, text=True, timeout=20)
            text = (proc.stdout or "").strip()
            if text.startswith("notAfter="):
                stamp = text.split("=", 1)[1].strip()
                parsed = datetime.strptime(stamp, "%b %d %H:%M:%S %Y %Z")
                return parsed.replace(tzinfo=timezone.utc)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(path.read_bytes())
        expiry = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") \
            else cert.not_valid_after.replace(tzinfo=timezone.utc)
        return expiry
    except Exception:
        return None


def cert_expiring_soon(cert_file: Path | None = None) -> bool:
    """True, если сертификат истёк или истечёт раньше, чем через месяц."""
    expiry = cert_not_after(cert_file)
    if expiry is None:
        return False
    return expiry < datetime.now(timezone.utc) + timedelta(days=RENEW_BEFORE_DAYS)


def certificate_san(cert_file: Path | None = None) -> list[str]:
    """Список альтернативных имён из сертификата (для диагностики и проверок).

    Возвращает значения в том виде, в каком их печатает openssl:
    ``DNS:01P00A…``, ``IP Address:127.0.0.1``. Пустой список — SAN нет,
    именно такой сертификат режет Studio Beta.
    """
    path = Path(cert_file or CERT_FILE)
    if not path.is_file():
        return []
    try:
        from cryptography import x509
        from cryptography.x509.oid import ExtensionOID
        cert = x509.load_pem_x509_certificate(path.read_bytes())
        try:
            ext = cert.extensions.get_extension_for_oid(
                ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        except x509.ExtensionNotFound:
            return []
        names: list[str] = []
        for value in ext.value:
            if isinstance(value, x509.DNSName):
                names.append(f"DNS:{value.value}")
            elif isinstance(value, x509.IPAddress):
                names.append(f"IP Address:{value.value}")
            else:
                names.append(str(value))
        return names
    except Exception:
        pass
    openssl = shutil.which("openssl")
    if not openssl:
        return []
    try:
        proc = subprocess.run(
            [openssl, "x509", "-in", str(path), "-noout", "-ext", "subjectAltName"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        # Старый openssl не знает -ext: берём весь текст сертификата.
        try:
            proc = subprocess.run(
                [openssl, "x509", "-in", str(path), "-noout", "-text"],
                capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return []
    text = proc.stdout or ""
    marker = "X509v3 Subject Alternative Name"
    if marker in text:
        text = text.split(marker, 1)[1]
    names = []
    for chunk in text.replace("\n", " ").split(","):
        chunk = chunk.strip()
        if chunk.startswith("DNS:") or chunk.startswith("IP Address:") \
                or chunk.startswith("IP:"):
            names.append(chunk)
        elif names:
            break
    return names


def ensure_certificate(cn: str = DEFAULT_CN) -> tuple[Path, Path]:
    """Вернуть пути к cert.pem/key.pem, создав их при необходимости.

    Сертификат перевыпускается, когда:

    * его нет или он пустой;
    * CN разошёлся с личностью шлюза (сменился серийный номер);
    * выпускался без SAN или с другим SAN — такой Studio Beta режет;
    * истёк или истечёт в ближайший месяц.
    """
    name = str(cn or DEFAULT_CN).strip() or DEFAULT_CN
    san = san_for(name)
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    if certificate_ready() and stored_cn() == name and stored_san() == san \
            and not cert_expiring_soon():
        return CERT_FILE, KEY_FILE
    errors: list[str] = []
    for attempt in (
        lambda: _via_openssl(name, san),
        lambda: _via_openssl(name, san, config_file=True),
        lambda: _via_cryptography(name, san),
    ):
        try:
            attempt()
            if certificate_ready() and _has_san(san):
                _remember(name, san)
                return CERT_FILE, KEY_FILE
            if certificate_ready():
                errors.append("выпуск без SAN — Studio Beta его отвергнет")
        except Exception as exc:
            errors.append(f"{getattr(exc, 'label', 'openssl')}: {exc}")

    # Крайний случай: ни openssl, ни cryptography в системе нет. Пробуем
    # поставить cryptography сами — иначе шлюз не поднимет TLS вообще и
    # владелец увидит «код=-1» без внятной причины.
    try:
        _autostall_cryptography()
        _via_cryptography(name, san)
        if certificate_ready() and _has_san(san):
            _remember(name, san)
            return CERT_FILE, KEY_FILE
    except Exception as exc:
        errors.append(f"auto-install: {exc}")

    raise CertError(
        "Не удалось создать TLS-сертификат шлюза с SAN "
        f"({san}). Установите openssl 1.1.1+ или пакет cryptography. "
        + "; ".join(dict.fromkeys(errors))
    )


def _has_san(san: str) -> bool:
    """Проверить, что выпущенный сертификат действительно содержит SAN."""
    names = certificate_san()
    if not names:
        # Разобрать нечем (нет ни cryptography, ни openssl) — верим метке:
        # сертификат только что создан одним из наших же способов.
        return True
    joined = ",".join(names).replace("IP Address:", "IP:").replace(" ", "")
    wanted = san.replace(" ", "")
    return all(part in joined for part in wanted.split(",") if part)


def _autostall_cryptography() -> None:
    import sys
    packages = ["cryptography>=41.0", "pyOpenSSL>=23.0"]
    base = [sys.executable, "-m", "pip", "install",
            "--disable-pip-version-check", "-q"]
    proc = subprocess.run(base + packages, capture_output=True, timeout=120)
    if proc.returncode != 0:
        # PEP 668 (Debian/Ubuntu): система требует явного разрешения.
        subprocess.run(base + ["--break-system-packages"] + packages,
                       capture_output=True, timeout=120)


class _Labelled(Exception):
    """Обёртка, чтобы в сообщении об ошибке было видно, чем выпускали."""

    def __init__(self, label: str, text: str):
        super().__init__(text)
        self.label = label


def _via_openssl(cn: str, san: str, config_file: bool = False) -> None:
    """Выпуск через openssl: ``-addext`` (1.1.1+) или временный конфиг."""
    openssl = shutil.which("openssl")
    if not openssl:
        raise _Labelled("openssl", "openssl не найден в PATH")
    # -nodes: ключ без пароля (шлюз читает его сам).
    cmd = [
        openssl, "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
        "-days", str(CERT_DAYS), "-nodes",
        "-subj", f"/CN={cn}",
    ]
    tmp_cfg: Path | None = None
    if config_file:
        # openssl 1.0.2 и сборки без -addext: расширения пишем в конфиг.
        tmp_cfg = CERT_DIR / "san.cnf"
        tmp_cfg.write_text(
            "[req]\n"
            "distinguished_name = dn\n"
            "x509_extensions = v3\n"
            "prompt = no\n"
            "[dn]\n"
            f"CN = {cn}\n"
            "[v3]\n"
            "basicConstraints = critical,CA:FALSE\n"
            "keyUsage = critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage = serverAuth\n"
            f"subjectAltName = {san}\n",
            encoding="utf-8")
        cmd += ["-config", str(tmp_cfg)]
        cmd.remove("-subj")
        cmd.remove(f"/CN={cn}")
    else:
        cmd += [
            "-addext", f"subjectAltName={san}",
            "-addext", "basicConstraints=critical,CA:FALSE",
            "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
            "-addext", "extendedKeyUsage=serverAuth",
        ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    finally:
        if tmp_cfg is not None:
            try:
                tmp_cfg.unlink()
            except OSError:
                pass
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:400]
        raise _Labelled("openssl" if not config_file else "openssl+config",
                        err or f"код {proc.returncode}")


def _via_cryptography(cn: str, san: str) -> None:
    """Выпуск через пакет cryptography (если он уже установлен)."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError as exc:
        raise _Labelled("cryptography", "пакет cryptography не установлен") from exc
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    dns_name = san.split("DNS:", 1)[1].split(",", 1)[0] if "DNS:" in san else cn
    alt_names: list[x509.GeneralName] = [x509.DNSName(dns_name)]
    for part in san.split(","):
        part = part.strip()
        if part.startswith("IP:"):
            try:
                alt_names.append(x509.IPAddress(ipaddress.ip_address(part[3:])))
            except ValueError:
                continue
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=CERT_DAYS))
        # Leaf, а не CA: студийный плагин проверяет extendedKeyUsage.
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=True, content_commitment=False,
                          key_encipherment=True, data_encipherment=False,
                          key_agreement=False, key_cert_sign=False,
                          crl_sign=False, encipher_only=False,
                          decipher_only=False),
            critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False)
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
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
