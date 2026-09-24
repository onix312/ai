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
  --accent-line: rgba(128, 131, 255, .34); --ok-soft: rgba(22, 163, 74, .18);
  --warn-soft: rgba(217, 119, 6, .18); --bad-soft: rgba(220, 38, 38, .18);
  --shadow: 0 1px 2px rgba(0, 0, 0, .4), 0 12px 32px -16px rgba(0, 0, 0, .7);
  --shadow-lg: 0 24px 60px -24px rgba(0, 0, 0, .8);
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin: 0; font: 14px/1.55 var(--font); color: var(--text); background:
  radial-gradient(1200px 600px at 100% -10%, var(--accent-soft), transparent 60%),
  radial-gradient(900px 500px at -10% 110%, rgba(139, 92, 246, .08), transparent 60%), var(--bg);
  -webkit-font-smoothing: antialiased; }
button, input, select, textarea { font: inherit; color: inherit; }
button { cursor: pointer; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 8px; }
.app { display: grid; grid-template-rows: auto 1fr; height: 100vh; }
.top { display: flex; align-items: center; gap: 14px; padding: 12px 20px;
  background: color-mix(in srgb, var(--panel) 82%, transparent); backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 5; }
.mark { width: 34px; height: 34px; border-radius: 11px; display: grid; place-items: center;
  color: #fff; font-weight: 800; letter-spacing: -.02em;
  background: linear-gradient(135deg, var(--accent), var(--accent-2)); box-shadow: 0 6px 18px -6px var(--accent); }
.brand { display: flex; flex-direction: column; line-height: 1.15; }
.brand b { font-size: 15px; letter-spacing: -.01em; }
.brand span { font-size: 12px; color: var(--muted); }
.chips { margin-left: auto; display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px;
  font-size: 12px; color: var(--text-2); background: var(--panel-2); border: 1px solid var(--line); white-space: nowrap; }
