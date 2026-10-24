// Интерфейс помощника: чат, черновик по модулю, маршрут, проверка, журнал.
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const history = [];
let lastSources = [];
let modulesCache = [];
let matrixCache = [];

if (new URLSearchParams(location.search).has("widget")) document.body.classList.add("widget");

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (r.status === 401) { showLogin(); throw new Error("401"); }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

// ---- Вход ----
function showLogin() { $("#login").classList.remove("hidden"); $("#app").classList.add("hidden"); }
async function start() {
  try {
    const me = await api("/api/me");
    $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
    $("#whoami").textContent = me.user;
    document.querySelectorAll(".adminOnly").forEach((e) => e.classList.toggle("hidden", me.role !== "doc_admin"));
    loadStatus(); loadModules(); loadMatrix();
  } catch (e) { /* покажется форма входа */ }
}
$("#loginForm").onsubmit = async (e) => {
  e.preventDefault(); $("#lgErr").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ login: $("#lgLogin").value, password: $("#lgPass").value }) });
    start();
  } catch (err) { $("#lgErr").textContent = "Неверный логин или пароль"; }
};
$("#logout").onclick = async (e) => { e.preventDefault(); await api("/api/logout", { method: "POST" }); showLogin(); };

async function loadStatus() {
  const s = await api("/api/status");
  const el = $("#status");
  el.textContent = s.llm ? "модель онлайн" : "модель офлайн — только цитаты";
  el.className = "pill " + (s.llm ? "ok" : "warn");
  $("#kb").innerHTML = `Версия базы: <b>${s.version}</b>, фрагментов: ${s.chunks} ` +
    `(${Object.entries(s.by_source).map(([k, v]) => `${k}: ${v}`).join(", ")}). ` +
    `Векторный поиск: ${s.vectors ? "включён" : "выключен"}. Модель: ${esc(s.model)}.`;
}

// ---- Вкладки ----
function openTab(name) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + name));
  if (name === "admin") loadLog();
}
document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => openTab(b.dataset.tab)));

// ---- Чат ----
function renderAnswer(text, sources) {
  // [п. 10.3] -> кликабельная ссылка на источник
  return esc(text).replace(/\[([^\[\]]{2,120})\]/g, (m, ref) => {
    const i = sources.findIndex((s) => s.ref.toLowerCase() === ref.trim().toLowerCase());
    return i >= 0 ? `<span class="ref" data-i="${i}">[${esc(ref)}]</span>` : `[${esc(ref)}]`;
  });
}

function sourcesHtml(sources) {
  if (!sources.length) return "";
  const card = (s, i) =>
    `<div class="src" data-i="${i}"><span class="rel">${Math.round(s.relevance * 100)}%</span><b>${esc(s.ref)}</b> · ${esc(s.title)}<br>${esc(s.text.slice(0, 160))}…</div>`;
  const top = sources.slice(0, 3).map(card).join("");
  const rest = sources.slice(3).map((s, i) => card(s, i + 3)).join("");
  return `<div class="sources">${top}${rest ? `<details><summary class="muted small">ещё ${sources.length - 3}</summary><div class="sources">${rest}</div></details>` : ""}</div>`;
}

const STATUS = { verified: "сверено с источником", unverified: "требует проверки", extractive: "дословная цитата", no_answer: "ответа нет в базе" };

function addMsg(cls, html) {
  const d = document.createElement("div");
  d.className = "msg " + cls; d.innerHTML = html;
  $("#messages").appendChild(d); d.scrollIntoView({ behavior: "smooth", block: "end" });
  return d;
}

async function ask(q) {
  addMsg("user", esc(q));
  const bot = addMsg("bot typing", "Ищу в Инструкции…");
  let text = "", sources = [];
  try {
    const r = await fetch("/api/ask/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q, history }) });
    if (r.status === 401) return showLogin();
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let k;
      while ((k = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, k); buf = buf.slice(k + 2);
        const ev = /event: (\w+)/.exec(block)?.[1]; const data = JSON.parse(/data: (.*)/s.exec(block)[1]);
        if (ev === "sources") { sources = data.sources; if (data.found) bot.textContent = ""; }
        if (ev === "token") { text += data.t; bot.innerHTML = renderAnswer(text, sources); }
        if (ev === "done") finishAnswer(bot, q, data);
      }
    }
  } catch (e) { bot.classList.remove("typing"); bot.textContent = "Ошибка: " + e.message; }
}

