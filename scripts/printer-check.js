#!/usr/bin/env node
'use strict';
const assert = require('assert/strict');
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const root = path.join(__dirname, '..');
const printer = fs.readFileSync(path.join(root, 'site/assets/printer.js'), 'utf8');
const app = fs.readFileSync(path.join(root, 'site/assets/app.js'), 'utf8');
const nodes = new Map();
const node = (id) => {
  if (!nodes.has(id)) nodes.set(id, {
    hidden: false, textContent: '', innerHTML: '', dataset: {}, open: false,
    classList: { toggle() {} }, removeAttribute() {},
    parentElement: { querySelector: node },
  });
  return nodes.get(id);
};
const context = vm.createContext({
  $: node, num: (x) => Number(x) || 0, esc: (x) => String(x), encodeURIComponent, Date,
  text: (id, value) => { node(id).textContent = value; },
  renderShots() {}, camStream: '',
});
function include(source, start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from + start.length);
  assert(from >= 0 && to > from, start);
  vm.runInContext(source.slice(from, to), context);
}
include(printer, 'function traySlotNum(', 'function trayPresent(');
include(printer, 'function renderTopPill(', 'function renderLive(');
include(printer, 'function camUrl(', '/* --------------------------- 119:');
include(app, 'function refreshHeroCamera(', 'function renderActivePrint(');
assert.equal(context.traySlotNum({ unit: 255, slot: 254 }), 254);
assert.equal(context.traySlotNum({ unit: 1, slot: 2 }), 6);
context.active = () => ({ name: 'P1S', connection: { connected: true },
  printer: { state: 'RUNNING', state_label: 'Печать', progress: 63 } });
context.STATE_KIND = { RUNNING: 'running' };
context.STATE_LABEL = { RUNNING: 'Печать' };
context.renderTopPill();
assert.equal(node('live_state').textContent, 'Печать · 63%');
assert.equal(node('live_pill').hidden, false);
for (const camera of [
  { available: false, error: 'timeout' },
  { available: true, age: 0, demo: true },
  { available: true, age: 60 },
  { available: true, age: 0, error: 'timeout' },
]) {
  context.renderCamera({ id: 'p1', camera, connection: {} });
  assert.equal(node('.cam-live').hidden, true);
  assert.notEqual(node('pr_cam_status').textContent, 'Прямой эфир');
}
context.renderCamera({ id: 'p1', camera: { available: true, age: 1 }, connection: {} });
assert.equal(node('.cam-live').hidden, false);
assert.match(node('pr_cam').src, /camera\.jpg\?printer_id=p1/);
const host = { querySelector: node };
context.refreshHeroCamera(host, { id: 'p1', camera: { available: true, age: 0, demo: true } });
assert.equal(node('.hero-cam-badge').textContent, 'ДЕМО');
context.refreshHeroCamera(host, { id: 'p1', camera: { available: true, age: 30 } });
assert.equal(node('.hero-cam-badge').textContent, 'ПОСЛЕДНИЙ КАДР');
const filter = app.slice(app.indexOf('function renderActivePrint(')).match(/const queuedJobs = (queue\.filter\([^\n]+);/);
assert(filter);
const pending = new Function('queue', 'p', 'return ' + filter[1]);
assert.deepEqual(pending([
  { id: 'busy', state: 'running', printer_id: 'p1' },
  { id: 'other', state: 'queued', printer_id: 'p2' },
  { id: 'own', state: 'queued', printer_id: 'p1' },
  { id: 'free', state: 'queued' },
], { id: 'p1' }).map((j) => j.id), ['own', 'free']);
assert.match(printer, /data-ams-load="\$\{esc\(String\(slotNum\)\)\}"/);
console.log('Принтеры: состояния камеры, JPEG, нумерация AMS и выбор очереди — OK');
