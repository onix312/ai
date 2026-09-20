/* Звуки панели (18.11): короткие синтезированные сигналы Web Audio — без
 * аудиофайлов, без задержки на загрузку. window.PFSound.
 *
 * Зачем: оператор у станка чаще слышит панель, чем смотрит на неё. Клик
 * подтверждает, что команда ушла; «печать завершена» и «тревога» слышны
 * из другого конца цеха. Настройки — на устройство (localStorage), а не на
 * ферму: у телевизора в цехе и у ноутбука бухгалтера разные потребности.
 *
 * Категории: «интерфейс» (клик, переключатель, щелчок регулятора) и
 * «события» (готово, печать завершена, внимание, тревога). Тихие часы —
 * события звучат вполголоса, интерфейс молчит.
 */
(function () {
  'use strict';
  const KEY = 'pf_sound';
  const DEFAULTS = { enabled: true, ui: true, events: true, volume: 60, quiet: false, quiet_from: 22, quiet_to: 7 };
  const SOUNDS = {
    click:   { label: 'Клик',                 sub: 'команда отправлена на станок',      cat: 'ui' },
    toggle:  { label: 'Переключатель',        sub: 'настройка включена или выключена',  cat: 'ui' },
    tick:    { label: 'Щелчок регулятора',    sub: 'деление регулятора скорости',        cat: 'ui' },
    success: { label: 'Готово',               sub: 'станок подтвердил команду',          cat: 'events' },
    finish:  { label: 'Печать завершена',     sub: 'деталь готова, можно снимать',       cat: 'events' },
    warn:    { label: 'Внимание',             sub: 'пауза, мало филамента, обслуживание', cat: 'events' },
    alert:   { label: 'Тревога',              sub: 'ошибка печати, стоп, обрыв связи',    cat: 'events' },
  };
  // Событие журнала → сигнал. Остальные виды молчат.
  const EVENT_SOUND = {
    complete: 'finish', error: 'alert', guard: 'alert', defect: 'alert', loss: 'alert',
    security: 'alert', filament_low: 'warn', maintenance: 'warn', pause: 'warn', dry: 'warn',
  };

  let prefs = load();
  let ctx = null;
  let unlocked = false;
  let lastAt = {};

  function load() {
    try {
      const raw = localStorage.getItem(KEY);
      const saved = raw ? JSON.parse(raw) : {};
      return Object.assign({}, DEFAULTS, saved && typeof saved === 'object' ? saved : {});
    } catch (e) { return Object.assign({}, DEFAULTS); }
  }
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch (e) { /* приватный режим */ }
    document.dispatchEvent(new CustomEvent('pf:sound', { detail: Object.assign({}, prefs) }));
    syncButton();
  }
  function audio() {
    if (ctx) return ctx;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    try { ctx = new Ctx(); } catch (e) { ctx = null; }
    return ctx;
  }
  // Браузер разрешает звук только после жеста пользователя — поднимаем
  // контекст на первом касании и больше не трогаем.
  function unlock() {
    if (unlocked) return;
    const c = audio();
    if (!c) return;
    unlocked = true;
    if (c.state === 'suspended') c.resume().catch(() => {});
  }
  document.addEventListener('pointerdown', unlock, { passive: true, capture: true });
  document.addEventListener('keydown', unlock, { capture: true });

  function quietNow() {
    if (!prefs.quiet) return false;
    const h = new Date().getHours();
    const from = Number(prefs.quiet_from), to = Number(prefs.quiet_to);
    return from > to ? (h >= from || h < to) : (h >= from && h < to);
  }
  function gainFor(cat) {
    if (!prefs.enabled) return 0;
    if (cat === 'ui' && (!prefs.ui || quietNow())) return 0;
    if (cat === 'events' && !prefs.events) return 0;
    const v = Math.max(0, Math.min(100, Number(prefs.volume) || 0)) / 100;
    return (quietNow() ? 0.35 : 1) * v * v; // квадрат — громкость воспринимается логарифмически
  }

  /* Один тон: осциллятор + огибающая. */
  function tone(c, t0, freq, dur, vol, type, glide) {
    const osc = c.createOscillator();
    const g = c.createGain();
    osc.type = type || 'sine';
    osc.frequency.setValueAtTime(freq, t0);
    if (glide) osc.frequency.exponentialRampToValueAtTime(glide, t0 + dur);
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(vol, t0 + 0.006);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    osc.connect(g).connect(c.destination);
    osc.start(t0);
    osc.stop(t0 + dur + 0.02);
  }
  function noise(c, t0, dur, vol) {
    const n = Math.floor(c.sampleRate * dur);
    const buf = c.createBuffer(1, n, c.sampleRate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < n; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / n);
    const src = c.createBufferSource();
    src.buffer = buf;
    const g = c.createGain();
    g.gain.value = vol;
    const f = c.createBiquadFilter();
    f.type = 'highpass';
    f.frequency.value = 1800;
    src.connect(f).connect(g).connect(c.destination);
    src.start(t0);
  }
  const PATTERNS = {
    click:   (c, t, v) => { noise(c, t, 0.025, v * 0.5); tone(c, t, 1500, 0.045, v * 0.35, 'triangle'); },
    toggle:  (c, t, v) => { tone(c, t, 880, 0.05, v * 0.4, 'triangle'); tone(c, t + 0.06, 1175, 0.07, v * 0.4, 'triangle'); },
    tick:    (c, t, v) => { noise(c, t, 0.018, v * 0.35); tone(c, t, 2400, 0.02, v * 0.25, 'square'); },
    success: (c, t, v) => { tone(c, t, 660, 0.09, v * 0.45); tone(c, t + 0.1, 990, 0.16, v * 0.45); },
    finish:  (c, t, v) => { tone(c, t, 784, 0.14, v * 0.5); tone(c, t + 0.16, 988, 0.14, v * 0.5); tone(c, t + 0.32, 1319, 0.34, v * 0.55); },
    warn:    (c, t, v) => { tone(c, t, 520, 0.16, v * 0.5, 'triangle'); tone(c, t + 0.22, 520, 0.16, v * 0.5, 'triangle'); },
    alert:   (c, t, v) => { for (let i = 0; i < 3; i++) tone(c, t + i * 0.19, 980, 0.12, v * 0.6, 'sawtooth', 620); },
  };

  function play(name, opts) {
    const spec = SOUNDS[name];
    if (!spec) return false;
    const force = opts && opts.force;
    const vol = force ? Math.max(0.15, Math.max(0, Math.min(100, Number(prefs.volume) || 60)) / 100) : gainFor(spec.cat);
    if (vol <= 0) return false;
    // Дребезг: один и тот же сигнал не чаще 8 раз в секунду (клики по списку).
    const now = Date.now();
    if (!force && lastAt[name] && now - lastAt[name] < 120) return false;
    lastAt[name] = now;
    const c = audio();
    if (!c) return false;
    if (c.state === 'suspended') { c.resume().catch(() => {}); if (c.state === 'suspended' && !force) return false; }
    try { PATTERNS[name](c, c.currentTime + 0.01, vol); } catch (e) { return false; }
    return true;
  }

  function set(patch) {
    prefs = Object.assign({}, prefs, patch || {});
    save();
    return prefs;
  }
  function toggle() {
    set({ enabled: !prefs.enabled });
    if (prefs.enabled) play('toggle', { force: true });
    return prefs.enabled;
  }
  let lastEventSoundAt = 0;
  function onEvent(row) {
    if (!row || !row.kind) return;
    const name = EVENT_SOUND[row.kind];
    if (name && play(name)) lastEventSoundAt = Date.now();
  }
  /* Тост об ошибке (сбой запроса, отказ станка) — «внимание», если только
   * что не прозвучал сигнал самого события: две сирены подряд не нужны. */
  function onToast(detail) {
    if (!detail || detail.kind !== 'bad') return;
    if (Date.now() - lastEventSoundAt < 1500) return;
    play('warn');
  }
  document.addEventListener('pf:toast', (e) => onToast(e.detail));

  /* ---- кнопка в шапке ---- */
  function syncButton() {
    const btn = document.getElementById('sound_btn');
    if (!btn) return;
    const on = !!prefs.enabled;
    btn.classList.toggle('on', on);
    btn.classList.toggle('muted', !on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.title = on ? 'Звуки панели включены — выключить (настройки: Система → Звуки панели)' : 'Звуки панели выключены — включить';
    const glyph = on ? 'volume' : 'volumeoff';
    if (window.PFIcons && PFIcons.svg) btn.innerHTML = `<span class="ic">${PFIcons.svg(glyph)}</span>`;
    else btn.textContent = on ? '🔔' : '🔕';
    if (on && quietNow()) btn.title += ' · сейчас тихие часы';
  }

  /* ---- карточка в настройках ---- */
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m])); }
  function renderCard(el) {
    if (!el) return;
    const p = prefs;
    const sw = (key, label, sub) => `<div class="set-row"><div class="sinfo"><b>${esc(label)}</b><small>${esc(sub)}</small></div>` +
      `<label class="switch"><input type="checkbox" data-sound-pref="${key}"${p[key] ? ' checked' : ''}><i></i></label></div>`;
    const list = Object.keys(SOUNDS).map((k) => `<div class="snd-row"><span class="snd-dot ${SOUNDS[k].cat}"></span><span><b>${esc(SOUNDS[k].label)}</b><small>${esc(SOUNDS[k].sub)}</small></span>` +
      `<button class="btn sm ghost" type="button" data-sound-play="${k}" title="Прослушать">▶ Прослушать</button></div>`).join('');
    const supported = !!(window.AudioContext || window.webkitAudioContext);
    el.innerHTML = (supported ? '' : '<div class="notice warn"><span>!</span><span>Этот браузер не поддерживает Web Audio — звуков не будет.</span></div>') +
      sw('enabled', 'Звуки включены', 'Общий выключатель; дублируется кнопкой 🔔 в шапке') +
      sw('ui', 'Интерфейс', 'Клики команд, переключатели, щелчки регулятора скорости') +
      sw('events', 'События станков', 'Печать завершена, пауза, ошибка, мало филамента') +
      `<div class="set-row"><div class="sinfo"><b>Громкость</b><small>Для сигналов событий — цех слышит из соседней комнаты</small></div>` +
      `<div class="row gap"><input type="range" min="0" max="100" step="5" value="${Number(p.volume) || 0}" data-sound-pref="volume" aria-label="Громкость" style="width:140px"><span class="mono" data-sound-vol>${Number(p.volume) || 0}%</span></div></div>` +
      sw('quiet', 'Тихие часы', `С ${p.quiet_from}:00 до ${p.quiet_to}:00 — события вполголоса, интерфейс молчит`) +
      `<div class="snd-list">${list}</div>` +
      '<div class="muted" style="font-size:12px;margin-top:6px">Настройки хранятся в этом браузере: у экрана в цехе и у ноутбука могут быть разные.</div>';
  }
  document.addEventListener('change', (e) => {
    const inp = e.target.closest && e.target.closest('[data-sound-pref]');
    if (!inp) return;
    const key = inp.dataset.soundPref;
    if (inp.type === 'checkbox') set({ [key]: inp.checked });
    else set({ [key]: Number(inp.value) });
    if (key !== 'volume') play('toggle', { force: prefs.enabled });
  });
  document.addEventListener('input', (e) => {
    const inp = e.target.closest && e.target.closest('[data-sound-pref="volume"]');
    if (!inp) return;
    const out = inp.parentElement && inp.parentElement.querySelector('[data-sound-vol]');
    if (out) out.textContent = inp.value + '%';
  });
  document.addEventListener('click', (e) => {
    const t = e.target && e.target.closest ? e.target : null;
    if (!t) return;
    const playBtn = t.closest('[data-sound-play]');
    if (playBtn) { unlock(); play(playBtn.dataset.soundPlay, { force: true }); return; }
    if (t.closest('#sound_btn')) { unlock(); toggle(); return; }
    // Интерфейсные клики: команды станку и первичные действия.
    if (t.closest('[data-hero-cmd],[data-hero-start-job],[data-cmd],[data-cmd-name],.btn.danger')) play('click');
  });

  function mount() {
    syncButton();
    const card = document.getElementById('set_sound');
    if (card) renderCard(card);
    if (window.PF && typeof PF.on === 'function') {
      PF.on('notify', onEvent);
      PF.on('view', (d) => { if (d && d.view === 'settings') { const c = document.getElementById('set_sound'); if (c) renderCard(c); } });
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();

  window.PFSound = { play, prefs: () => Object.assign({}, prefs), set, toggle, onEvent, onToast, renderCard, SOUNDS, EVENT_SOUND, quietNow };
})();
