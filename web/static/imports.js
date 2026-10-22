"use strict";

(() => {
  const form = document.getElementById("import-upload-form");
  if (!form) return;
  const input = document.getElementById("import-files");
  const button = document.getElementById("import-submit");
  const selection = document.getElementById("import-selection");
  const result = document.getElementById("import-result");
  const expected = JSON.parse(document.getElementById("import-expected-files").textContent);
  const append = (tag, text, parent = result) => {
    const element = document.createElement(tag);
    element.textContent = text;
    parent.append(element);
    return element;
  };
  const show = (message, state) => {
    result.hidden = false;
    result.dataset.outcome = state;
    result.replaceChildren();
    append("p", message);
  };
  const link = (text, destination) => {
    const anchor = append("a", text);
    anchor.href = destination;
  };
  const refreshHistory = async () => {
    try {
      const response = await fetch(location.pathname + location.search, {credentials: "same-origin", cache: "no-store"});
      if (!response.ok) return false;
      const documentCopy = new DOMParser().parseFromString(await response.text(), "text/html");
      const updated = documentCopy.getElementById("import-history");
      if (!updated) return false;
      document.getElementById("import-history").replaceWith(document.importNode(updated, true));
      return true;
    } catch { return false; }
  };
  input.addEventListener("change", () => {
    selection.replaceChildren();
    for (const file of input.files) append("li", `${file.name} · ${(file.size / 1024).toLocaleString("ru-RU", {maximumFractionDigits: 1})} КиБ`, selection);
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const files = Array.from(input.files);
    const names = new Set(files.map(file => file.name));
    if (files.length !== expected.length || names.size !== expected.length || !expected.every(name => names.has(name))) {
      show("Выберите все пять CSV с указанными именами, без повторов и дополнительных файлов. Импорт ещё не запущен.", "invalid");
      return;
    }
    if (files.some(file => file.size > 5 * 1024 * 1024)) {
      show("Один из файлов превышает 5 МиБ. Импорт ещё не запущен.", "invalid");
      return;
    }
    const body = new FormData();
    files.forEach(file => body.append("files", file, file.name));
    button.disabled = true;
    input.disabled = true;
    form.setAttribute("aria-busy", "true");
    show("Загрузка, проверка и расчёт набора… Дождитесь результата.", "running");
    try {
      const response = await fetch(form.action, {method: "POST", credentials: "same-origin", body});
      const data = await response.json();
      const historyUpdated = data.run_id ? await refreshHistory() : true;
      if (!response.ok) {
        show(data.error?.message || "Результат импорта не подтверждён. Проверьте историю.", data.outcome || "unavailable");
        if (!historyUpdated) append("p", "Не удалось обновить список истории. Результат попытки указан выше.");
        if (data.run_id) link("Открыть попытку импорта", `/imports/${encodeURIComponent(data.run_id)}`);
        if (response.status === 401) link("Войти заново", "/login");
        link("Проверить историю", "/imports");
        return;
      }
      show(data.message, data.reused ? "reused" : "published");
      if (!historyUpdated) append("p", "Не удалось обновить список истории. Результат импорта указан выше.");
      append("p", `Набор: ${data.run_id}. Активный снимок: ${data.active_run_id || "отсутствует"}.`);
      if (data.summary.partial) append("p", "Обнаружены замечания к данным. Проверьте качество и ограничения финансовых показателей.");
      link("Результат набора", `/imports/${encodeURIComponent(data.run_id)}`);
      link("Качество набора", `/quality?run_id=${encodeURIComponent(data.run_id)}`);
      if (data.active_run_id) link("Финансы активного снимка", `/dashboard?run_id=${encodeURIComponent(data.active_run_id)}`);
      link("Обновить историю", "/imports");
    } catch {
      show("Связь прервана или ответ не получен. Исход публикации неизвестен: проверьте историю перед повторной загрузкой.", "unknown");
      link("Проверить историю", "/imports");
    } finally {
      button.disabled = false;
      input.disabled = false;
      form.removeAttribute("aria-busy");
    }
  });
})();
