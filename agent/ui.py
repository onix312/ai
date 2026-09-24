"""Окно помощника (18.21): разговор, подтверждения, память и навыки на одной странице.

Почему страница, а не сразу `pywebview`.

Окно помощника обязано работать там, где работает агент: на компьютере цеха с
панелью, в службе Windows без рабочего стола и в отладочном контейнере. Поэтому
сначала появляется содержимое (страница на порту агента, только loopback), а
обёртка окна — второй слой: `window.py` открывает эту страницу в `pywebview`,
если он установлен, и честно печатает адрес, если нет.

Что изменилось в 18.21. Раньше окно было формой «выберите навык из списка и
заполните параметры», а результат показывался сырым JSON. Теперь главное —
разговор с мозгом помощника (`POST /chat`): фраза словами, ответ словами, под
ответом «как я понял» и следующие шаги кнопками. Подтверждения живут прямо в
ленте, память видна и стирается одной кнопкой, реестр навыков и ручной запуск
остались во вкладке «Навыки».

Дизайн окна (18.21, вторая итерация). Шапка показывает живые связи —
панель цеха и модель — цветными метками с причиной во всплывающей подсказке;
на узком окне метки прячутся, а связь с панелью остаётся точкой на знаке N.
Каркас — одна колонка `minmax(0, 1fr)`: в окне шириной 420 px ничего не уезжает
вбок. Боковая панель на узком окне выдвигается поверх ленты и закрывается
крестиком, Esc или щелчком мимо. Карточка «Нужно ваше решение» показывает,
сколько секунд действие ещё ждёт, и гаснет сама, если его решили в другом
месте. Ссылки из ответов панели («Открыть «Заказы» в панели») — только
абсолютные адреса панели, которые собрал сам агент. Навыки сгруппированы,
щелчок по навыку открывает ручной запуск, результат — фразой, JSON спрятан.
Журнал — по-русски: название навыка и исход («выполнено», «не вышло»).

Безопасность страницы: всё, что пришло с сервера (заголовки окон, тексты
журнала, объявления Авито), вставляется только через экранирование или
`textContent`. До 18.21 журнал подставлялся в `innerHTML` как есть — заголовок
объявления с разметкой исполнился бы в окне, где есть кнопка «Подтвердить».

Внешних ресурсов нет: ни шрифтов, ни скриптов с чужих доменов — инвариант
репозитория («данные машину не покидают») действует и для агента.
"""
from __future__ import annotations

from typing import Any

TITLE = "Помощник NOZZA"

