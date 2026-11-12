// herdr-spawn web UI. Plain JS, no build step. Everything shown from the server
// goes in through textContent: agent screens are arbitrary terminal text.
"use strict";

const TOKEN_KEY = "herdr-spawn-token";
const HOST_KEY = "herdr-spawn-host";
const $ = (id) => document.getElementById(id);
const state = { meta: null, browsing: null, session: null, view: "spawn" };

function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}

// --- API ---------------------------------------------------------------------

class Unauthorized extends Error {}

async function api(path, opts = {}) {
  const token = localStorage.getItem(TOKEN_KEY);
  if (!token) throw new Unauthorized();
  const headers = { Authorization: "Bearer " + token };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    cache: "no-store",
  });
  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    throw new Unauthorized();
  }
  let data;
  try { data = await res.json(); } catch { data = { status: "failed", reason: "HTTP " + res.status }; }
  return data;
}

function handle(err, where) {
  if (err instanceof Unauthorized) { show("token"); return; }
  where.textContent = String(err && err.message ? err.message : err);
}

// --- views ---------------------------------------------------------------------

function show(view) {
  for (const v of ["token", "spawn", "agents", "session"]) $(v + "-view").hidden = v !== view;
  document.querySelectorAll(".tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === view || (view === "session" && b.dataset.tab === "agents"));
  });
  if (view !== "token") state.view = view;
}

async function start() {
  if (!localStorage.getItem(TOKEN_KEY)) { show("token"); return; }
  try {
    state.meta = await api("/api/meta");
  } catch (err) { handle(err, $("browse-note")); return; }
  fillForm();
  show(state.view === "session" ? "agents" : state.view);
  if (state.view === "agents") loadAgents();
}

$("token-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = $("token-input").value.trim();
  if (!t) return;
  localStorage.setItem(TOKEN_KEY, t);
  $("token-input").value = "";
  start();
});

document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  if (!localStorage.getItem(TOKEN_KEY)) { show("token"); return; }
  show(b.dataset.tab);
  if (b.dataset.tab === "agents") loadAgents();
}));

// --- spawn ---------------------------------------------------------------------

function currentHost() {
  return (state.meta.hosts || []).find((h) => h.name === $("host").value);
}

function fillForm() {
  const hostSel = $("host");
  const previous = hostSel.value;
  hostSel.replaceChildren();
  for (const h of state.meta.hosts) {
    if (!h.enabled) continue;
    const o = el("option", h.name);
    o.value = h.name;
    hostSel.append(o);
  }
  const enabled = state.meta.hosts.filter((h) => h.enabled).map((h) => h.name);
  const remembered = localStorage.getItem(HOST_KEY);
  const here = state.meta.hosts.find((h) => h.enabled && h.this_machine);
  hostSel.value = [previous, remembered, here && here.name].find((n) => n && enabled.includes(n)) || enabled[0] || "";
  onHostChange();
}

function onHostChange() {
  const h = currentHost();
  if (!h) return;
  $("cwd").value = "";
  $("cwd").placeholder = h.default_cwd || "folder (required on this host)";

  const agentSel = $("agent");
  agentSel.replaceChildren();
  for (const a of h.agents || state.meta.agents) {
    const o = el("option", a);
    o.value = a;
    agentSel.append(o);
  }
  agentSel.value = state.meta.default_agent;

  const uiSel = $("ui");
  uiSel.replaceChildren();
  for (const u of h.uis) {
    const o = el("option", u);
    o.value = u;
    uiSel.append(o);
  }
  uiSel.value = h.uis.includes("hc") ? "hc" : (h.ui || h.uis[0] || "");
  browse(null);
}

$("host").addEventListener("change", () => {
  localStorage.setItem(HOST_KEY, $("host").value);
  onHostChange();
});

async function browse(path) {
  const note = $("browse-note");
  note.textContent = "";
  const q = new URLSearchParams({ host: $("host").value });
  if (path) q.set("path", path);
  let res;
  try { res = await api("/api/folders?" + q); } catch (err) { handle(err, note); return; }
  const list = $("folder-list");
  list.replaceChildren();
  if (res.status === "refused" || res.status === "failed") {
    note.textContent = res.reason || res.status;
    return;
  }
  state.browsing = res;
  $("browse-path").textContent = res.path || "";
  $("browse-up").disabled = !res.path;
  if (res.path) $("cwd").value = res.path;
  for (const f of res.folders) {
    const li = el("li", f.kind === "vault" ? f.name + " (vault)" : (res.path ? f.name : f.path));
    li.addEventListener("click", () => browse(f.path));
    list.append(li);
  }
  if (res.path && !res.folders.length) note.textContent = "No sub-folders.";
  if (res.truncated) note.textContent = "Only the first entries are shown.";
}