function finishAnswer(bot, q, res) {
  bot.classList.remove("typing");
  lastSources = res.sources;
  bot._sources = res.sources;
  let html = renderAnswer(res.answer, res.sources);
  html += `<div><span class="badge ${res.status}">${STATUS[res.status] || res.status}</span></div>`;
  html += sourcesHtml(res.sources);
  const acts = [];
  if (res.module) acts.push(`<button data-act="draft" data-mod="${esc(res.module.module_id)}">Подготовить черновик: ${esc(res.module.title)}</button>`);
  if (res.route) acts.push(`<button data-act="route" data-route="${esc(res.route.group)}|${res.route.row}">Показать маршрут визирования</button>`);
  acts.push(`<button data-act="rate" data-v="1">👍 полезно</button><button data-act="rate" data-v="-1">👎 не то</button>`);
  html += `<div class="actions">${acts.join("")}</div>`;
  bot.innerHTML = html;
  bot.dataset.log = res.log_id; bot.dataset.q = q;
  history.push({ role: "user", content: q }, { role: "assistant", content: res.answer });
}

$("#messages").addEventListener("click", async (e) => {
  const t = e.target.closest("[data-i],[data-act],.chip"); if (!t) return;
  const msg = t.closest(".msg");
  if (t.classList.contains("chip")) return ask(t.textContent);
  if (t.dataset.i !== undefined && msg?._sources) return showSource(msg._sources[+t.dataset.i]);
  if (t.dataset.act === "rate") {
    await api("/api/feedback", { method: "POST", body: JSON.stringify({ log_id: +msg.dataset.log, rating: +t.dataset.v }) });
    t.parentElement.querySelectorAll("[data-act=rate]").forEach((b) => (b.disabled = true));
  }
  if (t.dataset.act === "route") { openTab("route"); $("#rowSel").value = t.dataset.route; $("#routeQuestions").innerHTML = ""; loadRoute(); }
  if (t.dataset.act === "draft") { openTab("draft"); $("#modSel").value = t.dataset.mod; await loadSlots(); $("#modDesc").value = msg.dataset.q; }
});

async function showSource(s) {
  const full = await api("/api/source/" + encodeURIComponent(s.id));
  $("#srcTitle").textContent = `${full.ref} — ${full.title}`;
  $("#srcText").textContent = full.full_text;
  $("#srcDlg").showModal();
}

$("#askForm").onsubmit = (e) => { e.preventDefault(); const q = $("#q").value.trim(); if (q) { $("#q").value = ""; ask(q); } };
$("#q").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#askForm").requestSubmit(); } });

// ---- Черновик по модулю ----
let currentSlots = [];
async function loadModules() {
  modulesCache = await api("/api/modules");
  $("#modSel").innerHTML = modulesCache.map((m) => `<option value="${esc(m.id)}">${esc(m.kind)}: ${esc(m.heading)}</option>`).join("");
  $("#modSel").onchange = loadSlots;
  loadSlots();
}

async function loadSlots() {
  const m = await api("/api/modules/" + encodeURIComponent($("#modSel").value));
  currentSlots = m.slots; $("#fillResult").innerHTML = "";
  $("#slots").innerHTML = m.slots.map((s) => {
    const input = s.kind === "choice"
      ? `<select data-slot="${s.id}"><option value="">— выберите —</option>${s.options.map((o) => `<option>${esc(o)}</option>`).join("")}</select>`
      : `<input data-slot="${s.id}" placeholder="${s.kind === "date" ? "ДД.ММ.ГГГГ" : ""}">`;
    return `<div class="slot" id="slot-${s.id}"><label>${esc(s.label)}${s.footnote ? ` <span class="muted">(сноска ${s.footnote})</span>` : ""}</label>
      <div class="ctx">…${esc(s.context)} ___</div>${input}${s.hint ? `<div class="ctx">${esc(s.hint)}</div>` : ""}</div>`;
  }).join("");
  $("#modFill").dataset.matrix = JSON.stringify(m.matrix);
}

