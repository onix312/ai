"""Окно ассистента (18.14, идея И139): страница, которую отдаёт сам агент.

Почему страница, а не сразу `pywebview`.

Окно ассистента обязано работать там, где работает агент: на компьютере цеха с
панелью, в службе Windows без рабочего стола и в отладочном контейнере. Поэтому
сначала появляется содержимое (страница на порту агента, только loopback), а
обёртка окна — второй слой: `window.py` открывает эту страницу в `pywebview`,
если он установлен, и честно печатает адрес, если нет. Так окно не становится
единственным способом увидеть, что ассистент жив.

Внешних ресурсов на странице нет: ни шрифтов, ни скриптов с чужих доменов —
инвариант репозитория («данные машину не покидают») действует и для агента.
"""
from __future__ import annotations

from typing import Any

TITLE = "Ассистент NOZZA"

_CSS = """
:root { color-scheme: light dark; --line: #d8dee9; --muted: #5b6472; --ok: #1a7f37;
        --warn: #9a6700; --bad: #b42318; --accent: #1f6feb; }
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.5 system-ui, "Segoe UI", Roboto, sans-serif;
       background: #f6f8fa; color: #111; }
header { display: flex; gap: 12px; align-items: center; padding: 12px 16px;
         background: #fff; border-bottom: 1px solid var(--line); }
header h1 { font-size: 16px; margin: 0; }
header .state { margin-left: auto; display: flex; gap: 8px; flex-wrap: wrap; }
main { padding: 16px; display: grid; gap: 16px; }
section { background: #fff; border: 1px solid var(--line); border-radius: 10px;
          padding: 12px 14px; }
h2 { font-size: 14px; margin: 0 0 8px; }
.badge { border: 1px solid var(--line); border-radius: 999px; padding: 2px 8px;
         font-size: 12px; color: var(--muted); background: #f6f8fa; }
.badge.ok { color: var(--ok); border-color: #b7e0c3; background: #f0fbf3; }
.badge.bad { color: var(--bad); border-color: #f0c4c0; background: #fdf3f2; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
code { font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size: 12px; }
button { font: inherit; padding: 6px 12px; border-radius: 8px; cursor: pointer;
         border: 1px solid var(--line); background: #fff; }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button:disabled { opacity: .5; cursor: not-allowed; }
input, select, textarea { font: inherit; padding: 6px 8px; border-radius: 8px;
                          border: 1px solid var(--line); width: 100%; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.muted { color: var(--muted); font-size: 12px; }
pre { white-space: pre-wrap; word-break: break-word; margin: 8px 0 0; font-size: 12px;
      background: #f6f8fa; border: 1px solid var(--line); border-radius: 8px; padding: 8px; }
.pending { border-left: 4px solid var(--warn); }
"""

