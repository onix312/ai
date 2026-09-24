"""Два loopback-порта агента: речь (8791) и компьютер (8799).

Почему портов два, если программа одна: PrintFlow обращается к речи как к
рантайму расшифровки (срез 2) и к агенту как к источнику статуса окон (срез 3).
Раздельные порты позволяют выключить одно, не трогая другое, и совпадают с
настройками панели (`assistant_speech_url`, `assistant_agent_url`).

С 18.14 порт 8799 — ещё и личный ассистент компьютера: `/skills` отдаёт реестр
навыков с доступностью, `/skill` исполняет навык, `/journal` показывает след
действий на этом компьютере, а `/ui` отдаёт страницу окна ассистента.

С 18.21 у агента есть мозг (`brain.py`): `POST /chat` принимает фразу и отвечает
словами, `GET /chat/history` отдаёт разговор, `GET|POST /memory` — память.
Панель зовёт `/chat` с `mode="pc"`, когда фраза похожа на команду компьютеру.

Предохранители:
  * слушаем только 127.0.0.1 — агент недоступен из сети даже случайно;
  * любое действие в чужом окне и любой навык с риском `write` сначала
    становятся «ожидающими» и выполняются только после подтверждения человека
    на этом же компьютере;
  * признак подтверждения берётся из реестра навыков (`skills.confirm_required`),
    а не из тела запроса: понизить его вызовом нельзя;
  * снимок экрана возвращается байтами тому, кто спросил, и не сохраняется;
  * без зависимостей сервер жив и честно отдаёт причины в `/capabilities`;
  * 18.21: чужие сайты в браузере до агента не достают. Раньше ответы несли
    `Access-Control-Allow-Origin: *`, и любая открытая страница могла читать
    буфер обмена, список окон и найденные документы через `fetch` на
    127.0.0.1. Теперь запрос с чужим `Origin` или чужим `Host` (перепривязка
    DNS) получает 403, а CORS-заголовков нет вовсе: панель ходит к агенту с
    сервера, окно агента — со своего же адреса.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import brain as brain_mod
from . import capabilities, config, executor, pc, skills, speech, ui, window, winapi

# Ожидающее действие живёт недолго: неподтверждённый клик не должен висеть
# вечно и выстрелить через час, когда человек уже ушёл.
PENDING_TTL_SEC = 60.0
# Имена, под которыми агент доступен своему окну и панели. Всё остальное в
# `Host` — признак перепривязки DNS (сайт, выдающий себя за 127.0.0.1).
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


class Agent:
    """Состояние агента: речь, микрофон и очередь действий с подтверждением."""

    def __init__(self) -> None:
        self.state = config.State()
        self.capabilities = capabilities.detect()
        self.recognizer = speech.Recognizer()
        self.microphone = speech.Microphone(self.recognizer)
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        # Исполнитель навыков и своя база создаются по первому обращению: агент
        # обязан стартовать (и отвечать на `/health`) даже там, где папка базы
        # ещё не создана, а навыки никто не звал.
        self._runner: executor.Runner | None = None
        self._brain: brain_mod.Brain | None = None
        # Голос: фраза после стоп-слова идёт мозгу, ответ звучит вслух.
        self.microphone.handler = self.voice_phrase

    @property
    def runner(self) -> executor.Runner:
        """Исполнитель навыков: реестр, способности, своя база, клиент панели."""
        if self._runner is None:
            self._runner = executor.Runner()
        return self._runner

    @property
    def brain(self) -> brain_mod.Brain:
        """Мозг помощника: правила, контекст, память, модель — поверх навыков."""
        if self._brain is None:
            self._brain = brain_mod.Brain(self)
        return self._brain

    def chat(self, text: str, session: str = "main", mode: str = "full",
             plan: dict[str, Any] | None = None) -> dict[str, Any]:
        """Фраза человека → ответ мозга. Способности обновляются перед разговором."""
        if mode != "pc" or plan:
            self.runner.refresh_capabilities()
        return self.brain.chat(text, session=session, mode=mode, plan=plan)

    def voice_phrase(self, phrase: str) -> dict[str, Any]:
        """Голосовая петля (И311): команда компьютеру — здесь, вопрос цеха — панели; ответ вслух."""
        answer = self.chat(phrase, session="voice")
        reply = str(answer.get("reply") or "")
        if answer.get("kind") in ("clarify", "error") and not answer.get("skill"):
            panel = self.runner.panel.chat(phrase, session="voice", source="voice")
            if panel.get("ok") and panel.get("reply"):
                reply = str(panel["reply"])
                answer = {**answer, "reply": reply, "source": "panel", "kind": panel.get("kind") or "answer"}
        if reply and self.capabilities.get("speech_out", True):
            pc.speak(reply[:600])
        self.state.last_phrase = phrase[:500]
        return answer

    # --- статус -----------------------------------------------------------
    def health(self) -> dict[str, Any]:
        window, _reason = winapi.active_window()
        self.state.window = window
        self.state.wake_word = bool(self.microphone.armed)
        payload = self.state.payload(self.capabilities)
        payload["model"] = (self.recognizer.name if self.recognizer.loaded
                            else self.capabilities.get("speech_model", ""))
        return payload

    def refresh_capabilities(self) -> dict[str, Any]:
        self.capabilities = capabilities.detect()
        return self.capabilities

    # --- речь -------------------------------------------------------------
    def transcribe(self, audio: bytes, language: str = config.LANGUAGE) -> dict[str, Any]:
        text, reason = self.recognizer.transcribe_wav(audio, language)
        if reason:
            return {"ok": False, "text": "", "reason": reason}
        if not text:
            return {"ok": False, "text": "", "reason": "Речь не распознана"}
        wake = speech.is_wake_phrase(text)
        self.state.last_phrase = text[:500]
        return {"ok": True, "text": text, "wake_word": wake,
                "phrase": speech.phrase_after_wake_word(text) if wake else "",
                "model": self.recognizer.name}

    def arm_microphone(self, seconds: float = config.MIC_ARM_SECONDS) -> dict[str, Any]:
        ok, reason = self.microphone.arm(seconds)
        if not ok:
            return {"ok": False, "armed": False, "reason": reason}
        return {"ok": True, "armed": True, "reason": "",
                "until": self.microphone.armed_until, "wake_word": config.WAKE_WORD}

    def disarm_microphone(self) -> dict[str, Any]:
        self.microphone.disarm()
        return {"ok": True, "armed": False, "reason": ""}

    # --- навыки ассистента (18.14) ----------------------------------------
    def run_skill(self, name: str, params: Any = None) -> dict[str, Any]:
        """Выполнить навык. Риск «write» и выше — через подтверждение человека.

        Порядок тот же, что для клика в чужом окне: навык с подтверждением не
        исполняется сразу, а становится ожидающим. `confirmed` в теле запроса
        намеренно не читается — иначе любой loopback-вызов получил бы право
        двигать файлы и нажимать кнопки без человека.
        """
        runner = self.runner
        key = str(name or "").strip().casefold()
        skill = skills.get(key, runner.learned())
        if skill is None:
            return runner.run(key, params)
        clean, errors = skills.check_params(skill, params)
        if errors:
            # Путь отказа один: тот же `runner.run` запишет причину в журнал,
            # и её потом покажет навык `agent.why`.
            return runner.run(key, params)
        if skills.confirm_required(skill):
            return self.queue_action("skill", {"name": key, "params": clean})
        return runner.run(key, clean)

    def skills_payload(self) -> dict[str, Any]:
        rows = self.runner.catalog()
        return {"ok": True, "skills": rows, "count": len(rows),
                "ready": sum(1 for row in rows if row["available"]),
                "unavailable": [{"name": row["name"], "reason": row["reason"]}
                                for row in rows if not row["available"]],
                "stats": self.runner.store.stats()}

    def journal(self, limit: int = 30) -> dict[str, Any]:
        """Журнал действий на компьютере. Чтение журнала в журнал не пишется."""
        rows = self.runner.store.journal_recent(limit)
        return {"ok": True, "entries": rows, "count": len(rows),
                "stats": self.runner.store.stats()}

    # --- действия в чужих окнах -------------------------------------------
    def queue_action(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        """Действие становится ожидающим: без человека оно не выполняется."""
        # Навык подтверждается так же, как клик, но управление окнами ему не
        # нужно: файлы и панель существуют и не в Windows.
        if kind != "skill" and not self.capabilities.get("windows"):
            return {"ok": False, "queued": False,
                    "reason": self.capabilities.get("windows_reason") or "Не Windows"}
        if kind not in ("click", "type", "key", "activate", "skill"):
            return {"ok": False, "queued": False, "reason": f"Действие «{kind}» не известно"}
        action_id = uuid.uuid4().hex[:12]
        window, _reason = winapi.active_window()
        with self._lock:
            self._purge()
            self._pending[action_id] = {
                "id": action_id, "kind": kind, "params": params, "window": window,
                "created_at": time.time(),
                "text": _describe(kind, params, window),
            }
        self.state.last_action = f"ждёт подтверждения: {kind}"
        threading.Thread(target=self._ask, args=(action_id,), daemon=True,
                         name="agent-confirm").start()
        return {"ok": True, "queued": True, "id": action_id, "reason": "",
                "requires_confirmation": True,
                "text": self._pending[action_id]["text"]}

    def confirm_action(self, action_id: str, confirmed: bool) -> dict[str, Any]:
        """Подтверждение человека: выполнить или забыть."""
        with self._lock:
            self._purge()
            action = self._pending.pop(str(action_id or ""), None)
        if not action:
            return {"ok": False, "done": False,
                    "reason": "Действие не найдено или истекло — запросите заново"}
        if not confirmed:
            self.state.last_action = "отменено человеком"
            return {"ok": True, "done": False, "reason": "Отменено человеком"}
        if action["kind"] == "skill":
            params = action["params"] if isinstance(action["params"], dict) else {}
            result = self.runner.run(str(params.get("name") or ""),
                                     params.get("params") or {}, confirmed=True)
            ok = bool(result.get("ok"))
            reason = str(result.get("reason") or "")
            self.state.last_action = (f"навык {params.get('name')}: "
                                      + ("выполнен" if ok else reason))
            if not ok:
                return {"ok": False, "done": False, "reason": reason, "result": result}
            return {"ok": True, "done": True, "reason": "", "result": result}
        ok, reason = _execute(action["kind"], action["params"])
        self.state.last_action = f"{action['kind']}: {'выполнено' if ok else reason}"
        if not ok:
            return {"ok": False, "done": False, "reason": reason}
        return {"ok": True, "done": True, "reason": ""}

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            self._purge()
            return [{key: value for key, value in action.items()}
                    for action in self._pending.values()]

    def _purge(self) -> None:
        deadline = time.time() - PENDING_TTL_SEC
        for action_id in [key for key, value in self._pending.items()
                          if value["created_at"] < deadline]:
            self._pending.pop(action_id, None)

    def _ask(self, action_id: str) -> None:
        """Окно подтверждения на этом компьютере.

        Подтверждение обязано быть на машине агента: иначе любой, кто достучался
        до loopback, получил бы клавиатуру чужих приложений.
        """
        with self._lock:
            action = self._pending.get(action_id)
        if not action:
            return
        text = action["text"]
        try:
            # tkinter импортируется здесь, а не в шапке: в headless-среде (CI,
            # контейнер отладки) его может не быть вовсе, а агент обязан
            # запускаться и честно отказывать в действии.
            import tkinter

            root = tkinter.Tk()
            root.title("Агент NOZZA — подтверждение")
            root.attributes("-topmost", True)
            tkinter.Label(root, text=text, padx=18, pady=14,
                          justify="left").pack()
            box = tkinter.Frame(root)
            box.pack(pady=(0, 14))
            answer: list[bool] = []

            def decide(value: bool) -> None:
                answer.append(value)
                root.destroy()

            tkinter.Button(box, text="Подтвердить", width=14,
                           command=lambda: decide(True)).pack(side="left", padx=6)
            tkinter.Button(box, text="Отменить", width=14,
                           command=lambda: decide(False)).pack(side="left", padx=6)
            root.after(int(PENDING_TTL_SEC * 1000), root.destroy)
            root.mainloop()
            confirmed = bool(answer and answer[0])
        except (ImportError, Exception) as exc:  # noqa: B014 — нет экрана или tkinter
            # Нет экрана (служба, SSH): действие не выполняется вовсе.
            confirmed = False
            self.state.errors.append(f"Подтверждение не показано: {exc}")
        self.confirm_action(action_id, confirmed)


def _describe(kind: str, params: dict[str, Any], window: str) -> str:
    where = f" в окне «{window}»" if window else ""
    if kind == "skill":
        name = str(params.get("name") or "")
        skill = skills.get(name) or {"name": name, "title": name}
        inner = params.get("params") if isinstance(params.get("params"), dict) else {}
        return f"Навык ассистента: {executor.describe(skill, inner)}"
    if kind == "click":
        return f"Клик в точке {params.get('x')}, {params.get('y')}{where}"
    if kind == "type":
        text = str(params.get("text") or "")
        return f"Ввести текст{where}: «{text[:80]}{'…' if len(text) > 80 else ''}»"
    if kind == "key":
        return f"Нажать клавишу {params.get('key')}{where}"
    return f"Активировать окно «{params.get('title') or ''}»"


def _execute(kind: str, params: dict[str, Any]) -> tuple[bool, str]:
    if kind == "click":
        return winapi.click(int(params.get("x", 0)), int(params.get("y", 0)))
    if kind == "type":
        return winapi.type_text(str(params.get("text") or ""))
    if kind == "key":
        return winapi.press_key(str(params.get("key") or ""))
    if kind == "activate":
        rect, reason = winapi.window_rect(str(params.get("title") or ""))
        if not rect:
            return False, reason
        return winapi.click(int(rect["left"]) + 40, int(rect["top"]) + 12)
    return False, f"Действие «{kind}» не известно"


class AgentHandler(BaseHTTPRequestHandler):
    """HTTP только для loopback. Общий для обоих портов: роль задаёт `role`."""

    role = "agent"
    agent: Agent | None = None
    server_version = "PrintFlowAgent/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        pass

    # --- ответы -----------------------------------------------------------
    def _json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(min(length, 32 * 1024 * 1024))
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _read_multipart(self) -> tuple[dict[str, str], bytes]:
        """Поля и звук из multipart: речь приходит байтами, а не JSON."""
        length = int(self.headers.get("Content-Length") or 0)
        header = str(self.headers.get("Content-Type") or "")
        if not length or "boundary=" not in header:
            return {}, b""
        boundary = header.split("boundary=", 1)[1].strip().strip('"').encode("utf-8")
        body = self.rfile.read(min(length, 32 * 1024 * 1024))
        fields: dict[str, str] = {}
        audio = b""
        for part in body.split(b"--" + boundary):
            if b"Content-Disposition" not in part:
                continue
            head, _, payload = part.partition(b"\r\n\r\n")
            text = head.decode("utf-8", "replace")
            name = ""
            if 'name="' in text:
                name = text.split('name="', 1)[1].split('"', 1)[0]
            payload = payload.rstrip(b"\r\n")
            if 'filename="' in text:
                audio = payload
            elif name:
                fields[name] = payload.decode("utf-8", "replace")
        return fields, audio

    # --- маршруты ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 — имя задаёт BaseHTTPRequestHandler
        if not self._local_request():
            return None
        path = self.path.split("?", 1)[0]
        agent = self.agent
        if agent is None:
            return self._json(500, {"ok": False, "reason": "Агент не инициализирован"})
        if path == "/health":
            return self._json(200, agent.health())
        if path == "/capabilities":
            return self._json(200, {"ok": True, "capabilities": agent.refresh_capabilities(),
                                    "missing": capabilities.missing(agent.capabilities)})
        if self.role != "agent":
            return self._json(404, {"ok": False, "reason": f"Маршрута {path} нет"})
        if path == "/status":
            payload = agent.health()
            payload["pending"] = agent.pending()
            return self._json(200, payload)
        if path == "/windows":
            titles, reason = winapi.list_windows()
            return self._json(200, {"ok": not reason, "windows": titles, "reason": reason})
        if path == "/screen":
            image, reason = winapi.grab_screen()
            if not image:
                return self._json(200, {"ok": False, "reason": reason})
            return self._bytes(200, image, "image/png")
        if path == "/pending":
            return self._json(200, {"ok": True, "pending": agent.pending()})
        if path == "/skills":
            return self._json(200, agent.skills_payload())
        if path == "/journal":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            limit = int((query.get("limit") or ["30"])[0] or 30)
            return self._json(200, agent.journal(limit))
        if path == "/chat/history":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            session = brain_mod.session_key((query.get("session") or ["main"])[0])
            limit = int((query.get("limit") or ["40"])[0] or 40)
            return self._json(200, {"ok": True, "session": session,
                                    "turns": agent.runner.store.dialog(session, limit)})
        if path == "/memory":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            wanted = str((query.get("q") or [""])[0] or "")
            store = agent.runner.store
            rows = store.recall(wanted, 30, touch=False) if wanted else store.memories(100)
            return self._json(200, {"ok": True, "memories": rows, "count": len(rows)})
        if path == "/ui":
            body = ui.page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # Окно агента не встраивается в чужие страницы (кликджекинг «Подтвердить»).
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                             "connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)
            return None
        return self._json(404, {"ok": False, "reason": f"Маршрута {path} нет"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._local_request():
            return None
        path = self.path.split("?", 1)[0]
        agent = self.agent
        if agent is None:
            return self._json(500, {"ok": False, "reason": "Агент не инициализирован"})
        if path == "/health":
            return self._json(200, agent.health())
        if self.role == "speech" and path == "/transcribe":
            fields, audio = self._read_multipart()
            if not audio:
                body = self._read_json()
                audio = bytes(body.get("audio") or b"") if isinstance(body.get("audio"),
                                                                      (bytes, bytearray)) else b""
                fields = fields or {key: str(value) for key, value in body.items()}
            return self._json(200, agent.transcribe(audio, str(fields.get("language") or "ru")))
        if self.role == "speech" and path == "/mic/arm":
            body = self._read_json()
            return self._json(200, agent.arm_microphone(float(body.get("seconds") or
                                                              config.MIC_ARM_SECONDS)))
        if self.role == "speech" and path == "/mic/disarm":
            return self._json(200, agent.disarm_microphone())
        if self.role != "agent":
            return self._json(404, {"ok": False, "reason": f"Маршрута {path} нет"})
        body = self._read_json()
        if path == "/click":
            return self._json(200, agent.queue_action("click", body))
        if path == "/type":
            return self._json(200, agent.queue_action("type", body))
        if path == "/key":
            return self._json(200, agent.queue_action("key", body))
        if path == "/activate":
            return self._json(200, agent.queue_action("activate", body))
        if path == "/skill":
            return self._json(200, agent.run_skill(str(body.get("name") or ""),
                                                   body.get("params")))
        if path == "/action/confirm":
            return self._json(200, agent.confirm_action(str(body.get("id") or ""),
                                                        bool(body.get("confirmed"))))
        if path == "/chat":
            plan = body.get("plan") if isinstance(body.get("plan"), dict) else None
            mode = "pc" if str(body.get("mode") or "") == "pc" else "full"
            return self._json(200, agent.chat(str(body.get("text") or ""),
                                              str(body.get("session") or "main"), mode, plan))
        if path == "/chat/clear":
            session = brain_mod.session_key(str(body.get("session") or "main"))
            return self._json(200, {"ok": True, "session": session,
                                    "cleared": agent.runner.store.clear_dialog(session)})
        if path == "/memory":
            store = agent.runner.store
            op = str(body.get("op") or "remember")
            if op == "forget":
                rows = store.forget(body.get("id") if body.get("id") else str(body.get("text") or ""))
                return self._json(200, {"ok": bool(rows), "forgotten": rows,
                                        "reason": "" if rows else "Запись не найдена"})
            if op == "pin":
                done = store.pin_memory(int(body.get("id") or 0), bool(body.get("pinned", True)))
                return self._json(200, {"ok": done, "reason": "" if done else "Запись не найдена"})
            saved = store.remember(str(body.get("text") or ""), kind=str(body.get("kind") or "fact"),
                                   subject=str(body.get("subject") or ""), source="window")
            return self._json(200, saved)
        return self._json(404, {"ok": False, "reason": f"Маршрута {path} нет"})

    def do_OPTIONS(self) -> None:  # noqa: N802
        # Предзапрос браузера бывает только у чужого сайта: своему окну он не
        # нужен, панель ходит с сервера. Разрешений CORS агент не выдаёт.
        if not self._local_request():
            return None
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return None

    # --- граница loopback -------------------------------------------------
    def _local_request(self) -> bool:
        """Запрос от своего окна или от панели. Иначе — 403 и `False`.

        Три признака чужого: `Host` не наш (перепривязка DNS), `Origin` есть и
        он не адрес самого агента (страница другого сайта), `Origin: null`
        (песочница, файл с диска). Панель и `urllib` заголовка Origin не шлют.
        """
        host = str(self.headers.get("Host") or "").strip().lower()
        if host:
            name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
            if name not in LOCAL_HOSTS:
                self._json(403, {"ok": False, "reason": "Агент отвечает только по адресу этого компьютера"})
                return False
        origin = self.headers.get("Origin")
        if origin is not None:
            parts = urllib.parse.urlsplit(str(origin).strip().lower())
            port = self.server.server_address[1] if hasattr(self, "server") else None
            try:
                origin_port = parts.port
            except ValueError:
                origin_port = None
            if not (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")
                    and origin_port == port):
                self._json(403, {"ok": False, "reason": "Чужой сайт не может обращаться к агенту компьютера"})
                return False
        return True


def _handler(role: str, agent: Agent) -> type[AgentHandler]:
    return type(f"{role.title()}Handler", (AgentHandler,), {"role": role, "agent": agent})


def serve(agent: Agent | None = None) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer]:
    """Поднять оба порта. Исключение — если порт занят: агент не молчит."""
    agent = agent or Agent()
    speech_server = ThreadingHTTPServer(("127.0.0.1", config.SPEECH_PORT),
                                        _handler("speech", agent))
    agent_server = ThreadingHTTPServer(("127.0.0.1", config.AGENT_PORT),
                                       _handler("agent", agent))
    return speech_server, agent_server


def run() -> int:
    """Точка входа: оба сервера в потоках, остановка по Ctrl+C."""
    try:
        speech_server, agent_server = serve()
    except OSError as exc:
        print(f"Агент не запустился: {exc}")
        print("Порты 8791 и 8799 должны быть свободны, адрес — только 127.0.0.1.")
        return 1
    agent = agent_server.RequestHandlerClass.agent
    print("Агент NOZZA запущен")
    print(f"  Речь:      http://127.0.0.1:{config.SPEECH_PORT}/health")
    print(f"  Компьютер: http://127.0.0.1:{config.AGENT_PORT}/status")
    print(f"  Панель:    {config.PRINTFLOW_URL}")
    print(f"  Стоп-слово: «{config.WAKE_WORD}» (микрофон закрыт до включения)")
    print(f"  Ассистент: {window.url(config.AGENT_PORT)}")
    for line in capabilities.missing(agent.capabilities):
        print(f"  ⚠ {line}")
    if config.OPEN_WINDOW:
        opened = window.open_window(window.url(config.AGENT_PORT))
        if not opened["ok"]:
            print(f"  ⚠ {opened['reason']} — {opened['hint']}")
    # И263 (18.19): агент сам отмечает свой пульс, иначе надзор считал бы его
    # мёртвым сразу после старта и поднимал второй экземпляр на занятый порт.
    try:
        from . import watchdog as wd
        wd.beat("agent", note="старт агента")
        wd.beat_forever("agent", 30)
    except Exception as exc:
        print(f"  ⚠ Пульс надзора не включился: {exc}")
    threading.Thread(target=speech_server.serve_forever, daemon=True).start()
    try:
        agent_server.serve_forever()
    except KeyboardInterrupt:
        print("Останавливаю агента")
    finally:
        speech_server.shutdown()
        agent_server.shutdown()
    return 0
