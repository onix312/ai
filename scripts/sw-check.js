#!/usr/bin/env node
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const handlers = {};
const stores = new Map([['printflow-shell-v104', new Map([['/index.html', 'old']])], ['other-cache', new Map()]]);
let failInstall = false;
const cache = (name) => ({
  add: async (url) => { if (failInstall && url === '/index.html') throw new Error('offline'); stores.get(name).set(url, `new:${url}`); },
  match: async (url) => stores.get(name).get(typeof url === 'string' ? url : new URL(url.url).pathname),
  put: async (url, response) => { stores.get(name).set(url.url, response); },
});
const sandbox = {
  self: {
    location: { origin: 'http://localhost' },
    clients: { claim: async () => {} },
    skipWaiting: async () => {},
    addEventListener: (name, fn) => { handlers[name] = fn; },
  },
  caches: {
    open: async (name) => { if (!stores.has(name)) stores.set(name, new Map()); return cache(name); },
    keys: async () => [...stores.keys()],
    delete: async (name) => stores.delete(name),
  },
  URL,
  fetch: async () => { throw new Error('offline'); },
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../site/sw.js'), 'utf8'), sandbox);
function dispatch(name, request) {
  let promise;
  const event = { request, waitUntil: (p) => { promise = p; }, respondWith: (p) => { promise = p; } };
  handlers[name](event);
  return promise;
}
(async () => {
  failInstall = true;
  await assert.rejects(dispatch('install'));
  assert.equal(stores.has('printflow-shell-v104'), true, 'failed install must preserve old shell');
  failInstall = false;
  await dispatch('install');
  assert.equal(stores.has('printflow-shell-v104'), true, 'old cache must survive install');
  await dispatch('activate');
  assert.equal(stores.has('printflow-shell-v104'), false, 'old shell removed after activation');
  assert.equal(stores.has('other-cache'), true, 'unrelated cache preserved');
  const request = { method: 'GET', url: 'http://localhost/assets/refine.css?v=18.23.1', mode: 'no-cors' };
  assert.equal(await dispatch('fetch', request), 'new:/assets/refine.css', 'versioned URL resolves to offline shell');
  let intercepted = false;
  handlers.fetch({ request: { method: 'GET', url: 'http://localhost/api/orders' }, respondWith: () => { intercepted = true; } });
  assert.equal(intercepted, false, 'live API must not be cached');
  console.log('Service worker: old/new cache, offline versioned CSS and live API — OK');
})().catch((error) => { console.error(error); process.exitCode = 1; });