_CSS = """
:root {
  color-scheme: light;
  --bg: #f4f6fb; --bg-2: #eef1f8; --panel: #ffffff; --panel-2: #f7f8fc; --line: #e3e7f0;
  --line-strong: #cfd6e4; --text: #121826; --text-2: #3b4456; --muted: #6b7486;
  --accent: #5b5bf7; --accent-2: #8b5cf6; --accent-soft: rgba(91, 91, 247, .10);
  --accent-line: rgba(91, 91, 247, .28); --ok: #16a34a; --ok-soft: rgba(22, 163, 74, .10);
  --warn: #d97706; --warn-soft: rgba(217, 119, 6, .12); --bad: #dc2626; --bad-soft: rgba(220, 38, 38, .10);
  --shadow: 0 1px 2px rgba(16, 24, 40, .06), 0 8px 24px -12px rgba(16, 24, 40, .18);
  --shadow-lg: 0 24px 48px -24px rgba(16, 24, 40, .35);
  --r: 16px; --r-sm: 10px; --ease: cubic-bezier(.2, .8, .2, 1);
  --font: "Segoe UI Variable Text", "Segoe UI", Inter, system-ui, -apple-system, Roboto, sans-serif;
  --mono: "Cascadia Code", "JetBrains Mono", ui-monospace, Consolas, monospace;
}
html[data-theme="dark"] {
  color-scheme: dark;
  --bg: #0b0f19; --bg-2: #0f1422; --panel: #121826; --panel-2: #161d2d; --line: #222a3b;
  --line-strong: #2e3850; --text: #e8ecf5; --text-2: #c2c9d8; --muted: #8b94a8;
  --accent: #8083ff; --accent-2: #a78bfa; --accent-soft: rgba(128, 131, 255, .14);
  --accent-line: rgba(128, 131, 255, .34); --ok: #22c55e; --ok-soft: rgba(34, 197, 94, .16);
  --warn: #f59e0b; --warn-soft: rgba(245, 158, 11, .16); --bad: #f87171; --bad-soft: rgba(248, 113, 113, .16);
  --shadow: 0 1px 2px rgba(0, 0, 0, .4), 0 12px 32px -16px rgba(0, 0, 0, .7);
  --shadow-lg: 0 24px 60px -24px rgba(0, 0, 0, .8);
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin: 0; font: 14px/1.55 var(--font); color: var(--text); background:
  radial-gradient(1200px 600px at 100% -10%, var(--accent-soft), transparent 60%),
  radial-gradient(900px 500px at -10% 110%, rgba(139, 92, 246, .08), transparent 60%), var(--bg);
  -webkit-font-smoothing: antialiased; overflow: hidden; }
button, input, select, textarea { font: inherit; color: inherit; }
button { cursor: pointer; }
a { color: var(--accent); }
svg { flex: none; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 8px; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

/* --- каркас: одна колонка, которая не шире окна (узкое окно не уезжает вбок) --- */
.app { display: grid; grid-template-rows: auto minmax(0, 1fr); grid-template-columns: minmax(0, 1fr); height: 100vh; height: 100dvh; }
.top { display: flex; align-items: center; gap: 12px; padding: 10px 16px 10px 18px; min-width: 0;
  background: color-mix(in srgb, var(--panel) 82%, transparent); backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--line); position: relative; z-index: 5; }
.mark { position: relative; width: 34px; height: 34px; border-radius: 11px; display: grid; place-items: center; flex: none;
  color: #fff; font-weight: 800; letter-spacing: -.02em;
  background: linear-gradient(135deg, var(--accent), var(--accent-2)); box-shadow: 0 6px 18px -6px var(--accent); }
.mark .live { position: absolute; right: -3px; bottom: -3px; width: 12px; height: 12px; border-radius: 50%;
  background: var(--muted); border: 2px solid var(--panel); }
.mark .live.ok { background: var(--ok); } .mark .live.bad { background: var(--bad); } .mark .live.warn { background: var(--warn); }
.brand { display: flex; flex-direction: column; line-height: 1.15; min-width: 0; }
.brand b { font-size: 15px; letter-spacing: -.01em; white-space: nowrap; }
.brand span { font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.chips { margin-left: auto; display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; min-width: 0; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px; cursor: default;
  font-size: 12px; color: var(--text-2); background: var(--panel-2); border: 1px solid var(--line); white-space: nowrap; }
.chip i { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
.chip.ok i { background: var(--ok); box-shadow: 0 0 0 3px var(--ok-soft); }
.chip.bad i { background: var(--bad); box-shadow: 0 0 0 3px var(--bad-soft); }
.chip.warn i { background: var(--warn); box-shadow: 0 0 0 3px var(--warn-soft); }
.chip.info i { background: var(--accent); }
.tools { display: flex; gap: 6px; flex: none; }
.icon-btn { position: relative; border: 1px solid var(--line); background: var(--panel); width: 36px; height: 36px;
  border-radius: 10px; display: grid; place-items: center; color: var(--text-2); text-decoration: none;
  transition: border-color .15s, color .15s, background .15s; }
.icon-btn:hover { border-color: var(--line-strong); color: var(--text); background: var(--panel-2); }
.icon-btn svg { width: 18px; height: 18px; }
.icon-btn .badge { position: absolute; top: -5px; right: -5px; min-width: 17px; height: 17px; padding: 0 4px; border-radius: 999px;
  font-size: 10.5px; font-weight: 700; line-height: 17px; text-align: center; background: var(--warn); color: #fff; }
.only-narrow { display: none !important; }
.body { display: grid; grid-template-columns: minmax(0, 1fr) 360px; min-height: 0; min-width: 0; }
.chat { display: grid; grid-template-rows: minmax(0, 1fr) auto; min-height: 0; min-width: 0; }
.feed { overflow-y: auto; overflow-x: hidden; padding: 24px clamp(14px, 4vw, 48px) 12px; scroll-behavior: smooth; }
.feed-inner { max-width: 820px; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; min-width: 0; }

/* --- приветствие --- */
.hello { text-align: center; padding: 36px 8px 8px; }
.hello .mark { width: 60px; height: 60px; border-radius: 19px; font-size: 24px; margin: 0 auto 16px; }
.eyebrow { font-size: 12.5px; font-weight: 600; letter-spacing: .02em; color: var(--accent); margin-bottom: 4px; }
.hello h1 { font-size: 26px; letter-spacing: -.02em; margin: 0 0 8px; }
.hello p { color: var(--muted); margin: 0 auto 22px; max-width: 540px; }
.examples { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; text-align: left; }
.example { display: flex; gap: 10px; align-items: flex-start; border: 1px solid var(--line); background: var(--panel);
  border-radius: 14px; padding: 12px 14px; box-shadow: var(--shadow); text-align: left;
  transition: transform .18s var(--ease), border-color .18s, box-shadow .18s; }
.example:hover { transform: translateY(-2px); border-color: var(--accent-line); box-shadow: var(--shadow-lg); }
.example .ico { width: 30px; height: 30px; border-radius: 9px; display: grid; place-items: center; flex: none;
  color: var(--accent); background: var(--accent-soft); }
.example .ico svg { width: 16px; height: 16px; }
.example b { display: block; font-size: 13px; }
.example span { font-size: 12px; color: var(--muted); }

/* --- лента --- */
.msg { display: flex; gap: 10px; animation: rise .28s var(--ease) both; min-width: 0; }
.msg.me { justify-content: flex-end; }
.avatar { flex: 0 0 30px; height: 30px; border-radius: 10px; display: grid; place-items: center;
  font-size: 12px; font-weight: 800; color: #fff; background: linear-gradient(135deg, var(--accent), var(--accent-2)); }
.bubble { max-width: min(660px, 88%); min-width: 0; padding: 11px 14px; border-radius: 16px; background: var(--panel);
  border: 1px solid var(--line); box-shadow: var(--shadow); overflow-wrap: anywhere; }
.bubble .text { white-space: pre-wrap; }
.msg.me .bubble { background: linear-gradient(135deg, var(--accent), var(--accent-2)); color: #fff; white-space: pre-wrap;
  border-color: transparent; border-bottom-right-radius: 6px; }
.msg.bot .bubble { border-bottom-left-radius: 6px; }
.bubble.error { border-color: color-mix(in srgb, var(--bad) 35%, var(--line)); background: color-mix(in srgb, var(--bad-soft) 60%, var(--panel)); }
.foot { margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.tag { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--panel-2); color: var(--muted);
  border: 1px solid var(--line); white-space: nowrap; }
.tag.src { color: var(--accent); background: var(--accent-soft); border-color: var(--accent-line); }
.tag.time { border-color: transparent; background: transparent; padding: 2px 2px; }
details.steps { flex-basis: 100%; font-size: 12px; color: var(--muted); }
details.steps summary { cursor: pointer; list-style: none; user-select: none; display: inline-flex; align-items: center; gap: 4px; }
details.steps summary::-webkit-details-marker { display: none; }
details.steps summary::before { content: ""; width: 0; height: 0; border: 4px solid transparent; border-left: 5px solid currentColor;
  transition: transform .15s; margin-right: 2px; }
details.steps[open] summary::before { transform: rotate(90deg) translateX(2px); }
.step { display: flex; gap: 8px; padding: 3px 0 3px 12px; border-left: 2px solid var(--line); margin: 2px 0 0 4px; }
.step b { color: var(--text-2); font-weight: 600; white-space: nowrap; }
.link-btn { display: inline-flex; align-items: center; gap: 6px; margin-top: 10px; padding: 6px 12px; border-radius: 10px;
  font-size: 12.5px; font-weight: 600; text-decoration: none; color: var(--accent); background: var(--accent-soft);
  border: 1px solid var(--accent-line); }
.link-btn:hover { background: color-mix(in srgb, var(--accent) 18%, transparent); }
.link-btn svg { width: 14px; height: 14px; }
.sugg { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.sugg button { border: 1px solid var(--accent-line); background: var(--accent-soft); color: var(--accent);
  border-radius: 999px; padding: 5px 12px; font-size: 12.5px; transition: background .15s; }
.sugg button:hover { background: color-mix(in srgb, var(--accent) 18%, transparent); }

/* --- карточка подтверждения --- */
.confirm { margin-top: 10px; border: 1px solid color-mix(in srgb, var(--warn) 40%, var(--line));
  background: var(--warn-soft); border-radius: 12px; padding: 10px 12px; }
.confirm-head { display: flex; align-items: center; gap: 8px; color: var(--warn); }
.confirm-head b { color: var(--text); }
.confirm-head svg { width: 16px; height: 16px; }
.confirm-head .ttl { margin-left: auto; font-size: 11.5px; color: var(--muted); font-variant-numeric: tabular-nums; }
.confirm-text { margin-top: 4px; font-size: 13px; color: var(--text-2); }
.confirm .row { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
.confirm.done { border-color: color-mix(in srgb, var(--ok) 40%, var(--line)); background: var(--ok-soft); }
.confirm.failed { border-color: color-mix(in srgb, var(--bad) 40%, var(--line)); background: var(--bad-soft); }
.confirm.cancelled, .confirm.expired { border-color: var(--line); background: var(--panel-2); }
.confirm-result { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; }
.confirm.done .confirm-result { color: var(--ok); } .confirm.failed .confirm-result { color: var(--bad); }
.confirm.cancelled .confirm-result, .confirm.expired .confirm-result { color: var(--muted); }
.confirm-result svg { width: 16px; height: 16px; }
.btn { border: 1px solid var(--line); background: var(--panel); border-radius: 10px; padding: 7px 14px;
  font-weight: 600; font-size: 13px; transition: transform .12s, box-shadow .15s, background .15s; }
.btn:hover { border-color: var(--line-strong); }
.btn:active { transform: translateY(1px); }
.btn.primary { color: #fff; border-color: transparent; background: linear-gradient(135deg, var(--accent), var(--accent-2));
  box-shadow: 0 8px 18px -10px var(--accent); }
.btn:disabled { opacity: .55; cursor: not-allowed; }
.typing { display: inline-flex; gap: 4px; padding: 6px 2px; }
.typing i { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); animation: blink 1.2s infinite ease-in-out; }
.typing i:nth-child(2) { animation-delay: .15s; } .typing i:nth-child(3) { animation-delay: .3s; }

/* --- поле ввода --- */
.composer { padding: 10px clamp(14px, 4vw, 48px) 16px; min-width: 0; }
.composer-inner { max-width: 820px; margin: 0 auto; display: flex; gap: 8px; align-items: flex-end;
  background: var(--panel); border: 1px solid var(--line-strong); border-radius: 18px; padding: 8px;
  box-shadow: var(--shadow-lg); transition: border-color .15s, box-shadow .15s; }
.composer-inner:focus-within { border-color: var(--accent-line); box-shadow: 0 0 0 4px var(--accent-soft), var(--shadow-lg); }
.composer textarea { flex: 1; min-width: 0; border: 0; outline: none; resize: none; background: transparent; padding: 8px 10px;
  max-height: 180px; min-height: 40px; line-height: 1.5; }
.send { width: 40px; height: 40px; border-radius: 12px; border: 0; color: #fff; display: grid; place-items: center; flex: none;
  background: linear-gradient(135deg, var(--accent), var(--accent-2)); box-shadow: 0 8px 18px -8px var(--accent);
  transition: transform .12s, opacity .15s; }
.send svg { width: 18px; height: 18px; }
.send:active { transform: scale(.96); }
.send:disabled { opacity: .5; }
.hint { max-width: 820px; margin: 6px auto 0; font-size: 11.5px; color: var(--muted); text-align: center; }

/* --- боковая панель --- */
.side { border-left: 1px solid var(--line); background: color-mix(in srgb, var(--panel) 70%, transparent);
  display: grid; grid-template-rows: auto minmax(0, 1fr); min-height: 0; min-width: 0; }
.tabs { display: flex; gap: 4px; padding: 10px; border-bottom: 1px solid var(--line); align-items: center; }
.tab { flex: 1; border: 0; background: transparent; padding: 7px 4px; border-radius: 9px; color: var(--muted);
  font-size: 12.5px; font-weight: 600; white-space: nowrap; }
.tab:hover { color: var(--text-2); }
.tab[aria-selected="true"] { background: var(--panel); color: var(--text); box-shadow: var(--shadow); }
.tab .count { display: inline-grid; place-items: center; min-width: 18px; height: 18px; padding: 0 5px;
  margin-left: 4px; border-radius: 999px; font-size: 11px; background: var(--warn); color: #fff; }
.pane { overflow-y: auto; padding: 14px; display: none; }
.pane.on { display: block; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 12px; margin-bottom: 10px;
  box-shadow: var(--shadow); }
.card h3 { margin: 0 0 8px; font-size: 13px; }
.small { font-size: 12px; color: var(--muted); }
.list { display: flex; flex-direction: column; gap: 6px; }
.item { display: flex; gap: 10px; align-items: flex-start; padding: 8px 10px; border-radius: 10px; background: var(--panel-2);
  border: 1px solid var(--line); }
.item .grow { flex: 1; min-width: 0; }
.item .t { font-size: 13px; overflow-wrap: anywhere; }
.dot { width: 8px; height: 8px; border-radius: 50%; margin-top: 6px; flex: none; background: var(--muted); }
.dot.ok { background: var(--ok); } .dot.bad { background: var(--bad); } .dot.warn { background: var(--warn); }
.x { border: 0; background: transparent; color: var(--muted); width: 28px; height: 28px; border-radius: 8px; display: grid; place-items: center; flex: none; }
.x svg { width: 14px; height: 14px; }
.x:hover { color: var(--bad); background: var(--bad-soft); }
.field { width: 100%; border: 1px solid var(--line); background: var(--panel); border-radius: 10px; padding: 8px 10px; }
.field:focus { outline: none; border-color: var(--accent-line); box-shadow: 0 0 0 3px var(--accent-soft); }
.row2 { display: flex; gap: 6px; }
.empty { color: var(--muted); font-size: 12.5px; text-align: center; padding: 18px 8px; }
.empty-state { display: flex; flex-direction: column; align-items: center; gap: 6px; text-align: center; padding: 28px 14px;
  color: var(--muted); font-size: 12.5px; }
.empty-state .ico { width: 44px; height: 44px; border-radius: 14px; display: grid; place-items: center; margin-bottom: 4px;
  color: var(--ok); background: var(--ok-soft); }
.empty-state .ico svg { width: 22px; height: 22px; }
.empty-state b { color: var(--text); font-size: 13.5px; }

/* --- навыки --- */
details.run-card summary { cursor: pointer; list-style: none; display: flex; flex-direction: column; gap: 2px; }
details.run-card summary::-webkit-details-marker { display: none; }
details.run-card summary b { font-size: 13px; display: flex; justify-content: space-between; align-items: center; }
details.run-card summary b::after { content: "+"; color: var(--muted); font-weight: 400; font-size: 16px; }
details.run-card[open] summary b::after { content: "−"; }
details.run-card[open] summary { margin-bottom: 10px; }
.params label { display: block; margin-top: 8px; font-size: 12px; color: var(--muted); }
.params label .field { margin-top: 3px; }
.result { margin-top: 10px; }
.result:empty { display: none; }
.result-head { display: flex; gap: 8px; align-items: flex-start; font-size: 13px; font-weight: 600; padding: 8px 10px; border-radius: 10px; }
.result-head svg { width: 16px; height: 16px; margin-top: 2px; }
.result-head.ok { color: var(--ok); background: var(--ok-soft); }
.result-head.bad { color: var(--bad); background: var(--bad-soft); }
.result-head.warn { color: var(--warn); background: var(--warn-soft); }
.result details { margin-top: 6px; font-size: 12px; color: var(--muted); }
.result summary { cursor: pointer; }
pre.out { white-space: pre-wrap; overflow-wrap: anywhere; font: 11.5px/1.5 var(--mono); background: var(--panel-2); color: var(--text-2);
  border: 1px solid var(--line); border-radius: 10px; padding: 8px; max-height: 260px; overflow: auto; margin: 6px 0 0; }
.group { margin-bottom: 12px; }
.group-title { font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--muted);
  margin: 4px 2px 6px; display: flex; justify-content: space-between; }
.skill { display: flex; flex-direction: column; gap: 2px; width: 100%; text-align: left; padding: 8px 10px; border-radius: 10px;
  border: 1px solid var(--line); background: var(--panel-2); margin-bottom: 6px; transition: border-color .15s, background .15s; }
.skill:hover { border-color: var(--accent-line); background: var(--panel); }
.skill.off { opacity: .62; }
.skill-top { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.skill .r { font-size: 11px; color: var(--bad); }
.risk { font-size: 10.5px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); white-space: nowrap; }
.risk.write, .risk.system, .risk.irreversible { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 40%, var(--line)); }
.risk.soft { color: var(--accent); border-color: var(--accent-line); }
.scrim { display: none; }

@keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@keyframes blink { 0%, 80%, 100% { opacity: .25; transform: scale(.85); } 40% { opacity: 1; transform: scale(1); } }
@media (max-width: 1100px) { .chip.extra { display: none; } }
@media (max-width: 900px) {
  .body { grid-template-columns: minmax(0, 1fr); }
  .only-narrow { display: grid !important; }
  .side { display: none; }
  .body.side-open .side { display: grid; position: fixed; top: 0; right: 0; bottom: 0; width: min(400px, 100%); z-index: 20;
    background: var(--panel); box-shadow: var(--shadow-lg); animation: slide .22s var(--ease) both; }
  .body.side-open .scrim { display: block; position: fixed; inset: 0; z-index: 19; background: rgba(8, 11, 20, .38); }
}
@media (max-width: 760px) { .chips { display: none; } .tools { margin-left: auto; } }
@media (max-width: 520px) { .brand span { display: none; } .top { gap: 10px; padding: 8px 10px; } .tools { gap: 4px; }
  .icon-btn { width: 34px; height: 34px; } .hello { padding-top: 20px; } .hello h1 { font-size: 22px; }
  .bubble { max-width: 100%; } .msg.bot .avatar { display: none; } .hint { display: none; } }
@keyframes slide { from { transform: translateX(24px); opacity: 0; } to { transform: none; opacity: 1; } }
@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; scroll-behavior: auto !important; } }
"""

