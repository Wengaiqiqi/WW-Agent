const $ = (id) => document.getElementById(id);
const api = (path, opts = {}) =>
  fetch(path, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts });

let state = { user: null, convs: [], activeConv: null, models: [], domCache: {} };

async function init() {
  const me = await api("/api/me");
  if (me.ok) { state.user = await me.json(); await enterApp(); }
  else showAuth();
}

function showAuth() { $("auth-view").classList.remove("hidden"); $("app-view").classList.add("hidden"); }
function showApp() { $("auth-view").classList.add("hidden"); $("app-view").classList.remove("hidden"); }

async function doAuth(path) {
  $("auth-error").textContent = "";
  const body = {
    username: $("auth-username").value.trim(),
    password: $("auth-password").value,
    signup_code: $("auth-signup-code").value.trim() || undefined,
  };
  const r = await api(path, { method: "POST", body: JSON.stringify(body) });
  if (r.ok) { state.user = await r.json(); await enterApp(); }
  else { const e = await r.json().catch(() => ({})); $("auth-error").textContent = e.detail || "失败"; }
}

async function enterApp() {
  showApp();
  $("me-name").textContent = state.user.username;
  await loadModels();
  await loadConversations();
}

async function loadModels() {
  const r = await api("/api/models");
  state.models = r.ok ? await r.json() : [];
  $("model-select").innerHTML = state.models
    .map((m) => `<option value="${m.id}">${m.label} · ${m.model}</option>`).join("");
}

async function loadConversations() {
  const r = await api("/api/conversations");
  state.convs = r.ok ? await r.json() : [];
  renderConvList();
  if (!state.activeConv && state.convs.length) selectConv(state.convs[0].id);
}

function renderConvList() {
  const ul = $("conv-list");
  ul.innerHTML = "";
  for (const c of state.convs) {
    const li = document.createElement("li");
    li.className = c.id === state.activeConv ? "active" : "";
    const title = document.createElement("span");
    title.textContent = c.title;
    title.onclick = () => selectConv(c.id);
    const del = document.createElement("span");
    del.className = "del"; del.textContent = "✕";
    del.onclick = async (e) => { e.stopPropagation(); await deleteConv(c.id); };
    li.append(title, del);
    ul.append(li);
  }
}

async function newConv() {
  const r = await api("/api/conversations", { method: "POST", body: JSON.stringify({}) });
  if (r.ok) {
    const c = await r.json();
    stashMessages();              // keep the conversation we're leaving
    state.convs.unshift(c);
    state.activeConv = c.id;
    state.domCache[c.id] = [];    // brand new, nothing to load
    renderConvList();
    $("messages").replaceChildren();
    $("conv-title").textContent = c.title;
  }
}

async function deleteConv(id) {
  await api(`/api/conversations/${id}`, { method: "DELETE" });
  delete state.domCache[id];
  if (state.activeConv === id) { state.activeConv = null; $("messages").replaceChildren(); }
  await loadConversations();
}

// Detach the active conversation's rendered nodes so we can re-show them later
// instantly — no fetch, no markdown re-parse, no re-highlight.
function stashMessages() {
  if (state.activeConv) {
    state.domCache[state.activeConv] = Array.from($("messages").children);
  }
}

async function selectConv(id) {
  if (state.activeConv === id) return;
  stashMessages();                        // keep the conversation we're leaving
  state.activeConv = id;
  const c = state.convs.find((x) => x.id === id);
  $("conv-title").textContent = c ? c.title : "";
  renderConvList();
  const box = $("messages");

  // Already rendered this session -> instant node swap, no fetch / parse /
  // highlight. This is what makes switching back and forth feel instant.
  if (state.domCache[id]) {
    box.replaceChildren(...state.domCache[id]);
    box.scrollTop = box.scrollHeight;
    return;
  }

  // First visit: fetch + render once. The backend GET is ~5ms; the cost is the
  // markdown parse + syntax highlight, which we now pay only this one time.
  box.replaceChildren();                  // blank while the first load fetches
  const r = await api(`/api/conversations/${id}/messages`);
  if (state.activeConv !== id) return;    // a newer switch won; drop this result
  const msgs = r.ok ? await r.json() : [];
  if (state.activeConv !== id) return;
  // Build off-DOM and swap in once; reading scrollHeight per message while
  // appending to the live list forced a reflow every iteration (jank).
  const frag = document.createDocumentFragment();
  for (const m of msgs) {
    const events = m.events_json ? JSON.parse(m.events_json) : [];
    frag.appendChild(buildMessage(m.role, m.content, events));
  }
  box.replaceChildren(frag);
  box.scrollTop = box.scrollHeight;
  // Highlight after the frame paints so the text shows instantly.
  requestAnimationFrame(() => {
    if (state.activeConv === id) highlightAndCopy(box);
  });
}

