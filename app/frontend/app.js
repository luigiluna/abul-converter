// spot2deez — UI de uma tela, sem framework.
// Dados externos (títulos do Spotify/Deezer, erros) só entram no DOM via textContent.
// `?mock` (ou `?mock=analyzing|ready|downloading|done|error|none`) usa mocks/ em vez da API.
"use strict";

const params = new URLSearchParams(location.search);
const MOCK = params.has("mock");
const ACTIVE = new Set(["analyzing", "downloading", "packaging"]);
const QUALITY_RANK = { MP3_128: 1, MP3: 1, MP3_320: 2, FLAC: 3 };
const QUALITY_LABEL = { FLAC: "FLAC", MP3_320: "MP3 320", MP3_128: "MP3 128", MP3: "MP3" };
const KIND_LABEL = { track: "Música", album: "Álbum", playlist: "Playlist" };
const DL_LABEL = {
  queued: "Na fila", downloading: "Baixando", done: "Baixada",
  failed: "Falhou", cancelled: "Cancelada",
};

const $ = (id) => document.getElementById(id);
const HISTORY_SHOWN = 20;
const HISTORY_SEEN_KEY = "spot2deez.historySeen";const state = { me: null, job: null, timer: 0, lastCover: null, busy: false, historyFor: null };

$("theme").value = document.documentElement.dataset.theme || "system";
$("theme").addEventListener("change", (event) => {
  const value = event.target.value;
  document.documentElement.dataset.theme = value;
  try { localStorage.setItem("spot2deez-theme", value); } catch {}
});

/* ---------------- HTTP ---------------- */

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body) {
  if (MOCK) return mockApi(method, path, body);
  const opts = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
  if (method !== "GET") {
    opts.headers["X-Requested-With"] = "spot2deez";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body ?? {});
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new ApiError(0, "Sem conexão com o servidor. Confira a internet e tente de novo.");
  }
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch { /* corpo vazio ou não-JSON */ }
  if (!res.ok) {
    // sessão expirou (ou saiu em outra aba): volta para o login
    if (res.status === 401) location.replace("/login.html");
    const msg = data && typeof data.error === "string" ? data.error : `O servidor respondeu ${res.status}.`;
    throw new ApiError(res.status, msg);
  }
  return data;
}

