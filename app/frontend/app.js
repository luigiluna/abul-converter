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
const STATUS = {
  MATCHED:   { sym: "✓", cls: "g-ok",  label: "encontrada no Deezer" },
  AMBIGUOUS: { sym: "!", cls: "g-amb", label: "mais de uma opção, escolha uma" },
  NOT_FOUND: { sym: "✕", cls: "g-nf",  label: "não encontrada no Deezer" },
};
const DL_LABEL = {
  queued: "Na fila", downloading: "Baixando", done: "Baixada",
  failed: "Falhou", cancelled: "Cancelada",
};

const $ = (id) => document.getElementById(id);
const state = { me: null, job: null, timer: 0, lastCover: null, lastStatus: new Map() };

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
  if (button) button.disabled = true;
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
    if (button && button.isConnected) button.disabled = false;
  }
}

/* ---------------- render ---------------- */

function render() {
  const job = state.job;
  const has = !!job;
  $("intro").hidden = has;
  $("sleeve").hidden = !has;
  $("lista").hidden = !has || !job.items || job.items.length === 0;
  $("dock").hidden = !has;
  if (!has) return;

  renderCollection(job);
  renderPhase(job);
  renderItems(job);
  renderDock(job);
}

function renderCollection(job) {
  const c = job.collection;
  $("col-kind").hidden = !c;
  if (!c) {
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
      img.crossOrigin = "anonymous";
      img.onload = () => tintFrom(img);
      img.onerror = () => { img.removeAttribute("src"); resetTint(); };
      img.src = c.cover_url;
    } else {
      img.removeAttribute("src");
      resetTint();
    }
  }
}

function renderPhase(job) {
  const t = job.totals || {};
  const a = job.analysis || { done: 0, total: 0 };
  const melt = $("meter");
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
    case "cancelled": text = "Download cancelado. Nada foi guardado."; break;
    case "error": text = "Algo deu errado."; break;
  }
  $("phase-text").textContent = text;
  melt.hidden = pct == null;
  if (pct != null) {
    const v = Math.max(0, Math.min(100, Math.round(pct)));
    $("meter-fill").style.width = v + "%";
    melt.setAttribute("aria-valuenow", String(v));
  }
  showError($("job-error"), job.phase === "error" ? job.error || "Erro sem detalhes." : "");
  $("sleeve").classList.toggle("spin", job.phase === "downloading" || job.phase === "packaging");
}

function renderItems(job) {
  const list = $("track-list");
  const canChoose = job.phase === "ready";
  const rows = (job.items || []).map((it) => renderItem(it, canChoose, job.phase));
  list.replaceChildren(...rows);
}

function renderItem(it, canChoose, phase) {
  const li = el("li", "track");
  li.id = `t${it.index}`;
  li.tabIndex = -1;
  const m = it.match;
  const st = m ? STATUS[m.status] : null;

  // glifo de estado: símbolo + rótulo acessível (nunca só cor)
  const g = el("span", "glyph");
  g.setAttribute("role", "img");
  let key;
  if (it.skipped) {
    g.classList.add("g-skip"); g.textContent = "–"; g.setAttribute("aria-label", "pulada"); key = "skip";
  } else if (st) {
    g.classList.add(st.cls); g.textContent = st.sym; g.setAttribute("aria-label", st.label); key = m.status;
  } else {
    g.classList.add("g-wait"); g.textContent = "…"; g.setAttribute("aria-label", "aguardando análise"); key = "wait";
  }
  const prev = state.lastStatus.get(it.index);
  if (prev && prev !== key) g.classList.add("changed");
  state.lastStatus.set(it.index, key);
  li.classList.toggle("is-skipped", !!it.skipped);
  li.append(g);

  // linha do Spotify
  const main = el("p", "t-main");
  const artists = (it.artists || []).join(", ");
  if (artists) main.append(el("span", "t-artist", artists), " — ");
  main.append(el("span", "t-title", it.title || "Sem título"));
  li.append(main, el("span", "t-dur", fmtDur((it.duration_ms || 0) / 1000)));

  // destino no Deezer
  if (m && m.deezer) {
    li.append(destLine(m.deezer));
  } else if (m && m.status === "NOT_FOUND") {
    li.append(el("p", "t-dest none", "Nenhuma versão confiável no Deezer; fica de fora."));
  } else if (m && m.status === "AMBIGUOUS" && !canChoose) {
    li.append(el("p", "t-dest", "Ficou sem escolha; não será baixada."));
  }

  if (canChoose && m && m.status === "AMBIGUOUS") li.append(choices(it));

  const d = it.download;
  if (d && d.state && d.state !== "pending" && phase !== "ready") li.append(dlLine(d));
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
  box.append(el("span", `dl-state s-${d.state}`, DL_LABEL[d.state] || d.state));
  if (d.state === "downloading") {
    const bar = el("span", "dl-bar");
    const fill = el("span");
    fill.style.width = `${Math.max(0, Math.min(100, d.progress || 0))}%`;
    bar.append(fill);
    box.append(bar, el("span", "d-dur", `${Math.round(d.progress || 0)}%`));
  }
  if (d.quality) box.append(el("span", "dl-q", QUALITY_LABEL[d.quality] || d.quality));
  if (d.error) box.append(el("span", "dl-err", d.error));
  return box;
}

