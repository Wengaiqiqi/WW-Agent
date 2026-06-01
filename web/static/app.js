const $ = (id) => document.getElementById(id);
const api = (path, opts = {}) =>
  fetch(path, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts });

let state = {
  user: null,
  convs: [],
  activeConv: null,
  models: [],
  endpoints: [],
  domCache: {},
  loadToken: 0,
};

const HISTORY_RENDER_BATCH_SIZE = 8;

const nextFrame = () => new Promise((resolve) => requestAnimationFrame(resolve));

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
  const [mr, er] = await Promise.all([api("/api/models"), api("/api/endpoints")]);
  state.models = mr.ok ? await mr.json() : [];
  state.endpoints = er.ok ? await er.json() : [];
  renderModelSelect();
}

function renderModelSelect() {
  const sel = $("model-select");
  const prev = sel.value;
  const presets = state.models
    .map((m) => `<option value="preset:${m.id}">${escapeHtml(m.label)} · ${escapeHtml(m.model)}</option>`)
    .join("");
  const customs = state.endpoints
    .map((e) => `<option value="endpoint:${e.id}">★ ${escapeHtml(e.label)} · ${escapeHtml(e.model)}</option>`)
    .join("");
  sel.innerHTML =
    `<optgroup label="预置">${presets}</optgroup>` +
    (customs ? `<optgroup label="自定义">${customs}</optgroup>` : "");
  if (prev) sel.value = prev;  // keep the current selection across refreshes
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
    title.title = "双击重命名";
    title.onclick = () => selectConv(c.id);
    title.ondblclick = (e) => { e.stopPropagation(); beginRenameSidebar(c, title); };
    const del = document.createElement("span");
    del.className = "del"; del.textContent = "✕";
    del.onclick = async (e) => { e.stopPropagation(); await deleteConv(c.id); };
    li.append(title, del);
    ul.append(li);
  }
}

