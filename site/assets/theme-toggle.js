/* PrintFlow 17.0 — компактный тумблер темы для LAN-страниц (касса, СБП,
   банк, полка, трек, TV и т.д.). Панель использует свою кнопку в шапке,
   здесь — плавающая кнопка в углу. Выбор пишется в localStorage('pf_theme')
   и подхватывается всеми страницами через theme-init.js. */
(function () {
  if (window.__PF_THEME_TOGGLE__) return;
  window.__PF_THEME_TOGGLE__ = true;

  var ORDER = ['system', 'light', 'dark'];
  var LABEL = { system: 'Тема: как в системе', light: 'Тема: светлая', dark: 'Тема: тёмная' };

  function current() {
    try {
      var v = localStorage.getItem('pf_theme');
      if (v === 'system' || v === 'light' || v === 'dark') return v;
    } catch (e) {}
    return 'dark';
  }

  function apply(pref) {
    var dark = pref === 'dark' ||
      (pref === 'system' && window.matchMedia &&
        window.matchMedia('(prefers-color-scheme: dark)').matches) ||
      (pref === 'system' && !window.matchMedia);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    document.documentElement.dataset.pfThemePref = pref;
  }

  function cycle() {
    var next = ORDER[(ORDER.indexOf(current()) + 1) % ORDER.length];
    try { localStorage.setItem('pf_theme', next); } catch (e) {}
    apply(next);
    var b = document.getElementById('pf_theme_btn');
    if (b) b.title = LABEL[next] + ' (клик — переключить)';
  }

  function makeButton() {
    if (document.getElementById('pf_theme_btn')) return;
    if (!document.getElementById('pf_theme_btn_style')) {
      var st = document.createElement('style');
      st.id = 'pf_theme_btn_style';
      st.textContent = '@media print { #pf_theme_btn { display: none !important; } }' +
        '@media (max-width: 480px) { #pf_theme_btn { right: 8px; bottom: 8px; width: 36px; height: 36px; font-size: 16px; } }';
      document.head.appendChild(st);
    }
    var b = document.createElement('button');
    b.type = 'button';
    b.id = 'pf_theme_btn';
    b.setAttribute('aria-label', 'Переключить тему оформления');
    b.textContent = '◐';
    b.style.cssText =
      'position:fixed;right:12px;bottom:12px;z-index:9999;width:40px;height:40px;' +
      'border-radius:50%;border:1px solid var(--line,#ccc);cursor:pointer;' +
      'background:var(--panel,#fff);color:var(--text,#000);font-size:18px;' +
      'box-shadow:0 4px 14px rgba(0,0,0,.18);line-height:1;transition:opacity .2s;';
    b.title = LABEL[current()];
    b.addEventListener('click', cycle);
    document.addEventListener('DOMContentLoaded', function () { document.body.appendChild(b); });
    if (document.body) document.body.appendChild(b);
  }

  apply(current());
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
      if (current() === 'system') apply('system');
    });
  }
  makeButton();
})();