function renderDock(job) {
  const t = job.totals || {};
  const phase = job.phase;
  const downloadable = (job.items || []).some((it) => !it.skipped && it.match && it.match.status === "MATCHED");
  const dl = $("download");
  dl.hidden = !(phase === "ready" || phase === "analyzing");
  dl.disabled = phase !== "ready" || !downloadable;
  $("cancel").hidden = !(phase === "downloading" || phase === "packaging");
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

/* ---------------- cores da capa ---------------- */

function resetTint() {
  for (const k of ["--c1", "--c2", "--c3", "--c4"]) document.documentElement.style.removeProperty(k);
}

// Lê a capa num canvas 24×24 e pega as 3–4 cores saturadas mais frequentes.
// Só tinge a moldura (anéis, centro do disco); texto nunca usa essas cores.
function tintFrom(img) {
  let data;
  try {
    const cv = document.createElement("canvas");
    cv.width = cv.height = 24;
    const cx = cv.getContext("2d", { willReadFrequently: true });
    cx.drawImage(img, 0, 0, 24, 24);
    data = cx.getImageData(0, 0, 24, 24).data;
  } catch {
    resetTint(); // CORS bloqueou a leitura: fica a paleta da casa
    return;
  }
  const buckets = new Map();
  for (let i = 0; i < data.length; i += 4) {
    const [r, g, b] = [data[i], data[i + 1], data[i + 2]];
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
    const sat = mx ? (mx - mn) / mx : 0;
    if (sat < 0.3 || mx < 60) continue; // ignora cinzas e quase-pretos
    const k = ((r >> 5) << 6) | ((g >> 5) << 3) | (b >> 5);
    const e = buckets.get(k) || { n: 0, r: 0, g: 0, b: 0 };
    e.n++; e.r += r; e.g += g; e.b += b;
    buckets.set(k, e);
  }
  const top = [...buckets.values()].sort((a, b) => b.n - a.n).slice(0, 4)
    .map((e) => `rgb(${Math.round(e.r / e.n)} ${Math.round(e.g / e.n)} ${Math.round(e.b / e.n)})`);
  if (top.length < 2) { resetTint(); return; }
  const root = document.documentElement.style;
  ["--c1", "--c2", "--c3", "--c4"].forEach((k, i) => {
    if (top[i]) root.setProperty(k, top[i]); else root.removeProperty(k);
  });
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
    $("url").focus();
    return;
  }
  state.lastStatus.clear();
  const ok = await act("POST", "/api/analyze", { url }, $("analyze"));
  if (!ok) $("url").focus();
});

$("download").addEventListener("click", (ev) => act("POST", "/api/job/download", {}, ev.currentTarget));
$("cancel").addEventListener("click", (ev) => act("POST", "/api/job/cancel", {}, ev.currentTarget));

/* ---------------- ajustes ---------------- */

function renderMe() {
  const me = state.me;
  if (!me) return;
  const who = $("who");
  who.textContent = me.user;
  who.hidden = !me.user;
  for (const r of document.querySelectorAll('input[name="quality"]')) r.checked = r.value === me.quality;

  const conc = $("conc");
  const max = Math.max(1, me.max_concurrency || 3);
  if (conc.querySelectorAll("input").length !== max) {
    conc.querySelectorAll("label").forEach((n) => n.remove());
    for (let i = 1; i <= max; i++) {
      const lab = el("label");
      const inp = el("input");
      inp.type = "radio"; inp.name = "concurrency"; inp.value = String(i);
      lab.append(inp, el("span", null, String(i)));
      conc.append(lab);
    }
  }
  for (const r of conc.querySelectorAll("input")) r.checked = Number(r.value) === me.concurrency;

  let arlText;
  if (me.own_arl) arlText = "Usando seu ARL.";
  else if (me.default_arl_configured) arlText = "Usando o ARL padrão do servidor.";
  else arlText = "Nenhum ARL configurado no servidor. Salve o seu para poder baixar.";
  $("arl-state").textContent = arlText;
  $("arl-remove").hidden = !me.own_arl;
  if (state.job) renderDock(state.job);
}

async function saveMe(patch, msg, button) {
  if (button) button.disabled = true;
  try {
    state.me = await api("PUT", "/api/me", patch);
    $("settings-msg").textContent = msg;
    renderMe();
    return true;
  } catch (e) {
    $("settings-msg").textContent = e.message;
    renderMe();
    return false;
  } finally {
    if (button) button.disabled = false;
  }
}

$("settings").addEventListener("change", (ev) => {
  const t = ev.target;
  if (t.name === "quality") saveMe({ quality: t.value }, `Qualidade salva: ${QUALITY_LABEL[t.value]}.`);
  if (t.name === "concurrency") saveMe({ concurrency: Number(t.value) }, `Até ${t.value} downloads ao mesmo tempo.`);
});

$("arl-save").addEventListener("click", async (ev) => {
  const inp = $("arl");
  const v = inp.value.trim();
  if (!v) { $("settings-msg").textContent = "Cole o ARL no campo antes de salvar."; inp.focus(); return; }
  if (await saveMe({ arl: v }, "ARL salvo.", ev.currentTarget)) inp.value = "";
});
$("arl-remove").addEventListener("click", (ev) =>
  saveMe({ arl: null }, "ARL removido. Voltando ao ARL padrão.", ev.currentTarget));

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
  if (path === "/api/analyze") {
    if (job && ["downloading", "packaging"].includes(job.phase)) fail(409, "Já existe um download em andamento. Cancele ou espere terminar.");
    if (!/spotify/.test(body.url || "")) fail(400, "Esse link não parece ser do Spotify. Use um link de música, álbum ou playlist.");
    mock.job = await loadMock("job-analyzing");
    mock.job.analysis.done = 0;
    return { job_id: mock.job.job_id };
  }
  if (path === "/api/job") {
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