async function renameConv(id, newTitle) {
  const title = (newTitle || "").trim();
  if (!title) return;
  const r = await api(`/api/conversations/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
  if (!r.ok) { renderConvList(); return; }
  const c = await r.json();
  const conv = state.convs.find((x) => x.id === id);
  if (conv) conv.title = c.title;
  renderConvList();
  if (state.activeConv === id) $("conv-title").textContent = c.title;
}

// Double-click a sidebar item -> inline rename input.
function beginRenameSidebar(c, span) {
  const input = document.createElement("input");
  input.className = "rename-input";
  input.value = c.title;
  span.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const commit = (save) => {
    if (done) return;
    done = true;
    const v = input.value.trim();
    if (save && v && v !== c.title) renameConv(c.id, v);
    else renderConvList();        // restore original row
  };
  input.onclick = (e) => e.stopPropagation();
  input.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(true); }
    else if (e.key === "Escape") { e.preventDefault(); commit(false); }
  };
  input.onblur = () => commit(true);
}

// Click the header title -> edit in place (contenteditable keeps the #conv-title id).
function bindHeaderRename() {
  const ct = $("conv-title");
  ct.title = "点击重命名";
  ct.addEventListener("click", () => {
    if (!state.activeConv || ct.isContentEditable) return;
    ct.contentEditable = "true";
    ct.classList.add("editing");
    ct._cancel = false;
    const range = document.createRange();
    range.selectNodeContents(ct);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    ct.focus();
  });
  ct.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); ct.blur(); }
    else if (e.key === "Escape") { e.preventDefault(); ct._cancel = true; ct.blur(); }
  });
  ct.addEventListener("blur", () => {
    if (!ct.isContentEditable) return;
    ct.contentEditable = "false";
    ct.classList.remove("editing");
    const conv = state.convs.find((x) => x.id === state.activeConv);
    const v = ct.textContent.trim();
    if (!ct._cancel && conv && v && v !== conv.title) renameConv(state.activeConv, v);
    else ct.textContent = conv ? conv.title : "";
    ct._cancel = false;
  });
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
  if (state.activeConv && state.domCache[state.activeConv]) {
    state.domCache[state.activeConv] = Array.from($("messages").children);
  }
}

async function selectConv(id) {
  if (state.activeConv === id) return;
  stashMessages();                        // keep the conversation we're leaving
  const loadToken = ++state.loadToken;
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

  // First visit: fetch + render once. The backend GET is cheap; the expensive
  // part is markdown parsing, so render in small batches to keep clicks fluid.
  box.replaceChildren();                  // blank while the first load fetches
  const r = await api(`/api/conversations/${id}/messages`);
  if (state.activeConv !== id || state.loadToken !== loadToken) return;
  const msgs = r.ok ? await r.json() : [];
  if (state.activeConv !== id || state.loadToken !== loadToken) return;
  await renderMessagesChunked(msgs, id, loadToken);
}

async function renderMessagesChunked(msgs, convId, loadToken) {
  const box = $("messages");
  for (let i = 0; i < msgs.length; i += HISTORY_RENDER_BATCH_SIZE) {
    if (state.activeConv !== convId || state.loadToken !== loadToken) return false;
    const frag = document.createDocumentFragment();
    for (const m of msgs.slice(i, i + HISTORY_RENDER_BATCH_SIZE)) {
      const events = m.events_json ? JSON.parse(m.events_json) : [];
      frag.appendChild(buildMessage(m.role, m.content, events));
    }
    box.appendChild(frag);
    box.scrollTop = box.scrollHeight;
    await nextFrame();
  }
  if (state.activeConv !== convId || state.loadToken !== loadToken) return false;
  state.domCache[convId] = Array.from(box.children);
  highlightHistoryChunked(box, convId, loadToken);
  return true;
}

function highlightHistoryChunked(scope, convId, loadToken) {
  const blocks = Array.from(scope.querySelectorAll("pre code"));
  const highlightBatch = (start) => {
    if (state.activeConv !== convId || state.loadToken !== loadToken) return;
    for (const block of blocks.slice(start, start + HISTORY_RENDER_BATCH_SIZE)) {
      highlightCodeBlock(block);
    }
    if (start + HISTORY_RENDER_BATCH_SIZE < blocks.length) {
      requestAnimationFrame(() => highlightBatch(start + HISTORY_RENDER_BATCH_SIZE));
    }
  };
  requestAnimationFrame(() => highlightBatch(0));
}

function buildMessage(role, content, events = []) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  if (events.length) div.appendChild(renderProcess(events));
  const body = document.createElement("div");
  body.className = "body";
  body.innerHTML = role === "assistant" ? safeMarkdown(content) : escapeHtml(content);
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

function openEndpoints() {
  renderEndpointList();
  $("ep-error").textContent = "";
  $("endpoint-modal").classList.remove("hidden");
}
function closeEndpoints() { $("endpoint-modal").classList.add("hidden"); }

function renderEndpointList() {
  const ul = $("endpoint-list");
  ul.innerHTML = "";
  if (!state.endpoints.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "暂无自定义端点";
    ul.append(li);
    return;
  }
  for (const e of state.endpoints) {
    const li = document.createElement("li");
    const span = document.createElement("span");
    span.textContent = `${e.label} · ${e.model} · ${e.base_url}`;
    const del = document.createElement("button");
    del.className = "del";
    del.textContent = "✕";
    del.onclick = async () => {
      await api(`/api/endpoints/${e.id}`, { method: "DELETE" });
      await loadModels();
      renderEndpointList();
    };
    li.append(span, del);
    ul.append(li);
  }
}

async function saveEndpoint() {
  $("ep-error").textContent = "";
  const body = {
    label: $("ep-label").value.trim(),
    base_url: $("ep-base-url").value.trim(),
    api_key: $("ep-api-key").value,
    model: $("ep-model").value.trim(),
    protocol: $("ep-protocol").value,
  };
  const r = await api("/api/endpoints", { method: "POST", body: JSON.stringify(body) });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    $("ep-error").textContent = e.detail || "保存失败";
    return;
  }
  const created = await r.json();
  await loadModels();
  $("model-select").value = `endpoint:${created.id}`;
  for (const id of ["ep-label", "ep-base-url", "ep-api-key", "ep-model"]) $(id).value = "";
  closeEndpoints();
}

const thinkText = (e) => (e.text || e.thinking || e.content || "").trim();
const asText = (v) => (typeof v === "string" ? v : JSON.stringify(v ?? ""));

// `live` mirrors the CLI's blinking-bullet behavior: while the turn is still
// streaming, a tool_call with no matching tool_result yet is "running" and
// pulses; the moment its result arrives (or the turn ends with live=false) it
// freezes. Matching is positional — a tool_call is running iff it appears
// after the last tool_result, the same "freeze the previous call on the next
// tool_call/result" rule the terminal UI uses.
function renderProcess(events, live = true) {
  // Drop empty thinking events (no text) so we don't show blank 💭 rows.
  const steps = events.filter((e) => e.type !== "thinking" || thinkText(e));
  let lastResultIdx = -1;
  steps.forEach((e, i) => { if (e.type === "tool_result") lastResultIdx = i; });

  const d = document.createElement("details");
  d.className = "process";
  const s = document.createElement("summary");
  d.appendChild(s);

  let anyActive = false;
  for (let k = 0; k < steps.length; k++) {
    const e = steps[k];
    const row = document.createElement("div");
    row.className = "proc-step";
    let icon = "•", label = "", detail = "";

    if (e.type === "thinking") {
      icon = "💭"; label = "思考"; detail = thinkText(e);
    } else if (e.type === "tool_call") {
      icon = "🔧"; label = e.name || "tool";
      detail = e.args == null ? "" : truncate(asText(e.args), 240);
    } else if (e.type === "tool_result") {
      icon = "↩"; label = e.name ? `${e.name} 结果` : "结果";
      detail = truncate(asText(e.preview ?? e.result ?? e.output ?? e.content), 240);
    } else if (e.type === "error") {
      icon = "⚠"; label = "错误"; row.classList.add("err");
      detail = truncate(e.message || asText(e), 240);
    } else {
      label = e.type || "事件"; detail = truncate(asText(e), 240);
    }

    const isActive = live && e.type === "tool_call" && k > lastResultIdx;
    if (isActive) { row.classList.add("active"); anyActive = true; }

    const head = document.createElement("div");
    head.className = "proc-head";
    head.textContent = `${icon} ${label}`;
    if (isActive) {
      // Pulsing dot = "this tool is still running" (terminal blink parity).
      const dot = document.createElement("span");
      dot.className = "run-dot";
      head.appendChild(dot);
    }
    row.appendChild(head);
    if (detail) {
      const body = document.createElement("div");
      body.className = "proc-detail";
      body.textContent = detail;
      row.appendChild(body);
    }
    d.appendChild(row);
  }

  // Surface the running state on the (collapsed-by-default) summary too, so the
  // pulse is visible without expanding the panel.
  s.textContent = `过程 · ${steps.length} 步${anyActive ? " · 运行中" : ""}`;
  if (anyActive) {
    d.classList.add("running");
    const dot = document.createElement("span");
    dot.className = "run-dot";
    s.appendChild(dot);
  }
  return d;
}

async function sendMessage(text) {
  if (!state.activeConv) await newConv();
  renderMessage("user", text);
  const assistantDiv = renderMessage("assistant", "");
  const body = assistantDiv.querySelector(".body");
  body.innerHTML = '<span class="typing">加载中…</span>';  // until the first token
  const procEvents = [];
  let acc = "";
  const raw = $("model-select").value || "";
  const i = raw.indexOf(":");
  const kind = i >= 0 ? raw.slice(0, i) : "";
  const val = i >= 0 ? raw.slice(i + 1) : raw;
  const payload = { content: text };
  if (kind === "endpoint") payload.endpoint_id = val;
  else payload.model = val;
  const resp = await api(`/api/conversations/${state.activeConv}/messages`, {
    method: "POST",
    body: JSON.stringify(payload),
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
      if (ev.type === "text") { acc += ev.chunk || ""; body.innerHTML = safeMarkdown(acc); }
      else if (["thinking", "tool_call", "tool_result", "error"].includes(ev.type)) {
        procEvents.push(ev);
        const existing = assistantDiv.querySelector(".process");
        const fresh = renderProcess(procEvents);
        // Keep the panel open if the user expanded it; re-rendering otherwise
        // snaps it shut on every event.
        if (existing) { fresh.open = existing.open; existing.replaceWith(fresh); }
        else assistantDiv.prepend(fresh);
      } else if (ev.type === "done") {
        if (ev.text) { acc = ev.text; body.innerHTML = safeMarkdown(acc); }
        else if (!acc) { body.innerHTML = ""; }  // clear the 加载中 indicator if nothing streamed
        // Turn ended — freeze any still-pulsing tool call (live=false).
        const existing = assistantDiv.querySelector(".process");
        if (existing) {
          const frozen = renderProcess(procEvents, false);
          frozen.open = existing.open;
          existing.replaceWith(frozen);
        }
      }
    }
    highlightAndCopy(assistantDiv);
    $("messages").scrollTop = $("messages").scrollHeight;
  }
  loadConversations();  // refresh updated_at ordering
}

function highlightCodeBlock(block) {
  if (block.dataset.hl) return;
  hljs.highlightElement(block);
  block.dataset.hl = "1";
  const pre = block.parentElement;
  const btn = document.createElement("button");
  btn.className = "copy";
  btn.textContent = "Copy";
  btn.onclick = () => navigator.clipboard.writeText(block.textContent);
  pre.appendChild(btn);
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

// SECURITY: assistant content is unsanitized text from upstream LLMs (incl.
// user-supplied custom endpoints). Always run the marked output through
// DOMPurify before assigning to innerHTML; otherwise an attacker-controlled
// endpoint can stream <img onerror=…> and execute JS in the auth'd session.
function safeMarkdown(text) {
  const html = marked.parse(text || "");
  return window.DOMPurify ? DOMPurify.sanitize(html) : escapeHtml(text || "");
}

// Wire events
$("btn-login").onclick = () => doAuth("/api/auth/login");
$("btn-register").onclick = () => doAuth("/api/auth/register");
$("btn-logout").onclick = async () => { await api("/api/auth/logout", { method: "POST" }); location.reload(); };
$("btn-new-conv").onclick = newConv;
$("btn-endpoints").onclick = openEndpoints;
$("btn-close-endpoint").onclick = closeEndpoints;
$("btn-save-endpoint").onclick = saveEndpoint;
$("composer").onsubmit = (e) => {
  e.preventDefault();
  const v = $("input").value.trim();
  if (v) { $("input").value = ""; sendMessage(v); }
};
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); }
});

// ---- UI chrome: icons, sidebar collapse, footer menu, textarea autogrow ----
const ICONS = {
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  sidebar: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
  more: '<circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
  "arrow-up": '<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>',
  close: '<path d="M18 6 6 18"/><path d="M6 6l12 12"/>',
};
function mountIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((n) => {
    if (n.dataset.mounted) return;
    n.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[n.dataset.icon] || ""}</svg>`;
    n.dataset.mounted = "1";
  });
}
mountIcons();

// Sidebar collapse / expand
const appView = $("app-view");
const isMobile = () => window.matchMedia("(max-width: 820px)").matches;
function setCollapsed(on) {
  if (isMobile()) { appView.classList.toggle("mobile-open", !on); }
  else { appView.classList.toggle("collapsed", on); }
  $("btn-expand").hidden = isMobile() ? false : !on;
}
$("btn-collapse").onclick = () => setCollapsed(true);
$("btn-expand").onclick = () => setCollapsed(false);
$("btn-expand").hidden = !isMobile();

// Close the footer menu after picking an item
document.querySelectorAll(".footer-menu .popover-item").forEach((b) => {
  b.addEventListener("click", () => $("app-view").querySelector(".footer-menu").removeAttribute("open"));
});

// Auto-grow the composer textarea
const ta = $("input");
const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 200) + "px"; };
ta.addEventListener("input", grow);

bindHeaderRename();

init();