.chip i { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
.chip.ok i { background: var(--ok); box-shadow: 0 0 0 3px var(--ok-soft); }
.chip.bad i { background: var(--bad); box-shadow: 0 0 0 3px var(--bad-soft); }
.chip.warn i { background: var(--warn); box-shadow: 0 0 0 3px var(--warn-soft); }
.icon-btn { border: 1px solid var(--line); background: var(--panel); width: 34px; height: 34px;
  border-radius: 10px; display: grid; place-items: center; color: var(--text-2); }
.icon-btn:hover { border-color: var(--line-strong); color: var(--text); }
.body { display: grid; grid-template-columns: minmax(0, 1fr) 360px; min-height: 0; }
.chat { display: grid; grid-template-rows: 1fr auto; min-height: 0; }
.feed { overflow-y: auto; padding: 24px clamp(16px, 4vw, 48px) 12px; scroll-behavior: smooth; }
.feed-inner { max-width: 820px; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; }
.hello { text-align: center; padding: 40px 12px 8px; }
.hello .mark { width: 56px; height: 56px; border-radius: 18px; font-size: 22px; margin: 0 auto 14px; }
.hello h1 { font-size: 24px; letter-spacing: -.02em; margin: 0 0 6px; }
.hello p { color: var(--muted); margin: 0 auto 20px; max-width: 520px; }
.examples { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; text-align: left; }
.example { border: 1px solid var(--line); background: var(--panel); border-radius: 14px; padding: 12px 14px;
  box-shadow: var(--shadow); transition: transform .18s var(--ease), border-color .18s; text-align: left; }
.example:hover { transform: translateY(-2px); border-color: var(--accent-line); }
.example b { display: block; font-size: 13px; }
.example span { font-size: 12px; color: var(--muted); }
.msg { display: flex; gap: 10px; animation: rise .28s var(--ease) both; }
.msg.me { justify-content: flex-end; }
.avatar { flex: 0 0 30px; height: 30px; border-radius: 10px; display: grid; place-items: center;
  font-size: 12px; font-weight: 800; color: #fff; background: linear-gradient(135deg, var(--accent), var(--accent-2)); }
.bubble { max-width: min(640px, 86%); padding: 11px 14px; border-radius: 16px; background: var(--panel);
  border: 1px solid var(--line); box-shadow: var(--shadow); white-space: pre-wrap; word-break: break-word; }
.msg.me .bubble { background: linear-gradient(135deg, var(--accent), var(--accent-2)); color: #fff;
  border-color: transparent; border-bottom-right-radius: 6px; }
.msg.bot .bubble { border-bottom-left-radius: 6px; }
.bubble.error { border-color: color-mix(in srgb, var(--bad) 35%, var(--line)); background: color-mix(in srgb, var(--bad-soft) 60%, var(--panel)); }
.meta { margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.tag { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--panel-2); color: var(--muted);
  border: 1px solid var(--line); }
.tag.src { color: var(--accent); background: var(--accent-soft); border-color: var(--accent-line); }
details.steps { margin-top: 8px; font-size: 12px; color: var(--muted); }
details.steps summary { cursor: pointer; list-style: none; user-select: none; }
details.steps summary::-webkit-details-marker { display: none; }
details.steps summary::before { content: "▸ "; }
details.steps[open] summary::before { content: "▾ "; }
.step { display: flex; gap: 8px; padding: 3px 0 3px 12px; border-left: 2px solid var(--line); margin-left: 4px; }
.step b { color: var(--text-2); font-weight: 600; }
.sugg { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.sugg button { border: 1px solid var(--accent-line); background: var(--accent-soft); color: var(--accent);
  border-radius: 999px; padding: 5px 12px; font-size: 12.5px; transition: background .15s; }
.sugg button:hover { background: color-mix(in srgb, var(--accent) 18%, transparent); }
.confirm { margin-top: 10px; border: 1px solid color-mix(in srgb, var(--warn) 40%, var(--line));
  background: var(--warn-soft); border-radius: 12px; padding: 10px 12px; }
.confirm .row { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
.btn { border: 1px solid var(--line); background: var(--panel); border-radius: 10px; padding: 7px 14px;
  font-weight: 600; font-size: 13px; transition: transform .12s, box-shadow .15s, background .15s; }
.btn:hover { border-color: var(--line-strong); }
.btn:active { transform: translateY(1px); }
.btn.primary { color: #fff; border-color: transparent; background: linear-gradient(135deg, var(--accent), var(--accent-2));
  box-shadow: 0 8px 18px -10px var(--accent); }
.btn.danger { color: var(--bad); }
.btn:disabled { opacity: .55; cursor: not-allowed; }
.typing { display: inline-flex; gap: 4px; padding: 6px 2px; }
.typing i { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); animation: blink 1.2s infinite ease-in-out; }
.typing i:nth-child(2) { animation-delay: .15s; } .typing i:nth-child(3) { animation-delay: .3s; }
.composer { padding: 12px clamp(16px, 4vw, 48px) 18px; }
.composer-inner { max-width: 820px; margin: 0 auto; display: flex; gap: 8px; align-items: flex-end;
  background: var(--panel); border: 1px solid var(--line-strong); border-radius: 18px; padding: 8px;
  box-shadow: var(--shadow-lg); transition: border-color .15s, box-shadow .15s; }
.composer-inner:focus-within { border-color: var(--accent-line); box-shadow: 0 0 0 4px var(--accent-soft), var(--shadow-lg); }
.composer textarea { flex: 1; border: 0; outline: none; resize: none; background: transparent; padding: 8px 10px;
  max-height: 180px; min-height: 40px; line-height: 1.5; }
.send { width: 40px; height: 40px; border-radius: 12px; border: 0; color: #fff; display: grid; place-items: center;
  background: linear-gradient(135deg, var(--accent), var(--accent-2)); box-shadow: 0 8px 18px -8px var(--accent); }
.send:disabled { opacity: .5; }
.hint { max-width: 820px; margin: 6px auto 0; font-size: 11.5px; color: var(--muted); text-align: center; }
.side { border-left: 1px solid var(--line); background: color-mix(in srgb, var(--panel) 70%, transparent);
  display: grid; grid-template-rows: auto 1fr; min-height: 0; }
.tabs { display: flex; gap: 4px; padding: 10px; border-bottom: 1px solid var(--line); }
.tab { flex: 1; border: 0; background: transparent; padding: 7px 6px; border-radius: 9px; color: var(--muted);
  font-size: 12.5px; font-weight: 600; position: relative; }
.tab[aria-selected="true"] { background: var(--panel); color: var(--text); box-shadow: var(--shadow); }
.tab .count { display: inline-grid; place-items: center; min-width: 18px; height: 18px; padding: 0 5px;
  margin-left: 4px; border-radius: 999px; font-size: 11px; background: var(--warn); color: #fff; }
.pane { overflow-y: auto; padding: 14px; display: none; }
.pane.on { display: block; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 12px; margin-bottom: 10px;
  box-shadow: var(--shadow); }
.card h3 { margin: 0 0 4px; font-size: 13px; }
.small { font-size: 12px; color: var(--muted); }
.list { display: flex; flex-direction: column; gap: 6px; }
.item { display: flex; gap: 8px; align-items: flex-start; padding: 8px 10px; border-radius: 10px; background: var(--panel-2);
  border: 1px solid var(--line); }
.item .grow { flex: 1; min-width: 0; }
.item .t { font-size: 13px; word-break: break-word; }
.x { border: 0; background: transparent; color: var(--muted); width: 26px; height: 26px; border-radius: 8px; }
.x:hover { color: var(--bad); background: var(--bad-soft); }
.field { width: 100%; border: 1px solid var(--line); background: var(--panel); border-radius: 10px; padding: 8px 10px; }
.field:focus { outline: none; border-color: var(--accent-line); box-shadow: 0 0 0 3px var(--accent-soft); }
.row2 { display: flex; gap: 6px; }
.skill { padding: 8px 10px; border-radius: 10px; border: 1px solid var(--line); background: var(--panel-2); margin-bottom: 6px; }
.skill.off { opacity: .6; }
.skill .n { font-family: var(--mono); font-size: 11px; color: var(--muted); }
.skill .r { font-size: 11px; }
.risk { font-size: 10.5px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); }
.risk.write, .risk.system, .risk.irreversible { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 40%, var(--line)); }
.risk.soft { color: var(--accent); border-color: var(--accent-line); }
pre.out { white-space: pre-wrap; word-break: break-word; font: 11.5px/1.5 var(--mono); background: var(--panel-2);
  border: 1px solid var(--line); border-radius: 10px; padding: 8px; max-height: 240px; overflow: auto; margin: 8px 0 0; }
.empty { color: var(--muted); font-size: 12.5px; text-align: center; padding: 18px 8px; }
@keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@keyframes blink { 0%, 80%, 100% { opacity: .25; transform: scale(.85); } 40% { opacity: 1; transform: scale(1); } }
@media (max-width: 900px) { .body { grid-template-columns: 1fr; } .side { display: none; } .body.side-open .side { display: grid; position: fixed; inset: 58px 0 0 auto; width: min(380px, 100%); z-index: 6; box-shadow: var(--shadow-lg); } }
@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; scroll-behavior: auto !important; } }
"""

_JS = r"""
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };
  var SESSION = 'window';
  var state = { skills: [], pending: [], busy: false };

  function api(path, body) {
    var options = body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    };
    return fetch(path, options).then(function (response) {
      return response.json().catch(function () { return { ok: false, reason: 'Ответ не JSON (' + response.status + ')' }; });
    });
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

  function addTyping() {
    var node = document.createElement('div');
    node.className = 'msg bot';
    node.innerHTML = '<div class="avatar">N</div><div class="bubble"><span class="typing"><i></i><i></i><i></i></span></div>';
    feed.appendChild(node);
    scrollDown();
    return node;
  }

  var SOURCES = { rules: 'понял без модели', model: 'модель', memory: 'память', clock: 'часы', registry: 'реестр', panel: 'панель цеха' };

  function renderAnswer(node, data) {
    var bubble = node.querySelector('.bubble');
    bubble.className = 'bubble' + (data.kind === 'error' ? ' error' : '');
    bubble.innerHTML = '';
    var text = document.createElement('div');
    text.textContent = data.reply || data.reason || 'Пустой ответ';
    bubble.appendChild(text);
    var meta = [];
    if (data.source && SOURCES[data.source]) { meta.push('<span class="tag src">' + esc(SOURCES[data.source]) + '</span>'); }
    if (data.skill) { meta.push('<span class="tag">' + esc(data.skill) + '</span>'); }
    if (data.ms) { meta.push('<span class="tag">' + esc(data.ms) + ' мс</span>'); }
    if (meta.length) {
      var line = document.createElement('div');
      line.className = 'meta';
      line.innerHTML = meta.join('');
      bubble.appendChild(line);
    }
    if (data.steps && data.steps.length) {
      var steps = document.createElement('details');
      steps.className = 'steps';
      steps.innerHTML = '<summary>Как я понял</summary>' + data.steps.map(function (step) {
        return '<div class="step"><b>' + esc(step.title) + '</b><span>' + esc(step.detail || '') + '</span></div>';
      }).join('');
      bubble.appendChild(steps);
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
    scrollDown();
  }

  function confirmCard(pending) {
    var card = document.createElement('div');
    card.className = 'confirm';
    card.innerHTML = '<div><b>Нужно ваше решение</b></div><div class="small"></div>'
      + '<div class="row"><button class="btn primary" type="button" data-yes>Подтвердить</button>'
      + '<button class="btn" type="button" data-no>Отменить</button></div>';
    card.querySelector('.small').textContent = pending.text || '';
    card.querySelector('[data-yes]').addEventListener('click', function () { decide(pending.id, true, card); });
    card.querySelector('[data-no]').addEventListener('click', function () { decide(pending.id, false, card); });
    return card;
  }

  function decide(id, confirmed, card) {
    if (card) { card.querySelectorAll('button').forEach(function (b) { b.disabled = true; }); }
    return api('/action/confirm', { id: id, confirmed: confirmed }).then(function (data) {
      if (card) {
        card.innerHTML = '';
        var line = document.createElement('div');
        line.className = 'small';
        line.textContent = data.done ? '✓ Выполнено' : (confirmed ? '✗ ' + (data.reason || 'Не выполнено') : 'Отменено');
        card.appendChild(line);
      }
      loadPending(); loadJournal();
    });
  }

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
    }).catch(function (error) {
      renderAnswer(typing, { kind: 'error', reply: 'Агент не ответил: ' + (error && error.message || error) });
    }).then(function () {
      state.busy = false;
      $('send').disabled = false;
      $('input').focus();
    });
  }

  var input = $('input');
  function autosize() { input.style.height = 'auto'; input.style.height = Math.min(180, input.scrollHeight) + 'px'; }
  input.addEventListener('input', autosize);
  input.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && !event.shiftKey) {
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

  // --- вкладки ------------------------------------------------------------
  document.querySelectorAll('.tab').forEach(function (tab) {
    tab.addEventListener('click', function () {
      document.querySelectorAll('.tab').forEach(function (other) { other.setAttribute('aria-selected', 'false'); });
      document.querySelectorAll('.pane').forEach(function (pane) { pane.classList.remove('on'); });
      tab.setAttribute('aria-selected', 'true');
      $(tab.getAttribute('data-pane')).classList.add('on');
    });
  });
  $('side-toggle').addEventListener('click', function () { $('body').classList.toggle('side-open'); });

  // --- подтверждения ------------------------------------------------------
  function loadPending() {
    return api('/pending').then(function (data) {
      state.pending = data.pending || [];
      var count = $('pending-count');
      count.textContent = state.pending.length;
      count.style.display = state.pending.length ? '' : 'none';
      var host = $('pending');
      host.innerHTML = '';
      if (!state.pending.length) { host.innerHTML = '<div class="empty">Подтверждений не ждём</div>'; return; }
      state.pending.forEach(function (row) { host.appendChild(confirmCard(row)); });
    }).catch(function () {});
  }

  // --- память -------------------------------------------------------------
  function loadMemory() {
    return api('/memory').then(function (data) {
      var host = $('memory');
      var rows = data.memories || [];
      if (!rows.length) { host.innerHTML = '<div class="empty">Память пуста. Скажите «запомни, что…» или добавьте запись выше.</div>'; return; }
      host.innerHTML = '';
      rows.forEach(function (row) {
        var item = document.createElement('div');
        item.className = 'item';
        item.innerHTML = '<div class="grow"><div class="t"></div><div class="small"></div></div>'
          + '<button class="x" type="button" title="Забыть" aria-label="Забыть">✕</button>';
        item.querySelector('.t').textContent = row.text;
        item.querySelector('.small').textContent = (row.kind || 'fact') + ' · ' + String(row.updated_at || row.at || '').slice(0, 16);
        item.querySelector('.x').addEventListener('click', function () {
          api('/memory', { op: 'forget', id: row.id }).then(loadMemory);
        });
        host.appendChild(item);
      });
    }).catch(function () {});
  }
  $('memory-add').addEventListener('click', function () {
    var field = $('memory-text');
    var text = field.value.trim();
    if (!text) { field.focus(); return; }
    api('/memory', { op: 'remember', text: text }).then(function () { field.value = ''; loadMemory(); });
  });

  // --- навыки -------------------------------------------------------------
  var RISK = { read: 'чтение', own: 'своя база', soft: 'мягкое', write: 'подтверждение', system: 'подтверждение', irreversible: 'необратимо' };
  function renderSkills() {
    var query = ($('skill-filter').value || '').toLowerCase();
    var rows = state.skills.filter(function (row) {
      return !query || (row.name + ' ' + row.title + ' ' + row.description).toLowerCase().indexOf(query) >= 0;
    });
    $('skills').innerHTML = rows.map(function (row) {
      return '<div class="skill' + (row.available ? '' : ' off') + '">'
        + '<div class="row2" style="justify-content:space-between;align-items:center"><b>' + esc(row.title) + '</b>'
        + '<span class="risk ' + esc(row.risk) + '">' + esc(RISK[row.risk] || row.risk) + '</span></div>'
        + '<div class="n">' + esc(row.name) + '</div>'
        + '<div class="small">' + esc(row.description) + '</div>'
        + (row.available ? '' : '<div class="r" style="color:var(--bad)">' + esc(row.reason || 'недоступен') + '</div>')
        + '</div>';
    }).join('') || '<div class="empty">Ничего не нашлось</div>';
    var select = $('skill');
    select.innerHTML = state.skills.map(function (row) {
      return '<option value="' + esc(row.name) + '">' + esc(row.title) + (row.available ? '' : ' — недоступен') + '</option>';
    }).join('');
    pickSkill();
  }
  function loadSkills() {
    return api('/skills').then(function (data) {
      state.skills = data.skills || [];
      $('skills-count').textContent = (data.ready || 0) + ' из ' + state.skills.length;
      renderSkills();
    }).catch(function () {});
  }
  $('skill-filter').addEventListener('input', renderSkills);
  function pickSkill() {
    var name = $('skill').value;
    var skill = state.skills.find(function (row) { return row.name === name; }) || {};
    var keys = Object.keys(skill.params || {});
    var box = $('params');
    box.innerHTML = '';
    keys.forEach(function (key) {
      var label = document.createElement('label');
      label.className = 'small';
      label.style.display = 'block';
      label.style.marginTop = '6px';
      label.textContent = key + ' (' + skill.params[key] + ')';
      var field = document.createElement('input');
      field.className = 'field';
      field.name = key;
      field.autocomplete = 'off';
      label.appendChild(field);
      box.appendChild(label);
    });
    if (!keys.length) { box.innerHTML = '<div class="small" style="margin-top:6px">Параметров нет</div>'; }
    $('run').disabled = !skill.available;
  }
  $('skill').addEventListener('change', pickSkill);
  $('run').addEventListener('click', function () {
    var params = {};
    $('params').querySelectorAll('input').forEach(function (field) { if (field.value.trim()) { params[field.name] = field.value.trim(); } });
    $('run').disabled = true;
    api('/skill', { name: $('skill').value, params: params }).then(function (data) {
      $('result').textContent = JSON.stringify(data, null, 2);
      if (data.queued || data.needs_confirmation) { loadPending(); }
      loadJournal();
    }).then(function () { $('run').disabled = false; });
  });

  // --- журнал -------------------------------------------------------------
  function loadJournal() {
    return api('/journal?limit=25').then(function (data) {
      var rows = data.entries || [];
      $('journal').innerHTML = rows.map(function (row) {
        var tone = row.outcome === 'done' ? 'var(--ok)' : (row.outcome === 'needs_confirmation' ? 'var(--warn)' : 'var(--bad)');
        return '<div class="item"><div class="grow"><div class="t"><b style="color:' + tone + '">●</b> ' + esc(row.skill)
          + ' <span class="small">' + esc(row.outcome) + '</span></div><div class="small">' + esc(String(row.at || '').slice(5, 16))
          + ' · ' + esc(row.detail || '') + '</div></div></div>';
      }).join('') || '<div class="empty">Журнал пуст</div>';
    }).catch(function () {});
  }

  // --- состояние ----------------------------------------------------------
  function chip(id, ok, text, warn) {
    var node = $(id);
    node.className = 'chip ' + (ok ? 'ok' : (warn ? 'warn' : 'bad'));
    node.lastChild.textContent = text;
  }
  function loadStatus() {
    return api('/capabilities').then(function (data) {
      var caps = data.capabilities || {};
      chip('c-windows', caps.windows, caps.windows ? 'Windows' : 'без Windows', true);
      chip('c-voice', caps.speech_in || caps.speech_out, caps.speech_in ? 'голос' : (caps.speech_out ? 'озвучка' : 'без голоса'), true);
    }).catch(function () {});
  }
  function loadHistory() {
    return api('/chat/history?session=' + SESSION + '&limit=30').then(function (data) {
      var turns = data.turns || [];
      if (!turns.length) { return; }
      hideHello();
      turns.forEach(function (turn) {
        if (turn.role === 'user') { addMine(turn.text); return; }
        var node = document.createElement('div');
        node.className = 'msg bot';
        node.innerHTML = '<div class="avatar">N</div><div class="bubble"></div>';
        feed.appendChild(node);
        renderAnswer(node, { reply: turn.text, kind: (turn.meta || {}).kind, skill: (turn.meta || {}).skill });
      });
    }).catch(function () {});
  }
  $('clear').addEventListener('click', function () {
    api('/chat/clear', { session: SESSION }).then(function () { window.location.reload(); });
  });

  loadHistory(); loadSkills(); loadPending(); loadJournal(); loadMemory(); loadStatus();
  setInterval(loadPending, 3000);
  input.focus();
})();
"""

_EXAMPLES = (
    ("Как там компьютер?", "процессор, память, диски"),
    ("Открой блокнот", "программы из белого списка"),
    ("Громкость 30", "звук, пауза, треки"),
    ("Какие окна открыты?", "переключиться, свернуть"),
    ("Найди файл договор", "файлы и документы"),
    ("Запомни, что я работаю до 19:00", "память помощника"),
)


def page() -> str:
    """Страница окна помощника. Данные подставляет JavaScript, всегда через экранирование."""
    examples = "".join(
        f'<button class="example" type="button" data-say="{say}"><b>{say}</b><span>{hint}</span></button>'
        for say, hint in _EXAMPLES)
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{TITLE}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="app">
  <header class="top">
    <div class="mark">N</div>
    <div class="brand"><b>{TITLE}</b><span>компьютер цеха · только 127.0.0.1</span></div>
    <div class="chips">
      <span class="chip" id="c-windows"><i></i><span>…</span></span>
      <span class="chip" id="c-voice"><i></i><span>…</span></span>
      <span class="chip"><i style="background:var(--accent)"></i><span>навыков <b id="skills-count">…</b></span></span>
    </div>
    <button class="icon-btn" id="clear" type="button" title="Начать разговор заново" aria-label="Начать заново">⟲</button>
    <button class="icon-btn" id="theme" type="button" title="Тема" aria-label="Тема">◐</button>
    <button class="icon-btn" id="side-toggle" type="button" title="Панель" aria-label="Панель">☰</button>
  </header>
  <div class="body" id="body">
    <main class="chat">
      <div class="feed" id="feed-scroll">
        <div class="feed-inner" id="feed">
          <section class="hello" id="hello">
            <div class="mark">N</div>
            <h1>Чем помочь?</h1>
            <p>Говорите обычными словами: помощник понимает команды компьютеру, помнит разговор и
               спрашивает подтверждение перед тем, как что-то менять.</p>
            <div class="examples">{examples}</div>
          </section>
        </div>
      </div>
      <div class="composer">
        <div class="composer-inner">
          <textarea id="input" rows="1" placeholder="Напишите, что сделать… Enter — отправить, Shift+Enter — новая строка" aria-label="Сообщение помощнику"></textarea>
          <button class="send" id="send" type="button" aria-label="Отправить">➤</button>
        </div>
        <div class="hint">Ввод в чужие окна, питание и файлы — только после «Подтвердить» на этом компьютере.</div>
      </div>
    </main>
    <aside class="side">
      <nav class="tabs" role="tablist">
        <button class="tab" type="button" role="tab" aria-selected="true" data-pane="pane-pending">Ждёт<span class="count" id="pending-count" style="display:none">0</span></button>
        <button class="tab" type="button" role="tab" aria-selected="false" data-pane="pane-memory">Память</button>
        <button class="tab" type="button" role="tab" aria-selected="false" data-pane="pane-skills">Навыки</button>
        <button class="tab" type="button" role="tab" aria-selected="false" data-pane="pane-journal">Журнал</button>
      </nav>
      <section class="pane on" id="pane-pending"><div id="pending"><div class="empty">Подтверждений не ждём</div></div></section>
      <section class="pane" id="pane-memory">
        <div class="card"><h3>Добавить в память</h3>
          <div class="row2"><input class="field" id="memory-text" placeholder="Например: Мария берёт только PETG" autocomplete="off">
          <button class="btn primary" id="memory-add" type="button">+</button></div></div>
        <div class="list" id="memory"></div>
      </section>
      <section class="pane" id="pane-skills">
        <input class="field" id="skill-filter" placeholder="Поиск навыка…" autocomplete="off" style="margin-bottom:10px">
        <div id="skills"></div>
        <div class="card"><h3>Запустить вручную</h3>
          <select class="field" id="skill" aria-label="Навык"></select>
          <div id="params"></div>
          <button class="btn primary" id="run" type="button" style="margin-top:8px">Выполнить</button>
          <pre class="out" id="result">Результат появится здесь.</pre></div>
      </section>
      <section class="pane" id="pane-journal"><div class="list" id="journal"></div></section>
    </aside>
  </div>
</div>
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
