"use strict";

for (const kind of ["login", "logout"]) {
  const form = document.getElementById(`${kind}-form`);
  if (!form) continue;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const status = document.getElementById("status");
    const button = form.querySelector("button");
    button.disabled = true;
    status.textContent = "";
    try {
      const options = { method: "POST", credentials: "same-origin" };
      if (kind === "login") {
        const data = new FormData(form);
        options.headers = { "Content-Type": "application/json" };
        options.body = JSON.stringify({ username: data.get("username"), password: data.get("password") });
      }
      const response = await fetch(form.action, options);
      if (response.ok || (kind === "logout" && response.status === 401)) {
        window.location.assign(kind === "login" ? "/dashboard" : "/login");
      } else {
        const body = await response.json();
        status.textContent = body.error?.message || "Не удалось выполнить запрос.";
      }
    } catch {
      status.textContent = "Сервис недоступен. Повторите попытку позже.";
    } finally {
      if (kind === "login") form.elements.password.value = "";
      button.disabled = false;
    }
  });
}
