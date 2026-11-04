// spot2deez — tela de login. A sessão fica num cookie HttpOnly posto pelo servidor;
// aqui não se guarda nada. Mensagens de erro só entram no DOM via textContent.
"use strict";

const $ = (id) => document.getElementById(id);

$("theme").value = document.documentElement.dataset.theme || "system";
$("theme").addEventListener("change", (event) => {
  const value = event.target.value;
  document.documentElement.dataset.theme = value;
  try { localStorage.setItem("spot2deez-theme", value); } catch {}
});

function showError(msg) {
  $("form-error").textContent = msg || "";
  $("form-error").hidden = !msg;
}

// quem já tem sessão não precisa ver esta tela
fetch("/api/me", { credentials: "same-origin" })
  .then((res) => { if (res.ok) location.replace("/"); })
  .catch(() => {});

$("login").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const user = $("user").value.trim();
  const password = $("password").value;
  for (const field of [$("user"), $("password")]) field.removeAttribute("aria-invalid");
  if (!user || !password) {
    const empty = user ? $("password") : $("user");
    showError("Preencha o usuário e a senha.");
    empty.setAttribute("aria-invalid", "true");
    empty.focus();
    return;
  }
  const button = $("enter");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  showError("");
  let res;
  let data = null;
  try {
    res = await fetch("/api/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json", "X-Requested-With": "spot2deez" },
      body: JSON.stringify({ user, password }),
    });
    try { data = await res.json(); } catch { /* corpo vazio ou não-JSON */ }
  } catch {
    res = null;
  }
  if (res && res.ok) {
    location.replace("/");
    return;
  }
  button.disabled = false;
  button.removeAttribute("aria-busy");
  if (!res) {
    showError("Sem conexão com o servidor. Confira a internet e tente de novo.");
    return;
  }
  showError(data && typeof data.error === "string" ? data.error : `O servidor respondeu ${res.status}.`);
  if (res.status === 401) {
    $("password").value = "";
    $("password").setAttribute("aria-invalid", "true");
    $("password").focus();
  }
});
