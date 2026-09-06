// PrintFlow 17.0 — реестр LAN-страниц для хаба «Страницы».
// Единый источник правды для меню-кассы и хаба: адрес, название, устройство,
// видимость (публичная/внутренняя), иконка из реестра PFIcons.
// Категории: shop (телефон покупателя), tool (телефон сотрудника),
//            tv (экран/ТВ), print (печать), admin (панель/внутреннее).
export const PAGES = [
  { href: '/', file: 'index.html', icon: 'dashboard', cat: 'admin', title: 'Панель управления', sub: '15 разделов: цех, склад, касса, настройки' },
  { href: '/cashier', file: 'cashier.html', icon: 'ruble', cat: 'tool', lock: true, title: 'Мобильная касса', sub: 'Продажа с телефона, код доступа' },
  { href: '/sbp', file: 'sbp.html', icon: 'qr', cat: 'tool', lock: true, title: 'СБП · входящие', sub: 'Платежи по QR, авто-подтверждение' },
  { href: '/bank', file: 'bank.html', icon: 'bank', cat: 'tool', lock: true, title: 'Поступления из банка', sub: 'Выписка и сопоставление с заказами' },
  { href: '/m', file: 'm.html', icon: 'printer', cat: 'tool', title: 'Станок (мобильная)', sub: 'Пульты у принтера: старт, пауза, съём' },
  { href: '/shelf', file: 'shelf.html', icon: 'shelf', cat: 'tv', title: 'Экран стеллажа', sub: 'Витрина полки для покупателей' },
  { href: '/tv', file: 'tv.html', icon: 'tv', cat: 'tv', title: 'ТВ-дашборд', sub: 'Очередь и статусы на большой экран' },
  { href: '/order', file: 'order.html', icon: 'cart', cat: 'shop', title: 'Витрина и заказ', sub: 'Каталог, корзина, заявка с телефона' },
  { href: '/track', file: 'track.html', icon: 'track', cat: 'shop', title: 'Статус заказа', sub: 'Покупатель следит за готовностью' },
  { href: '/my', file: 'my.html', icon: 'user', cat: 'shop', title: 'Мои заказы', sub: 'Личный кабинет покупателя' },
  { href: '/spool', file: 'spool.html', icon: 'spool', cat: 'tool', title: 'Катушки', sub: 'Взвешивание и списание пластика' },
  { href: '/labels', file: 'labels.html', icon: 'tag', cat: 'print', title: 'Этикетки', sub: 'Печать наклеек и бирок с QR' },
  { href: '/price-tags', file: 'price-tags.html', icon: 'tag', cat: 'print', title: 'Ценники', sub: 'Ценники и промостенды 67×32' },
  { href: '/design', file: 'design.html', icon: 'pen', cat: 'shop', title: 'Заявка на дизайн', sub: 'Приём индивидуальных заказов' },
];

export const CATS = {
  admin: { label: 'Панель', tone: 'accent' },
  tool: { label: 'Сотрудникам', tone: 'info' },
  tv: { label: 'Экраны', tone: 'violet' },
  shop: { label: 'Покупателям', tone: 'ok' },
  print: { label: 'Печать', tone: 'warn' },
};