/* ---------------- helpers de DOM ---------------- */

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function fmtDur(seconds) {
  if (seconds == null || !isFinite(seconds)) return "";
  const s = Math.round(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function showError(node, msg) {
  node.textContent = msg || "";
  node.hidden = !msg;
}

/* ---------------- job: busca + polling ---------------- */

async function refresh() {
  clearTimeout(state.timer);
  try {
    state.job = await api("GET", "/api/job");
    render();
  } catch (e) {
    showError($("form-error"), e.message);
    // falha temporária durante o polling: tenta de novo com folga
    if (state.job && ACTIVE.has(state.job.phase)) state.timer = setTimeout(refresh, 3000);
    return;
  }
  if (state.job && ACTIVE.has(state.job.phase)) state.timer = setTimeout(refresh, 1000);
}

async function act(method, path, body, button) {
  if (state.busy) return false;
  state.busy = true;
  if (button) { button.disabled = true; button.setAttribute("aria-busy", "true"); }
  showError($("form-error"), "");
  try {
    const snap = await api(method, path, body);
    if (snap && snap.phase) {
      state.job = snap;
      render();
      clearTimeout(state.timer);
      if (ACTIVE.has(snap.phase)) state.timer = setTimeout(refresh, 1000);
    } else {
      await refresh();
    }
    return true;
  } catch (e) {
    showError($("form-error"), e.message);
    render();
    return false;
  } finally {
    state.busy = false;
    if (button && button.isConnected) { button.disabled = false; button.removeAttribute("aria-busy"); }
    render();
  }
}

/* ---------------- render ---------------- */

function render() {
  const job = state.job;
  const has = !!job;
  const active = !!job && ACTIVE.has(job.phase);
  $("analyze").disabled = state.busy || active;
  $("analyze").textContent = job?.phase === "analyzing" ? "Analisando…" : "Analisar";
  $("analyze").setAttribute("aria-busy", String(state.busy || job?.phase === "analyzing"));
  $("url").disabled = active;
  $("dock").dataset.phase = job?.phase || "none";
  document.body.classList.toggle("is-busy", active);
  if (!has) document.title = "Abul Converter";
  $("intro").hidden = has;
  $("sleeve").hidden = !has;
  $("lista").hidden = !has || !job.items || job.items.length === 0;
  $("dock").hidden = !has;
  if (!has) return;
  // um download que acabou de terminar entra na lista de todos
  if (job.phase === "done" && state.historyFor !== job.job_id) loadHistory(job.job_id);

  renderCollection(job);
  renderPhase(job);
  renderItems(job);
  renderDock(job);
}

function renderCollection(job) {
  const c = job.collection;
  $("col-kind").hidden = !c;
  if (!c) {
    $("cover").hidden = true;
    $("cover").removeAttribute("src");
    state.lastCover = null;
    $("col-title").textContent = "Lendo o link…";
    $("col-sub").textContent = "";
    $("truncated").hidden = true;
    return;
  }
  $("col-kind").textContent = KIND_LABEL[c.kind] || c.kind;
  $("col-title").textContent = c.title || "Sem título";
  const n = c.track_count ?? (job.items || []).length;
  const parts = [];
  if (c.subtitle) parts.push(c.subtitle);
  parts.push(n === 1 ? "1 música" : `${n} músicas`);
  $("col-sub").textContent = parts.join(", ");
  $("truncated").hidden = !c.truncated;

  if (c.cover_url !== state.lastCover) {
    state.lastCover = c.cover_url;
    const img = $("cover");
    if (c.cover_url) {
      img.hidden = true;
      img.onload = () => { img.hidden = false; };
      img.onerror = () => { img.hidden = true; img.removeAttribute("src"); };
      img.src = c.cover_url;
    } else {
      img.removeAttribute("src");
      img.hidden = true;
    }
  }
}

function renderPhase(job) {
  const t = job.totals || {};
  const a = job.analysis || { done: 0, total: 0 };
  const meter = $("meter");
  let text = "";
  let pct = null;
  switch (job.phase) {
    case "analyzing":
      text = a.total ? `Procurando cada faixa no Deezer: ${a.done} de ${a.total}` : "Lendo o link do Spotify…";
      pct = a.total ? (100 * a.done) / a.total : 0;
      break;
    case "ready": {
      const bits = [`${t.matched || 0} encontradas`];
      if (t.ambiguous) bits.push(`${t.ambiguous} para você escolher`);
      if (t.not_found) bits.push(`${t.not_found} sem par no Deezer`);
      if (t.skipped) bits.push(`${t.skipped} puladas`);
      text = bits.join(", ") + ".";
      break;
    }
    case "downloading": {
      const total = (t.queued || 0) + (t.downloading || 0) + (t.done || 0) + (t.failed || 0) + (t.cancelled || 0);
      const fin = (t.done || 0) + (t.failed || 0) + (t.cancelled || 0);
      text = `Baixando: ${t.done || 0} de ${total} prontas`;
      // progresso fino: soma o andamento das faixas em curso
      const partial = (job.items || []).reduce(
        (s, it) => s + (it.download && it.download.state === "downloading" ? (it.download.progress || 0) / 100 : 0), 0);
      pct = total ? (100 * (fin + partial)) / total : 0;
      break;
    }
    case "packaging": text = "Juntando tudo num arquivo…"; pct = 100; break;
    case "done": text = "Pronto. O arquivo está esperando por você."; break;
    case "cancelled": text = "Download cancelado."; break;
    case "error": text = "Algo deu errado."; break;
  }
  $("phase-text").textContent = text;
  meter.hidden = pct == null;
  let prefix = job.phase === "done" ? "Pronto · " : "";
  if (pct != null) {
    const v = Math.max(0, Math.min(100, Math.round(pct)));
    $("meter-fill").style.width = v + "%";
    meter.setAttribute("aria-valuenow", String(v));
    prefix = v + "% · ";
  }
  // o andamento aparece na aba, para quem deixa o download rodando em segundo plano
  document.title = prefix + "Abul Converter";
  showError($("job-error"), job.phase === "error" ? job.error || "Erro sem detalhes." : "");
}

function renderItems(job) {
  const list = $("track-list");
  // Preserve disclosures and keyboard focus across polling updates.
  const open = new Set([...list.querySelectorAll("details[open]")].map((node) => node.closest("li").id));
  const focused = document.activeElement;
  const focusedRow = focused?.closest(".track");
  const wasSummary = focused?.tagName === "SUMMARY";
  list.replaceChildren(...(job.items || []).map((it) => renderItem(it, job.phase === "ready", job.phase)));
  for (const id of open) $(id)?.querySelector("details")?.setAttribute("open", "");
  if (focusedRow) {
    const row = $(focusedRow.id);
    (wasSummary ? row?.querySelector("summary") : row)?.focus({ preventScroll: true });
  }
}

function renderItem(it, canChoose, phase) {
  const li = el("li", "track");
  li.id = "t" + it.index;
  li.tabIndex = -1;
  li.classList.toggle("is-skipped", !!it.skipped);
  const m = it.match;
  const d = it.download || {};
  const downloading = d.state && d.state !== "pending" && phase !== "ready";
  const main = el("div", "t-main");
  main.append(el("span", "t-title", it.title || "Sem título"), el("span", "t-artist", (it.artists || []).join(", ")));
  const number = el("span", "track-number", String(it.index + 1).padStart(2, "0"));
  number.setAttribute("aria-hidden", "true");
  const duration = el("span", "t-dur", fmtDur((it.duration_ms || 0) / 1000));
  duration.setAttribute("aria-label", "Duração: " + duration.textContent);
  const format = el("span", "t-format", d.quality ? QUALITY_LABEL[d.quality] || d.quality : "—");
  format.classList.toggle("is-empty", !d.quality);
  format.setAttribute("aria-label", d.quality ? "Formato: " + format.textContent : "Formato disponível após iniciar o download");
  let label = "Em análise";
  if (it.skipped) label = "Pulada";
  else if (downloading) label = DL_LABEL[d.state] || d.state;
  else if (m?.status === "MATCHED") label = "Encontrada";
  else if (m?.status === "AMBIGUOUS") label = canChoose ? "Escolher versão" : "Sem escolha";
  else if (m?.status === "NOT_FOUND") label = "Não encontrada";
  const status = el("span", "track-status", label);
  if (label === "Encontrada" || label === "Não encontrada") {
    const found = label === "Encontrada";
    status.textContent = "";
    status.title = label;
    status.classList.add("has-symbol");
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 20 20");
    icon.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", found ? "M4 10l4 4 8-8" : "M6 6l8 8m0-8-8 8");
    icon.append(path);
    status.append(icon, el("span", "visually-hidden", label));
  }
  status.classList.toggle("is-active", !it.skipped && (d.state === "downloading" || (canChoose && m?.status === "AMBIGUOUS")));
  status.classList.toggle("is-error", !it.skipped && d.state === "failed");
  status.classList.toggle("is-success", !it.skipped && (downloading ? d.state === "done" : m?.status === "MATCHED"));
  status.classList.toggle("is-warning", !it.skipped && !downloading && m?.status === "NOT_FOUND");
  li.append(number, main, duration, format, status);
  if (m?.deezer && !it.skipped) {
    const details = el("details", "match-details");
    details.append(el("summary", null, "Versão no Deezer"), destLine(m.deezer));
    li.append(details);
  }
  if (canChoose && m?.status === "AMBIGUOUS") li.append(choices(it));
  if (downloading && (d.state === "downloading" || d.error)) li.append(dlLine(d));
  return li;
}

function destLine(dz) {
  const p = el("p", "t-dest");
  const label = `${dz.artist || "?"} — ${dz.title || "?"}`;
  p.append(el("span", "visually-hidden", "No Deezer: "));
  p.append(el("span", null, "↳ ")); p.lastChild.setAttribute("aria-hidden", "true");
  p.append(label);
  if (dz.album) p.append(` [${dz.album}]`);
  p.append(el("span", "d-dur", fmtDur(dz.duration_s)));
  return p;
}

function choices(it) {
  const fs = el("fieldset", "alts");
  fs.append(el("legend", null, it.skipped ? "Pulada. Prefere alguma destas?" : "Qual destas é a certa?"));
  for (const alt of it.match.alternatives || []) {
    const dz = alt.deezer || {};
    const b = el("button", "alt");
    b.type = "button";
    const left = el("span", null, `${dz.artist || "?"} — ${dz.title || "?"}`);
    if (dz.album) left.append(` [${dz.album}]`);
    const pct = alt.score != null ? `${Math.round(alt.score * 100)}%` : "";
    b.append(left, el("span", "a-meta", [fmtDur(dz.duration_s), pct].filter(Boolean).join("  ")));
    b.setAttribute("aria-label", `Escolher ${dz.artist || ""} — ${dz.title || ""}${dz.album ? `, álbum ${dz.album}` : ""}, ${fmtDur(dz.duration_s)}, semelhança ${pct}`);
    b.addEventListener("click", () => choose(it.index, dz.id, b));
    fs.append(b);
  }
  if (!it.skipped) {
    const skip = el("button", "btn btn-quiet alt-skip", "Pular esta música");
    skip.type = "button";
    skip.addEventListener("click", () => choose(it.index, null, skip));
    fs.append(skip);
  }
  return fs;
}

function dlLine(d) {
  const box = el("div", "dl");
  if (d.state === "downloading") {
    const bar = el("span", "dl-bar");
    const fill = el("span");
    fill.style.width = `${Math.max(0, Math.min(100, d.progress || 0))}%`;
    bar.append(fill);
    box.append(bar, el("span", "d-dur", `${Math.round(d.progress || 0)}%`));
  }
  if (d.error) box.append(el("span", "dl-err", d.error));
  return box;
}

function renderDock(job) {
  const t = job.totals || {};
  const phase = job.phase;
  const downloadable = (job.items || []).some((it) => !it.skipped && it.match && it.match.status === "MATCHED");
  const dl = $("download");
  dl.hidden = !(phase === "ready" || phase === "analyzing");
  dl.disabled = state.busy || phase !== "ready" || !downloadable;
  $("cancel").disabled = state.busy;
  $("cancel").hidden = !(phase === "downloading" || phase === "packaging");
  $("reset").disabled = state.busy;
  $("reset").hidden = phase === "downloading" || phase === "packaging";
  const file = $("file");
  if (job.download_url) {
    file.href = job.download_url;
    file.hidden = false;
  } else {
    file.hidden = true;
    file.removeAttribute("href");
  }

  let tally;
  if (phase === "ready" || phase === "analyzing") {
    tally = `${t.matched || 0} para baixar` + (t.ambiguous ? `, ${t.ambiguous} esperando escolha` : "");
  } else {
    tally = `${t.done || 0} concluídas, ${t.failed || 0} falhas, ${t.cancelled || 0} canceladas`;
    if (t.queued || t.downloading) tally += `, ${(t.queued || 0) + (t.downloading || 0)} faltando`;
  }
  if (phase === "ready") tally = "Confira as versões antes de baixar.";
  else if (phase === "analyzing") tally = "Buscando correspondências no Deezer.";
  else if (phase === "cancelled") tally = "Cole o link novamente para tentar de novo.";
  else if (phase === "error") tally = "Confira o erro abaixo e tente novamente.";
  $("tally").textContent = tally;

  const warn = $("quality-warn");
  const want = state.me && state.me.quality;
  const max = job.account && job.account.max_quality;
  if (want && max && (QUALITY_RANK[max] || 0) < (QUALITY_RANK[want] || 0)) {
    warn.textContent = `Sua conta Deezer só permite ${QUALITY_LABEL[max] || max}; os arquivos saem nessa qualidade.`;
    warn.hidden = false;
  } else {
    warn.hidden = true;
  }
}

/* ---------------- ações ---------------- */

async function choose(index, deezerId, button) {
  const ok = await act("POST", `/api/job/items/${index}/choose`, { deezer_id: deezerId }, button);
  if (ok) {
    const row = $(`t${index}`);
    if (row) row.focus({ preventScroll: true });
  }
}

$("seek").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const url = $("url").value.trim();
  if (!url) {
    showError($("form-error"), "Cole um link do Spotify antes de analisar.");
    $("url").setAttribute("aria-invalid", "true");
    $("url").focus();
    return;
  }
  $("url").removeAttribute("aria-invalid");
  const ok = await act("POST", "/api/analyze", { url }, $("analyze"));
  if (!ok) $("url").focus();
});