$("#modExtract").onclick = async () => {
  const b = $("#modExtract"); b.disabled = true;
  const t0 = Date.now();
  const tick = setInterval(() => (b.textContent = `Модель заполняет поля… ${Math.round((Date.now() - t0) / 1000)} с`), 500);
  try {
    const r = await api(`/api/modules/${encodeURIComponent($("#modSel").value)}/extract`, { method: "POST", body: JSON.stringify({ description: $("#modDesc").value }) });
    for (const [k, v] of Object.entries(r.values)) {
      const el = document.querySelector(`[data-slot="${k}"]`);
      if (el) { el.value = v; el.closest(".slot").classList.add("filled"); }
    }
  } catch (e) { alert(e.message); }
  clearInterval(tick);
  b.disabled = false; b.textContent = "Заполнить поля по описанию";
};

$("#modFill").onclick = async () => {
  const values = {};
  document.querySelectorAll("[data-slot]").forEach((el) => (values[el.dataset.slot] = el.value));
  const r = await api(`/api/modules/${encodeURIComponent($("#modSel").value)}/fill`, { method: "POST", body: JSON.stringify({ values }) });
  const matrix = JSON.parse($("#modFill").dataset.matrix || "null");
  $("#fillResult").innerHTML =
    `<p><a href="${r.url}">⬇ Скачать ${esc(r.file)}</a></p>` +
    (r.warnings.length ? r.warnings.map((w) => `<div class="issue">${esc(w)}</div>`).join("") : `<div class="good">Все поля заполнены. Служебные элементы модуля удалены по п. 3.3 Методических рекомендаций.</div>`) +
    (matrix ? `<p><button id="toRoute">Показать маршрут визирования</button></p>` : "");
  if (matrix) $("#toRoute").onclick = () => { openTab("route"); $("#rowSel").value = `${matrix.group}|${matrix.row}`; loadRoute(); };
};

// ---- Маршрут ----
async function loadMatrix() {
  matrixCache = await api("/api/matrix");
  $("#rowSel").innerHTML = matrixCache.map((r) => `<option value="${esc(r.group)}|${r.row}">${esc(r.group)}: ${esc(r.title)}</option>`).join("");
  $("#rowSel").onchange = () => { $("#routeQuestions").innerHTML = ""; loadRoute(); };
  $("#modular").onchange = loadRoute;
  loadRoute();
}

async function loadRoute() {
  const [group, row] = $("#rowSel").value.split("|");
  const flags = {};
  document.querySelectorAll("[data-flag]").forEach((el) => (flags[el.dataset.flag] = el.checked));
  const r = await api("/api/route", { method: "POST", body: JSON.stringify({ group, row: +row, flags, modular: $("#modular").checked }) });
  $("#routeQuestions").innerHTML = r.questions.map((q) =>
    `<label class="row"><input type="checkbox" data-flag="${q.flag}" ${flags[q.flag] ? "checked" : ""}> ${esc(q.text)}</label>`).join("");
  document.querySelectorAll("[data-flag]").forEach((el) => (el.onchange = loadRoute));
  $("#routeOut").innerHTML = `<h2>Маршрут</h2><ol class="route">${r.steps.map((s) =>
    `<li><b>${esc(s.name)}</b>${s.reason ? ` — ${esc(s.reason)}` : ""} <span class="muted small">[${esc(s.ref)}]</span></li>`).join("")}</ol>` +
    r.notes.map((n) => `<p class="muted small">${esc(n)}</p>`).join("");
}

// ---- Проверка ----
$("#checkFile").onchange = async (e) => {
  const fd = new FormData(); fd.append("file", e.target.files[0]);
  const r = await fetch("/api/check", { method: "POST", body: fd });
  const data = await r.json();
  $("#checkOut").innerHTML = data.issues?.length
    ? data.issues.map((i) => `<div class="issue">${esc(i.message)} <span class="small">[${esc(i.ref)}]</span></div>`).join("")
    : `<div class="good">Замечаний нет.</div>`;
};

// ---- Журнал ----
async function loadLog() {
  const rows = await api("/api/admin/log");
  $("#logTbl tbody").innerHTML = rows.map((r) =>
    `<tr><td>${new Date(r.ts * 1000).toLocaleString("ru")}</td><td>${esc(r.user)}</td><td>${esc(r.question)}</td><td>${STATUS[r.status] || r.status}</td><td>${r.rating ?? ""}</td></tr>`).join("");
}
$("#reindex").onclick = async () => { await api("/api/admin/reindex", { method: "POST" }); loadStatus(); };

start();