function buildMessage(role, content, events = []) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  if (events.length) div.appendChild(renderProcess(events));
  const body = document.createElement("div");
  body.className = "body";
  body.innerHTML = role === "assistant" ? marked.parse(content || "") : escapeHtml(content);
  div.appendChild(body);
  return div;
}

function renderMessage(role, content, events = []) {
  const div = buildMessage(role, content, events);
  const box = $("messages");
  box.appendChild(div);
  highlightAndCopy(div);
  box.scrollTop = box.scrollHeight;
  return div;
}

function renderProcess(events) {
  const d = document.createElement("details");
  d.className = "process";
  const s = document.createElement("summary");
  s.textContent = `过程(${events.length})`;
  d.appendChild(s);
  for (const e of events) {
    const p = document.createElement("div");
    if (e.type === "thinking") p.textContent = `💭 ${e.text || ""}`;
    else if (e.type === "tool_call") p.textContent = `🔧 ${e.name || "tool"}(${JSON.stringify(e.args || {})})`;
    else if (e.type === "tool_result") p.textContent = `↩ ${truncate(JSON.stringify(e.result ?? e), 200)}`;
    else if (e.type === "error") p.textContent = `⚠ ${e.message || ""}`;
    else p.textContent = JSON.stringify(e);
    d.appendChild(p);
  }
  return d;
}

async function sendMessage(text) {
  if (!state.activeConv) await newConv();
  renderMessage("user", text);
  const assistantDiv = renderMessage("assistant", "");
  const body = assistantDiv.querySelector(".body");
  body.innerHTML = '<span class="typing">思考中…</span>';  // until the first token
  const procEvents = [];
  let acc = "";
  const resp = await api(`/api/conversations/${state.activeConv}/messages`, {
    method: "POST",
    body: JSON.stringify({ content: text, model: $("model-select").value }),
  });
  if (!resp.ok) { body.textContent = `[error] ${resp.status}`; return; }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
      if (!raw.startsWith("data: ")) continue;
      const ev = JSON.parse(raw.slice(6));
      if (ev.type === "text") { acc += ev.chunk || ""; body.innerHTML = marked.parse(acc); }
      else if (["thinking", "tool_call", "tool_result", "error"].includes(ev.type)) {
        procEvents.push(ev);
        const existing = assistantDiv.querySelector(".process");
        const fresh = renderProcess(procEvents);
        if (existing) existing.replaceWith(fresh); else assistantDiv.prepend(fresh);
      } else if (ev.type === "done") {
        if (ev.text) { acc = ev.text; body.innerHTML = marked.parse(acc); }
        else if (!acc) { body.innerHTML = ""; }  // clear the 思考中 indicator if nothing streamed
      }
    }
    highlightAndCopy(assistantDiv);
    $("messages").scrollTop = $("messages").scrollHeight;
  }
  loadConversations();  // refresh updated_at ordering
}

function highlightAndCopy(scope) {
  scope.querySelectorAll("pre code").forEach((block) => {
    if (block.dataset.hl) return;
    hljs.highlightElement(block);
    block.dataset.hl = "1";
    const pre = block.parentElement;
    const btn = document.createElement("button");
    btn.className = "copy"; btn.textContent = "复制";
    btn.onclick = () => navigator.clipboard.writeText(block.textContent);
    pre.appendChild(btn);
  });
}

const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const truncate = (s, n) => (s.length > n ? s.slice(0, n) + "…" : s);

// Wire events
$("btn-login").onclick = () => doAuth("/api/auth/login");
$("btn-register").onclick = () => doAuth("/api/auth/register");
$("btn-logout").onclick = async () => { await api("/api/auth/logout", { method: "POST" }); location.reload(); };
$("btn-new-conv").onclick = newConv;
$("composer").onsubmit = (e) => {
  e.preventDefault();
  const v = $("input").value.trim();
  if (v) { $("input").value = ""; sendMessage(v); }
};
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); }
});

init();