# Иконки — встроенный SVG (без xmlns: внутри HTML он не нужен, а внешних адресов на странице нет).
_ICONS = {
    "new": '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/>',
    "theme": '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor"/>',
    "side": '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M15 4v16"/>',
    "send": '<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>',
    "close": '<path d="M18 6 6 18M6 6l12 12"/>',
    "panel": '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/>'
             '<rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    "external": '<path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
    "shield": '<path d="M12 3 5 6v6c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6z"/><path d="m9 12 2 2 4-4"/>',
    "alert": '<path d="M12 3 2 20h20z"/><path d="M12 10v4"/><path d="M12 17.5v.01"/>',
    "check": '<path d="m5 12 5 5 9-10"/>',
    "cross": '<path d="M18 6 6 18M6 6l12 12"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "printer": '<path d="M6 9V3h12v6"/><rect x="3" y="9" width="18" height="8" rx="2"/><path d="M7 14h10v7H7z"/>',
    "cpu": '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
    "wallet": '<rect x="3" y="6" width="18" height="13" rx="2"/><path d="M16 12.5h2"/><path d="M3 10h18"/>',
    "volume": '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16.5 8.5a5 5 0 0 1 0 7"/>',
    "windows": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18"/>',
    "memory": '<path d="M12 21s-7-4.5-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 11c0 5.5-7 10-7 10z"/>',
}


