/* PrintFlow 9.3.4 — клиентский Telegram-бот и единый inbox.
   Панель работает только с локальным API: секрет токена никогда не приходит
   обратно в браузер, а публичные рассылки требуют отдельного opt-in. */
(() => {
'use strict';

const { $, esc, num, money, nfmt, toast, fail, agoText, ask } = PF.ui;
const { get, post } = PF.api;

const COMMANDS = [
  ['старт', 'приветствие, deep-link и главное меню'],
  ['каталог', 'опубликованные позиции, варианты и остаток'],
  ['свой заказ', 'мастер заявки из 4 шагов с проверкой перед отправкой'],
  ['заказ 3', 'добавить позицию в корзину и оформить заявку'],
  ['новинки', 'пять последних позиций каталога'],
  ['как получить', 'адрес, часы и срок хранения готового заказа'],
  ['отзывы', 'оценки и свежие отзывы покупателей'],
  ['индивидуальный …', 'мастер заявки: задача, файл/фото, срок и контакт'],
  ['фото [1001]', 'добавить референс к заявке по номеру заказа'],
  ['мои заказы', 'статусы, ETA и счётчик до скидки'],
  ['статус 1001', 'цена, срок, реквизиты и повторный заказ'],
  ['телефон +7…', 'привязать номер только через контакт Telegram'],
  ['оператор', 'связаться с мастерской — ответит человек'],
  ['избранное', 'wishlist и быстрый повтор заказа'],
  ['вопрос-ответ', 'материалы, сроки, доставка и оплата'],
  ['помощь', 'список команд'],
];

const ROLE_RIGHTS_LABEL = {
  view: 'обзоры и камера', shelf: 'полка: продажа и приход',
  orders: 'заказы и оплата', finance: 'деньги и отчёты',
  printers: 'управление принтерами', staff: 'управление командой',
  inbox: 'ответы покупателям',
};
const RIGHTS_ORDER = ['view', 'shelf', 'orders', 'finance', 'printers', 'inbox', 'staff'];

let loadSeq = 0;
let bound = false;
let latestTemplates = [];
let latestDefaults = [];

const text = (value, fallback = '—') => {
  const valueText = String(value ?? '').trim();
  return valueText || fallback;
};
const dt = (value) => {
  if (!value) return '—';
  const raw = String(value).replace('T', ' ');
  return raw.length > 16 ? raw.slice(0, 16) : raw;
};
const requestId = (prefix) => {
  try { return `${prefix}-${crypto.randomUUID()}`; }
  catch (e) { return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`; }
};
const visible = (id, yes) => { const el = $(id); if (el) el.hidden = !yes; };

function setBotLoading(loading, error = '') {
  visible('cb_loading', loading);
  visible('cb_error', !!error);
  const errorText = $('cb_error_text');
  if (errorText) errorText.textContent = error || 'Не удалось загрузить данные.';
  const refresh = $('cb_refresh');
  if (refresh) refresh.disabled = loading;
}

function kpi(label, value, sub, kind = '') {
  return `<div class="kpi ${esc(kind)}"><span class="label">${esc(label)}</span>`
    + `<span class="value">${esc(value)}</span><span class="sub">${esc(sub)}</span></div>`;
}

function statusName(id) {
  const found = (PF.state.statuses || []).find((item) => item.id === id);
  return found ? (found.name || found.id) : text(id);
}
function paymentStatus(status) {
  const map = {
    pending: ['ожидает проверки', 'warn'],
    confirmed: ['подтверждено', 'ok'],
    rejected: ['отклонено', 'bad'],
  };
  return map[status] || [text(status), ''];
}
function inboxStatus(status) {
  const map = { open: 'открыт', pending: 'ждёт ответа', closed: 'закрыт' };
  return map[status] || 'открыт';
}

function renderAnalytics(analytics) {
  const host = $('cb_analytics');
  if (!host) return;
  const events = analytics && Array.isArray(analytics.events) ? analytics.events : [];
  const sources = analytics && Array.isArray(analytics.sources) ? analytics.sources : [];
  const max = Math.max(1, ...events.map((item) => num(item.count)));
  const eventNames = {
    start: 'запуск бота', catalog: 'каталог', product_view: 'карточка товара',
    order_created: 'заказ', custom_order_created: 'индивидуальная заявка',
    photo_order: 'фото к заказу', file_order: 'файл к заказу',
    payment_intent: '«Я оплатил»', payment_confirmed: 'оплата подтверждена',
    delivered: 'выдача', review: 'отзыв',
  };
  const eventHtml = events.length
    ? events.slice(0, 8).map((item) => `<div class="clientbot-bar-row"><span>${esc(eventNames[item.event] || item.event || 'событие')}</span>`
      + `<b>${nfmt(item.count)}</b><i><em style="width:${Math.min(100, num(item.count) / max * 100)}%"></em></i></div>`).join('')
    : '<div class="empty">События появятся после первого запуска бота.</div>';
  const sourceHtml = sources.length
    ? `<div class="clientbot-sources">${sources.slice(0, 6).map((item) => `<span><b>${esc(item.source || 'direct')}</b>`
      + ` <small>${nfmt(item.chats)} чатов · ${nfmt(item.orders)} заказов</small></span>`).join('')}</div>`
    : '';
  host.innerHTML = `<div class="clientbot-analytics-summary"><b>${nfmt(analytics && analytics.unique_chats || 0)}</b><span>уникальных чатов</span>`
    + `<b>${nfmt(analytics && analytics.orders || 0)}</b><span>заказов</span><b>${nfmt(analytics && analytics.conversion || 0, 1)}%</b><span>конверсия</span></div>`
    + `<div class="clientbot-bars">${eventHtml}</div>${sourceHtml}`;
}

function renderInbox(items, templates = []) {
  const host = $('cb_inbox');
  if (!host) return;
  const rows = Array.isArray(items) ? items : [];
  const byChat = new Map();
  rows.forEach((item) => {
    const key = String(item.chat_id || '');
    if (!key) return;
    if (!byChat.has(key)) byChat.set(key, []);
    byChat.get(key).push(item);
  });
  const unread = rows.length;
  const badge = $('cb_unread_badge');
  if (badge) { badge.textContent = String(unread); badge.hidden = !unread; }
  if (!byChat.size) {
    host.innerHTML = `<div class="empty clientbot-empty-state"><span class="empty-icon">✓</span><b>Непрочитанных сообщений нет</b><small>Новые вопросы покупателя появятся здесь.</small></div>`;
    return;
  }
  host.innerHTML = [...byChat.entries()].map(([chatId, messages]) => {
    const last = messages[0] || {};
    const name = text(last.name, last.username ? '@' + last.username : `Чат ${chatId}`);
    const status = last.inbox_status || 'open';
    const messageHtml = messages.slice(0, 5).map((item) => `<div class="clientbot-message ${item.direction === 'out' ? 'out' : 'in'}">`
      + `<div>${esc(item.text || '')}</div><small>${esc(dt(item.at))}${item.kind && item.kind !== 'message' ? ' · ' + esc(item.kind) : ''}</small></div>`).join('');
    return `<article class="clientbot-thread" data-chat-id="${esc(chatId)}"><header class="clientbot-thread-head">`
      + `<div><b>${esc(name)}</b><small>Telegram ID ${esc(chatId)}${last.username ? ' · @' + esc(last.username) : ''}</small></div>`
      + `<span class="tag ${status === 'closed' ? '' : status === 'pending' ? 'warn' : 'ok'}">${esc(inboxStatus(status))}</span></header>`
      + `<div class="clientbot-thread-messages">${messageHtml}</div>`
      + `<div class="clientbot-reply"><div class="clientbot-quick" data-quick-row>`
      + quickReplies().map((item, i) => `<button class="btn sm ghost" type="button" data-quick-reply data-quick-index="${i}" title="${esc(item.text)}">${esc(item.name)}</button>`).join('')
      + `</div><textarea data-reply-input rows="2" maxlength="3800" placeholder="Ответить покупателю…"></textarea>`
      + (templates.length ? `<select data-inbox-template aria-label="Шаблон ответа"><option value="">Вставить шаблон…</option>${templates.filter((item) => item.enabled !== false).map((item) => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join('')}</select>` : '')
      + `<div class="clientbot-thread-actions"><button class="btn sm primary" type="button" data-inbox-reply>Ответить</button>`
      + `<button class="btn sm" type="button" data-inbox-read>Прочитано</button>`
      + `<select data-inbox-status aria-label="Статус диалога"><option value="open" ${status === 'open' ? 'selected' : ''}>открыт</option>`
      + `<option value="pending" ${status === 'pending' ? 'selected' : ''}>ждёт ответа</option><option value="closed" ${status === 'closed' ? 'selected' : ''}>закрыт</option></select></div></div></article>`;
  }).join('');
}

function quickReplies() {
  // К18: до четырёх заготовок одной кнопкой в каждом диалоге — сначала
  // свои шаблоны, затем готовая библиотека.
  const own = (latestTemplates || []).filter((item) => item.enabled !== false);
  const lib = latestDefaults || [];
  return own.slice(0, 2).concat(lib.slice(0, own.length >= 2 ? 2 : 4 - own.length)).slice(0, 4);
}

async function quickReplyAction(button) {
  const replies = quickReplies();
  const item = replies[Number(button.dataset.quickIndex || 0)];
  const thread = button.closest('.clientbot-thread');
  const input = thread && thread.querySelector('[data-reply-input]');
  if (item && input) {
    input.value = item.text || '';
    input.focus();
  }
}

async function defaultTemplateUse(button) {
  const name = (button.dataset.useName || '').trim();
  const text = (button.dataset.useText || '').trim();
  if (!name || !text) return;
  try {
    await post('/api/client-bot/template/save', { name, text, actor: 'panel' });
    toast(`Шаблон «${name}» добавлен к вашим`);
    await renderBot();
  } catch (error) {
    fail(error);
  }
}

function renderDefaultTemplates(items) {
  const host = $('cb_default_templates');
  if (!host) return;
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) { host.innerHTML = ''; return; }
  host.innerHTML = rows.map((item) => `<div class="clientbot-template clientbot-template-lib">`
    + `<div class="clientbot-template-lib-head"><b>${esc(item.name || '')}</b>`
    + `<button class="btn sm" type="button" data-template-use data-use-name="${esc(item.name || '')}" data-use-text="${esc(item.text || '')}">+ К моим</button></div>`
    + `<div class="clientbot-template-text">${esc(item.text || '')}</div></div>`).join('');
}

function renderTemplates(items) {
  const host = $('cb_templates');
  if (!host) return;
  const rows = Array.isArray(items) ? items : [];
  host.innerHTML = rows.map((item) => `<div class="clientbot-template" data-template-id="${esc(item.id)}">`
    + `<input type="text" data-template-name maxlength="100" value="${esc(item.name || '')}" aria-label="Название шаблона">`
    + `<textarea data-template-text maxlength="1500" aria-label="Текст шаблона">${esc(item.text || '')}</textarea>`
    + `<div class="clientbot-template-actions"><button class="btn sm" type="button" data-template-save>Сохранить</button><button class="btn sm danger-outline" type="button" data-template-delete>Удалить</button></div></div>`).join('')
    + `<div class="clientbot-template-new"><input id="cb_template_new_name" type="text" maxlength="100" placeholder="Название нового шаблона">`
    + `<textarea id="cb_template_new_text" maxlength="1500" placeholder="Например: цена, срок и следующий шаг"></textarea>`
    + `<button class="btn sm" type="button" data-template-add>+ Добавить шаблон</button></div>`;
}

function renderPayments(items) {
  const host = $('cb_payments');
  if (!host) return;
  const rows = Array.isArray(items) ? items : [];
  host.innerHTML = rows.length ? rows.map((item) => {
    const [label, kind] = paymentStatus(item.status);
    const action = item.status === 'pending'
      ? `<div class="table-actions"><button class="btn sm primary" type="button" data-payment-action="confirm" data-payment-id="${esc(item.id)}">Подтвердить</button>`
        + `<button class="btn sm danger-outline" type="button" data-payment-action="reject" data-payment-id="${esc(item.id)}">Отклонить</button></div>` : '';
    return `<tr><td><b>№${esc(item.number || item.order_id || '')}</b><br><small class="muted">${esc(item.id || '')}</small></td>`
      + `<td>${esc(text(item.name, 'Без имени'))}<br><small class="muted">${esc(item.product || '')}</small></td>`
      + `<td class="right"><b>${money(item.amount)}</b><br><small class="muted">${esc(item.purpose || '')}</small></td>`
      + `<td><span class="tag ${kind}">${esc(label)}</span>${item.reject_reason ? `<small class="muted block">${esc(item.reject_reason)}</small>` : ''}</td>`
      + `<td class="muted">${esc(dt(item.created_at))}</td><td>${action}</td></tr>`;
  }).join('') : '<tr><td colspan="6" class="empty">Заявок на ручную проверку пока нет.</td></tr>';
}

function renderOrders(items) {
  const host = $('cb_orders');
  if (!host) return;
  const rows = Array.isArray(items) ? items : [];
  host.innerHTML = rows.length ? rows.map((item) => `<tr><td><b>№${esc(item.number || '')}</b></td>`
    + `<td>${esc(item.product || 'Индивидуальная заявка')}${num(item.photos) ? ' 📎' : ''}${item.cancel_requested_at ? ' ✖️ просит отмену' : ''}</td>`
    + `<td>${esc(text(item.name))}</td><td><span class="status-dot">${esc(statusName(item.status))}</span></td>`
    + `<td class="right">${num(item.price) ? money(item.price) : '—'}</td>`
    + `<td class="muted">${esc(item.source || 'direct')}</td><td class="muted">${esc(dt(item.linked_at))}</td></tr>`).join('')
    : '<tr><td colspan="7" class="empty">Заказов из бота пока нет — включите бота и дайте QR с полки.</td></tr>';
}

function renderReviews(items) {
  const host = $('cb_reviews');
  if (!host) return;
  const rows = Array.isArray(items) ? items : [];
  host.innerHTML = rows.length ? rows.map((item) => {
    const rating = item.rating === 'good' ? '👍 Хорошо' : item.rating === 'bad' ? '👎 Проблема' : '—';
    const stateTag = item.state === 'resolved'
      ? '<span class="tag ok">обработан</span>'
      : item.state === 'answered' ? '<span class="tag ok">отвечен</span>'
        : '<span class="tag warn">в работе</span>';
    // КБ4: быстрый ответ покупателю прямо из строки отзыва
    const replyBox = (item.state === 'resolved' || item.state === 'answered') ? ''
      : `<div class="clientbot-review-reply"><input type="text" maxlength="3800" data-review-reply-text placeholder="Ответ покупателю…" data-order-id="${esc(item.order_id)}" data-chat-id="${esc(item.chat_id)}">`
        + `<button class="btn sm primary" type="button" data-review-reply data-order-id="${esc(item.order_id)}" data-chat-id="${esc(item.chat_id)}">Ответить</button></div>`;
    const action = item.state === 'resolved' ? '<span class="tag ok">закрыт</span>'
      : `<button class="btn sm" type="button" data-review-resolve data-order-id="${esc(item.order_id)}" data-chat-id="${esc(item.chat_id)}">Закрыть</button>`;
    return `<tr><td><b>№${esc(item.number || item.order_id || '')}</b><br><small class="muted">${esc(item.product || '')}</small></td>`
      + `<td>${esc(rating)}</td><td>${esc(item.comment || '—')}${replyBox}</td>`
      + `<td>${stateTag}</td>`
      + `<td class="muted">${esc(dt(item.created_at || item.asked_at))}</td><td>${action}</td></tr>`;
  }).join('') : '<tr><td colspan="6" class="empty">Отзывов ещё нет: вопрос отправляется после фактической выдачи.</td></tr>';
}

function renderChats(items) {
  const host = $('cb_chats');
  if (!host) return;
  const rows = Array.isArray(items) ? items : [];
  host.innerHTML = rows.length ? rows.map((item) => `<tr><td><b>${esc(text(item.name, 'Без имени'))}</b>`
    + `<br><small class="muted">${item.username ? '@' + esc(item.username) : 'без username'}</small></td>`
    + `<td>${esc(item.tg_user_id || item.chat_id || '—')}</td><td>${esc(item.phone || '—')}</td>`
    + `<td class="right">${nfmt(item.orders)}</td>`
    + `<td class="muted">${Number(item.banned) ? '⛔️ заблокирован · ' : ''}${Number(item.marketing_opt_in) ? 'рассылка' : 'только сервис'} · ${Number(item.status_notify) ? 'статусы' : 'без статусов'}</td>`
    + `<td class="muted">${esc(dt(item.last_seen))}</td></tr>`).join('')
    : '<tr><td colspan="6" class="empty">Покупатели ещё не писали.</td></tr>';
}

function renderLog(items) {
  const host = $('cb_log');
  if (!host) return;
  const rows = Array.isArray(items) ? items : [];
  host.innerHTML = rows.length ? rows.slice(0, 80).map((item) => `<tr><td class="muted">${esc(dt(item.at))}</td>`
    + `<td>${esc(text(item.name, item.chat_id || '—'))}</td><td>${esc(item.direction === 'out' ? 'ответ' : item.direction === 'system' ? 'система' : 'входящее')}</td>`
    + `<td>${esc(item.text || '')}</td><td class="muted">${esc(item.answer || item.operator || '')}</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">Диалогов пока нет.</td></tr>';
}

/* ============================================================ клиент-бот */
async function renderBot() {
  const view = $('view-clientbot');
  if (!view) return;
  const seq = ++loadSeq;
  setBotLoading(true);
  try {
    const data = await get('/api/client-bot');
    if (seq !== loadSeq) return;

    $('cb_enabled').checked = !!data.enabled;
    $('cb_catalog').checked = data.catalog !== false;
    $('cb_notify').checked = data.notify !== false;
    $('cb_review').checked = data.review !== false;
    $('cb_pickup_days').value = data.pickup_days ?? 3;
    $('cb_pickup_info').value = data.pickup_info || '';
    $('cb_ready_photo').checked = data.ready_photo !== false;
    $('cb_faq_materials').value = data.faq_materials || '';
    $('cb_faq').value = data.faq || '';
    $('cb_pay_info').value = data.pay_info || '';
    $('cb_pay_qr').value = data.pay_qr || '';
    $('cb_payment_purpose').value = data.payment_purpose || '';
    $('cb_quiet_enabled').checked = !!data.quiet_hours_enabled;
    $('cb_quiet_from').value = data.quiet_from || '22:00';
    $('cb_quiet_to').value = data.quiet_to || '08:00';
    $('cb_marketing_enabled').checked = !!data.marketing_enabled;
    $('cb_track_url').value = data.track_url || '';
    $('cb_welcome').value = data.welcome || '';
    $('cb_token').value = '';
    $('cb_token_hint').textContent = data.has_token
      ? 'Токен сохранён — оставьте поле пустым, чтобы не менять'
      : 'Создайте отдельного бота у @BotFather и вставьте токен';

    const tag = $('cb_state_tag');
    tag.hidden = false;
    if (data.enabled && data.alive) { tag.textContent = 'работает'; tag.className = 'tag ok'; }
    else if (data.enabled) { tag.textContent = 'включён, ждёт Telegram'; tag.className = 'tag warn'; }
    else { tag.textContent = 'выключен'; tag.className = 'tag'; }

    const stats = data.stats || {};
    $('cb_kpis').innerHTML = [
      kpi('Покупателей', nfmt(stats.chats || 0), 'чатов писали боту'),
      kpi('Заказов из бота', nfmt(stats.orders || 0), 'связанных заявок Telegram'),
      kpi('Сообщений сегодня', nfmt(stats.messages_today || 0), `всего ${nfmt(stats.messages || 0)}`),
      kpi('Ожидают действий', nfmt((stats.unread || 0) + (stats.pending_payments || 0)),
        `${nfmt(stats.unread || 0)} диалогов · ${nfmt(stats.pending_payments || 0)} оплаты`,
        (stats.unread || stats.pending_payments) ? 'warn' : 'ok'),
    ].join('');

    $('cb_commands').innerHTML = COMMANDS.map(([command, description]) =>
      `<tr><td><b>${esc(command)}</b></td><td class="muted">${esc(description)}</td></tr>`).join('');
    latestTemplates = Array.isArray(data.templates) ? data.templates : [];
    latestDefaults = Array.isArray(data.default_templates) ? data.default_templates : [];
    renderInbox(data.inbox || [], latestTemplates);
    renderTemplates(latestTemplates);
    renderDefaultTemplates(latestDefaults);
    renderPayments(data.payments || []);
    renderOrders(data.orders || []);
    renderReviews(data.reviews || []);
    renderChats(data.chats || []);
    renderAnalytics(data.analytics || {});
    renderLog(data.log || []);
    setBotLoading(false);
  } catch (error) {
    setBotLoading(false, error && error.message ? error.message : 'Ошибка API клиентского бота');
    fail(error);
  }
}

function botPayload() {
  const payload = {
    client_bot_enabled: $('cb_enabled').checked,
    client_bot_catalog: $('cb_catalog').checked,
    client_bot_notify: $('cb_notify').checked,
    client_bot_review: $('cb_review').checked,
    client_bot_pickup_days: Math.max(0, Math.min(30, Math.round(num($('cb_pickup_days').value, 3)))),
    client_bot_pickup_info: $('cb_pickup_info').value.trim(),
    client_bot_ready_photo: $('cb_ready_photo').checked,
    client_bot_faq_materials: $('cb_faq_materials').value.trim(),
    client_bot_faq: $('cb_faq').value.trim(),
    client_bot_pay_info: $('cb_pay_info').value.trim(),
    client_bot_pay_qr: $('cb_pay_qr').value.trim(),
    client_bot_payment_purpose: $('cb_payment_purpose').value.trim(),
    client_bot_quiet_hours_enabled: $('cb_quiet_enabled').checked,
    client_bot_quiet_from: $('cb_quiet_from').value || '22:00',
    client_bot_quiet_to: $('cb_quiet_to').value || '08:00',
    client_bot_marketing_enabled: $('cb_marketing_enabled').checked,
    client_bot_track_url: $('cb_track_url').value.trim(),
    client_bot_welcome: $('cb_welcome').value.trim(),
  };
  const token = $('cb_token').value.trim();
  if (token) payload.client_bot_token = token;
  return payload;
}

async function saveBot() {
  const button = $('cb_save');
  if (button) { button.disabled = true; button.dataset.oldText = button.textContent; button.textContent = 'Сохраняем…'; }
  try {
    await post('/api/client-bot/save', botPayload());
    toast('Настройки бота сохранены', 'Секрет токена не выводится обратно в панель');
    await renderBot();
  } catch (error) { fail(error); }
  finally { if (button) { button.disabled = false; button.textContent = button.dataset.oldText || 'Сохранить'; } }
}

async function testBot() {
  const button = $('cb_test');
  if (button) { button.disabled = true; button.dataset.oldText = button.textContent; button.textContent = 'Проверяем…'; }
  try {
    const result = await post('/api/client-bot/test', $('cb_token').value.trim()
      ? { client_bot_token: $('cb_token').value.trim() } : {});
    if (result.ok) toast('Токен принят', `Бот @${result.username || 'без username'} — «${result.name || 'без имени'}»`);
    else fail(new Error(result.error || 'Токен не принят'));
  } catch (error) { fail(error); }
  finally { if (button) { button.disabled = false; button.textContent = button.dataset.oldText || 'Проверить токен'; } }
}

function inboxTemplateChange(select) {
  const thread = select.closest('[data-chat-id]');
  const input = thread && thread.querySelector('[data-reply-input]');
  const template = latestTemplates.find((item) => item.id === select.value);
  if (input && template) {
    input.value = template.text || '';
    input.focus();
  }
  select.value = '';
}

async function templateAction(button) {
  const host = button.closest('[data-template-id]');
  try {
    if (button.matches('[data-template-add]')) {
      const name = $('cb_template_new_name')?.value.trim();
      const textValue = $('cb_template_new_text')?.value.trim();
      await post('/api/client-bot/template/save', { name, text: textValue, actor: 'panel' });
      toast('Шаблон добавлен');
    } else if (host && button.matches('[data-template-save]')) {
      await post('/api/client-bot/template/save', {
        id: host.dataset.templateId,
        name: host.querySelector('[data-template-name]')?.value.trim(),
        text: host.querySelector('[data-template-text]')?.value.trim(),
        actor: 'panel',
      });
      toast('Шаблон сохранён');
    } else if (host && button.matches('[data-template-delete]')) {
      if (!window.confirm('Удалить этот шаблон ответа?')) return;
      await post('/api/client-bot/template/delete', { id: host.dataset.templateId, actor: 'panel' });
      toast('Шаблон удалён');
    } else return;
    await renderBot();
  } catch (error) { fail(error); }
}

async function inboxAction(button) {
  const thread = button.closest('[data-chat-id]');
  if (!thread) return;
  const chatId = thread.dataset.chatId;
  try {
    if (button.matches('[data-inbox-reply]')) {
      const input = thread.querySelector('[data-reply-input]');
      const message = input && input.value.trim();
      if (!message) return toast('Пустой ответ', 'Введите сообщение покупателю', 'warn');
      button.disabled = true;
      await post('/api/client-bot/reply', { chat_id: chatId, text: message, request_id: requestId('panel-reply') });
      toast('Ответ поставлен в очередь', 'Доставка повторится автоматически при сбое Telegram');
    } else if (button.matches('[data-inbox-read]')) {
      await post('/api/client-bot/read', { chat_id: chatId });
      toast('Диалог отмечен прочитанным');
    }
    await renderBot();
  } catch (error) { fail(error); }
  finally { button.disabled = false; }
}

async function inboxStatusChange(select) {
  const thread = select.closest('[data-chat-id]');
  if (!thread) return;
  try {
    await post('/api/client-bot/chat/status', { chat_id: thread.dataset.chatId, status: select.value });
    toast('Статус диалога обновлён');
    await renderBot();
  } catch (error) { fail(error); }
}

async function paymentAction(button) {
  const action = button.dataset.paymentAction;
  const id = button.dataset.paymentId;
  if (!id) return;
  if (action === 'confirm' && !window.confirm('Подтвердить оплату и записать платёж в кассу?')) return;
  let reason = '';
  if (action === 'reject') {
    const typed = await ask({
      title: 'Отклонить оплату',
      fields: [{
        name: 'reason', label: 'Причина', type: 'text',
        value: 'Оплата не подтверждена', required: false,
      }],
      ok: 'Отклонить',
    });
    if (typed == null) return;
    reason = String(typed).trim() || 'Оплата не подтверждена';
  }
  try {
    button.disabled = true;
    await post('/api/client-bot/payment', { intent_id: id, action, reason, actor: 'panel' });
    toast(action === 'confirm' ? 'Оплата подтверждена' : 'Оплата отклонена', 'Событие записано в аудит');
    await renderBot();
  } catch (error) { fail(error); }
  finally { button.disabled = false; }
}

async function resolveReview(button) {
  try {
    button.disabled = true;
    await post('/api/client-bot/review/resolve', {
      order_id: button.dataset.orderId,
      chat_id: button.dataset.chatId,
      actor: 'panel',
    });
    toast('Отзыв отмечен обработанным');
    await renderBot();
  } catch (error) { fail(error); }
  finally { button.disabled = false; }
}

async function reviewReply(button) {
  // КБ4: ответ на отзыв уходит покупателю в чат клиентского бота
  const input = document.querySelector(
    `[data-review-reply-text][data-chat-id="${button.dataset.chatId}"][data-order-id="${button.dataset.orderId}"]`);
  const text = input ? input.value.trim() : '';
  if (!text) { toast('Сначала введите текст ответа'); return; }
  try {
    button.disabled = true;
    await post('/api/client-bot/review/reply', {
      order_id: button.dataset.orderId,
      chat_id: button.dataset.chatId,
      text, actor: 'panel',
    });
    toast('Ответ отправлен покупателю');
    await renderBot();
  } catch (error) { fail(error); }
  finally { button.disabled = false; }
}

async function broadcast() {
  const input = $('cb_broadcast_text');
  const value = input && input.value.trim();
  if (!value) return toast('Нет текста', 'Введите сообщение для opt-in клиентов', 'warn');
  if (!$('cb_marketing_enabled').checked) return toast('Рассылки выключены', 'Сначала включите opt-in-рассылки в настройках', 'warn');
  if (!window.confirm('Отправить это сообщение только клиентам с согласием на рассылку?')) return;
  try {
    const result = await post('/api/client-bot/broadcast', {
      text: value, confirmed: true, request_id: requestId('broadcast'), actor: 'panel',
    });
    toast('Рассылка поставлена', `отправлено: ${result.sent || 0}, пропущено из-за тихих часов: ${result.skipped || 0}`);
    input.value = '';
    await renderBot();
  } catch (error) { fail(error); }
}

/* ================================================================= команда */
async function renderStaff() {
  const host = $('set_staff');
  if (!host) return;
  let data;
  try { data = await get('/api/staff'); }
  catch (error) { host.innerHTML = '<p class="muted">Не удалось загрузить команду.</p>'; return; }
  const roles = data.roles || {};
  const rights = (role) => RIGHTS_ORDER.filter((right) => ((roles[role] || {}).rights || []).includes(right))
    .map((right) => ROLE_RIGHTS_LABEL[right]).join(' · ') || '—';
  const rows = (data.staff || []).map((member) => `<div class="set-row" data-staff-id="${esc(member.id)}">`
    + `<div class="sinfo"><b>${esc(member.name || 'Без имени')}${Number(member.active) ? '' : ' · отключён'}</b>`
    + `<small>${esc(member.role_name || member.role || '')} · chat_id ${esc(member.chat_id || '')}${member.note ? ' · ' + esc(member.note) : ''}<br>Права: ${esc(rights(member.role))}</small></div>`
    + `<div class="btn-grid">${Number(member.active)
      ? `<button class="btn sm" type="button" data-staff-off="${esc(member.id)}">Отключить</button>`
      : `<button class="btn sm" type="button" data-staff-on="${esc(member.id)}">Вернуть</button>`}</div></div>`).join('');
  const invites = (data.invites || []).filter((item) => !Number(item.used));
  const inviteRows = invites.length ? invites.map((item) => `<div class="set-row"><div class="sinfo"><b>${esc(item.code)}</b>`
    + `<small>${esc(item.role_name || item.role || '')}${item.name ? ' · ' + esc(item.name) : ''} — напишите боту: старт ${esc(item.code)}</small></div>`
    + `<button class="btn sm" type="button" data-invite-del="${esc(item.code)}">✕</button></div>`).join('')
    : '<p class="muted" style="font-size:12.5px">Активных кодов нет. Код одноразовый и не хранит пароль.</p>';
  host.innerHTML = `<div class="notice"><span>ℹ</span><span>Владелец — Chat ID из настроек: <b>${data.owner_chat ? esc(data.owner_chat) : 'не задан'}</b>. Права разделены по ролям.</span></div>`
    + (rows || '<p class="muted" style="font-size:12.5px">Команда пуста — добавьте сотрудника или руководителя.</p>')
    + `<div class="card-head" style="margin-top:14px"><div><h3>Добавить участника</h3><p>chat_id человек узнает командой «код» в рабочем боте</p></div></div>`
    + `<div class="set-row"><div class="sinfo"><b>Имя</b><small>Как показывать в списке</small></div><input type="text" id="staff_name" placeholder="Ваня" style="max-width:150px">`
    + `<select id="staff_role"><option value="employee">сотрудник</option><option value="manager">руководитель</option></select><input type="text" id="staff_chat" placeholder="chat_id" style="max-width:130px">`
    + `<button class="btn sm primary" type="button" id="staff_add">Добавить</button></div>`
    + `<div class="card-head" style="margin-top:14px"><div><h3>Приглашения</h3><p>Код вместо ручного ввода chat_id</p></div><button class="btn sm" type="button" id="staff_invite_make">+ Код для сотрудника</button></div>${inviteRows}`;
}

function bindStaff() {
  const host = $('set_staff');
  if (!host || host.dataset.bound) return;
  host.dataset.bound = '1';
  host.addEventListener('click', async (event) => {
    const target = event.target;
    try {
      if (target.closest('#staff_add')) {
        await post('/api/staff/save', { name: $('staff_name').value.trim(), role: $('staff_role').value, chat_id: $('staff_chat').value.trim() });
        toast('Участник добавлен', 'Права применятся сразу'); renderStaff(); return;
      }
      if (target.closest('#staff_invite_make')) {
        const result = await post('/api/staff/invite', { role: 'employee' });
        toast(`Код ${result.invite.code}`, `Напишите рабочему боту: старт ${result.invite.code}`); renderStaff(); return;
      }
      const off = target.closest('[data-staff-off]');
      if (off) { await post('/api/staff/delete', { id: off.dataset.staffOff }); renderStaff(); return; }
      const on = target.closest('[data-staff-on]');
      if (on) { await post('/api/staff/restore', { id: on.dataset.staffOn }); renderStaff(); return; }
      const del = target.closest('[data-invite-del]');
      if (del) { await post('/api/staff/invite/delete', { code: del.dataset.inviteDel }); renderStaff(); }
    } catch (error) { fail(error); }
  });
}

function bind() {
  if (bound) return;
  bound = true;
  $('cb_save')?.addEventListener('click', saveBot);
  $('cb_test')?.addEventListener('click', testBot);
  $('cb_refresh')?.addEventListener('click', renderBot);
  $('cb_error_retry')?.addEventListener('click', renderBot);
  $('cb_broadcast')?.addEventListener('click', broadcast);
  $('cb_inbox')?.addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (button && (button.matches('[data-inbox-reply]') || button.matches('[data-inbox-read]'))) inboxAction(button);
    if (button && button.matches('[data-quick-reply]')) quickReplyAction(button);
  });
  $('cb_default_templates')?.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-template-use]');
    if (button) defaultTemplateUse(button);
  });
  $('cb_inbox')?.addEventListener('change', (event) => {
    if (event.target.matches('[data-inbox-status]')) inboxStatusChange(event.target);
    if (event.target.matches('[data-inbox-template]')) inboxTemplateChange(event.target);
  });
  $('cb_templates')?.addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (button) templateAction(button);
  });
  $('cb_payments')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-payment-action]');
    if (button) paymentAction(button);
  });
  $('cb_reviews')?.addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (button && button.matches('[data-review-resolve]')) resolveReview(button);
    if (button && button.matches('[data-review-reply]')) reviewReply(button);
  });
  $('cb_reviews')?.addEventListener('keyup', (event) => {
    if (event.key !== 'Enter' || !event.target.matches('[data-review-reply-text]')) return;
    const chatId = event.target.dataset.chatId;
    const orderId = event.target.dataset.orderId;
    const button = document.querySelector(`[data-review-reply][data-chat-id="${chatId}"][data-order-id="${orderId}"]`);
    if (button) reviewReply(button);
  });
  bindStaff();
}

PF.on('ready', () => {
  bind();
  if ($('view-clientbot')?.classList.contains('on')) renderBot();
});
PF.on('view', (detail) => {
  if (detail && detail.view === 'clientbot') renderBot();
  if (detail && detail.view === 'settings') renderStaff();
});
PF.on('bootstrap', () => { if ($('view-clientbot')?.classList.contains('on')) renderBot(); });
PF.modules.clientbot = { renderBot, renderStaff, saveBot, testBot };
})();
