"""Сессия Bambu Cloud: вход в аккаунт, принтеры, история, облачная печать.

Позволяет управлять принтером, не включая на нём LAN Only Mode / Developer
Mode: принтер остаётся в обычном облачном режиме (Bambu Handy продолжает
работать), а команды и телеметрия идут через облачный MQTT-брокер Bambu
(см. cloud_bridge.py). Запуск печати устроен так же, как у Bambu Studio:
файл заливается в облачное хранилище Bambu, а печать диспетчеризуется
запросом POST /v1/user-service/my/task — принтер сам скачивает 3MF по
своему облачному каналу. Никакой подписи команд не требуется.

Протокол неофициальный (reverse-engineered сообществом: OpenBambuAPI,
pybambu/Home Assistant, open-bamboo-networking). Весь облачный код собран
в этом модуле, чтобы изменения Bambu чинились в одном месте. Только
стандартная библиотека.
"""
from __future__ import annotations

import hashlib
import json
import platform
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

API_HOSTS = {
    "global": "https://api.bambulab.com",
    "china": "https://api.bambulab.cn",
}
MQTT_HOSTS = {
    "global": "us.mqtt.bambulab.com",
    "china": "cn.mqtt.bambulab.com",
}

# POST /my/task проверяет это значение по содержимому: иначе 403.
CLIENT_NAME = "BambuStudio"

_OS_TYPE = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")

# Незавершённые входы: email → {region, tfa_key}. Живут только в памяти,
# в базу не пишутся (tfaKey короткоживущий).
_PENDING: dict[str, dict] = {}


class CloudError(Exception):
    """Ошибка облачного API Bambu с человекочитаемым текстом."""


def api_host(region: str) -> str:
    return API_HOSTS.get(region or "global", API_HOSTS["global"])


def mqtt_host(region: str) -> str:
    return MQTT_HOSTS.get(region or "global", MQTT_HOSTS["global"])


def bbl_headers(token: str = "", uid: str = "", content_type: bool = True) -> dict[str, str]:
    """Заголовки «слайсера»: Cloudflare пропускает, а /my/task валидирует."""
    headers = {
        "Accept": "application/json",
        "User-Agent": "bambu_network_agent/01.09.05.01",
        "X-BBL-Client-Name": CLIENT_NAME,
        "X-BBL-Client-Type": "slicer",
        "X-BBL-Client-Version": "01.09.05.51",
        "X-BBL-OS-Type": _OS_TYPE,
        "X-BBL-Agent-OS-Type": _OS_TYPE,
        "X-BBL-Language": "en-US",
        "X-BBL-Executable-info": "{}",
    }
    if content_type:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if uid:
        headers["X-BBL-Client-ID"] = f"slicer:{uid}:printflow"
    return headers


def _read_body(response) -> str:
    return response.read().decode("utf-8", errors="replace")


def request(method: str, url: str, *, token: str = "", uid: str = "",
            body: Any = None, timeout: float = 60, content_type: bool = True) -> dict:
    """HTTP-запрос к Bambu Cloud. Ошибки превращаются в CloudError."""
    data = None
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method.upper())
    for key, value in bbl_headers(token, uid, content_type).items():
        req.add_header(key, value)
    last_error = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ssl.create_default_context()) as response:
                text = _read_body(response)
                try:
                    return json.loads(text) if text.strip() else {}
                except json.JSONDecodeError:
                    return {"_raw": text}
        except urllib.error.HTTPError as exc:
            text = _read_body(exc)
            if exc.code in (401, 403) and attempt == 0:
                last_error = f"HTTP {exc.code}"
                time.sleep(1.0)
                continue
            raise CloudError(_http_error_text(exc.code, text)) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = str(exc)
            if attempt < 2:
                time.sleep(1.0 + attempt)
                continue
            raise CloudError(f"Не удалось соединиться с Bambu Cloud: {last_error}") from None
    raise CloudError(f"Не удалось соединиться с Bambu Cloud: {last_error}")


def _http_error_text(code: int, body: str) -> str:
    """Понятный текст ошибки: серверный ответ Bambu часто уже читаемый."""
    snippet = body[:200].replace("\n", " ")
    if code == 401:
        return "Bambu Cloud: неверный логин или пароль (401)"
    if code == 403:
        if "cloudflare" in body.lower() or "cf-" in body.lower():
            return "Bambu Cloud: запрос отклонён защитой (Cloudflare). Попробуйте ещё раз через пару минут."
        if "missing_cookie" in body:
            return "Bambu Cloud: вход через сайт требует cookie. Войдите логином и паролем."
        return f"Bambu Cloud отклонил запрос (403). {snippet or 'Проверьте аккаунт и регион.'}"
    if code == 400:
        return f"Bambu Cloud: неверный запрос (400). {snippet or ''}".rstrip()
    if code == 429:
        return "Bambu Cloud: слишком много запросов (429). Подождите и повторите."
    return f"Bambu Cloud: ошибка {code}. {snippet or ''}".rstrip()