$("download").addEventListener("click", (ev) => act("POST", "/api/job/download", {}, ev.currentTarget));
$("cancel").addEventListener("click", (ev) => act("POST", "/api/job/cancel", {}, ev.currentTarget));
// Limpar: esquece o job no servidor e volta para a tela inicial.
$("reset").addEventListener("click", async (ev) => {
  if (!(await act("DELETE", "/api/job", {}, ev.currentTarget))) return;
  $("url").value = "";
  $("url").removeAttribute("aria-invalid");
  $("url").focus();
});

$("logout").addEventListener("click", async (ev) => {
  ev.currentTarget.disabled = true;
  try { await api("POST", "/api/logout"); } catch { /* sem sessão dá no mesmo */ }
  location.replace("/login.html");
});

/* ---------------- ajustes ---------------- */

function renderMe() {
  const me = state.me;
  if (!me) return;
  const who = $("who");
  who.textContent = me.user;
  who.hidden = !me.user;
  who.title = "Perfil: " + (me.user || "");
  $("logout").hidden = !me.user;
  $("settings-preview").textContent = QUALITY_LABEL[me.quality] || me.quality;
  for (const r of document.querySelectorAll('input[name="quality"]')) r.checked = r.value === me.quality;

  let arlText;
  if (me.own_arl) arlText = "Usando seu ARL.";
  else if (me.default_arl_configured) arlText = "Usando o ARL padrão do servidor.";
  else arlText = "Nenhum ARL configurado no servidor. Salve o seu para poder baixar.";
  $("arl-state").textContent = arlText;
  $("arl-remove").hidden = !me.own_arl;
  if (state.job) renderDock(state.job);
}

