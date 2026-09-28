#!/usr/bin/env node
/* Focused regression check for the lazy-loaded client bot inbox renderer. */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../site/assets/clientbot.js'), 'utf8');
const from = source.indexOf('function renderInbox(items, templates = []) {');
const to = source.indexOf('\nfunction quickReplies()', from);
assert.ok(from >= 0 && to > from, 'renderInbox source is present');
const renderInbox = source.slice(from, to);

const host = { innerHTML: '' };
const logHost = { innerHTML: '' };
const badge = { textContent: '', hidden: true, className: '' };
const tabBadge = { textContent: '', hidden: true, className: '' };
const context = vm.createContext({
  Array, Date, Map, String, Math,
  PF: { ui: {
    $: id => id === 'cb_inbox' ? host : id === 'cb_log' ? logHost : id === 'cb_unread_badge' ? badge : null,
    $$: selector => {
      assert.equal(selector, '[data-tab-badge="clientbot"]');
      return [tabBadge];
    },
  } },
  esc: value => String(value ?? '').replace(/[&<>"']/g, ''),
  nfmt: value => String(value),
  text: (value, fallback = '—') => String(value || fallback),
  dt: value => String(value || '—'),
  dateText: value => String(value),
  avColor: () => 'blue', initials: () => 'П', avatarEmoji: () => '👤',
  inboxStatus: value => String(value),
  quickReplies: () => [],
});

vm.runInContext("const { $, $$ } = PF.ui;\n" + renderInbox, context);
vm.runInContext('renderInbox([])', context);
assert.equal(tabBadge.hidden, true, 'empty inbox hides unread badge');
assert.match(host.innerHTML, /Непрочитанных сообщений нет/);

vm.runInContext("renderInbox([{chat_id: '42', name: 'Покупатель', text: 'Здравствуйте'}])", context);
assert.equal(badge.textContent, '1');
assert.equal(tabBadge.textContent, '1');
assert.equal(tabBadge.hidden, false);
assert.match(host.innerHTML, /Покупатель/);
assert.match(host.innerHTML, /Здравствуйте/);

const logFrom = source.indexOf('function renderLog(items) {');
const logTo = source.indexOf('\n/* Н55:', logFrom);
assert.ok(logFrom >= 0 && logTo > logFrom, 'renderLog source is present');
assert.match(source, /const U = PF\.ui;/, 'avatar helper namespace is imported');
vm.runInContext("const U = { avColor, initials, avatarEmoji };\n" + source.slice(logFrom, logTo), context);
vm.runInContext("renderLog([{chat_id:'42', name:'Покупатель', text:'Проверка'}])", context);
assert.match(logHost.innerHTML, /--av:blue/);
assert.match(logHost.innerHTML, /Проверка/);

console.log('Client bot: inbox, unread badges and dialog avatars — ok.');
