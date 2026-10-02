/* PrintFlow 19 shell controls: integrated Nozza rail over the local Luma core. */
(() => {
'use strict';

const $ = (id) => document.getElementById(id);
const body = document.body;
const rail = $('pf_ai_rail');
const scrim = $('pf_ai_scrim');
const trigger = $('pf_ai_open');
const nav = $('pf_ai_nav');
const close = $('pf_ai_close');
const frame = $('pf_ai_frame');
const stateText = $('pf_ai_state');

if (!rail || !trigger) return;

function setOpen(open, focus = true) {
  body.classList.toggle('pf-ai-open', !!open);
  rail.setAttribute('aria-hidden', open ? 'false' : 'true');
  trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (nav) nav.setAttribute('aria-expanded', open ? 'true' : 'false');
  try { localStorage.setItem('pf_v19_ai_open', open ? '1' : '0'); } catch (e) {}
  if (open && frame && !frame.getAttribute('src')) {
    frame.setAttribute('src', frame.dataset.src || '/assistant.html?embed=1');
  }
  if (open && focus && frame) {
    window.setTimeout(() => {
      try { frame.contentWindow && frame.contentWindow.focus(); } catch (e) {}
    }, 180);
  }
}

function toggle() {
  setOpen(!body.classList.contains('pf-ai-open'));
}

async function syncAssistantStatus() {
  if (!window.PF || !PF.api || !PF.api.get) return;
  try {
    const data = await PF.api.get('/api/assistant/status');
    trigger.classList.toggle('ready', !!data.available);
    trigger.classList.toggle('warn', !data.available);
    const model = String(data.model || '').trim();
    const reason = String(data.reason || '').trim();
    if (stateText) {
      stateText.textContent = data.available
        ? (model ? 'локальный · ' + model : 'локальный · готов')
        : (reason || 'локальный AI недоступен');
    }
    trigger.title = data.available
      ? 'Nozza готова' + (model ? ' · ' + model : '')
      : (reason || 'Nozza недоступна');
  } catch (e) {
    trigger.classList.remove('ready');
    trigger.classList.add('warn');
    if (stateText) stateText.textContent = 'нет связи с AI';
  }
}

trigger.addEventListener('click', toggle);
['system_map_ai', 'system_map_ai_bottom'].forEach((id) => {
  const button = document.getElementById(id);
  if (button) button.addEventListener('click', () => setOpen(true));
});
if (nav) nav.addEventListener('click', (event) => {
  event.preventDefault();
  setOpen(true);
});
if (close) close.addEventListener('click', () => setOpen(false));
if (scrim) scrim.addEventListener('click', () => setOpen(false));

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && body.classList.contains('pf-ai-open')) {
    setOpen(false);
    return;
  }
  if (event.altKey && String(event.key || '').toLowerCase() === 'a') {
    event.preventDefault();
    toggle();
  }
});

if (window.PF && PF.on) {
  PF.on('ready', syncAssistantStatus);
  PF.on('view', () => {
    if (body.classList.contains('pf-ai-open')) syncAssistantStatus();
  });
}
syncAssistantStatus();
window.setInterval(syncAssistantStatus, 30000);

let restore = false;
try { restore = localStorage.getItem('pf_v19_ai_open') === '1'; } catch (e) {}
if (restore && window.innerWidth > 1180) setOpen(true, false);
})();
