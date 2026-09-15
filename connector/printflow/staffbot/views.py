"""Обзорные тексты бота: всё, что читает данные и не меняет их.

Статусы, датчики, доктор, очередь, деньги, дайджесты и отчёты — здесь.
Каждый метод возвращает готовую строку для Telegram (HTML не используем:
чистый текст переживает любую длину и любой клиент). Изменяющие команды
живут в соседних модулях (sales/catalog/orders/printers).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .. import APP_VERSION
from ..accounting import num
from ..config import now_iso
from ..db import SCHEMA_VERSION, list_backups
from .ui import HELP, STATE_RU, hm, money


class ViewsMixin:
    """Read-only экраны: цех, деньги, полка, пластик — одним сообщением."""

    # ------------------------------------------------------------- принтеры
    def text_status(self) -> str:
        state = self.manager.snapshot()
        printers = state.get("printers") or []
        if not printers:
            return "Принтеры не добавлены."
        blocks = []
        for snap in printers:
            info = snap["printer"]
            head = f"{snap['name']} — {STATE_RU.get(info['state'], info.get('state_label') or info['state'])}"
            if not snap["connection"]["connected"]:
                blocks.append(head + "\nНет связи по локальной сети.")
                continue
            lines = [head]
            if info["state"] in ("RUNNING", "PAUSE", "PREPARE"):
                lines.append(f"{info.get('task') or 'задание'} · {round(num(info.get('progress')))}%")
                if info.get("layer"):
                    lines.append(f"Слой {info['layer']} из {info.get('total_layers') or '—'}")
                if num(info.get("remaining_min")):
                    lines.append(f"Осталось {hm(info['remaining_min'])}"
                                 + (f", финиш в {str(info.get('eta'))[11:16]}" if info.get("eta") else ""))
                job = snap.get("job") or {}
                order = job.get("order") or {}
                if order:
                    lines.append(f"Заказ №{order.get('number')} · {order.get('product') or ''}")
                if num(job.get("spent")):
                    lines.append(f"Потрачено {money(job['spent'])}"
                                 + (f", всего будет ≈ {money(job['cost_total'])}"
                                    if num(job.get("cost_total")) else ""))
                if job.get("profit") is not None and num(job.get("price")):
                    lines.append(f"Прибыль {money(job['profit'])}"
                                 + (f" ({round(num(job.get('margin_pct')))}%)"
                                    if job.get("margin_pct") is not None else ""))
            lines.append(f"Сопло {round(num(snap['temperature'].get('nozzle')))}°, "
                         f"стол {round(num(snap['temperature'].get('bed')))}°")
            for alert in (snap.get("guard") or {}).get("alerts", [])[:3]:
                lines.append(f"⚠ {alert.get('title')}: {alert.get('reason', '')}")
            due = (snap.get("maintenance") or {}).get("due", 0)
            if due:
                lines.append(f"🔧 Просрочено работ по обслуживанию: {due}")
            blocks.append("\n".join(lines))
        farm = state.get("farm") or {}
        blocks.append(f"Парк: печатают {farm.get('printing', 0)} из {len(printers)}, "
                      f"в очереди {farm.get('queued', 0)}.")
        return "\n\n".join(blocks)

    def text_sensors(self) -> str:
        """«датчики» (A.1.4): телеметрия парка одной сводкой.

        Только чтение снимка: температуры с целями, вентиляторы, скорость,
        WiFi/прошивка, AMS (влажность, температура, слоты с остатками) и
        расшифрованные HMS-коды. Катушка в слоте показывается по имени со
        склада (сверка по tray_uuid), иначе — тип и цвет слота.
        """
        state = self.manager.snapshot()
        printers = state.get("printers") or []
        if not printers:
            return "Принтеры не добавлены."
        spools = {str(sp.get("tray_uuid") or ""): sp for sp in
                  self.db.query("SELECT * FROM spools WHERE archived=0")
                  if str(sp.get("tray_uuid") or "")}
        blocks = []
        for snap in printers:
            info = snap["printer"]
            temp = snap.get("temperature") or {}
            fans = snap.get("fans") or {}
            ams = snap.get("ams") or {}
            lines = [f"🌡 {snap['name']} — {STATE_RU.get(info['state'], info.get('state_label') or info['state'])}"]
            if not snap["connection"]["connected"]:
                lines.append("Нет связи по локальной сети — датчики неактуальны.")

            def t(key: str, target_key: str, title: str) -> str:
                value = temp.get(key)
                target = temp.get(target_key)
                if not value and not target:
                    return ""
                out = f"{title} {round(num(value))}°"
                if num(target) and round(num(target)) != round(num(value)):
                    out += f" → {round(num(target))}°"
                return out
            heads = [t("nozzle", "nozzle_target", "Сопло"),
                     t("bed", "bed_target", "Стол"),
                     t("chamber", "", "Камера")]
            heads = [h for h in heads if h]
            if heads:
                lines.append(" · ".join(heads))
            fan_parts = [f"{title} {round(num(v))}%"
                         for title, v in (("Обдув", fans.get("part")),
                                          ("Вспом.", fans.get("aux")),
                                          ("Камерный", fans.get("chamber")))
                         if v is not None]
            if fan_parts:
                lines.append(" · ".join(fan_parts))
            speed = []
            if info.get("speed_label"):
                speed.append(info["speed_label"])
            if num(info.get("speed_percent")) and num(info.get("speed_percent")) != 100:
                speed.append(f"{round(num(info.get('speed_percent')))}%")
            if info.get("wifi"):
                speed.append(f"WiFi {info['wifi']}")
            if info.get("firmware"):
                speed.append(f"прошивка {info['firmware']}")
            if speed:
                lines.append(" · ".join(speed))
            env = []
            if ams.get("temperature") is not None:
                env.append(f"температура {ams['temperature']}°")
            if ams.get("humidity") is not None:
                env.append(f"влажность {ams['humidity']}")
            if env:
                lines.append(f"AMS ({ams.get('units', 0)} бл.): " + " · ".join(env))
            for tray in ams.get("trays") or []:
                spool = spools.get(str(tray.get("uuid") or ""))
                name = (f"{spool.get('material')} {spool.get('color_name')}".strip()
                        if spool else
                        f"{tray.get('type') or 'пластик'} {tray.get('color') or ''}".strip())
                remain = tray.get("remain")
                left = f"{round(num(remain))}%" if remain is not None else "—"
                mark = "▸ " if tray.get("active") else "  · "
                lines.append(f"{mark}{name} ({tray['label']}) — {left}")
            for problem in info.get("problems") or []:
                lines.append(f"⚠ {problem.get('severity_label')}: "
                             f"{problem.get('title')}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _doctor_problems(self) -> list[str]:
        """Короткий список проблем цеха — для «доктора» и утреннего дайджеста (#86).

        Проверяется только локальное: жив ли опрос бота, каналы принтеров,
        свежесть резервных копий и место на диске. Сеть наружу не ходим —
        «доктор» должен отвечать даже когда Интернета нет.
        """
        problems: list[str] = []
        if self.last_poll:
            age = datetime.now().timestamp() - self.last_poll
            if age > 90:
                problems.append(f"🤖 Бот: последний успешный опрос {int(age)} с назад")
        else:
            problems.append("🤖 Бот: ещё не было успешного опроса Telegram")
        try:
            from ..workshop_v9 import heartbeat_channels
            channels = heartbeat_channels(self.manager, self.db)
        except Exception:
            channels = {}
        for unit, label in (("mqtt", "MQTT"), ("ftps", "FTPS")):
            for pr in (channels.get(unit) or {}).get("printers", []):
                if not pr.get("ok"):
                    problems.append(f"🔌 {pr.get('name')}: {label} — "
                                    f"{pr.get('error') or 'нет связи'}")
        disk = channels.get("disk") or {}
        if not disk.get("ok", True):
            problems.append(f"💿 Диск: {disk.get('error') or 'мало места'}")
        try:
            backups = list_backups()
            newest = max((b.get("at") or "" for b in backups), default="")
            if not newest:
                problems.append("💾 Резервных копий базы ещё нет")
            else:
                try:
                    age_h = (datetime.now()
                             - datetime.fromisoformat(str(newest).replace("Z", ""))).total_seconds() / 3600
                    if age_h > 48:
                        problems.append(
                            f"💾 Последняя копия базы {round(age_h)} ч назад")
                except Exception:
                    pass
        except Exception:
            pass
        return problems

    def text_doctor(self) -> str:
        """«доктор» (#80): здоровье цеха одним сообщением, без внешней сети."""
        problems = self._doctor_problems()
        lines = ["🩺 Доктор PrintFlow:"]
        if problems:
            lines.append(f"⚠ Проблем: {len(problems)}")
            lines.extend(f"  · {p}" for p in problems)
        else:
            lines.append("✅ Цех здоров: бот, связь, копии и диск — ок.")
        lines.append(f"Версия {APP_VERSION} · схема {SCHEMA_VERSION}. "
                     "Подробная диагностика: python pf.py doctor.")
        return "\n".join(lines)

    def text_queue(self) -> str:
        jobs = [j for j in self.manager.queue() if j.get("state") in ("queued", "running", "starting")]
        if not jobs:
            return "Очередь пуста."
        lines = ["Очередь печати:"]
        for index, job in enumerate(jobs[:12], 1):
            order = job.get("order") or {}
            title = order.get("number") and f"№{order['number']} {order.get('product') or ''}" or job.get("name") or "задание"
            mark = {"running": "▶", "starting": "▶"}.get(job.get("state"), f"{index}.")
            lines.append(f"{mark} {title}")
        if self.manager.quiet_now():
            lines.append("\nСейчас тихие часы — автозапуск отложен до утра.")
        return "\n".join(lines)

    # --------------------------------------------------------------- деньги
    def text_money(self) -> str:
        summary = self.manager.acc.summary(30)
        debts = self.manager.acc.debts()
        lines = [
            "Деньги за 30 дней:",
            f"Выручка {money(summary.get('income'))}",
            f"Расходы {money(summary.get('expense'))}",
            f"Прибыль {money(summary.get('profit'))}",
        ]
        if summary.get("orders"):
            lines.append(f"Заказов {summary['orders']}, средний чек {money(summary.get('avg_check'))}")
        if num(debts.get("total")) > 0:
            lines.append(f"\nЖдём оплату: {money(debts['total'])} по {debts.get('count', 0)} заказам")
            if num(debts.get("overdue")) > 0:
                lines.append(f"Из них просрочено: {money(debts['overdue'])}")
        return "\n".join(lines)

    def text_shop_cash(self) -> str:
        """Касса стеллажа (магазин): сколько продано, забрано и лежит в магазине."""
        from ..shelf import Shelf
        c = Shelf(self.db).shop_cash()
        lines = [
            "🛍 Касса стеллажа (магазин):",
            f"Продано со стеллажа: {money(c.get('shelf_income'))}",
            f"Забрали из магазина: {money(c.get('collected_total'))}",
            f"Лежит в магазине: {money(c.get('in_shop'))}",
        ]
        rows = c.get("collections") or []
        if rows:
            lines.append("\nПоследние выемки:")
            for r in rows[:5]:
                when = str(r.get("at") or "")[:16].replace("T", " ")
                note = str(r.get("note") or "").strip()
                lines.append(f"· {money(r.get('amount'))} — {when}"
                             + (f" ({note[:40]})" if note else ""))
        lines.append("\nЗаписать выемку: «забрали 5000» · «забрали 2500 картой» ·"
                     " «забрали все».")
        return "\n".join(lines)

    def text_today(self) -> str:
        today = now_iso()[:10]
        rows = self.db.query(
            "SELECT kind, SUM(amount) AS total FROM transactions WHERE substr(at,1,10)=? GROUP BY kind",
            (today,))
        money_by = {r["kind"]: num(r["total"]) for r in rows}
        jobs = self.db.query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(grams),0) g, COALESCE(SUM(duration_min),0) m"
            " FROM print_jobs WHERE substr(COALESCE(finished_at,queued_at),1,10)=? AND state='done'",
            (today,))
        job = jobs[0] if jobs else {}
        lines = [
            f"Итоги дня {today}:",
            f"Приход {money(money_by.get('income'))}, расход {money(money_by.get('expense'))}",
            f"Напечатано заданий: {int(num(job.get('n')))}, пластика {round(num(job.get('g')))} г, "
            f"время печати {hm(num(job.get('m')))}",
        ]
        # Хвосты учёта: заказы без цены и т.п. — чтобы не копились до конца месяца.
        try:
            from ..repo import Repo
            problems = Repo(self.db).data_check().get("problems") or []
        except Exception:
            problems = []
        if problems:
            lines.append(f"⚠ Хвосты учёта: {len(problems)} — «хвосты» покажет список")
        return "\n".join(lines)

    def text_loose_ends(self) -> str:
        """«хвосты» — незакрытые дыры в учёте: заказы без цены и т.п."""
        from ..repo import Repo
        problems = Repo(self.db).data_check().get("problems") or []
        if not problems:
            return "✅ Хвостов нет — учёт чистый."
        lines = [f"⚠ Хвосты учёта: {len(problems)}"]
        for problem in problems[:12]:
            lines.append(f"· {problem.get('title')}"
                         + (f" — {problem.get('detail')}" if problem.get("detail") else ""))
        return "\n".join(lines)

    def text_debts(self) -> str:
        """«долги» — кто и сколько должен, с просрочкой."""
        debts = self.manager.acc.debts()
        if not debts.get("rows"):
            return "💰 Долгов нет — всё оплачено."
        lines = [f"💰 Долги клиентов: {money(debts['total'])} "
                 f"по {debts.get('count', 0)} заказам"]
        if num(debts.get("overdue")) > 0:
            lines.append(f"Просрочено: {money(debts['overdue'])}")
        for row in debts["rows"][:10]:
            who = (row.get("customer") or "").strip() or "без имени"
            age = f" · {row['days']} дн" if row.get("days") else ""
            lines.append(f"· №{row.get('number')} {who} — {money(row['debt'])}{age}")
        return "\n".join(lines)

    def text_defects(self, days: int = 30) -> str:
        """«брак» — сорванные печати и сколько денег они съели."""
        since = (datetime.now() - timedelta(days=max(1, int(days)))).isoformat()
        jobs = self.db.query(
            "SELECT * FROM print_jobs WHERE state='failed' AND finished_at>=?"
            " ORDER BY finished_at DESC", (since,))
        facts = self.manager.acc.defects_cost(days)
        lines = [f"❌ Брак за {int(days)} дней: {len(jobs)} печатей"]
        lines.append(
            f"Потеряно: {round(num(facts.get('grams')))} г пластика, "
            f"{hm(num(facts.get('minutes')))} времени"
        )
        lines.append(
            f"≈ {money(num(facts.get('cost')))}; подтверждённые разборы взяты по факту"
        )
        for job in jobs[:6]:
            error = str(job.get("error") or "").strip()[:70]
            lines.append(f"· {job.get('name') or 'без названия'}"
                         + (f" — {error}" if error else ""))
        return "\n".join(lines)

    def text_rating(self) -> str:
        """«рейтинг» — ABC изделий: что приносит деньги, что висит балластом."""
        items = self.manager.acc.abc_report(30).get("items", [])
        if not items:
            return "За 30 дней продаж не было — рейтинг пуст."
        lines = ["🏆 Рейтинг изделий (30 дней):"]
        for item in items[:10]:
            lines.append(
                f"{item.get('cls', '')} · {item.get('name')} — {money(item.get('revenue'))}"
                f" ({item.get('share')}%), прибыль {money(item.get('profit'))}")
        return "\n".join(lines)

    def text_month_report(self) -> str:
        """«итоги месяца» — P&L месяца, печать, брак, долги."""
        key = now_iso()[:7]
        pnl = self.manager.acc.pnl_month(key)
        jobs = self.db.query(
            "SELECT state, COUNT(*) n, COALESCE(SUM(grams),0) g,"
            " COALESCE(SUM(duration_min),0) m FROM print_jobs"
            " WHERE finished_at>=? GROUP BY state",
            (f"{key}-01",))
        by_state = {row["state"]: row for row in jobs}
        done = by_state.get("done") or {}
        failed = by_state.get("failed") or {}
        lines = [
            f"📊 Итоги месяца {key}:",
            f"Выручка {money(pnl.get('income'))}, расход {money(pnl.get('expense'))}",
            f"Прибыль {money(pnl.get('profit'))} (маржа {round(num(pnl.get('margin')))}%)",
            f"Печать: {int(num(done.get('n')))} заданий, {round(num(done.get('g')))} г,"
            f" {hm(num(done.get('m')))}",
        ]
        if int(num(failed.get("n"))):
            lines.append(f"⚠ Брак: {int(num(failed.get('n')))} печатей")
        debts = self.manager.acc.debts()
        if num(debts.get("total")) > 0:
            lines.append(f"💰 Долги: {money(debts['total'])}")
        tax = self.month_tax_estimate(key)
        if num(tax) > 0:
            lines.append(f"🧾 Налог месяца (оценка): {money(tax)}")
        return "\n".join(lines)

    def month_tax_estimate(self, key: str) -> float:
        """Оценка налога месяца — из мастера «Закрыть месяц»."""
        try:
            from ..month_close import MonthClose
            return num(MonthClose(self.db).month_tax(key).get("tax"))
        except Exception:
            return 0.0

    def _month_close(self, text: str) -> str:
        """«закрыть месяц» — состояние мастера; «закрыть месяц fixed» — шаг."""
        from ..month_close import MonthClose, STEP_ORDER
        master = MonthClose(self.db)
        parts = text.split()
        if len(parts) > 2 and parts[2] in STEP_ORDER:
            step = parts[2]
            result = master.run("", step)
            if not result.get("ok") and result.get("done"):
                return f"Шаг «{step}» уже выполнен в этом месяце."
            if not result.get("ok"):
                return f"Шаг не выполнен: {result.get('error')}"
            return f"✅ {result.get('message') or 'готово'}"
        state = master.state()
        lines = [f"🧾 Закрыть месяц {state['key']}:"]
        for step in state["order"]:
            mark = "✅" if state["done"][step] else ("▸" if state["next"] == step else "·")
            lines.append(f"{mark} {state['titles'][step]}")
        if state["next"]:
            lines.append(f"\nШаг: «закрыть месяц {state['next']}»")
        return "\n".join(lines)

    # -------------------------------------------------------------- панель
    def text_panel(self) -> str:
        """Главная панель: печать, деньги, план, долги — одним сообщением."""
        blocks = []
        state = self.manager.snapshot()
        printers = state.get("printers") or []
        farm = state.get("farm") or {}
        if printers:
            active = next((p for p in printers if p["printer"]["state"] in ("RUNNING", "PAUSE", "PREPARE")), None)
            if active:
                info = active["printer"]
                blocks.append(f"🖨 {active['name']} — {STATE_RU.get(info['state'], info['state'])} "
                              f"{round(num(info.get('progress')))}%"
                              + (f", осталось {hm(info['remaining_min'])}" if num(info.get("remaining_min")) else ""))
            else:
                blocks.append(f"🖨 Парк свободен ({len(printers)} принтер(ов), в очереди {farm.get('queued', 0)})")
        summary = self.manager.acc.summary(30)
        blocks.append(f"₽ За 30 дней: доход {money(summary.get('income'))}, "
                      f"прибыль {money(summary.get('profit'))}")
        debts = self.manager.acc.debts()
        if num(debts.get("total")) > 0:
            blocks.append(f"💰 Ждут оплаты {money(debts['total'])} по {debts.get('count', 0)} заказам")
        # что печатать следующим
        try:
            from ..planner import Planner
            from ..batches import Batches
            planner = Planner(self.db, Batches(self.db))
            plan = planner.day_plan()
            next_task = plan.get("suggested_next")
            if next_task:
                label = "заказ" if next_task.get("kind") == "order" else "полка"
                blocks.append(f"⚑ Следующее ({label}): {next_task.get('title')} · {hm(next_task.get('hours', 0) * 60)}")
            elif plan.get("sequence"):
                blocks.append("⚑ Очередь пуста, но есть план — откройте «план».")
            else:
                blocks.append("⚑ Печатать нечего: очередь и полка в порядке.")
        except Exception:
            pass
        alerts = []
        for p in printers:
            for a in (p.get("guard") or {}).get("alerts", [])[:2]:
                alerts.append(f"⚠ {a.get('title')}")
        if alerts:
            blocks.append("\n".join(alerts[:3]))
        return "\n".join(blocks)

    def text_plan(self) -> str:
        """Что печатать сегодня — из мастер-плана производства."""
        try:
            from ..planner import Planner
            from ..batches import Batches
            planner = Planner(self.db, Batches(self.db))
            plan = planner.day_plan()
        except Exception:
            return "План сейчас недоступен."
        lines = [f"⚑ План на сегодня ({plan.get('verdict_text') or ''})",
                 f"Занято {plan.get('in_progress_hours')} ч, план {plan.get('planned_hours')} ч, "
                 f"загрузка {round(num(plan.get('load_pct')))}%"]
        for i, t in enumerate(plan.get("sequence")[:8], 1):
            kind = "▦" if t.get("kind") == "order" else "▤"
            issues = " ✕" if not t.get("ready") else ""
            lines.append(f"{kind} {t.get('title')} · {hm(num(t.get('hours')) * 60)}{issues}")
        if not plan.get("sequence"):
            lines.append("Печатать нечего — полка и очередь в порядке.")
        return "\n".join(lines)

    def text_ask(self, text: str) -> str:
        """«Спроси принтер»: сколько осталось / что печатает / когда закончит /
        сколько заработал — разбор по ключевым словам, без ИИ."""
        state = self.manager.snapshot()
        printers = state.get("printers") or []
        printer = next((p for p in printers
                        if p["printer"]["state"] in ("RUNNING", "PAUSE", "PREPARE")), None)
        if "заработал" in text or "заработано" in text or "прибыль" in text:
            summary = self.manager.acc.summary(1)
            return (f"Сегодня: приход {money(summary.get('income'))}, "
                    f"прибыль {money(summary.get('profit'))}, "
                    f"часов печати {round(num(summary.get('print_hours')), 1)}")
        if not printer:
            return "Сейчас ничего не печатается."
        info = printer["printer"]
        if "когда" in text or "закончит" in text or "во сколько" in text:
            if num(info.get("remaining_min")):
                eta = str(info.get("eta") or "")
                return (f"Готово через {hm(info['remaining_min'])}"
                        + (f", примерно в {eta[11:16]}" if eta else ""))
            return "Прогноз времени пока не готов — печать только началась."
        if "осталось" in text or "сколько" in text or "прогресс" in text:
            return (f"{printer['name']}: {round(num(info.get('progress')))}% · "
                    f"слой {info.get('layer')} из {info.get('total_layers') or '—'}"
                    + (f" · осталось {hm(info['remaining_min'])}"
                       if num(info.get("remaining_min")) else ""))
        if "печатает" in text or "задание" in text or "задача" in text:
            job = printer.get("job") or {}
            order = job.get("order") or {}
            extra = f" · заказ №{order.get('number')}" if order else ""
            return (f"{printer['name']} печатает «{info.get('task') or job.get('name') or '—'}»{extra}"
                    f" · {round(num(info.get('progress')))}%")
        # не распознали вопрос — покажем компактный статус
        return self.text_status()

    # -------------------------------------------------------------- полка
    def text_shelf(self, only_needs: bool = False) -> str:
        """Короткий честный срез стеллажа для телефона.

        Не подменяет инвентаризацию: показывает учётный остаток и помечает
        позиции, которые надо проверить или пополнить физически.
        """
        from ..shelf import Shelf
        shelf = Shelf(self.db)
        items = shelf.items()
        if not items:
            return "🛍 Стеллаж пуст: активных позиций пока нет."
        needs = [i for i in items if i.get("status") in ("empty", "low", "dead") or num(i.get("plan_qty")) > 0]
        total_qty = sum(num(i.get("qty")) for i in items)
        total_value = sum(num(i.get("stock_value")) for i in items)
        if only_needs:
            if not needs:
                return "✅ Стеллаж в порядке: пустых, низких и залежавшихся позиций нет."
            title = f"⚠ Внимание к стеллажу: {len(needs)}"
            rows = needs
        else:
            title = (f"🛍 Стеллаж: {len(items)} поз. · {round(total_qty, 1)} шт · "
                     f"учётная стоимость {money(total_value)}")
            rows = sorted(needs, key=lambda i: (i.get("status") == "empty", num(i.get("plan_qty"))), reverse=True) or items
        lines = [title]
        for item in rows[:8]:
            # В базовой аналитике «нет продаж» приоритетнее low. Для человека
            # на полке это две разные причины действия, поэтому не прячем
            # низкий остаток за статусом залежавшегося товара.
            marks = []
            if num(item.get("qty")) <= 0:
                marks.append("нет")
            elif item.get("low"):
                marks.append("мало")
            elif not item.get("dead"):
                marks.append("в норме")
            if item.get("dead"):
                marks.append("нет продаж")
            status = " · ".join(marks) or "проверить"
            extra = f" · печать +{int(num(item.get('plan_qty')))}" if num(item.get("plan_qty")) > 0 else ""
            lines.append(f"• {item.get('name') or 'Без названия'} — {round(num(item.get('qty')), 1)} шт · {status}{extra}")
        if len(rows) > 8:
            lines.append(f"… ещё {len(rows) - 8} поз. в полной панели.")
        today = shelf.today_sales()
        cash = shelf.shop_cash()
        answer = "\n".join(lines)
        if not only_needs:
            answer += ("\n\n📈 Сегодня: "
                       f"{round(today.get('qty', 0), 1)} шт · {money(today.get('money', 0))}"
                       f" · в кассе магазина {money(cash.get('in_shop', 0))}")
            online_money = num(today.get("online_money", 0))
            online_total = num(cash.get("online_income", 0))
            if online_money > 0 or online_total > 0:
                answer += (f"\n🌐 Онлайн (Авито/ТГ): сегодня {money(online_money)} · "
                           f"всего {money(online_total)} — на счёте, не в кассе")
        return answer

    def text_shelf_cash(self) -> str:
        """Касса стеллажа: сколько лежит в магазине и как записать выемку."""
        from ..shelf import Shelf
        shelf = Shelf(self.db)
        cash = shelf.shop_cash()
        today = shelf.today_sales()
        lines = [
            "💰 Касса стеллажа",
            f"• Продано сегодня: {round(today.get('qty', 0), 1)} шт · "
            f"{money(today.get('money', 0))}"
            f" (полка {money(today.get('shop_money', 0))} · "
            f"онлайн {money(today.get('online_money', 0))})",
            f"• Продано за все время: {money(cash.get('shelf_income'))}",
            f"• Забрали из магазина: {money(cash.get('collected_total'))}",
            f"• Лежит в магазине: {money(cash.get('in_shop'))}",
            f"• Онлайн (Авито/ТГ): {money(cash.get('online_income'))}"
            " — на счёте, в кассу магазина не входит",
            "",
            "Быстрая выемка — кнопками ниже. Точная сумма: «забрали 5000».",
        ]
        return "\n".join(lines)

    def text_shelf_moves(self, limit: int = 12) -> str:
        """Последние движения стеллажа одной лентой."""
        from ..shelf import Shelf
        moves = Shelf(self.db).moves(limit=limit)
        if not moves:
            return "Движений стеллажа пока нет."
        labels = {"produce": "приход", "sale": "продажа", "online": "онлайн",
                  "writeoff": "списание", "inventory": "инв."}
        lines = ["🧾 Последние движения стеллажа:"]
        for m in moves:
            q = num(m.get("qty"))
            sign = "+" if q > 0 else ""
            name = m.get("item_name") or "позиция"
            day = str(m.get("at") or "")[:16].replace("T", " ")
            price = num(m.get("price"))
            tail = f" · {money(price * abs(q))}" if price and q < 0 else ""
            lines.append(f"{day} · {labels.get(m.get('kind'), m.get('kind'))} · "
                         f"{name} {sign}{round(q,1)} шт{tail}")
        return "\n".join(lines)

    def text_shelf_sales(self, days: int = 7) -> str:
        """Что реально продалось со стеллажа за период, по позициям."""
        since = (datetime.now() - timedelta(days=days)).isoformat()
        rows = self.db.query(
            "SELECT m.item_id, i.name, SUM(-m.qty) qty, SUM(-m.qty*m.price) money"
            " FROM shelf_moves m LEFT JOIN shelf_items i ON i.id=m.item_id"
            " WHERE m.kind IN ('sale','online') AND m.qty<0 AND m.at>=?"
            " GROUP BY m.item_id ORDER BY money DESC", (since,))
        if not rows:
            return f"За {days} дн продаж со стеллажа не было."
        total_qty = sum(num(r.get("qty")) for r in rows)
        total_money = sum(num(r.get("money")) for r in rows)
        lines = [f"📊 Продажи стеллажа за {days} дн: {round(total_qty,1)} шт · {money(total_money)}"]
        for r in rows[:15]:
            lines.append(f"• {r.get('name') or 'позиция'} — {round(num(r.get('qty')),1)} шт · {money(r.get('money'))}")
        if len(rows) > 15:
            lines.append(f"… ещё {len(rows) - 15} поз.")
        return "\n".join(lines)

    # ------------------------------------------------------------ пластик
    def text_filament(self) -> str:
        """Остатки филамента на складе и в AMS: граммы, %, прогноз окончания."""
        spools = self.db.query("SELECT * FROM spools WHERE archived=0 ORDER BY remaining_grams DESC")
        if not spools:
            return "Катушек на складе нет — добавьте через раздел «Склад»."
        threshold = num(self.db.setting("filament_low_threshold", 15.0), 15.0)
        lines = ["🧵 Филамент на складе:"]
        for s in spools[:12]:
            total = max(1.0, num(s.get("total_grams"), 1000))
            left = num(s.get("remaining_grams"))
            pct = left / total * 100
            mark = "⚠" if pct <= threshold else "·"
            slot = f" · AMS слот {s.get('ams_slot')}" if str(s.get("ams_slot") or "") != "" else ""
            lines.append(f"{mark} {s.get('material')} {s.get('color_name') or ''} — "
                         f"{round(left)} г ({round(pct)}%){slot}")
        # Прогноз закупки по темпу расхода за 30 дней
        usage = self.db.one(
            "SELECT UPPER(material) m, SUM(grams) g FROM filament_usage"
            " WHERE at>=? GROUP BY UPPER(material) ORDER BY g DESC LIMIT 3",
            ((datetime.now() - timedelta(days=30)).isoformat(),))
        hints = []
        for row in usage or []:
            rate = num(row.get("g")) / 30.0  # г/день
            stock = num(self.db.one(
                "SELECT COALESCE(SUM(remaining_grams),0) v FROM spools"
                " WHERE archived=0 AND UPPER(material)=?", (row.get("m"),))["v"])
            if rate > 0:
                days = stock / rate
                hints.append(f"{row.get('m')}: хватит на ~{int(days)} дн (темп {round(rate)} г/дн)")
        if hints:
            lines.append("\nПрогноз:")
            lines.extend("  " + h for h in hints)
        return "\n".join(lines)

    def _shopping(self, text: str) -> str:
        """Список закупок: показать, автозаполнить или добавить вручную."""
        from ..shopping import ShoppingList
        shop = ShoppingList(self.db)
        words = text.lower().replace("ё", "е").split()
        # «закупка авто» — автозаполнение из низких катушек и темпа расхода
        if len(words) > 1 and words[1] in ("авто", "обновить", "заполнить"):
            result = shop.auto_fill()
            if result.get("count"):
                return f"🛒 Добавлено в закупку: {result['count']} позиций.\n\n" + shop.text()
            return "🛒 Добавлять нечего — катушки и запас в порядке.\n\n" + shop.text()
        # Одной команды недостаточно для честной приёмки: нужны фактические
        # количество/вес, сумма и касса. Не закрываем строку без этих данных.
        if len(words) > 1 and words[1] in ("купил", "получил", "принял"):
            return ("Оформите приём в панели: Склад пластика → Список закупок → «Принять». "
                    "Там PrintFlow создаст катушки и запишет подтверждённый расход без дублей.")
        # «закупка <материал> <qty>» — добавить вручную
        if len(words) > 1 and words[1] not in ("авто", "купил", "обновить", "заполнить"):
            material = words[1].upper()
            qty = next((w for w in words[2:] if w.isdigit()), "1")
            shop.add({"name": material, "material": material, "qty": float(qty),
                      "unit": "кг", "source": "manual"})
            return f"🛒 Добавлено: {material} {qty} кг.\n\n" + shop.text()
        return shop.text()

    # ----------------------------------------------------------- дайджесты
    def text_digest(self) -> str:
        """Утренний дайджест: дедлайны, очередь, остатки, кому написать."""
        today = now_iso()[:10]
        finals = {r["id"] for r in self.db.query("SELECT id FROM statuses WHERE is_final=1")}
        orders = self.db.query("SELECT * FROM orders")
        active = [o for o in orders if o["status"] not in finals]
        due_today = [o for o in active if o.get("due") == today]
        late = [o for o in active if o.get("due") and o["due"] < today]
        lines = ["☀ Доброе утро! Дайджест PrintFlow:"]
        if late:
            lines.append(f"⚠ Просрочено заказов: {len(late)}")
            for o in late[:3]:
                lines.append(f"  №{o.get('number')} {o.get('product') or ''} (срок {o.get('due')})")
        if due_today:
            lines.append(f"📌 Срок сегодня: {len(due_today)}")
            for o in due_today[:3]:
                lines.append(f"  №{o.get('number')} {o.get('product') or ''}")
        else:
            lines.append("📌 Заказов со сроком на сегодня нет.")
        queue = [j for j in self.manager.queue() if j.get("state") == "queued"]
        if queue:
            lines.append(f"🖨 В очереди {len(queue)} заданий:")
            for j in queue[:5]:
                order = j.get("order") or {}
                lines.append(f"  · {order.get('number') and ('№' + str(order['number']) + ' ') or ''}{j.get('name') or ''}")
        else:
            lines.append("🖨 Очередь печати пуста.")
        low = self.db.query(
            "SELECT material, color_name, remaining_grams FROM spools WHERE archived=0")
        low = [s for s in low
               if num(s["remaining_grams"]) / max(1.0, num(self.db.setting("default_spool_weight", 1000))) * 100
               <= num(self.db.setting("filament_low_threshold", 15.0))]
        if low:
            lines.append("🧵 Мало пластика:")
            for s in low[:5]:
                lines.append(f"  · {s['material']} {s['color_name']} — {round(num(s['remaining_grams']))} г")
        debts = self.manager.acc.debts()
        if num(debts.get("total")) > 0:
            lines.append(f"💰 Ждут оплаты {money(debts['total'])} по {debts.get('count', 0)} заказам")
        # #86 «Утренний авто-доктор»: дайджест сам говорит, здоров ли цех.
        problems = self._doctor_problems()
        if problems:
            lines.append(f"🩺 Цех требует внимания ({len(problems)}):")
            lines.extend(f"  · {p}" for p in problems[:4])
        else:
            lines.append("🩺 Цех здоров: бот, связь, копии и диск — ок.")
        return "\n".join(lines)

    def text_weekly(self) -> str:
        """Еженедельный отчёт: деньги, печать, брак, пластик."""
        summary = self.manager.acc.summary(7)
        jobs = self.db.query(
            "SELECT state, COUNT(*) n, COALESCE(SUM(grams),0) g, COALESCE(SUM(duration_min),0) m"
            " FROM print_jobs WHERE finished_at>=? GROUP BY state",
            ((datetime.now() - timedelta(days=7)).isoformat(),))
        by_state = {r["state"]: r for r in jobs}
        lines = [
            "📊 Недельный отчёт PrintFlow:",
            f"Выручка {money(summary.get('income'))}, расход {money(summary.get('expense'))}",
            f"Прибыль {money(summary.get('profit'))} (маржа {round(num(summary.get('margin')))}%)",
            f"Печать: {int(num((by_state.get('done') or {}).get('n')))} заданий, "
            f"{round(num((by_state.get('done') or {}).get('g')))} г, "
            f"{hm(num((by_state.get('done') or {}).get('m')))}",
        ]
        failed = int(num((by_state.get("failed") or {}).get("n")))
        if failed:
            lines.append(f"⚠ Брак: {failed} печатей")
        stock = self.db.one("SELECT COALESCE(SUM(remaining_grams),0) v FROM spools WHERE archived=0") or {}
        lines.append(f"🧵 Пластика на складе: {round(num(stock.get('v')))} г")
        debts = self.manager.acc.debts()
        if num(debts.get("total")) > 0:
            lines.append(f"💰 Долги: {money(debts['total'])}")
        return "\n".join(lines)

    # ---------------------------------------------------- текстовые команды
    # Каждая — одна строка: маршрут из router.py приводит сюда, тексты выше.
    def cmd_help(self, chat: str, raw: str, text: str) -> None:
        self._reply_keyboard(chat, HELP)

    def cmd_more(self, chat: str, raw: str, text: str) -> None:
        self.more_keyboard(chat)

    def cmd_panel(self, chat: str, raw: str, text: str) -> None:
        self._reply_keyboard(chat, self.text_panel())

    def cmd_plan(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.text_plan())

    def cmd_sensors(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.text_sensors())

    def cmd_doctor(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.text_doctor())

    def cmd_ask(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.text_ask(text))

    def cmd_rating(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.text_rating())

    def cmd_defects(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.text_defects())

    def cmd_loose_ends(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.text_loose_ends())

    def cmd_money(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.text_money())

    def cmd_today(self, chat: str, raw: str, text: str) -> None:
        # «итоги недели» и «итоги месяца» — те же кнопки «Итоги» по смыслу.
        if "недел" in text:
            return self._reply(chat, self.text_weekly())
        if "месяц" in text:
            return self._reply(chat, self.text_month_report())
        self._reply(chat, self.text_today())

    def cmd_debts(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.text_debts())

    def cmd_filament(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.text_filament())

    def cmd_shopping(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._shopping(text))

    # ------------------------------------------------------- кнопки (callback)
    def cb_help(self, chat: str, params: str) -> str:
        return HELP

    def cb_panel(self, chat: str, params: str) -> str:
        return self.text_panel()

    def cb_sensors(self, chat: str, params: str) -> str:
        return self.text_sensors()

    def cb_doctor(self, chat: str, params: str) -> str:
        return self.text_doctor()

    def cb_plan(self, chat: str, params: str) -> str:
        return self.text_plan()

    def cb_filament(self, chat: str, params: str) -> str:
        return self.text_filament()

    def cb_money(self, chat: str, params: str) -> str:
        return self.text_money()

    def cb_today(self, chat: str, params: str) -> str:
        return self.text_today()

    def cb_weekly(self, chat: str, params: str) -> str:
        return self.text_weekly()
