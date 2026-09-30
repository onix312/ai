const BRIDGE = "http://127.0.0.1:8799/browser/bridge";
let polling = false;

async function send(payload) {
  let {token, client_id, browser_choice} = await chrome.storage.local.get(["token", "client_id", "browser_choice"]);
  if (!token) return {ok: false, reason: "Введите ключ подключения"};
  if (!client_id) {
    client_id = crypto.randomUUID();
    await chrome.storage.local.set({client_id});
  }
  const agent = navigator.userAgent;
  const browser = browser_choice && browser_choice !== "auto" ? browser_choice :
    /YaBrowser/i.test(agent) ? "yandex" : /Edg\//i.test(agent) ? "edge" : "chrome";
  try {
    const response = await fetch(BRIDGE, {
      method: "POST", headers: {"Content-Type": "text/plain"},
      body: JSON.stringify({...payload, token, client_id, browser}),
    });
    return await response.json();
  } catch (_error) {
    return {ok: false, reason: "Luma не отвечает на 127.0.0.1:8799"};
  }
}

function publicTab(tab) {
  return {id: tab.id, window_id: tab.windowId, title: (tab.title || "").slice(0, 300),
          url: tab.url || "", active: Boolean(tab.active)};
}

function capturePage() {
  const visible = (element) => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const selector = (element) => {
    if (element.id) return `#${CSS.escape(element.id)}`;
    const parts = [];
    for (let node = element; node && node !== document.body && parts.length < 6; node = node.parentElement) {
      const siblings = [...node.parentElement.children].filter((item) => item.tagName === node.tagName);
      parts.unshift(`${node.tagName.toLowerCase()}:nth-of-type(${siblings.indexOf(node) + 1})`);
    }
    return `body > ${parts.join(" > ")}`;
  };
  const name = (element) => (element.getAttribute("aria-label") ||
    element.getAttribute("title") ||
    (element.matches("input,textarea,select,[contenteditable]") ?
      element.getAttribute("placeholder") : element.innerText) || "").trim().slice(0, 200);
  const links = [...document.querySelectorAll("a[href]")].filter(visible).slice(0, 100)
    .filter((a) => /^https?:\/\//i.test(a.href || ""))
    .map((a) => ({text: name(a), href: a.href}));
  const buttons = [...document.querySelectorAll("button,input[type=button],input[type=submit],[role=button]")]
    .filter(visible).slice(0, 100).map((item) => ({text: name(item), selector: selector(item)}));
  const fields = [...document.querySelectorAll("input,textarea,select,[contenteditable=true]")]
    .filter((item) => visible(item) && !/password|cc-|card|cvc|cvv|one-time-code/i.test(item.autocomplete || "") &&
      !["password", "hidden", "file", "checkbox", "radio", "submit",
                                         "button", "reset", "image", "color", "range"].includes(item.type))
    .slice(0, 100).map((item) => ({selector: selector(item), type: item.type || item.tagName.toLowerCase(),
                                   label: name(item), required: Boolean(item.required)}));
  const accessibility = [...document.querySelectorAll("[role],[aria-label],h1,h2,h3")]
    .filter(visible).slice(0, 150).map((item) => ({role: item.getAttribute("role") || item.tagName.toLowerCase(),
                                                   name: name(item)}));
  let text = "";
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode() && text.length < 12000) {
    const node = walker.currentNode;
    const parent = node.parentElement;
    if (!parent || parent.closest("script,style,noscript,textarea,select,[contenteditable]")) continue;
    if (visible(parent)) text += ` ${node.textContent || ""}`;
  }
  const selection = getSelection();
  const selectionParent = selection?.anchorNode?.parentElement;
  const selectedText = selectionParent?.closest("input,textarea,select,[contenteditable]") ? "" :
    (selection?.toString() || "");
  return {text: text.replace(/\s+/g, " ").trim().slice(0, 12000),
          selected_text: selectedText.slice(0, 2000),
          links, buttons, fields, accessibility};
}

async function observe(tabId) {
  const tab = tabId ? await chrome.tabs.get(tabId) : (await chrome.tabs.query({active: true, lastFocusedWindow: true}))[0];
  if (!tab || !/^https?:\/\//.test(tab.url || "")) {
    return {ok: false, reason: "Откройте обычную веб-страницу в браузере"};
  }
  try {
    const [{result}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: capturePage});
    return {ok: true, tab: publicTab(tab), page: result};
  } catch (_error) {
    return {ok: false, reason: "Браузер не разрешил прочитать эту страницу"};
  }
}

async function execute(command) {
  try {
    const args = command.args || {};
    if (command.kind === "tabs") {
      const tabs = await chrome.tabs.query({});
      return {ok: true, tabs: tabs.slice(0, 100).map(publicTab)};
    }
    if (command.kind === "observe") return await observe(Number(args.tab_id) || 0);
    return {ok: false, reason: "Неизвестная команда браузера"};
  } catch (_error) {
    return {ok: false, reason: "Браузер не выполнил запрос"};
  }
}

async function poll() {
  if (polling) return {ok: true};
  polling = true;
  try {
    const reply = await send({op: "poll"});
    if (reply.command) {
      const result = await execute(reply.command);
      await send({op: "result", id: reply.command.id, result});
    }
    return reply;
  } finally {
    polling = false;
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.op === "poll") {
    poll().then(sendResponse);
    return true;
  }
});
chrome.alarms.create("nozza-poll", {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener((alarm) => {if (alarm.name === "nozza-poll") poll();});
setInterval(poll, 1500);
poll();