_JS = """
const state = { skills: [], pending: [], last: null };

async function loadSkills() {
  const answer = await fetch('/skills');
  const payload = await answer.json();
  state.skills = payload.skills || [];
  const ready = payload.ready || 0;
  document.getElementById('skills-count').textContent =
    `${ready} из ${state.skills.length} навыков доступны`;
  const select = document.getElementById('skill');
  select.innerHTML = state.skills.map((row) => {
    const mark = row.available ? '' : ' — недоступен';
    return `<option value="${row.name}">${row.title}${mark}</option>`;
  }).join('');
  const rows = state.skills.map((row) => `
    <tr>
      <td><code>${row.name}</code></td>
      <td>${row.title}<div class="muted">${row.description}</div></td>
      <td>${row.confirm ? 'подтверждение' : row.risk}</td>
      <td>${row.available
        ? '<span class="badge ok">доступен</span>'
        : `<span class="badge bad">${row.reason || 'недоступен'}</span>`}</td>
    </tr>`).join('');
  document.getElementById('skills-body').innerHTML = rows;
  pickSkill();
}

function pickSkill() {
  const name = document.getElementById('skill').value;
  const skill = state.skills.find((row) => row.name === name) || {};
  const box = document.getElementById('params');
  const keys = Object.keys(skill.params || {});
  box.innerHTML = keys.length
    ? keys.map((key) => `<label class="muted">${key} <span>(${skill.params[key]})</span>
        <input name="${key}" autocomplete="off"></label>`).join('')
    : '<div class="muted">Параметров нет</div>';
  document.getElementById('skill-doc').textContent = skill.doc || '';
  document.getElementById('run').disabled = !skill.available;
}

async function runSkill() {
  const name = document.getElementById('skill').value;
  const params = {};
  for (const input of document.getElementById('params').querySelectorAll('input')) {
    if (input.value.trim()) { params[input.name] = input.value.trim(); }
  }
  document.getElementById('run').disabled = true;
  const answer = await fetch('/skill', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, params })
  });
  const payload = await answer.json();
  state.last = payload;
  document.getElementById('run').disabled = false;
  document.getElementById('result').textContent = JSON.stringify(payload, null, 2);
  if (payload.needs_confirmation) { loadPending(); }
  loadJournal();
}

async function loadPending() {
  const answer = await fetch('/pending');
  const payload = await answer.json();
  state.pending = payload.pending || [];
  document.getElementById('pending').innerHTML = state.pending.length
    ? state.pending.map((row) => `
      <div class="pending" style="padding:8px 0">
        <div>${row.text}</div>
        <div class="muted">ждёт человека на этом компьютере</div>
        <button class="primary" onclick="decide('${row.id}', true)">Подтвердить</button>
        <button onclick="decide('${row.id}', false)">Отменить</button>
      </div>`).join('')
    : '<div class="muted">Подтверждений не ждём</div>';
}

async function decide(id, confirmed) {
  await fetch('/action/confirm', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, confirmed })
  });
  loadPending(); loadJournal();
}

async function loadJournal() {
  const answer = await fetch('/journal?limit=15');
  const payload = await answer.json();
  document.getElementById('journal').innerHTML = (payload.entries || []).map((row) => `
    <tr><td>${row.at}</td><td><code>${row.skill}</code></td>
        <td>${row.outcome}</td><td class="muted">${row.detail || ''}</td></tr>`).join('')
    || '<tr><td colspan="4" class="muted">Журнал пуст</td></tr>';
}

document.getElementById('skill').addEventListener('change', pickSkill);
document.getElementById('run').addEventListener('click', runSkill);
loadSkills(); loadPending(); loadJournal();
setInterval(loadPending, 4000);
"""


def page() -> str:
    """Страница окна ассистента. Данные в неё подставляет JavaScript с /skills."""
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{TITLE}</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <h1>{TITLE}</h1>
  <span class="badge" id="skills-count">загружаю навыки…</span>
  <span class="badge">агент слушает только 127.0.0.1</span>
</header>
<main>
  <section>
    <h2>Навык</h2>
    <div class="row">
      <select id="skill" aria-label="Навык"></select>
      <button class="primary" id="run">Выполнить</button>
    </div>
    <div id="params" style="margin-top:8px"></div>
    <div class="muted" id="skill-doc"></div>
    <pre id="result">Результат появится здесь. Навык с риском «write» сначала спросит
подтверждение на этом компьютере.</pre>
  </section>
  <section>
    <h2>Ждёт подтверждения</h2>
    <div id="pending" class="muted">Подтверждений не ждём</div>
  </section>
  <section>
    <h2>Реестр навыков</h2>
    <table>
      <thead><tr><th>Навык</th><th>Что делает</th><th>Риск</th><th>Доступность</th></tr></thead>
      <tbody id="skills-body"></tbody>
    </table>
  </section>
  <section>
    <h2>Журнал действий на компьютере</h2>
    <table>
      <thead><tr><th>Время</th><th>Навык</th><th>Исход</th><th>Подробность</th></tr></thead>
      <tbody id="journal"></tbody>
    </table>
  </section>
</main>
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
