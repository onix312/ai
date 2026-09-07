// PrintFlow 17.0 — хаб «Страницы»: все LAN-страницы карточками с QR для
// открытия с телефона. Lit-компонент (<pf-pages-hub>), стили — на токенах
// панели (наследует CSS-переменные из tokens.css), QR — наш window.QR.svg.
import { LitElement, html, css } from 'lit';
import { PAGES, CATS } from './pages-data.js';

class PfPagesHub extends LitElement {
  static properties = {
    filter: { state: true },
    cat: { state: true },
    qrFor: { state: true },
  };

  static styles = css`
    :host { display: block; }
    .hub-toolbar {
      display: flex; flex-wrap: wrap; gap: var(--sp-3); align-items: center;
      margin: var(--sp-4) 0;
    }
    .hub-search {
      flex: 1 1 220px; display: flex; align-items: center; gap: var(--sp-2);
      background: var(--input-bg); border: 1px solid var(--input-line);
      border-radius: var(--input-radius); padding: 0 var(--sp-3);
      height: var(--input-h);
    }
    .hub-search:focus-within {
      background: var(--input-bg-focus); border-color: var(--input-line-focus);
      box-shadow: var(--focus-ring);
    }
    .hub-search input {
      flex: 1; border: 0; background: transparent; color: var(--input-text);
      font: inherit; outline: none; font-size: var(--fs-base);
    }
    .hub-cats { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
    .hub-cat {
      border: 1px solid var(--btn-line); background: var(--btn-bg);
      color: var(--btn-text); border-radius: var(--chip-radius);
      padding: 6px 12px; font-size: var(--fs-sm); font-weight: var(--fw-medium);
      cursor: pointer; line-height: 1;
    }
    .hub-cat:hover { background: var(--btn-bg-hover); }
    .hub-cat.on {
      background: var(--accent); border-color: var(--accent); color: var(--accent-ink);
    }
    .hub-grid {
      display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
      gap: var(--sp-4);
    }
    .pcard {
      position: relative; display: flex; flex-direction: column; gap: var(--sp-2);
      background: var(--card-bg); border: 1px solid var(--card-line);
      border-radius: var(--card-radius); padding: var(--sp-4);
      text-decoration: none; color: var(--text);
      box-shadow: var(--elev-1);
    }
    .pcard:hover {
      transform: translateY(var(--card-hover-y)); box-shadow: var(--elev-2);
      border-color: var(--accent-line);
    }
    .pcard-top { display: flex; align-items: center; gap: var(--sp-3); }
    .pcard-ic {
      width: 40px; height: 40px; flex: none; border-radius: var(--r-sm);
      display: grid; place-items: center;
      background: var(--accent-soft); color: var(--accent);
    }
    .pcard-ic ::slotted(svg), .pcard-ic svg { width: 22px; height: 22px; }
    .pcard-title { font-weight: var(--fw-bold); font-size: var(--fs-md); line-height: 1.25; }
    .pcard-sub { color: var(--muted); font-size: var(--fs-sm); line-height: var(--lh-base); }
    .pcard-meta { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-1); }
    .badge {
      font-size: var(--fs-xs); font-weight: var(--fw-medium);
      padding: 3px 9px; border-radius: var(--chip-radius);
      background: var(--panel-3); color: var(--text-2);
    }
    .badge.tone-accent { background: var(--accent-soft); color: var(--accent); }
    .badge.tone-info { background: var(--info-soft); color: var(--info); }
    .badge.tone-ok { background: var(--ok-soft); color: var(--ok); }
    .badge.tone-warn { background: var(--warn-soft); color: var(--warn); }
    .badge.tone-violet { background: rgba(124,58,237,.14); color: var(--accent-2); }
    .badge.lock { background: var(--warn-soft); color: var(--warn); }
    .pcard-qr {
      margin-left: auto; border: 1px solid var(--btn-line); background: var(--btn-bg);
      color: var(--btn-text); border-radius: var(--r-xs);
      width: 30px; height: 30px; display: grid; place-items: center;
      cursor: pointer; font-size: 15px;
    }
    .pcard-qr:hover { background: var(--btn-bg-hover); }
    .pcard-addr {
      font-family: var(--mono); font-size: var(--fs-xs); color: var(--muted);
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .hub-empty {
      padding: var(--sp-8) var(--sp-4); text-align: center; color: var(--muted);
      border: 1px dashed var(--line-strong); border-radius: var(--card-radius);
    }
    /* модалка QR */
    .qr-overlay {
      position: fixed; inset: 0; background: var(--modal-overlay);
      display: grid; place-items: center; z-index: 80; padding: var(--sp-4);
    }
    .qr-modal {
      background: var(--modal-bg); border-radius: var(--modal-radius);
      box-shadow: var(--modal-shadow); padding: var(--sp-6);
      max-width: 360px; width: 100%; text-align: center;
    }
    .qr-modal h3 { margin: 0 0 var(--sp-1); font-size: var(--fs-lg); }
    .qr-modal p { margin: 0 0 var(--sp-4); color: var(--muted); font-size: var(--fs-sm); }
    .qr-box {
      background: #fff; border-radius: var(--r); padding: var(--sp-3);
      width: 240px; height: 240px; margin: 0 auto var(--sp-4);
      display: grid; place-items: center;
    }
    .qr-box svg { width: 100%; height: 100%; }
    .qr-addr {
      font-family: var(--mono); font-size: var(--fs-sm); word-break: break-all;
      color: var(--text-2); margin-bottom: var(--sp-4);
    }
    .qr-actions { display: flex; gap: var(--sp-3); justify-content: center; }
    .btn {
      display: inline-flex; align-items: center; justify-content: center; gap: var(--sp-2);
      border: 1px solid var(--btn-line); background: var(--btn-bg); color: var(--btn-text);
      border-radius: var(--btn-radius); padding: 8px 16px; font-size: var(--fs-sm);
      font-weight: var(--fw-medium); cursor: pointer; text-decoration: none; line-height: 1.2;
      transition: background var(--t-fast), border-color var(--t-fast);
    }
    .btn:hover { background: var(--btn-bg-hover); }
    .btn.primary {
      background: var(--accent); border-color: var(--accent); color: var(--accent-ink);
    }
    .btn.primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
    @media (prefers-reduced-motion: reduce) {
      .pcard { transition: none; }
      .pcard:hover { transform: none; }
    }
  `;