async function saveMe(patch, msg) {
  if ($("settings").getAttribute("aria-busy") === "true") return false;
  $("settings").setAttribute("aria-busy", "true");
  $("settings-msg").classList.remove("is-error");
  $("settings-msg").textContent = "Salvando…";
  const controls = [...$("settings").querySelectorAll("input, button")];
  controls.forEach((control) => { control.disabled = true; });
  try {
    state.me = await api("PUT", "/api/me", patch);
    $("settings-msg").textContent = msg;
    renderMe();
    return true;
  } catch (e) {
    $("settings-msg").classList.add("is-error");
    $("settings-msg").textContent = e.message;
    renderMe();
    return false;
  } finally {
    controls.forEach((control) => { control.disabled = false; });
    $("settings").removeAttribute("aria-busy");
  }
}

$("settings").addEventListener("change", (ev) => {
  const t = ev.target;
  if (t.name === "quality") saveMe({ quality: t.value }, `Qualidade salva: ${QUALITY_LABEL[t.value]}.`);
});

$("arl-save").addEventListener("click", async (ev) => {
  const inp = $("arl");
  const v = inp.value.trim();
  if (!v) { $("settings-msg").textContent = "Cole o ARL no campo antes de salvar."; inp.focus(); return; }
  if (await saveMe({ arl: v }, "ARL salvo.")) inp.value = "";
});
$("arl-remove").addEventListener("click", (ev) =>
  saveMe({ arl: null }, "ARL removido. Voltando ao ARL padrão."));