$("browse-top").addEventListener("click", () => { $("cwd").value = ""; browse(null); });
$("browse-up").addEventListener("click", () => {
  const b = state.browsing;
  if (!b) return;
  if (b.parent) browse(b.parent); else { $("cwd").value = ""; browse(null); }
});

$("spawn-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("spawn-btn");
  if (btn.disabled) return;  // one click, one agent: never resubmit
  const body = {
    host: $("host").value,
    prompt: $("prompt").value,
    agent: $("agent").value || undefined,
    cwd: $("cwd").value.trim() || undefined,
    ui: $("ui").value || undefined,
    model: $("model").value.trim() || undefined,
    effort: $("effort").value || undefined,
  };
  const out = $("spawn-result");
  out.hidden = false;
  out.replaceChildren(el("p", "Starting…", "muted"));
  btn.disabled = true;
  try {
    renderSpawn(await api("/api/spawn", { method: "POST", body }));
  } catch (err) {
    handle(err, out);
  } finally {
    btn.disabled = false;
  }
});

function renderSpawn(res) {
  const out = $("spawn-result");
  out.replaceChildren();
  out.append(el("h2", res.status, "s-" + res.status));
  if (res.remote_url) {
    const a = el("a", "Open in Claude");
    a.href = res.remote_url;
    a.target = "_blank";
    a.rel = "noopener";
    out.append(a);
  }
  if (res.reason) out.append(el("p", res.reason));
  if (res.rc_unavailable) out.append(el("p", "No Remote Control link: " + res.rc_unavailable, "muted"));
  const dl = el("dl");
  for (const k of ["session_name", "host", "agent", "cwd", "terminal", "space_id", "continue_with"]) {
    if (res[k] == null) continue;
    dl.append(el("dt", k), el("dd", String(res[k])));
  }
  out.append(dl);
  if (res.session_name && res.status !== "refused") {
    const b = el("button", "Open session");
    b.type = "button";
    b.addEventListener("click", () => openSession(res.session_name));
    out.append(b);
  }
  if (res.screen || res.pane_tail) out.append(el("pre", res.screen || res.pane_tail));
}

// --- agents ----------------------------------------------------------------------

async function loadAgents() {
  const note = $("agents-note");
  note.textContent = "Loading…";
  let res;
  try { res = await api("/api/agents"); } catch (err) { handle(err, note); return; }
  note.textContent = "";
  const list = $("agent-list");
  list.replaceChildren();
  if (res.status === "refused") { note.textContent = res.reason; return; }
  if (res.errors) note.textContent = Object.entries(res.errors).map(([h, e]) => h + ": " + e).join("; ");
  for (const a of res.agents || []) {
    const li = el("li");
    const top = el("div");
    top.append(el("strong", a.spawned || a.name || a.terminal || "?"));
    if (a.spawned) top.append(el("span", "spawned", "badge spawned"));
    top.append(el("span", a.status, "badge s-" + a.status));
    li.append(top);
    li.append(el("div", [a.host, a.agent, a.where].filter(Boolean).join(" · "), "muted"));
    if (a.cwd) li.append(el("div", a.cwd, "muted path"));
    if (a.spawned) li.addEventListener("click", () => openSession(a.spawned));
    else li.classList.add("static");
    list.append(li);
  }
  if (!list.children.length && !note.textContent) note.textContent = "No agents running.";
}

$("agents-refresh").addEventListener("click", loadAgents);

// --- one session -------------------------------------------------------------------

async function openSession(name, message) {
  state.session = name;
  show("session");
  $("session-title").textContent = name;
  $("session-status").textContent = message ? "Sending…" : "Loading…";
  const path = "/api/sessions/" + encodeURIComponent(name);
  let res;
  try {
    res = message ? await api(path, { method: "POST", body: { message } }) : await api(path);
  } catch (err) { handle(err, $("session-status")); return; }
  const bits = [res.status, res.host, res.agent, res.doing, res.note, res.reason].filter(Boolean);
  if (res.sent) bits.push("sent");
  $("session-status").textContent = bits.join(" · ");
  $("session-status").className = "muted s-" + res.status;
  $("session-screen").textContent = res.screen || "";
}

$("session-back").addEventListener("click", () => { show("agents"); loadAgents(); });
$("session-refresh").addEventListener("click", () => state.session && openSession(state.session));
$("message-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("message").value.trim();
  if (!text || !state.session) return;
  const btn = e.submitter || $("message-form").querySelector("button");
  btn.disabled = true;
  try {
    await openSession(state.session, text);
    $("message").value = "";
  } finally {
    btn.disabled = false;
  }
});

if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
start();
