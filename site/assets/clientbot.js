/* PrintFlow 9.3 — клиентский бот и команда Telegram.
   Два экрана: вкладка «Клиент-бот» (витрина для покупателей, заказы и
   диалоги) и карточка «Команда» в настройках (роли сотрудник/руководитель).
   Данные — только из локального API; никаких внешних сервисов. */
(() => {
'use strict';

const { $, $$, esc, num, money, nfmt, toast, fail, agoText } = PF.ui;
const { get, post } = PF.api;

const COMMANDS = [
  ['старт', 'приветствие и меню бота'],
  ['каталог', 'витрина с ценами и кнопками заказа'],
  ['заказ 3', 'заказать позицию №3 из каталога'],
  ['индивидуальный …', 'заявка на печать по своей задаче'],
  ['мои заказы', 'статусы заказов + кнопка на каждый заказ'],
  ['статус 1001', 'статус по номеру заказа'],
  ['телефон +7…', 'привязать номер: подтянутся заказы с полки'],
  ['фото / файл / голосовое', 'подсказка-заявка; подпись к фото работает как команда'],
  ['кнопка «Отправить номер»', 'телефон одним касанием — Telegram-контакт'],
  ['помощь', 'список команд'],
];

const ROLE_RIGHTS_LABEL = {
  view: 'обзоры и камера', shelf: 'полка: продажа и приход',
  orders: 'заказы и оплата', finance: 'деньги и отчёты',
  printers: 'управление принтерами', staff: 'управление командой',
};
const RIGHTS_ORDER = ['view', 'shelf', 'orders', 'finance', 'printers', 'staff'];

let staffRoles = {};
let loadSeq = 0;

/* ============================================================ клиент-бот */
async function renderBot() {
  const seq = ++loadSeq;
  let data;
  try {
    data = await get('/api/client-bot');
  } catch (e) { fail(e); return; }
  if (seq !== loadSeq) return;
  if (!$('view-clientbot')) return;

  $('cb_enabled').checked = !!data.enabled;
  $('cb_catalog').checked = data.catalog !== false;
  $('cb_notify').checked = data.notify !== false;
  $('cb_welcome').value = data.welcome || '';
  $('cb_token').value = '';
  $('cb_token_hint').textContent = data.has_token
    ? 'Токен сохранён — оставьте поле пустым, чтобы не менять'
    : 'Создайте второго бота у @BotFather и вставьте токен';

  const tag = $('cb_state_tag');
  tag.hidden = false;
  if (data.enabled && data.alive) { tag.textContent = 'работает'; tag.className = 'tag ok'; }
  else if (data.enabled) { tag.textContent = 'включён, ждёт Telegram'; tag.className = 'tag warn'; }
  else { tag.textContent = 'выключен'; tag.className = ''; }

  const s = data.stats || {};
  $('cb_kpis').innerHTML = [
    kpi('Покупателей', String(s.chats ?? 0), 'чатов писали боту'),
    kpi('Заказов из бота', String(s.orders ?? 0), 'заявок канала Telegram'),
    kpi('Сообщений сегодня', String(s.messages_today ?? 0), `всего ${nfmt(s.messages ?? 0)}`),
    kpi('Опрос Telegram', data.alive ? 'на связи' : '—',
        s.last_poll ? `последний ${agoText(s.last_poll * 1000)}` : 'ещё не спрашивал',
        data.alive ? 'ok' : ''),
  ].join('');

  $('cb_commands').innerHTML = COMMANDS.map(([c, d]) =>
    `<tr><td><b>${esc(c)}</b></td><td class="muted">${esc(d)}</td></tr>`).join('');

  const st = PF.state.statuses || [];
  const statusName = (id) => (st.find((x) => x.id === id) || {}).name || id || '—';
  $('cb_orders').innerHTML = (data.orders || []).length
    ? data.orders.map((o) => `<tr><td><b>№${esc(o.number)}</b></td>`
      + `<td>${esc(o.product || '')}</td><td>${esc(o.name || '')}</td>`
      + `<td>${esc(statusName(o.status))}</td>`
      + `<td class="right">${num(o.price) ? money(o.price) : '—'}</td>`
      + `<td class="muted">${esc(String(o.linked_at || '').slice(0, 16).replace('T', ' '))}</td></tr>`).join('')
    : '<tr><td colspan="6" class="empty">Заказов из бота пока нет — включите бота и дайте QR с полки.</td></tr>';

  $('cb_chats').innerHTML = (data.chats || []).length
    ? data.chats.map((c) => `<tr><td><b>${esc(c.name || 'Без имени')}</b></td>`
      + `<td>${c.username ? '@' + esc(c.username) : '—'}</td>`
      + `<td>${esc(c.phone || '—')}</td><td class="right">${nfmt(c.orders)}</td>`
      + `<td class="muted">${esc(String(c.last_seen || '').slice(0, 16).replace('T', ' '))}</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">Покупатели ещё не писали.</td></tr>';

  $('cb_log').innerHTML = (data.log || []).length
    ? data.log.map((l) => `<tr><td class="muted">${esc(String(l.at || '').slice(5, 16).replace('T', ' '))}</td>`
      + `<td>${esc(l.name || '')}</td><td>${esc(l.text || '')}</td>`
      + `<td class="muted">${esc(l.answer || '')}</td></tr>`).join('')
    : '<tr><td colspan="4" class="empty">Диалогов пока нет.</td></tr>';
}

function kpi(label, value, sub, kind) {
  return `<div class="kpi ${kind || ''}"><span class="label">${esc(label)}</span>`
    + `<span class="value">${value}</span><span class="sub">${esc(sub)}</span></div>`;
}

async function saveBot() {
  const body = {
    client_bot_enabled: $('cb_enabled').checked,
    client_bot_catalog: $('cb_catalog').checked,
    client_bot_notify: $('cb_notify').checked,
    client_bot_welcome: $('cb_welcome').value.trim(),
  };
  if ($('cb_token').value.trim()) body.client_bot_token = $('cb_token').value.trim();
  try {
    await post('/api/client-bot/save', body);
    toast('Настройки бота сохранены',
          'Если включили впервые — через минуту бот начнёт отвечать');
    renderBot();
  } catch (e) { fail(e); }
}

async function testBot() {
  const body = {};
  if ($('cb_token').value.trim()) body.client_bot_token = $('cb_token').value.trim();
  try {
    const r = await post('/api/client-bot/test', body);
    if (r.ok) toast('Токен принят', `Бот @${r.username} — «${r.name}»`);
    else fail(new Error(r.error || 'Токен не принят'));
  } catch (e) { fail(e); }
}

/* ================================================================= команда */
async function renderStaff() {
  const host = $('set_staff');
  if (!host) return;
  let data;
  try { data = await get('/api/staff'); } catch (e) { host.innerHTML = ''; return; }
  staffRoles = data.roles || {};

  const rights = (role) => RIGHTS_ORDER
    .filter((g) => ((staffRoles[role] || {}).rights || []).includes(g))
    .map((g) => ROLE_RIGHTS_LABEL[g]).join(' · ') || '—';

  const rows = (data.staff || []).map((m) => `<div class="set-row" data-staff-id="${esc(m.id)}">`
    + `<div class="sinfo"><b>${esc(m.name)}${Number(m.active) ? '' : ' · отключён'}</b>`
    + `<small>${esc(m.role_name)} · chat_id ${esc(m.chat_id)}${m.note ? ' · ' + esc(m.note) : ''}<br>Права: ${esc(rights(m.role))}</small></div>`
    + `<div class="btn-grid">`
    + (Number(m.active)
        ? `<button class="btn sm" type="button" data-staff-off="${esc(m.id)}">Отключить</button>`
        : `<button class="btn sm" type="button" data-staff-on="${esc(m.id)}">Вернуть</button>`)
    + `</div></div>`).join('');

  const invites = (data.invites || []).filter((i) => !Number(i.used));
  const inviteRows = invites.length
    ? invites.map((i) => `<div class="set-row"><div class="sinfo"><b>${esc(i.code)}</b>`
      + `<small>${esc(i.role_name)}${i.name ? ' · ' + esc(i.name) : ''} — напишите боту: старт ${esc(i.code)}</small></div>`
      + `<button class="btn sm" type="button" data-invite-del="${esc(i.code)}">✕</button></div>`).join('')
    : '<p class="muted" style="font-size:12.5px">Активных кодов нет. Код одноразовый: человек пишет боту «старт КОД» и получает роль кода.</p>';

  host.innerHTML =
    `<div class="notice"><span>ℹ</span><span>Владелец — это Chat ID из блока выше (сейчас: <b>${data.owner_chat ? esc(data.owner_chat) : 'не задан'}</b>).`
    + ` Руководителю открыты деньги, заказы и принтеры; сотруднику — обзоры, полка и фото.</span></div>`
    + (rows || '<p class="muted" style="font-size:12.5px">Команда пуста — добавьте сотрудника или руководителя.</p>')
    + `<div class="card-head" style="margin-top:14px"><div><h3>Добавить участника</h3><p>chat_id человек узнает командой «код» в боте</p></div></div>`
    + `<div class="set-row"><div class="sinfo"><b>Имя</b><small>Как показывать в списке</small></div>`
    + `<input type="text" id="staff_name" placeholder="Ваня" style="max-width:150px">`
    + `<select id="staff_role"><option value="employee">сотрудник</option><option value="manager">руководитель</option></select>`
    + `<input type="text" id="staff_chat" placeholder="chat_id" style="max-width:130px">`
    + `<button class="btn sm primary" type="button" id="staff_add">Добавить</button></div>`
    + `<div class="card-head" style="margin-top:14px"><div><h3>Приглашения</h3><p>Код вместо ручного ввода chat_id</p></div>`
    + `<button class="btn sm" type="button" id="staff_invite_make">+ Код для сотрудника</button></div>`
    + inviteRows;
}

async function bindStaff() {
  const host = $('set_staff');
  if (!host) return;
  host.addEventListener('click', async (e) => {
    const add = e.target.closest('#staff_add');
    if (add) {
      try {
        await post('/api/staff/save', {
          name: $('staff_name').value.trim(),
          role: $('staff_role').value,
          chat_id: $('staff_chat').value.trim(),
        });
        toast('Участник добавлен', 'Права применятся сразу — без перезапуска');
        renderStaff();
      } catch (err) { fail(err); }
      return;
    }
    const invite = e.target.closest('#staff_invite_make');
    if (invite) {
      try {
        const r = await post('/api/staff/invite', { role: 'employee' });
        toast(`Код ${r.invite.code}`, 'Отправьте человеку: он пишет боту «старт ' + r.invite.code + '»');
        renderStaff();
      } catch (err) { fail(err); }
      return;
    }
    const off = e.target.closest('[data-staff-off]');
    if (off) {
      try { await post('/api/staff/delete', { id: off.dataset.staffOff }); renderStaff(); }
      catch (err) { fail(err); }
      return;
    }
    const on = e.target.closest('[data-staff-on]');
    if (on) {
      try { await post('/api/staff/restore', { id: on.dataset.staffOn }); renderStaff(); }
      catch (err) { fail(err); }
      return;
    }
    const del = e.target.closest('[data-invite-del]');
    if (del) {
      try { await post('/api/staff/invite/delete', { code: del.dataset.inviteDel }); renderStaff(); }
      catch (err) { fail(err); }
    }
  });
}

/* ==================================================================== старт */
function bind() {
  if ($('cb_save')) $('cb_save').addEventListener('click', saveBot);
  if ($('cb_test')) $('cb_test').addEventListener('click', testBot);
  if ($('cb_refresh')) $('cb_refresh').addEventListener('click', renderBot);
  bindStaff();
}

PF.on('ready', bind);
PF.on('view', (d) => {
  if (d.view === 'clientbot') renderBot();
  if (d.view === 'settings') renderStaff();
});
PF.on('bootstrap', renderStaff);

PF.modules.clientbot = { renderBot, renderStaff, saveBot, testBot };
})();