/* ---------------- histórico (de todos os usuários) ---------------- */

async function loadHistory(forJob) {
  state.historyFor = forJob || "boot";
  let items;
  try {
    items = (await api("GET", "/api/history")).items || [];
  } catch {
    return; // enfeite: se falhar, a seção só não aparece
  }
  items = items.slice(0, HISTORY_SHOWN);
  $("history-list").replaceChildren(...items.map(historyItem));
  $("history-empty").hidden = items.length > 0;
  // The badge counts what other people downloaded since this browser last opened the drawer.
  let seen = 0;
  try {
    if ($("history").classList.contains("is-open") && items.length) localStorage.setItem(HISTORY_SEEN_KEY, items[0].at);
    seen = Number(localStorage.getItem(HISTORY_SEEN_KEY)) || 0;
  } catch { /* sem storage: o contador só não zera */ }
  const fresh = items.filter((h) => h.at > seen && h.user !== state.me?.user).length;
  $("history-badge").textContent = fresh > 9 ? "9+" : fresh;
  $("history-badge").hidden = fresh === 0 || $("history").classList.contains("is-open");
  $("history-open").setAttribute("aria-label", "Baixados recentemente" + (fresh ? `, ${fresh} novos` : ""));
}

function setDrawer(open) {
  const drawer = $("history"), toggle = $("history-open");
  if (drawer.classList.contains("is-open") === open) return;
  drawer.classList.toggle("is-open", open);
  drawer.inert = !open;
  toggle.setAttribute("aria-expanded", String(open));
  if (open) {
    $("history-badge").hidden = true;
    $("history-close").focus();
    loadHistory(state.historyFor);
  } else if (drawer.contains(document.activeElement)) {
    toggle.focus();
  }
}

