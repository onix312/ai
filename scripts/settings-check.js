#!/usr/bin/env node
// Regression checks for the real settings handlers, without a running printer/server.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../site/assets/app.js'), 'utf8');
const section = (from, to) => source.slice(source.indexOf(from), source.indexOf(to, source.indexOf(from)));
const field = (key, value, type = 'text') => ({ dataset: { setting: key }, value, type, tagName: 'INPUT' });

async function main() {
  const host = {};
  const button = { textContent: 'Сохранить', disabled: false };
  let fields = [field('labor_rate', '100', 'number'), field('theme', 'dark')];
  let calls = [], errors = [], messages = [], reject = false, finish;
  const context = vm.createContext({
    Map, JSON, Number, isNaN,
    $: id => id === 'view-settings' ? host : id === 'settings_save' ? button : null,
    $$: (selector, root) => { assert.equal(root, host, 'only settings-page fields'); return fields; },
    num: Number,
    PF: { state: { settings: { theme: 'light' } }, setSettings() {}, applyTheme() {}, refreshFinance() {}, refreshCore() {} },
    renderSettings() {},
    toast: (...args) => messages.push(args), fail: e => errors.push(e),
    post: async (url, payload) => {
      assert.equal(url, '/api/settings'); calls.push(payload);
      if (reject) throw new Error('offline');
      if (finish) await new Promise(resolve => { finish.resolve = resolve; });
      return { settings: payload, warnings: ['Проверьте допуски'] };
    },
  });
  vm.runInContext(section('const settingsDraft = new Map();', 'let schemaCache = null;')
    + section('async function saveSettings() {', '/* ============================================================== бэкап */'), context);
  const run = code => vm.runInContext(code, context);

  run("settingsDraft.set('labor_rate', { value: '250' }); restoreSettingsDraft();");
  assert.equal(fields[0].value, '250');
  // Re-render replaces DOM nodes with old server values.
  fields = [field('labor_rate', '100', 'number'), field('theme', 'dark')];
  await run('saveSettings()');
  assert.equal(calls[0].labor_rate, 250);
  assert.equal(calls[0].theme, 'dark');
  assert.equal(run('settingsDraft.size'), 0);
  assert.match(messages[0][1], /Проверьте допуски/);
  assert.equal(button.disabled, false);

  reject = true;
  run("settingsDraft.set('labor_rate', { value: '300' });");
  await run('saveSettings()');
  assert.equal(errors.length, 1);
  assert.equal(run('settingsDraft.size'), 1, 'failure keeps the draft');
  assert.equal(button.textContent, 'Сохранить');
  reject = false;

  finish = {};
  const pending = run('saveSettings()');
  assert.equal(button.disabled, true);
  const count = calls.length;
  await run('saveSettings()');
  assert.equal(calls.length, count, 'double click sends only one request');
  run("settingsDraft.set('labor_rate', { value: '400' });");
  finish.resolve();
  await pending;
  assert.equal(run("settingsDraft.get('labor_rate').value"), '400', 'edits during request survive');
  assert.equal(button.disabled, false);
  finish = null;

  run('settingsDraft.clear()');
  const secret = field('telegram_token', ''); secret.dataset.secret = '1';
  fields = [secret, field('telegram_enabled', '', 'checkbox')];
  fields[1].checked = true;
  await run('saveSettings()');
  assert.equal(calls.at(-1).telegram_enabled, true);
  assert.equal('telegram_token' in calls.at(-1), false);

  fields = [field('bank_rules', '{broken')]; fields[0].dataset.json = '1';
  const before = calls.length;
  await run('saveSettings()');
  assert.equal(calls.length, before, 'invalid JSON must not be sent');
  console.log('Settings: draft, theme, scoped payload, retry, double click, concurrent edits, secrets, JSON — ok.');
}
main().catch(e => { console.error(e); process.exitCode = 1; });