def icon(name: str) -> str:
    """SVG-иконка по имени: обводка цветом текста, размер задаёт CSS."""
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{_ICONS[name]}</svg>')


_JS = r"""
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };
  var ICON = JSON.parse($('icons').textContent);
  var SESSION = 'window';
  var state = { skills: [], titles: {}, pending: [], busy: false };

  function api(path, body) {
    var options = body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    };
    return fetch(path, options).then(function (response) {
      return response.json().catch(function () { return { ok: false, reason: 'Ответ не JSON (' + response.status + ')' }; });
    });
  }
  function two(n) { return String(n).padStart(2, '0'); }
  function hhmm(date) { return two(date.getHours()) + ':' + two(date.getMinutes()); }
  function fmtAt(at) {
    var text = String(at || '');
    if (text.length < 16) { return text; }
    var today = new Date();
    var day = today.getFullYear() + '-' + two(today.getMonth() + 1) + '-' + two(today.getDate());
    return text.slice(0, 10) === day ? text.slice(11, 16) : text.slice(8, 10) + '.' + text.slice(5, 7) + ' ' + text.slice(11, 16);
  }

  // --- тема ---------------------------------------------------------------
  function applyTheme(mode) {
    var dark = mode === 'dark' || (mode !== 'light' && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  }
  applyTheme(localStorage.getItem('nozza-agent-theme') || 'auto');
  $('theme').addEventListener('click', function () {
    var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    localStorage.setItem('nozza-agent-theme', next);
    applyTheme(next);
  });
  (function greet() {
    var hour = new Date().getHours();
    $('hello-time').textContent = hour < 5 ? 'Доброй ночи' : (hour < 12 ? 'Доброе утро' : (hour < 18 ? 'Добрый день' : 'Добрый вечер'));
  })();

  // --- лента --------------------------------------------------------------
  var feed = $('feed');
  function scrollDown() { var box = $('feed-scroll'); box.scrollTop = box.scrollHeight; }
  function hideHello() { var hello = $('hello'); if (hello) { hello.remove(); } }

  function addMine(text) {
    hideHello();
    var node = document.createElement('div');
    node.className = 'msg me';
    node.innerHTML = '<div class="bubble"></div>';
    node.firstChild.textContent = text;
    feed.appendChild(node);
    scrollDown();
  }

  function botNode() {
    var node = document.createElement('div');
    node.className = 'msg bot';
    node.innerHTML = '<div class="avatar" aria-hidden="true">N</div><div class="bubble"></div>';
    feed.appendChild(node);
    return node;
  }

  function addTyping() {
    var node = botNode();
    node.querySelector('.bubble').innerHTML = '<span class="typing" aria-label="Думаю"><i></i><i></i><i></i></span>';
    scrollDown();
    return node;
  }

  var SOURCES = { rules: 'понял без модели', model: 'модель', memory: 'память', clock: 'часы', math: 'арифметика',
    talk: 'разговор', registry: 'реестр навыков', panel: 'панель цеха' };

  function renderAnswer(node, data, at) {
    var bubble = node.querySelector('.bubble');
    bubble.className = 'bubble' + (data.kind === 'error' ? ' error' : '');
    bubble.innerHTML = '';
    var text = document.createElement('div');
    text.className = 'text';
    text.textContent = data.reply || data.reason || 'Пустой ответ';
    bubble.appendChild(text);
    var link = data.link || {};
    if (/^https?:\/\//.test(String(link.href || ''))) {
      var anchor = document.createElement('a');
      anchor.className = 'link-btn';
      anchor.href = link.href;
      anchor.target = '_blank';
      anchor.rel = 'noopener noreferrer';
      anchor.innerHTML = ICON.external;
      anchor.appendChild(document.createTextNode('Открыть «' + (link.title || 'Панель') + '» в панели'));
      bubble.appendChild(anchor);
    }
    if (data.pending && data.pending.id) {
      bubble.appendChild(confirmCard(data.pending));
    }
    if (data.suggestions && data.suggestions.length) {
      var box = document.createElement('div');
      box.className = 'sugg';
      data.suggestions.slice(0, 4).forEach(function (label) {
        var button = document.createElement('button');
        button.type = 'button';
        button.textContent = label;
        button.addEventListener('click', function () { send(label); });
        box.appendChild(button);
      });
      bubble.appendChild(box);
    }
    var tags = [];
    if (data.source && SOURCES[data.source]) { tags.push('<span class="tag src">' + esc(SOURCES[data.source]) + '</span>'); }
    if (data.skill) { tags.push('<span class="tag" title="' + esc(data.skill) + '">' + esc(state.titles[data.skill] || data.skill) + '</span>'); }
    if (data.ms) { tags.push('<span class="tag">' + esc(data.ms) + ' мс</span>'); }
    tags.push('<span class="tag time">' + esc(at || hhmm(new Date())) + '</span>');
    var foot = document.createElement('div');
    foot.className = 'foot';
    foot.innerHTML = tags.join('');
    if (data.steps && data.steps.length) {
      var steps = document.createElement('details');
      steps.className = 'steps';
      steps.innerHTML = '<summary>Как я понял</summary>' + data.steps.map(function (step) {
        return '<div class="step"><b>' + esc(step.title) + '</b><span>' + esc(step.detail || '') + '</span></div>';
      }).join('');
      foot.appendChild(steps);
    }
    bubble.appendChild(foot);
    scrollDown();
  }

  // --- подтверждения ------------------------------------------------------
  function safeId(id) { return String(id || '').replace(/[^a-zA-Z0-9_-]/g, ''); }

  function confirmCard(pending) {
    var card = document.createElement('div');
    card.className = 'confirm';
    card.setAttribute('data-id', safeId(pending.id));
    card.innerHTML = '<div class="confirm-head">' + ICON.alert + '<b>Нужно ваше решение</b><span class="ttl"></span></div>'
      + '<div class="confirm-text"></div>'
      + '<div class="row"><button class="btn primary" type="button" data-yes>Подтвердить</button>'
      + '<button class="btn" type="button" data-no>Отменить</button></div>';
    card.querySelector('.confirm-text').textContent = pending.text || '';
    var deadline = pending.expires_at ? pending.expires_at * 1000 : (pending.ttl ? Date.now() + pending.ttl * 1000 : 0);
    if (deadline) { card.setAttribute('data-deadline', String(Math.round(deadline))); }
    card.querySelector('[data-yes]').addEventListener('click', function () { decide(pending.id, true, card); });
    card.querySelector('[data-no]').addEventListener('click', function () { decide(pending.id, false, card); });
    tick(card);
    return card;
  }

  function tick(card) {
    var deadline = Number(card.getAttribute('data-deadline') || 0);
    var label = card.querySelector('.ttl');
    if (!deadline || !label) { return; }
    var left = Math.round((deadline - Date.now()) / 1000);
    if (left > 0) { label.textContent = 'ждёт ещё ' + left + ' с'; return; }
    finish(card, 'expired', 'Время вышло — попросите ещё раз, если это ещё нужно.');
  }
  setInterval(function () { document.querySelectorAll('.confirm[data-deadline]').forEach(tick); }, 1000);

  function finish(card, tone, text) {
    card.className = 'confirm ' + tone;
    card.removeAttribute('data-deadline');
    card.innerHTML = '<div class="confirm-result">' + (tone === 'done' ? ICON.check : (tone === 'failed' ? ICON.cross : ICON.clock)) + '</div>';
    card.firstChild.appendChild(document.createTextNode(text));
  }

  function decide(id, confirmed, card) {
    if (card) { card.querySelectorAll('button').forEach(function (b) { b.disabled = true; }); }
    return api('/action/confirm', { id: id, confirmed: confirmed }).then(function (data) {
      var tone = data.done ? 'done' : (confirmed ? 'failed' : 'cancelled');
      var text = data.done ? 'Выполнено' : (confirmed ? (data.reason || 'Не выполнено') : 'Отменено');
      document.querySelectorAll('.confirm[data-id="' + safeId(id) + '"]').forEach(function (other) { finish(other, tone, text); });
      loadPending(); loadJournal();
    });
  }

  var EMPTY_PENDING = '<div class="empty-state"><div class="ico">' + ICON.shield + '</div><b>Ничего не ждёт</b>'
    + '<span>Здесь появятся действия, которым нужно ваше «Подтвердить»: ввод в чужие окна, питание компьютера, команды станкам.</span></div>';

  function loadPending() {
    return api('/pending').then(function (data) {
      state.pending = data.pending || [];
      var alive = {};
      state.pending.forEach(function (item) { alive[safeId(item.id)] = true; });
      var count = state.pending.length;
      $('pending-count').textContent = count;
      $('pending-count').style.display = count ? '' : 'none';
      $('side-badge').textContent = count;
      $('side-badge').style.display = count ? '' : 'none';
      var host = $('pending');
      host.innerHTML = '';
      if (!count) { host.innerHTML = EMPTY_PENDING; }
      state.pending.forEach(function (item) { host.appendChild(confirmCard(item)); });
      // Карточка в ленте, которую уже решили в другом месте (вкладка, другое окно) или время вышло.
      feed.querySelectorAll('.confirm[data-id]').forEach(function (card) {
        if (card.querySelector('[data-yes]:not(:disabled)') && !alive[card.getAttribute('data-id')]) {
          finish(card, 'cancelled', 'Уже не ждёт подтверждения.');
        }
      });
    }).catch(function () {});
  }

  // --- отправка -----------------------------------------------------------
  function send(text) {
    text = String(text || '').trim();
    if (!text || state.busy) { return; }
    state.busy = true;
    $('send').disabled = true;
    addMine(text);
    var typing = addTyping();
    api('/chat', { text: text, session: SESSION }).then(function (data) {
      renderAnswer(typing, data);
      if (data.pending) { loadPending(); }
      if (data.kind === 'memory') { loadMemory(); }
      if (data.skill) { loadJournal(); }
    }).catch(function (error) {
      renderAnswer(typing, { kind: 'error', reply: 'Агент не ответил: ' + (error && error.message || error) });
    }).then(function () {
      state.busy = false;
      $('send').disabled = false;
      input.focus();
    });
  }

  var input = $('input');
  function autosize() { input.style.height = 'auto'; input.style.height = Math.min(180, input.scrollHeight) + 'px'; }
  input.addEventListener('input', autosize);
  input.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      var text = input.value;
      input.value = '';
      autosize();
      send(text);
    }
  });
  $('send').addEventListener('click', function () { var text = input.value; input.value = ''; autosize(); send(text); });
  document.querySelectorAll('[data-say]').forEach(function (button) {
    button.addEventListener('click', function () { send(button.getAttribute('data-say')); });
  });

  // --- вкладки и боковая панель --------------------------------------------
  function openTab(id) {
    document.querySelectorAll('.tab').forEach(function (tab) {
      tab.setAttribute('aria-selected', tab.getAttribute('data-pane') === id ? 'true' : 'false');
    });
    document.querySelectorAll('.pane').forEach(function (pane) { pane.classList.toggle('on', pane.id === id); });
  }
  document.querySelectorAll('.tab').forEach(function (tab) {
    tab.addEventListener('click', function () { openTab(tab.getAttribute('data-pane')); });
  });
  function side(open) { $('body').classList.toggle('side-open', open); }
  $('side-toggle').addEventListener('click', function () { side(!$('body').classList.contains('side-open')); });
  $('side-close').addEventListener('click', function () { side(false); });
  $('scrim').addEventListener('click', function () { side(false); });
  document.addEventListener('keydown', function (event) { if (event.key === 'Escape') { side(false); } });

  // --- память -------------------------------------------------------------
  var KINDS = { fact: 'факт', profile: 'о вас', preference: 'предпочтение', note: 'заметка', task: 'задача', rule: 'правило' };
  function loadMemory() {
    return api('/memory').then(function (data) {
      var host = $('memory');
      var rows = data.memories || [];
      $('memory-count').textContent = rows.length ? String(rows.length) : '';
      if (!rows.length) { host.innerHTML = '<div class="empty">Память пуста. Скажите «запомни, что…» или добавьте запись выше.</div>'; return; }
      host.innerHTML = '';
      rows.forEach(function (row) {
        var item = document.createElement('div');
        item.className = 'item';
        item.innerHTML = '<div class="grow"><div class="t"></div><div class="small"></div></div>'
          + '<button class="x" type="button" title="Забыть" aria-label="Забыть">' + ICON.cross + '</button>';
        item.querySelector('.t').textContent = row.text;
        item.querySelector('.small').textContent = (KINDS[row.kind] || row.kind || 'факт') + ' · ' + fmtAt(row.updated_at || row.at || '');
        item.querySelector('.x').addEventListener('click', function () {
          api('/memory', { op: 'forget', id: row.id }).then(loadMemory);
        });
        host.appendChild(item);
      });
    }).catch(function () {});
  }
  function addMemory() {
    var field = $('memory-text');
    var text = field.value.trim();
    if (!text) { field.focus(); return; }
    api('/memory', { op: 'remember', text: text }).then(function () { field.value = ''; loadMemory(); });
  }
  $('memory-add').addEventListener('click', addMemory);
  $('memory-text').addEventListener('keydown', function (event) { if (event.key === 'Enter') { event.preventDefault(); addMemory(); } });

  // --- навыки -------------------------------------------------------------
  var RISK = { read: 'чтение', own: 'своя база', soft: 'мягкое', write: 'подтверждение', system: 'подтверждение', irreversible: 'необратимо' };
  var GROUPS = { system: 'Компьютер', window: 'Окна', app: 'Программы', files: 'Файлы', screen: 'Экран', clipboard: 'Буфер обмена',
    voice: 'Голос', memory: 'Память', panel: 'Панель цеха', day: 'День', scheduler: 'Таймеры', knowledge: 'Знания цеха',
    avito: 'Авито', tg: 'Telegram', assistant: 'Макросы', safety: 'Безопасность', agent: 'Сам помощник' };
  var ORDER = Object.keys(GROUPS);
  function rank(key) { var at = ORDER.indexOf(key); return at < 0 ? ORDER.length : at; }
  function skillHtml(item) {
    return '<button type="button" class="skill' + (item.available ? '' : ' off') + '" data-name="' + esc(item.name) + '">'
      + '<span class="skill-top"><b>' + esc(item.title) + '</b><span class="risk ' + esc(item.risk) + '">'
      + esc(RISK[item.risk] || item.risk) + '</span></span>'
      + '<span class="small">' + esc(item.description) + '</span>'
      + (item.available ? '' : '<span class="r">' + esc(item.reason || 'недоступен') + '</span>')
      + '</button>';
  }
  function renderSkills() {
    var query = ($('skill-filter').value || '').toLowerCase();
    var groups = [];
    var byGroup = {};
    state.skills.forEach(function (item) {
      if (query && (item.name + ' ' + item.title + ' ' + item.description).toLowerCase().indexOf(query) < 0) { return; }
      var key = String(item.name).split('.')[0];
      if (!byGroup[key]) { byGroup[key] = []; groups.push(key); }
      byGroup[key].push(item);
    });
    groups.sort(function (a, b) { return rank(a) - rank(b); });
    $('skills').innerHTML = groups.map(function (key) {
      return '<div class="group"><div class="group-title"><span>' + esc(GROUPS[key] || key) + '</span><span>'
        + esc(byGroup[key].length) + '</span></div>' + byGroup[key].map(skillHtml).join('') + '</div>';
    }).join('') || '<div class="empty">Ничего не нашлось</div>';
  }
  function fillSelect() {
    var select = $('skill');
    var current = select.value;
    select.innerHTML = state.skills.map(function (item) {
      return '<option value="' + esc(item.name) + '">' + esc(item.title) + (item.available ? '' : ' — недоступен') + '</option>';
    }).join('');
    if (current) { select.value = current; }
    pickSkill();
  }
  function loadSkills() {
    return api('/skills').then(function (data) {
      state.skills = data.skills || [];
      state.titles = {};
      state.skills.forEach(function (item) { state.titles[item.name] = item.title; });
      $('skills-count').textContent = (data.ready || 0) + ' из ' + state.skills.length;
      renderSkills();
      fillSelect();
    }).catch(function () {});
  }
  $('skill-filter').addEventListener('input', renderSkills);
  $('skills').addEventListener('click', function (event) {
    var button = event.target.closest('[data-name]');
    if (!button) { return; }
    $('skill').value = button.getAttribute('data-name');
    pickSkill();
    $('run-card').open = true;
    $('pane-skills').scrollTop = 0;
    var first = $('params').querySelector('input');
    (first || $('run')).focus();
  });
  function pickSkill() {
    var name = $('skill').value;
    var skill = state.skills.find(function (item) { return item.name === name; }) || {};
    var keys = Object.keys(skill.params || {});
    var box = $('params');
    box.innerHTML = '';
    $('skill-about').textContent = skill.description || '';
    keys.forEach(function (key) {
      var label = document.createElement('label');
      label.textContent = key + ' · ' + skill.params[key];
      var field = document.createElement('input');
      field.className = 'field';
      field.name = key;
      field.autocomplete = 'off';
      label.appendChild(field);
      box.appendChild(label);
    });
    if (!keys.length) { box.innerHTML = '<div class="small" style="margin-top:8px">Параметров нет</div>'; }
    $('run').disabled = !skill.available;
    $('run').title = skill.available ? '' : (skill.reason || 'Навык недоступен');
  }
  $('skill').addEventListener('change', pickSkill);
  function showResult(data) {
    var host = $('result');
    host.innerHTML = '';
    var head = document.createElement('div');
    var tone = data.queued ? 'warn' : (data.ok ? 'ok' : 'bad');
    head.className = 'result-head ' + tone;
    head.innerHTML = tone === 'ok' ? ICON.check : (tone === 'bad' ? ICON.cross : ICON.alert);
    head.appendChild(document.createTextNode(data.queued ? 'Ждёт вашего «Подтвердить» — вкладка «Ждёт».'
      : (data.summary || (data.ok ? 'Готово.' : 'Не получилось: ' + (data.reason || 'без причины')))));
    host.appendChild(head);
    var details = document.createElement('details');
    details.innerHTML = '<summary>Ответ навыка целиком</summary><pre class="out"></pre>';
    details.querySelector('pre').textContent = JSON.stringify(data, null, 2);
    host.appendChild(details);
  }
  $('run').addEventListener('click', function () {
    var params = {};
    $('params').querySelectorAll('input').forEach(function (field) { if (field.value.trim()) { params[field.name] = field.value.trim(); } });
    $('run').disabled = true;
    api('/skill', { name: $('skill').value, params: params, where: 'window' }).then(function (data) {
      showResult(data);
      if (data.queued) { loadPending(); openTab('pane-pending'); }
      loadJournal();
    }).then(function () { $('run').disabled = false; });
  });

  // --- журнал -------------------------------------------------------------
  var OUTCOMES = { done: ['ok', 'выполнено'], failed: ['bad', 'не вышло'], refused: ['bad', 'отказано'],
    unavailable: ['warn', 'недоступно'], needs_confirmation: ['warn', 'ждёт подтверждения'], queued: ['warn', 'ждёт подтверждения'] };
  function loadJournal() {
    return api('/journal?limit=30').then(function (data) {
      var rows = data.entries || [];
      $('journal').innerHTML = rows.map(function (row) {
        var outcome = OUTCOMES[row.outcome] || ['', row.outcome || ''];
        return '<div class="item"><i class="dot ' + esc(outcome[0]) + '"></i><div class="grow"><div class="t"><b>'
          + esc(state.titles[row.skill] || row.skill) + '</b> <span class="small">' + esc(outcome[1]) + '</span></div>'
          + '<div class="small">' + esc(fmtAt(row.at)) + (row.detail ? ' · ' + esc(row.detail) : '') + '</div></div></div>';
      }).join('') || '<div class="empty">Журнал пуст: здесь будет всё, что помощник делал на компьютере.</div>';
    }).catch(function () {});
  }

  // --- состояние ----------------------------------------------------------
  function chip(id, tone, text, tip) {
    var node = $(id);
    node.className = 'chip ' + tone + (node.classList.contains('extra') ? ' extra' : '');
    node.lastChild.textContent = text;
    node.title = tip || '';
  }
  function loadStatus() {
    return api('/capabilities').then(function (data) {
      var caps = data.capabilities || {};
      chip('c-panel', caps.panel ? 'ok' : 'bad', caps.panel ? 'панель цеха' : 'панель не отвечает',
        caps.panel ? 'Вопросы про станки, заказы и деньги отвечает панель цеха' : (caps.panel_reason || 'Панель цеха недоступна'));
      chip('c-model', caps.model ? 'ok' : 'warn', caps.model ? 'модель' : 'без модели',
        caps.model ? 'Свободные вопросы понимает модель' : 'Без модели работают правила: команды, вопросы цеха, память и счёт. ' + (caps.model_reason || ''));
      chip('c-windows', caps.windows ? 'ok' : 'warn', caps.windows ? 'Windows' : 'без Windows', caps.windows_reason || '');
      chip('c-voice', (caps.speech_in || caps.speech_out) ? 'ok' : 'warn',
        caps.speech_in ? 'голос' : (caps.speech_out ? 'озвучка' : 'без голоса'), caps.speech_reason || '');
      $('live').className = 'live ' + (caps.panel ? 'ok' : 'bad');
      $('live').title = caps.panel ? 'Панель цеха на связи' : 'Панель цеха не отвечает';
      var url = String(data.panel_url || '');
      if (/^https?:\/\//.test(url)) { $('open-panel').href = url + '/'; $('open-panel').style.display = ''; }
    }).catch(function () {});
  }
  function loadHistory() {
    return api('/chat/history?session=' + SESSION + '&limit=30').then(function (data) {
      var turns = data.turns || [];
      if (!turns.length) { return; }
      hideHello();
      turns.forEach(function (turn) {
        if (turn.role === 'user') { addMine(turn.text); return; }
        var meta = turn.meta || {};
        renderAnswer(botNode(), { reply: turn.text, kind: meta.kind, skill: meta.skill, source: meta.source, link: meta.link },
          fmtAt(turn.at));
      });
    }).catch(function () {});
  }
  $('clear').addEventListener('click', function () {
    api('/chat/clear', { session: SESSION }).then(function () { window.location.reload(); });
  });

  loadSkills().then(function () { loadHistory(); loadJournal(); });
  loadPending(); loadMemory(); loadStatus();
  setInterval(loadPending, 3000);
  setInterval(loadStatus, 30000);
  input.focus();
})();
"""