$("history-open").addEventListener("click", () => setDrawer(!$("history").classList.contains("is-open")));
$("history-close").addEventListener("click", () => setDrawer(false));document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") setDrawer(false); });
document.addEventListener("click", (ev) => {
  if (!$("history").contains(ev.target) && !$("history-open").contains(ev.target)) setDrawer(false);
});

function fmtAgo(at) {
  if (!at) return "";
  const min = Math.floor((Date.now() / 1000 - at) / 60);
  if (min < 1) return "agora";
  if (min < 60) return `há ${min} min`;
  if (min < 60 * 24) return `há ${Math.floor(min / 60)} h`;
  if (min < 60 * 24 * 30) return `há ${Math.floor(min / 60 / 24)} d`;
  return new Date(at * 1000).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit", year: "2-digit" });
}

function historyItem(h) {
  const li = el("li");
  const b = el("button", "history-item");
  b.type = "button";
  const frame = el("span", "history-cover");
  frame.append(el("span", "cover-placeholder", "♫"));
  if (h.cover_url) {
    const img = el("img");
    img.alt = "";
    img.loading = "lazy";
    img.decoding = "async";
    img.onerror = () => img.remove();
    img.src = h.cover_url;
    frame.append(img);
  }
  const title = h.title || "Sem título";
  const main = el("span", "history-main");
  main.append(el("span", "history-title", title),
    el("span", "history-sub", [KIND_LABEL[h.kind] || h.kind, h.subtitle].filter(Boolean).join(" · ")));
  const meta = el("span", "history-meta");
  if (h.quality) meta.append(el("span", "t-format", QUALITY_LABEL[h.quality] || h.quality));
  const when = el("span", "history-when");
  when.append(el("span", "history-user", h.user || ""), el("span", null, fmtAgo(h.at)));
  meta.append(when);
  b.append(frame, main, meta);
  b.title = "Usar este link";
  b.setAttribute("aria-label",
    `${[title, h.subtitle].filter(Boolean).join(", ")}. Baixado por ${h.user}, ${fmtAgo(h.at)}. Usar este link.`);
  b.addEventListener("click", () => {
    const url = $("url");
    if (!h.url || url.disabled) return;
    url.value = h.url;
    url.removeAttribute("aria-invalid");
    showError($("form-error"), "");
    setDrawer(false);
    $("seek").scrollIntoView({ block: "start" });
    url.focus({ preventScroll: true });
  });
  li.append(b);
  return li;
}

/* ---------------- início ---------------- */

async function boot() {
  try {
    state.me = await api("GET", "/api/me");
    renderMe();
  } catch (e) {
    showError($("form-error"), e.message);
  }
  await refresh();
  if (!state.job) render();
  if (!state.historyFor) loadHistory();
}

/* ---------------- mock (só com ?mock) ---------------- */

const mock = { job: undefined, me: null, ticks: 0 };

async function loadMock(name) {
  const res = await fetch(`mocks/${name}.json`, { cache: "no-store" });
  return res.json();
}

function mockTotals(job) {
  const t = { matched: 0, ambiguous: 0, not_found: 0, skipped: 0, queued: 0, downloading: 0, done: 0, failed: 0, cancelled: 0 };
  for (const it of job.items) {
    if (it.skipped) t.skipped++;
    else if (it.match) t[it.match.status.toLowerCase()]++;
    if (it.download.state in t) t[it.download.state]++;
  }
  job.totals = t;
}

