(() => {
  const app = document.getElementById('app');
  const stage = document.getElementById('stage');
  const toast = document.getElementById('toast');
  const $ = id => document.getElementById(id);
  const screens = {orders:'Заказы', printer:'Принтеры', assistant:'Помощник'};
  const stateCopy = {
    orders: {
      loading:['Загружаем заказы','Список и фильтры появятся после ответа панели.'],
      empty:['Заказов по этим условиям нет','Попробуйте сбросить фильтры или создать новый заказ.'],
      error:['Не удалось загрузить заказы','Данные не потеряны. Проверьте связь с панелью и повторите запрос.']
    },
    printer: {
      loading:['Получаем состояние принтеров','Ожидаем свежую телеметрию от устройств.'],
      empty:['Принтеры ещё не добавлены','Подключите первое устройство в настройках оборудования.'],
      error:['Связь с принтерами потеряна','Последнее состояние может быть устаревшим. Проверьте подключение.']
    },
    assistant: {
      loading:['Подключаем помощника','Проверяем модель и доступ к данным панели.'],
      empty:['Начните разговор','Например, спросите, какие заказы требуют внимания сегодня.'],
      error:['Модель сейчас недоступна','Точные данные заказов и принтеров по-прежнему можно открыть в панели.']
    }
  };
  const orders = {
    '1048':{product:'Кронштейн для полки',customer:'Ирина К.',due:'Сегодня, до 18:00',price:'4 200 ₽',status:'Ждёт печати',badge:'amber',next:'Проверьте файл и материал. После этого заказ можно поставить в очередь.',action:'Подготовить печать →'},
    '1047':{product:'Корпус датчика',customer:'Михаил Р.',due:'Завтра, до 12:00',price:'2 800 ₽',status:'Готов к выдаче',badge:'green',next:'Изделие готово. Свяжитесь с клиентом и согласуйте выдачу.',action:'Оформить выдачу →'},
    '1046':{product:'Набор бирок, 20 шт.',customer:'Ольга М.',due:'30 сентября, до 17:00',price:'3 600 ₽',status:'Печатается',badge:'blue',next:'Печать идёт. Следите за окончанием задания и проверкой качества.',action:'Открыть задание →'},
    '1045':{product:'Подставка для планшета',customer:'Алексей Т.',due:'2 октября, до 16:00',price:'5 400 ₽',status:'Согласование',badge:'neutral',next:'Ожидается согласование макета и цены с клиентом.',action:'Открыть согласование →'}
  };
  let screen = 'orders';
  let filter = 'all';
  let toastTimer;

  function announce(message) {
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 3800);
  }
  function updateUrl() {
    const url = new URL(location.href);
    url.searchParams.set('screen', screen);
    url.searchParams.set('device', app.classList.contains('mobile') ? 'mobile' : 'desktop');
    url.searchParams.set('theme', app.dataset.theme);
    url.searchParams.set('state', app.dataset.state);
    history.replaceState(null, '', url);
  }
  function selectScreen(name) {
    if (!screens[name]) return;
    screen = name;
    document.querySelectorAll('.screen').forEach(el => el.classList.toggle('active', el.id === 'screen-' + name));
    document.querySelectorAll('[data-go]').forEach(el => el.classList.toggle('active', el.dataset.go === name));
    document.querySelectorAll('[data-review-screen]').forEach(el => el.setAttribute('aria-pressed', String(el.dataset.reviewScreen === name)));
    $('crumb-title').textContent = screens[name];
    app.classList.remove('order-open');
    closeMobileMenu();
    renderState();
    updateUrl();
  }
  function selectDevice(name) {
    app.classList.toggle('mobile', name === 'mobile');
    app.classList.toggle('desktop', name !== 'mobile');
    app.classList.remove('order-open');
    closeMobileMenu();
    document.querySelectorAll('[data-device]').forEach(el => el.setAttribute('aria-pressed', String(el.dataset.device === name)));
    updateUrl();
  }
  function selectTheme(name) {
    app.dataset.theme = name;
    stage.dataset.theme = name;
    document.querySelectorAll('[data-review-theme]').forEach(el => el.setAttribute('aria-pressed', String(el.dataset.reviewTheme === name)));
    updateUrl();
  }
  function selectState(name) {
    app.dataset.state = ['ready','loading','empty','error'].includes(name) ? name : 'ready';
    $('review-state').value = app.dataset.state;
    app.classList.remove('order-open');
    renderState();
    updateUrl();
  }
  function renderState() {
    $('printer-device-count').textContent = app.dataset.state === 'empty' && screen === 'printer' ? '0 УСТРОЙСТВ' : '2 УСТРОЙСТВА';
    document.querySelectorAll('[data-state-slot]').forEach(slot => {
      const kind = slot.dataset.stateSlot;
      const status = app.dataset.state;
      slot.replaceChildren();
      slot.hidden = status === 'ready' || kind !== screen;
      if (slot.hidden) return;
      const [title, description] = stateCopy[kind][status];
      const card = document.createElement('div');
      card.className = 'state-card';
      const icon = document.createElement('div');
      icon.className = 'state-icon';
      icon.textContent = status === 'error' ? '!' : status === 'empty' ? '○' : '···';
      const heading = document.createElement('h2');
      heading.textContent = title;
      const detail = document.createElement('p');
      detail.textContent = description;
      card.append(icon, heading, detail);
      if (status === 'loading') {
        const skeletons = document.createElement('div');
        skeletons.className = 'skeleton-wrap';
        for (let i=0;i<3;i++) { const line=document.createElement('div'); line.className='skeleton'; skeletons.append(line); }
        card.append(skeletons);
      } else {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'button ' + (status === 'error' ? 'primary' : 'secondary');
        button.textContent = status === 'error' ? 'Повторить' : kind === 'orders' ? 'Сбросить фильтры' : kind === 'printer' ? 'Добавить принтер' : 'Начать разговор';
        button.addEventListener('click', () => {
          if (status === 'empty' && kind === 'printer') { announce('Макет: откроются настройки подключения принтера.'); return; }
          selectState('ready');
          if (kind === 'orders') $('clear-filters').click();
          if (kind === 'assistant') $('assistant-input').focus();
        });
        card.append(button);
      }
      slot.append(card);
    });
  }
  function buildMobileOrders() {
    const list = $('mobile-order-list');
    Object.keys(orders).sort((a, b) => Number(b) - Number(a)).forEach(id => {
      const order = orders[id];
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'mobile-order-card';
      button.dataset.order = id;
      button.dataset.tags = id === '1048' ? 'attention' : id === '1047' ? 'ready' : 'work';
      button.innerHTML = `<span class="mobile-order-head"><strong>№ ${id} · ${order.customer}</strong><span class="badge ${order.badge}"><i></i>${order.status}</span></span><span class="mobile-order-product">${order.product}</span><span class="mobile-order-meta"><span>${order.due}</span><span>${order.price}</span></span>`;
      button.addEventListener('click', () => openOrder(id));
      list.append(button);
    });
  }
  function applyOrderFilter() {
    const q = $('order-search').value.trim().toLocaleLowerCase('ru');
    const visibleIds = Object.entries(orders).filter(([id, order]) => {
      const found = !q || (`${id} ${order.product} ${order.customer}`).toLocaleLowerCase('ru').includes(q);
      const tag = id === '1048' ? 'attention' : id === '1047' ? 'ready' : 'work';
      return found && (filter === 'all' || tag === filter);
    }).map(([id]) => id);
    document.querySelectorAll('[data-order][data-tags]').forEach(row => {
      row.hidden = !visibleIds.includes(row.dataset.order);
    });
    $('order-count').textContent = `Показано ${visibleIds.length} из 12 активных`;
    $('no-order-match').hidden = visibleIds.length !== 0;
  }
  function openOrder(id) {
    const order = orders[id];
    if (!order) return;
    $('detail-number').textContent = 'Заказ № ' + id;
    $('detail-product').textContent = order.product;
    $('detail-customer').textContent = order.customer;
    $('detail-due').textContent = order.due;
    $('detail-price').textContent = order.price;
    $('detail-next').textContent = order.next;
    $('order-action').textContent = order.action;
    $('order-action').dataset.demo = order.action.replace(/\s*→$/, '');
    $('detail-badge').className = 'badge ' + order.badge;
    $('detail-badge').innerHTML = '<i></i>' + order.status;
    const flow = {
      '1048': [['done','✓ Заказ принят'],['current','2 Подготовить печать'],['','3 Изготовить и выдать']],
      '1047': [['done','✓ Заказ принят'],['done','✓ Изделие изготовлено'],['current','3 Оформить выдачу']],
      '1046': [['done','✓ Заказ принят'],['current','2 Печать изделия'],['','3 Проверить и выдать']],
      '1045': [['current','1 Согласовать заказ'],['','2 Изготовить изделие'],['','3 Выдать клиенту']]
    }[id];
    $('detail-flow').replaceChildren(...flow.map(([kind, label]) => {
      const step = document.createElement('span');
      step.className = kind;
      step.textContent = label;
      return step;
    }));
    document.querySelectorAll('tr[data-order],.mobile-order-card').forEach(row => row.classList.toggle('selected-row', row.dataset.order === id));
    app.classList.add('order-open');
  }
  function selectPrinter(id) {
    const free = id === 'a1';
    document.querySelectorAll('[data-printer]').forEach(button => {
      const chosen = button.dataset.printer === id;
      button.classList.toggle('selected', chosen);
      button.setAttribute('aria-pressed', String(chosen));
    });
    $('printer-job').textContent = free ? 'Нет активного задания' : 'Корпус датчика · заказ № 1046';
    $('printer-file').textContent = free ? 'Принтер готов к следующей работе' : 'sensor_case_v4.3mf · PETG чёрный';
    $('printer-status').className = 'badge ' + (free ? 'neutral' : 'green');
    $('printer-status').innerHTML = '<i></i>' + (free ? 'Свободен' : 'Печатает');
    $('printer-percent').textContent = free ? '0%' : '68%';
    $('printer-remaining').textContent = free ? 'Готов к новому заданию' : 'Осталось ≈ 1 ч 24 мин';
    $('progress-fill').style.width = free ? '0%' : '68%';
    $('printer-end').textContent = free ? '—' : '16:42';
    $('print-times').hidden = free;
    $('printer-nozzle').textContent = free ? '—' : '245 °C';
    $('printer-bed').textContent = free ? '—' : '80 °C';
    $('printer-material').textContent = free ? 'Не указан' : 'PETG · чёрный';
    $('camera-detail').textContent = free ? 'Принтер не присылает кадр. Активного задания нет.' : 'Принтер не присылает кадр. Печать продолжается.';
    $('printer-main-action').textContent = free ? 'Открыть очередь →' : 'Открыть задание →';
    $('printer-main-action').dataset.demo = free ? 'Открыть очередь' : 'Открыть задание в очереди';
    $('pause-printer').disabled = free;
    $('stop-printer').disabled = free;
  }
  function confirmAction(title, description) {
    $('confirm-title').textContent = title;
    $('confirm-text').textContent = description + ' Это только макет: команда не отправляется.';
    $('confirm-backdrop').hidden = false;
    $('confirm-cancel').focus();
  }
  function closeMobileMenu() {
    $('mobile-menu').hidden = true;
    $('mobile-more').setAttribute('aria-expanded', 'false');
  }
  function addChat(role, message) {
    const flow = $('chat-flow');
    const bubble = document.createElement('div');
    bubble.className = 'chat-message ' + role;
    if (role === 'bot') {
      const icon = document.createElement('span'); icon.className='bot-mark'; icon.textContent='✦';
      const content = document.createElement('div'); const p=document.createElement('p'); p.textContent=message; content.append(p);
      bubble.append(icon, content);
    } else bubble.textContent=message;
    flow.append(bubble); flow.scrollTop=flow.scrollHeight;
  }
  function sendAssistant() {
    const field = $('assistant-input');
    const text = field.value.trim();
    if (!text) return;
    if (text.length > 1000) { announce('В макете действует лимит 1000 символов. Текст остаётся в поле.'); return; }
    addChat('me', text);
    addChat('bot', /заказ/i.test(text) ? 'Пример ответа: заказ № 1048 ждёт печати; срок сегодня до 18:00. Источник — карточка заказа.' : /принтер|p1s/i.test(text) ? 'Пример ответа: P1S печатает корпус датчика, прогресс 68%. Камера не присылает кадр.' : 'Это макет диалога. В рабочей версии ответ придёт от помощника и проверенных данных панели.');
    field.value=''; field.focus();
  }

  document.querySelectorAll('[data-review-screen]').forEach(button => button.addEventListener('click', () => selectScreen(button.dataset.reviewScreen)));
  document.querySelectorAll('[data-go]').forEach(button => button.addEventListener('click', () => selectScreen(button.dataset.go)));
  document.querySelectorAll('[data-device]').forEach(button => button.addEventListener('click', () => selectDevice(button.dataset.device)));
  document.querySelectorAll('[data-review-theme]').forEach(button => button.addEventListener('click', () => selectTheme(button.dataset.reviewTheme)));
  document.querySelectorAll('[data-demo]').forEach(button => button.addEventListener('click', () => announce('Макет: ' + button.dataset.demo)));
  $('review-state').addEventListener('change', event => selectState(event.target.value));
  $('order-search').addEventListener('input', applyOrderFilter);
  $('clear-filters').addEventListener('click', () => { $('order-search').value=''; filter='all'; document.querySelector('[data-order-filter=all]').click(); });
  document.querySelectorAll('[data-order-filter]').forEach(button => button.addEventListener('click', () => {
    filter=button.dataset.orderFilter;
    document.querySelectorAll('[data-order-filter]').forEach(item => { item.classList.toggle('selected',item===button); item.setAttribute('aria-pressed',String(item===button)); });
    applyOrderFilter();
  }));
  document.querySelectorAll('[data-open-order]').forEach(button => button.addEventListener('click', () => openOrder(button.dataset.openOrder)));
  $('close-order').addEventListener('click', () => app.classList.remove('order-open'));
  document.querySelectorAll('[data-printer]').forEach(button => button.addEventListener('click', () => selectPrinter(button.dataset.printer)));
  $('pause-printer').addEventListener('click', () => confirmAction('Приостановить печать на P1S?', 'Текущее задание: «Корпус датчика», прогресс 68%.'));
  $('stop-printer').addEventListener('click', () => confirmAction('Остановить печать на P1S?', 'Текущее задание будет остановлено после подтверждения в рабочей панели.'));
  $('confirm-proposal').addEventListener('click', () => confirmAction('Приостановить печать на P1S?', 'Предложение помощника связано с текущим заданием «Корпус датчика».'));
  $('cancel-proposal').addEventListener('click', () => { document.querySelector('.proposal').hidden=true; addChat('bot','Предложение отменено. Команда не отправлялась.'); });
  $('confirm-cancel').addEventListener('click', () => { $('confirm-backdrop').hidden=true; });
  $('confirm-accept').addEventListener('click', () => { $('confirm-backdrop').hidden=true; announce('Это макет. Действие не выполнялось.'); });
  $('confirm-backdrop').addEventListener('click', event => { if(event.target===$('confirm-backdrop')) $('confirm-backdrop').hidden=true; });
  $('new-chat').addEventListener('click', () => { $('chat-flow').replaceChildren(); addChat('bot','Здравствуйте. Спросите о заказе, принтере или задаче мастерской.'); });
  $('assistant-send').addEventListener('click', sendAssistant);
  $('assistant-input').addEventListener('keydown', event => { if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendAssistant();} });
  document.querySelectorAll('[data-suggest]').forEach(button => button.addEventListener('click', () => { $('assistant-input').value=button.dataset.suggest;sendAssistant(); }));
  $('context-pill').addEventListener('click', () => { document.querySelector('.context-pills').hidden=true; announce('Контекст убран из следующего вопроса.'); });
  $('mobile-more').addEventListener('click', () => {
    const opening = $('mobile-menu').hidden;
    $('mobile-menu').hidden = !opening;
    $('mobile-more').setAttribute('aria-expanded', String(opening));
    if (opening) $('close-mobile-menu').focus();
  });
  $('close-mobile-menu').addEventListener('click', () => { closeMobileMenu(); $('mobile-more').focus(); });
  document.querySelectorAll('.mobile-menu-links button').forEach(button => button.addEventListener('click', closeMobileMenu));
  document.addEventListener('keydown', event => {
    if (event.key==='Escape') { $('confirm-backdrop').hidden=true; app.classList.remove('order-open'); closeMobileMenu(); }
  });
  buildMobileOrders();
  const query = new URLSearchParams(location.search);
  selectDevice(query.get('device')==='mobile' || (!query.has('device') && innerWidth < 700) ? 'mobile' : 'desktop');
  selectTheme(query.get('theme')==='dark'?'dark':'light');
  selectScreen(screens[query.get('screen')]?query.get('screen'):'orders');
  selectState(['ready','loading','empty','error'].includes(query.get('state'))?query.get('state'):'ready');
})();