# ------------------------------------------------------------------ вход
def login(email: str, password: str = "", region: str = "global", code: str = "",
          tfa_code: str = "") -> dict:
    """Вход в аккаунт Bambu. Возвращает {status, message, token?, uid?}.

    status: "ok" — токен получен; "need_code" — Bambu прислал код на почту/SMS;
    "need_tfa" — нужен код из приложения-аутентификатора.
    """
    email = (email or "").strip()
    region = region or "global"
    if not email and _PENDING:
        email = next(iter(_PENDING.keys()))
        if not region or region == "global":
            region = _PENDING[email].get("region", "global")
    if not email:
        raise CloudError("Укажите email аккаунта Bambu")
    base = api_host(region)
    if code:
        payload = {"account": email, "code": code.strip()}
    elif tfa_code and email in _PENDING and _PENDING[email].get("tfa_key"):
        payload = {"tfaKey": _PENDING[email]["tfa_key"], "tfaCode": tfa_code.strip()}
    else:
        if not password:
            raise CloudError("Укажите пароль аккаунта Bambu")
        payload = {"account": email, "password": password}
    data = request("POST", f"{base}/v1/user-service/user/login", body=payload,
                   timeout=45)
    login_type = str(data.get("loginType") or "")
    token = str(data.get("accessToken") or "")
    if token:
        _PENDING.pop(email, None)
        uid = get_uid(token, region)
        return {"status": "ok", "token": token, "uid": uid,
                "message": "Вход выполнен"}
    if login_type == "verifyCode":
        _PENDING[email] = {"region": region}
        send_code(email, region)
        return {"status": "need_code",
                "message": "Bambu прислал код на почту/SMS. Введите его."}
    if login_type == "tfa":
        _PENDING[email] = {"region": region, "tfa_key": str(data.get("tfaKey") or "")}
        return {"status": "need_tfa",
                "message": "Введите код из приложения-аутентификатора."}
    message = str(data.get("message") or data.get("error") or login_type or "Неизвестный ответ")
    raise CloudError(f"Не удалось войти: {message}")


def send_code(email: str, region: str = "global") -> None:
    """Запросить код входа по email (нужен только сам email)."""
    base = api_host(region)
    try:
        request("POST", f"{base}/v1/user-service/user/sendemail/code",
                body={"email": email.strip(), "type": "codeLogin"}, timeout=30)
    except CloudError:
        pass  # письмо могло уйти, даже если API огрызнулся — не блокируем


def get_uid(token: str, region: str = "global") -> str:
    """Числовой id аккаунта — логин облачного MQTT (u_uid)."""
    base = api_host(region)
    data = request("GET", f"{base}/v1/design-user-service/my/preference",
                   token=token, timeout=30)
    uid = data.get("uid")
    if uid is None:
        uid = data.get("uidStr")  # часть аккаунтов отдаёт строкой
    if uid is None:  # запасной путь — профиль пользователя
        profile = request("GET", f"{base}/v1/user-service/my/profile",
                          token=token, timeout=30)
        uid = profile.get("uidStr") or profile.get("uid")
    if uid is None:
        raise CloudError("Bambu Cloud не отдал id аккаунта (uid) — вход не завершён")
    return str(uid)


def get_devices(token: str, region: str = "global") -> list[dict]:
    """Список принтеров аккаунта. Без Access Code — он наружу не отдаётся."""
    base = api_host(region)
    devices: list[dict] = []
    for path in (f"{base}/v1/iot-service/api/user/bind",
                 f"{base}/v1/iot-service/api/user/print"):
        try:
            data = request("GET", path, token=token, timeout=30)
        except CloudError:
            continue
        raw = data.get("devices") or []
        if raw:
            devices = raw
            break
    out = []
    for dev in devices or []:
        out.append({
            "serial": str(dev.get("dev_id") or ""),
            "name": str(dev.get("name") or "Bambu Lab"),
            "model": str(dev.get("dev_product_name") or dev.get("dev_model_name") or ""),
            "online": bool(dev.get("online")),
            "print_status": str(dev.get("print_status") or ""),
        })
    return [d for d in out if d["serial"]]