function mockTick(job) {
  mock.ticks++;
  if (job.phase === "analyzing") {
    job.analysis.done = Math.min(job.analysis.total, job.analysis.done + 2);
    if (job.analysis.done >= job.analysis.total) mock.pending = "ready";
  } else if (job.phase === "downloading") {
    let busy = 0;
    for (const it of job.items) {
      const d = it.download;
      if (d.state === "downloading") {
        d.progress = Math.min(100, d.progress + 29);
        if (d.progress >= 100) d.state = "done";
      }
      if (d.state === "downloading") busy++;
    }
    for (const it of job.items) {
      if (busy >= 2) break;
      if (it.download.state === "queued") {
        Object.assign(it.download, { state: "downloading", progress: 5, quality: job.account.max_quality });
        busy++;
      }
    }
    if (!job.items.some((it) => ["queued", "downloading"].includes(it.download.state))) job.phase = "packaging";
  } else if (job.phase === "packaging") {
    job.phase = "done";
    job.download_url = "/api/job/file";
  }
  mockTotals(job);
}

async function mockApi(method, path, body) {
  await new Promise((r) => setTimeout(r, 150));
  if (mock.job === undefined) {
    const scen = params.get("mock") || "ready";
    mock.job = scen === "none" ? null : await loadMock(`job-${scen}`);
    mock.me = await loadMock("me");
  }
  const job = mock.job;
  const fail = (s, m) => { throw new ApiError(s, m); };

  if (path === "/api/me") {
    if (method === "PUT") {
      if ("quality" in body) mock.me.quality = body.quality;
      if ("concurrency" in body) mock.me.concurrency = body.concurrency;
      if ("arl" in body) mock.me.own_arl = body.arl != null;
    }
    return structuredClone(mock.me);
  }
  if (path === "/api/history") return loadMock("history");
  if (path === "/api/analyze") {
    if (job && ["downloading", "packaging"].includes(job.phase)) fail(409, "Já existe um download em andamento. Cancele ou espere terminar.");
    if (!/spotify/.test(body.url || "")) fail(400, "Esse link não parece ser do Spotify. Use um link de música, álbum ou playlist.");
    mock.job = await loadMock("job-analyzing");
    mock.job.analysis.done = 0;
    return { job_id: mock.job.job_id };
  }
  if (path === "/api/job") {
    if (method === "DELETE") { mock.job = null; mock.pending = null; return null; }
    if (!job) return null;
    if (mock.pending) { mock.job = await loadMock(`job-${mock.pending}`); mock.pending = null; return structuredClone(mock.job); }
    if (ACTIVE.has(job.phase)) mockTick(job);
    if (job.download_url) job.download_url = "mocks/cover.svg"; // arquivo local qualquer p/ testar o link
    return structuredClone(job);
  }
  if (!job) fail(404, "Nenhuma análise em andamento.");
  const ch = path.match(/^\/api\/job\/items\/(\d+)\/choose$/);
  if (ch) {
    if (job.phase !== "ready") fail(409, "Só dá para escolher antes de baixar.");
    const it = job.items[Number(ch[1])];
    if (!it) fail(404, "Música não encontrada.");
    if (body.deezer_id == null) {
      it.skipped = true;
    } else {
      const alt = (it.match.alternatives || []).find((a) => a.deezer.id === body.deezer_id);
      if (!alt) fail(400, "Essa opção não pertence a esta música.");
      Object.assign(it.match, { status: "MATCHED", method: "manual", deezer: alt.deezer, score: alt.score });
      it.skipped = false;
    }
    mockTotals(job);
    return structuredClone(job);
  }
  if (path === "/api/job/download") {
    if (job.phase !== "ready") fail(409, "Esse job não está pronto para baixar.");
    job.phase = "downloading";
    job.account = { max_quality: "MP3_128", using_own_arl: mock.me.own_arl };
    for (const it of job.items) {
      if (!it.skipped && it.match && it.match.status === "MATCHED") it.download.state = "queued";
    }
    const victim = job.items.find((it) => it.download.state === "queued" && it.index > 2);
    if (victim) Object.assign(victim.download, { state: "failed", error: "Faixa indisponível no seu país" });
    mockTotals(job);
    return structuredClone(job);
  }
  if (path === "/api/job/cancel") {
    if (ACTIVE.has(job.phase)) {
      job.phase = "cancelled";
      for (const it of job.items) {
        if (["queued", "downloading"].includes(it.download.state)) it.download.state = "cancelled";
      }
      mockTotals(job);
    }
    return structuredClone(job);
  }
  fail(404, "Rota desconhecida no mock.");
}

boot();