_EXAMPLES = (
    ("Что сейчас печатается?", "станки и очередь — из панели цеха", "printer"),
    ("Как там компьютер?", "процессор, память, диски", "cpu"),
    ("Кто нам должен?", "долги клиентов — из панели цеха", "wallet"),
    ("Громкость 30", "звук, пауза, треки", "volume"),
    ("Какие окна открыты?", "переключиться, свернуть", "windows"),
    ("Запомни, что я работаю до 19:00", "память помощника", "memory"),
)


def page() -> str:
    """Страница окна помощника. Данные подставляет JavaScript, всегда через экранирование."""
    import json

    examples = "".join(
        f'<button class="example" type="button" data-say="{say}"><span class="ico">{icon(ico)}</span>'
        f'<span><b>{say}</b><span>{hint}</span></span></button>'
        for say, hint, ico in _EXAMPLES)
    icons = json.dumps({name: icon(name) for name in ("external", "alert", "check", "cross", "clock", "shield")},
                       ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{TITLE}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="app">
  <header class="top">
    <div class="mark">N<i class="live" id="live"></i></div>
    <div class="brand"><b>{TITLE}</b><span>компьютер цеха · работает только на этом ПК</span></div>
    <div class="chips" aria-label="Состояние">
      <span class="chip" id="c-panel"><i></i><span>панель…</span></span>
      <span class="chip" id="c-model"><i></i><span>модель…</span></span>
      <span class="chip extra" id="c-windows"><i></i><span>…</span></span>
      <span class="chip extra" id="c-voice"><i></i><span>…</span></span>
      <span class="chip info extra"><i></i><span>навыков <b id="skills-count">…</b></span></span>
    </div>
    <div class="tools">
      <a class="icon-btn" id="open-panel" href="#" target="_blank" rel="noopener noreferrer" title="Открыть панель цеха в браузере" aria-label="Открыть панель цеха" style="display:none">{icon("panel")}</a>
      <button class="icon-btn" id="clear" type="button" title="Начать разговор заново (память останется)" aria-label="Начать заново">{icon("new")}</button>
      <button class="icon-btn" id="theme" type="button" title="Светлая или тёмная тема" aria-label="Тема">{icon("theme")}</button>
      <button class="icon-btn only-narrow" id="side-toggle" type="button" title="Подтверждения, память, навыки, журнал" aria-label="Боковая панель">{icon("side")}<span class="badge" id="side-badge" style="display:none">0</span></button>
    </div>
  </header>
  <div class="body" id="body">
    <main class="chat">
      <div class="feed" id="feed-scroll">
        <div class="feed-inner" id="feed" aria-live="polite">
          <section class="hello" id="hello">
            <div class="mark">N</div>
            <div class="eyebrow" id="hello-time">Здравствуйте</div>
            <h1>Чем помочь?</h1>
            <p>Говорите обычными словами. Компьютером помощник управляет сам, про станки, заказы и деньги спрашивает
               панель цеха, помнит разговор — и ничего не меняет без вашего «Подтвердить».</p>
            <div class="examples">{examples}</div>
          </section>
        </div>
      </div>
      <div class="composer">
        <div class="composer-inner">
          <textarea id="input" rows="1" placeholder="Напишите, что сделать…" aria-label="Сообщение помощнику"></textarea>
          <button class="send" id="send" type="button" aria-label="Отправить" title="Отправить (Enter)">{icon("send")}</button>
        </div>
        <div class="hint">Enter — отправить, Shift+Enter — новая строка. Всё, что меняет компьютер или цех, — только после «Подтвердить».</div>
      </div>
    </main>
    <div class="scrim" id="scrim"></div>
    <aside class="side" aria-label="Подтверждения, память, навыки, журнал">
      <nav class="tabs" role="tablist">
        <button class="tab" type="button" role="tab" aria-selected="true" data-pane="pane-pending">Ждёт<span class="count" id="pending-count" style="display:none">0</span></button>
        <button class="tab" type="button" role="tab" aria-selected="false" data-pane="pane-memory">Память <span class="small" id="memory-count"></span></button>
        <button class="tab" type="button" role="tab" aria-selected="false" data-pane="pane-skills">Навыки</button>
        <button class="tab" type="button" role="tab" aria-selected="false" data-pane="pane-journal">Журнал</button>
        <button class="x only-narrow" id="side-close" type="button" title="Закрыть" aria-label="Закрыть панель">{icon("close")}</button>
      </nav>
      <section class="pane on" id="pane-pending" role="tabpanel"><div id="pending"></div></section>
      <section class="pane" id="pane-memory" role="tabpanel">
        <div class="card"><h3>Добавить в память</h3>
          <div class="row2"><input class="field" id="memory-text" placeholder="Например: Мария берёт только PETG" autocomplete="off">
          <button class="btn primary" id="memory-add" type="button" aria-label="Запомнить">+</button></div></div>
        <div class="list" id="memory"></div>
      </section>
      <section class="pane" id="pane-skills" role="tabpanel">
        <details class="card run-card" id="run-card">
          <summary><b>Запустить навык вручную</b><span class="small">Без разговора, с теми же проверками и подтверждением</span></summary>
          <select class="field" id="skill" aria-label="Навык"></select>
          <div class="small" id="skill-about" style="margin-top:6px"></div>
          <div class="params" id="params"></div>
          <button class="btn primary" id="run" type="button" style="margin-top:10px">Выполнить</button>
          <div class="result" id="result"></div>
        </details>
        <input class="field" id="skill-filter" placeholder="Поиск навыка…" autocomplete="off" style="margin-bottom:12px" aria-label="Поиск навыка">
        <div id="skills"></div>
      </section>
      <section class="pane" id="pane-journal" role="tabpanel"><div class="list" id="journal"></div></section>
    </aside>
  </div>
</div>
<script type="application/json" id="icons">{icons}</script>
<script>{_JS}</script>
</body>
</html>
"""


def payload(agent: Any) -> dict[str, Any]:
    """Состояние для заголовка окна: что живо, чего не хватает."""
    runner = getattr(agent, "runner", None)
    caps = dict(getattr(agent, "capabilities", {}) or {})
    if runner is not None:
        caps.update(runner.caps)
    rows = runner.catalog() if runner is not None else []
    return {"ok": True, "title": TITLE, "skills": rows,
            "count": len(rows), "ready": sum(1 for row in rows if row["available"]),
            "capabilities": caps,
            "missing": [str(value) for key, value in caps.items()
                        if key.endswith("_reason") and value]}