  constructor() {
    super();
    this.filter = '';
    this.cat = 'all';
    this.qrFor = null;
  }

  get pages() {
    const f = this.filter.trim().toLowerCase();
    return PAGES.filter((p) => {
      if (this.cat !== 'all' && p.cat !== this.cat) return false;
      if (!f) return true;
      return (p.title + ' ' + p.sub + ' ' + p.file).toLowerCase().includes(f);
    });
  }

  absUrl(href) {
    // адрес для телефона в той же LAN: текущий хост + короткий путь
    return location.origin + href;
  }

  qrSvg(href) {
    const QR = window.QR;
    if (!QR) return '';
    return QR.svg(this.absUrl(href), { ecl: 'M', size: 240, margin: 1, dark: '#0f172a', light: '#ffffff' });
  }

  iconSvg(name) {
    const reg = (window.PFIcons && window.PFIcons.svg) ? window.PFIcons.svg(name) : '';
    return reg;
  }

  render() {
    const pages = this.pages;
    return html`
      <div class="hub-toolbar">
        <label class="hub-search">
          <span aria-hidden="true">⌕</span>
          <input type="search" placeholder="Найти страницу: касса, трек, полка…"
                 .value=${this.filter}
                 @input=${(e) => (this.filter = e.target.value)}
                 aria-label="Поиск по страницам">
        </label>
        <div class="hub-cats" role="group" aria-label="Фильтр страниц">
          <button type="button" class="hub-cat ${this.cat === 'all' ? 'on' : ''}"
                  @click=${() => (this.cat = 'all')}>Все</button>
          ${Object.entries(CATS).map(([key, c]) => html`
            <button type="button" class="hub-cat ${this.cat === key ? 'on' : ''}"
                    @click=${() => (this.cat = key)}>${c.label}</button>`)}
        </div>
      </div>

      ${pages.length === 0
        ? html`<div class="hub-empty">Ничего не нашлось — попробуйте другой запрос.</div>`
        : html`<div class="hub-grid">
            ${pages.map((p) => html`
              <a class="pcard" href=${p.href} data-view="">
                <div class="pcard-top">
                  <span class="pcard-ic" .innerHTML=${this.iconSvg(p.icon) || '◈'}></span>
                  <span class="pcard-title">${p.title}</span>
                  <button type="button" class="pcard-qr" title="QR для открытия с телефона"
                          aria-label="QR-код страницы «${p.title}»"
                          @click=${(e) => { e.preventDefault(); e.stopPropagation(); this.qrFor = p; }}>▦</button>
                </div>
                <span class="pcard-sub">${p.sub}</span>
                <div class="pcard-meta">
                  <span class="badge tone-${CATS[p.cat].tone}">${CATS[p.cat].label}</span>
                  ${p.lock ? html`<span class="badge lock" title="Требуется код доступа">🔒 код</span>` : ''}
                </div>
                <span class="pcard-addr">${p.file}</span>
              </a>`)}
          </div>`}

      ${this.qrFor ? html`
        <div class="qr-overlay" role="dialog" aria-modal="true"
             aria-label="QR страницы ${this.qrFor.title}"
             @click=${(e) => { if (e.target === e.currentTarget) this.qrFor = null; }}>
          <div class="qr-modal">
            <h3>${this.qrFor.title}</h3>
            <p>Наведите камеру телефона — страница откроется в локальной сети</p>
            <div class="qr-box" .innerHTML=${this.qrSvg(this.qrFor.href)}></div>
            <div class="qr-addr">${this.absUrl(this.qrFor.href)}</div>
            <div class="qr-actions">
              <button type="button" class="btn" @click=${() => (this.qrFor = null)}>Закрыть</button>
              <a class="btn primary" href=${this.qrFor.href}>Открыть</a>
            </div>
          </div>
        </div>` : ''}
    `;
  }
}

customElements.define('pf-pages-hub', PfPagesHub);
