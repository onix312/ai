const tokenInput = document.getElementById("token");
const browserInput = document.getElementById("browser");
const status = document.getElementById("status");
chrome.storage.local.get(["token", "browser_choice"]).then(({token, browser_choice}) => {
  browserInput.value = browser_choice || "auto";
  if (token) {
    tokenInput.value = token;
    status.textContent = "Ключ сохранён. Проверяю соединение…";
    chrome.runtime.sendMessage({op: "poll"}).then((reply) => {
      status.textContent = reply?.ok ? "Подключено к NOZZA" : (reply?.reason || "Агент не отвечает");
    });
  }
});
document.getElementById("save").addEventListener("click", async () => {
  const token = tokenInput.value.trim();
  if (!token) {
    status.textContent = "Вставьте ключ со страницы Luma";
    return;
  }
  await chrome.storage.local.set({token, browser_choice: browserInput.value});
  const reply = await chrome.runtime.sendMessage({op: "poll"});
  status.textContent = reply?.ok ? "Подключено к NOZZA" : (reply?.reason || "Агент не отвечает");
});
