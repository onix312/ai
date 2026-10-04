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

function styleAssistantFrame() {
  if (!frame) return;
  try {
    const doc = frame.contentDocument;
    if (!doc || !doc.documentElement.classList.contains('pf-ai-embed') || doc.getElementById('nozza-rail-v2')) return;
    const style = doc.createElement('style');
    style.id = 'nozza-rail-v2';
    style.textContent = `
      html.pf-ai-embed,html.pf-ai-embed body,html.pf-ai-embed .as-app,html.pf-ai-embed .as-main,
      html.pf-ai-embed .as-pane,html.pf-ai-embed .as-chat,html.pf-ai-embed .as-conv,
      html.pf-ai-embed .as-log { background:transparent!important; }
      html.pf-ai-embed .as-log { padding:14px 12px 10px!important; }
      html.pf-ai-embed .msg.hello .bubble { background:rgba(44,35,46,.88)!important;border:1px solid rgba(240,163,104,.48);border-radius:12px!important; }
      html.pf-ai-embed .msg:not(.hello) .bubble { background:rgba(35,28,38,.92)!important; }
      html.pf-ai-embed .as-composer { background:rgba(35,28,38,.94)!important;border:1px solid rgba(240,163,104,.48)!important;border-radius:12px!important; }
      html.pf-ai-embed .chip { background:rgba(181,43,255,.17)!important;border-color:rgba(206,139,255,.42)!important;color:#E7CEFF!important; }
      html.pf-ai-embed .as-toast { display:none!important; }
    `;
    doc.head.appendChild(style);
  } catch (e) {}
}
if (frame) frame.addEventListener('load', styleAssistantFrame);

function setOpen(open, focus = true) {
  const visible = !!open;
  const desktopRail = window.matchMedia('(min-width: 1381px)').matches;
  const collapsed = desktopRail && !visible;
  body.classList.toggle('pf-ai-open', !!open);
  body.classList.toggle('pf-ai-collapsed', collapsed);
  rail.setAttribute('aria-hidden', visible ? 'false' : 'true');
  trigger.setAttribute('aria-expanded', visible ? 'true' : 'false');
  if (nav) nav.setAttribute('aria-expanded', visible ? 'true' : 'false');
  try { localStorage.setItem('pf_v19_ai_open', open ? '1' : '0'); } catch (e) {}
  if (visible && frame && !frame.getAttribute('src')) {
    frame.setAttribute('src', frame.dataset.src || '/assistant.html?embed=1&v=2');
  }
  if (visible) window.setTimeout(styleAssistantFrame, 50);
  window.dispatchEvent(new CustomEvent('pf-ai-rail-state', { detail: { open: visible } }));
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

if (window.matchMedia('(min-width: 1381px)').matches) setOpen(false, false);
else {
  let restore = false;
  try { restore = localStorage.getItem('pf_v19_ai_open') === '1'; } catch (e) {}
  if (restore && window.innerWidth > 900) setOpen(true, false);
}

window.matchMedia('(min-width: 1381px)').addEventListener('change', (event) => {
  if (event.matches) {
    body.classList.remove('pf-ai-collapsed');
    setOpen(true, false);
  } else {
    body.classList.remove('pf-ai-open', 'pf-ai-collapsed');
    rail.setAttribute('aria-hidden', 'true');
    if (!body.classList.contains('pf-ai-open')) trigger.setAttribute('aria-expanded', 'false');
  }
});
})();