def get_tasks(token: str, region: str = "global", device_id: str = "",
              limit: int = 20) -> list[dict]:
    """История печатей аккаунта (для журнала PrintFlow)."""
    base = api_host(region)
    query = urllib.parse.urlencode({"limit": max(1, min(int(limit), 100))})
    if device_id:
        query += "&deviceId=" + urllib.parse.quote(str(device_id))
    data = request("GET", f"{base}/v1/user-service/my/tasks?{query}",
                   token=token, timeout=30)
    return data.get("hits") or []


# ---------------------------------------------------- облачная печать
def md5_hex_upper(data: bytes) -> str:
    return hashlib.md5(data).hexdigest().upper()  # noqa: S324 — протокол Bambu


def _s3_put(url: str, data: bytes, timeout: float = 300) -> None:
    """PUT в presigned S3 URL Bambu. Без Content-Type: подпись SigV2
    посчитана для пустого Content-Type, лишний заголовок ломает её."""
    req = urllib.request.Request(url, data=data, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout,
                                context=ssl.create_default_context()) as response:
        response.read()


def _ams_mapping2(flat: list[int] | None) -> list[dict]:
    """Плоский маппинг AMS → camelCase для /my/task (-1 → {amsId:255, slotId:0})."""
    out = []
    for idx in (flat or []):
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = -1
        if idx < 0:
            out.append({"amsId": 255, "slotId": 0})
        else:
            out.append({"amsId": idx // 4, "slotId": idx % 4})
    return out


def build_task_body(name: str, device_id: str, model_id: str, profile_id: str,
                    plate: int = 1, use_ams: bool = True,
                    ams_mapping: list[int] | None = None,
                    bed_level: bool = True, flow_cali: bool = False,
                    vibration_cali: bool = True, layer_inspect: bool = True,
                    timelapse: bool = False, mode: str = "cloud_file") -> dict:
    """Тело POST /v1/user-service/my/task — диспетчер облачной печати."""
    mapping2 = _ams_mapping2(ams_mapping)
    body: dict[str, Any] = {
        "amsDetailMapping": [],
        "autoBedLeveling": 1 if bed_level else 0,
        "bedLeveling": bool(bed_level),
        "bedType": "auto",
        "cfg": "0",
        "cover": "",
        "deviceId": device_id,
        "extrudeCaliFlag": 0,
        "filamentSettingIds": [],
        "flowCali": bool(flow_cali),
        "layerInspect": bool(layer_inspect),
        "mode": mode,
        "modelId": model_id,
        "nozzleInfos": [],
        "nozzleOffsetCali": 0,
        "plateIndex": max(1, int(plate)),
        "profileId": str(profile_id),
        "sequence_id": "20001",
        "timelapse": bool(timelapse),
        "title": name,
        "useAms": bool(use_ams),
        "vibrationCali": bool(vibration_cali),
    }
    if mapping2:
        body["amsMapping"] = [max(-1, int(x or -1)) for x in (ams_mapping or [])]
        body["amsMapping2"] = mapping2
    return body


def upload_project(path: str | Path, token: str, uid: str, region: str = "global",
                   progress: Callable[[int, int], None] | None = None) -> dict:
    """Заливка 3MF в облачное хранилище Bambu (шаги Bambu Studio).

    Возвращает манифест {project_id, model_id, profile_id, url, md5} —
    его передают в create_task() для запуска печати.
    """
    local = Path(path)
    if not local.exists():
        raise CloudError(f"Файл не найден: {local.name}")
    data = local.read_bytes()
    base = api_host(region)
    name = local.name

    def report(pct: int) -> None:
        if progress:
            try:
                progress(int(pct), 100)
            except Exception:
                pass

    report(2)
    created = request("POST", f"{base}/v1/iot-service/api/user/project",
                      token=token, uid=uid, body={"name": name}, timeout=60)
    project_id = str(created.get("project_id") or "")
    model_id = str(created.get("model_id") or "")
    profile_id = str(created.get("profile_id") or "")
    upload_url = str(created.get("upload_url") or "")
    ticket = str(created.get("upload_ticket") or "")
    if not project_id or not upload_url:
        raise CloudError("Bambu Cloud не выдал project_id — облачная заливка недоступна")
    report(8)
    # Конфиг 3mf (нужен для истории печати). Отдельного файла у нас нет —
    # по опыту open-bamboo-networking допустимо залить сам 3MF.
    _s3_put(upload_url, data)
    report(15)
    request("PUT", f"{base}/v1/iot-service/api/user/notification",
            token=token, uid=uid,
            body={"upload": {"origin_file_name": name, "ticket": ticket}},
            timeout=45)
    report(18)
    for _ in range(20):  # асинхронная обработка загрузки: running → success
        status = request(
            "GET", f"{base}/v1/iot-service/api/user/notification"
                   f"?action=upload&ticket={urllib.parse.quote(ticket)}",
            token=token, uid=uid, timeout=30, content_type=False)
        if str(status.get("message") or "") == "success":
            break
        time.sleep(0.5)
    else:
        raise CloudError("Bambu Cloud не подтвердил загрузку файла (таймаут)")
    report(20)
    slot = f"{model_id}_{profile_id}_{1}.3mf"
    uploads = request(
        "GET", f"{base}/v1/iot-service/api/user/upload"
               f"?models={urllib.parse.quote(slot)}",
        token=token, uid=uid, timeout=45, content_type=False)
    urls = uploads.get("urls") or []
    main_url = str(urls[0].get("url") or "") if urls else ""
    if not main_url:
        raise CloudError("Bambu Cloud не выдал ссылку для загрузки 3MF")
    report(30)
    _s3_put(main_url, data)
    report(80)
    md5 = md5_hex_upper(data)
    manifest = {"project_id": project_id, "model_id": model_id,
                "profile_id": profile_id, "url": main_url, "md5": md5,
                "name": name, "bytes": len(data)}
    patch_project_url(token, uid, region, manifest, main_url)
    report(95)
    return manifest


def patch_project_url(token: str, uid: str, region: str, manifest: dict,
                      url: str, plate: int = 1) -> dict:
    """PATCH /project/{id}: указать, откуда принтеру брать 3MF.

    Для облачной печати url = S3-ссылка; для гибрида «FTPS + облако»
    url = ftp:///файл — принтер качает по локальной сети, а диспетчеризация
    и история печати остаются облачными.
    """
    base = api_host(region)
    return request("PATCH", f"{base}/v1/iot-service/api/user/project/"
                            f"{manifest.get('project_id')}",
                   token=token, uid=uid,
                   body={"profile_id": str(manifest.get("profile_id") or ""),
                         "profile_print_3mf": [{
                             "md5": manifest.get("md5") or "",
                             "plate_idx": max(1, int(plate)),
                             "url": url}]},
                   timeout=60)


def create_task(token: str, uid: str, region: str, manifest: dict, device_id: str,
                plate: int = 1, use_ams: bool = True,
                ams_mapping: list[int] | None = None,
                bed_level: bool = True, flow_cali: bool = False,
                vibration_cali: bool = True, layer_inspect: bool = True,
                timelapse: bool = False, mode: str = "cloud_file") -> dict:
    """POST /my/task — облако диспетчеризует печать (принтер качает сам)."""
    base = api_host(region)
    body = build_task_body(
        str(manifest.get("name") or ""), device_id,
        str(manifest.get("model_id") or ""), str(manifest.get("profile_id") or ""),
        plate=plate, use_ams=use_ams, ams_mapping=ams_mapping,
        bed_level=bed_level, flow_cali=flow_cali, vibration_cali=vibration_cali,
        layer_inspect=layer_inspect, timelapse=timelapse, mode=mode)
    data = request("POST", f"{base}/v1/user-service/my/task",
                   token=token, uid=uid, body=body, timeout=90)
    task_id = str(data.get("id") or data.get("taskId") or "")
    if not task_id:  # некоторые ответы кладут id числом в другое поле
        for key in ("id", "taskId", "task_id"):
            value = data.get(key)
            if value is not None:
                task_id = str(value)
                break
    return {"task_id": task_id, "body": body}


def upload_and_dispatch(path: str | Path, token: str, uid: str, region: str,
                        device_id: str, plate: int = 1, use_ams: bool = True,
                        ams_mapping: list[int] | None = None,
                        bed_level: bool = True, flow_cali: bool = False,
                        vibration_cali: bool = True, layer_inspect: bool = True,
                        timelapse: bool = False,
                        progress: Callable[[int, int], None] | None = None) -> dict:
    """Полный облачный запуск: заливка 3MF + диспетчеризация печати."""
    manifest = upload_project(path, token, uid, region, progress)
    result = create_task(token, uid, region, manifest, device_id,
                         plate=plate, use_ams=use_ams, ams_mapping=ams_mapping,
                         bed_level=bed_level, flow_cali=flow_cali,
                         vibration_cali=vibration_cali, layer_inspect=layer_inspect,
                         timelapse=timelapse, mode="cloud_file")
    return {**manifest, **result}
