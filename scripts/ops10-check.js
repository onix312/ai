#!/usr/bin/env node
'use strict';

/* Regression for nested PF.html templates in the shift center. */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'site', 'assets', 'ops10.js'), 'utf8');
const hosts = new Map();
const host = (id) => {
  if (!hosts.has(id)) hosts.set(id, {
    innerHTML: '', addEventListener() {}, querySelectorAll() { return []; },
  });
  return hosts.get(id);
};
const esc = (value) => String(value).replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));
const raw = (value) => ({ __raw: true, raw: String(value == null ? '' : value) });
const safe = (value) => value == null || value === false ? ''
  : value && value.__raw ? value.raw : Array.isArray(value) ? value.map(safe).join('') : esc(value);
const html = (parts, ...values) => parts.reduce((out, part, i) => out + part + (i < values.length ? safe(values[i]) : ''), '');
const responses = {
  '/api/ops10/overview': {
    pipeline: [],
    inbox: [{ chat_id: '42', name: 'Покупатель', text: 'Нужна деталь', unread: true, pipeline_stage: 'new' }],
    printers: [{ name: 'P1S', connection: { connected: true }, printer: { progress: 50 },
      ams: { trays: [{ active: true, material: 'PLA', color: 'черный', remain: 12 }] } }],
    rules: [], rule_runs: [],
  },
  '/api/ops10/production': { planfact: {}, queue: [] },
};
const PF = {
  ui: {
    $: host, esc, num: (v) => Number(v) || 0, toast() {}, fail(e) { throw e; },
    debounce: (fn) => fn, emptyHtml: () => '', html, raw, render: (el, markup) => { el.innerHTML = String(markup); },
    fmt: { stamp: () => 'сейчас', grams: (v) => String(v || 0) },
  },
  api: { get: async (url) => responses[url], post: async () => ({}) },
  module(_name, init) { init(); }, on() {}, viewOn: () => true,
};
vm.runInNewContext(source, { PF, location: { hash: '#ops10' }, console, setTimeout, clearTimeout });
setImmediate(() => {
  const inbox = host('ops10_inbox').innerHTML;
  const printers = host('ops10_printers').innerHTML;
  if (!inbox.includes('<option value="new" selected>Новое</option>') || inbox.includes('&lt;option')) {
    throw new Error('Inbox stage options were escaped instead of rendered as markup');
  }
  if (!printers.includes('<span class="tag">PLA черный · 12%</span>') || printers.includes('&lt;span')) {
    throw new Error('AMS chips were escaped instead of rendered as markup');
  }
  console.log('Центр смены: стадии обращения и AMS-чипы отрисованы корректно.');
});
