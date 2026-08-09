const state = {
  user: null, conversations: [], projects: [], currentConversation: null,
  pendingAttachments: [], controller: null, sending: false, pinnedToBottom: true,
};
const $ = (selector) => document.querySelector(selector);
const loginView = $("#login-view"), appView = $("#app-view"), messages = $("#messages");
const welcome = $("#welcome"), input = $("#message-input"), sendButton = $("#send-button");
const composerStatus = $("#composer-status"), avatarStage = $(".avatar-stage");

class ControlledSpeechRecognition {
  constructor() { this.interimResults = true; this.continuous = false; }
  start() {
    this.onstart?.();
    setTimeout(() => { const results = [[{ transcript: "voice acceptance proof" }]]; results[0].isFinal = true; this.onresult?.({ resultIndex: 0, results }); this.onend?.(); }, 80);
  }
  stop() { this.onend?.(); }
  abort() { this.onerror?.({ error: "aborted" }); this.onend?.(); }
}

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: options.body instanceof FormData ? options.headers : { "Content-Type": "application/json", ...(options.headers || {}) } });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { const body = await response.json(); detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail); } catch {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function toast(message, kind = "info") {
  const item = document.createElement("div"); item.className = `toast ${kind}`; item.textContent = message;
  $("#toast-region").append(item); setTimeout(() => item.remove(), 4200);
}

function showLogin(config) {
  state.user = null; appView.classList.add("hidden"); loginView.classList.remove("hidden");
  $("#google-login").classList.toggle("hidden", config.mode !== "google");
  $("#test-login").classList.toggle("hidden", config.mode !== "test");
  if (config.mode === "google" && !config.google_ready) { $("#login-status").textContent = "Google OIDC needs operator configuration."; $("#google-login").disabled = true; }
}

async function boot() {
  const config = await api("/api/auth/config");
  if (config.mode === "test" && new URLSearchParams(location.search).get("voice_test") === "1") window.__XV12_SPEECH_RECOGNITION__ = ControlledSpeechRecognition;
  try { state.user = await api("/api/auth/me"); showApp(); await Promise.all([loadConversations(), loadProjects(), checkHealth()]); }
  catch { showLogin(config); }
}

function showApp() {
  loginView.classList.add("hidden"); appView.classList.remove("hidden");
  const name = state.user.conversational_name || state.user.display_name;
  $("#user-name").textContent = name; $("#user-role").textContent = state.user.role;
  $("#top-user").textContent = `${name} · ${state.user.role}`;
  $("#user-initial").textContent = name.slice(0, 1).toUpperCase(); $("#welcome-name").textContent = `What are we working on, ${name}?`;
}

async function checkHealth() {
  const label = $("#health-label");
  try {
    const health = await api("/api/health"); label.className = health.ok ? "healthy" : "unhealthy";
    const ciq = health.services?.calibration_iq?.status || "unknown";
    label.innerHTML = `<i></i>${health.ok ? "X ready" : "Model unavailable"} · Calibration IQ ${ciq}`;
  } catch { label.className = "unhealthy"; label.innerHTML = "<i></i>XV12 health unavailable"; }
}

async function loadConversations(selectId = null) {
  state.conversations = await api("/api/conversations"); renderConversationList();
  if (selectId) await openConversation(selectId);
}

function relativeTime(value) {
  const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 60) return "now"; if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`; return new Date(value).toLocaleDateString([], { month: "short", day: "numeric" });
}

function renderConversationList() {
  const list = $("#conversation-list"), query = $("#history-search").value.trim().toLowerCase(); list.replaceChildren();
  state.conversations.filter((item) => !query || item.title.toLowerCase().includes(query)).forEach((conversation) => {
    const row = document.createElement("div"); row.className = `conversation-row${conversation.id === state.currentConversation?.id ? " active" : ""}`;
    const button = document.createElement("button"); button.className = "conversation-item"; button.innerHTML = `<span></span><time>${relativeTime(conversation.updated_at)}</time>`;
    button.querySelector("span").textContent = conversation.title; button.title = conversation.title; button.addEventListener("click", () => openConversation(conversation.id));
    const menu = document.createElement("button"); menu.className = "conversation-menu"; menu.textContent = "•••"; menu.ariaLabel = `Manage ${conversation.title}`;
    menu.addEventListener("click", async () => {
      const action = prompt("Type rename or delete");
      if (action?.toLowerCase() === "rename") { const title = prompt("Conversation title", conversation.title); if (title) { await api(`/api/conversations/${conversation.id}`, { method: "PATCH", body: JSON.stringify({ title }) }); await loadConversations(); toast("Conversation renamed.", "success"); } }
      if (action?.toLowerCase() === "delete" && confirm(`Delete “${conversation.title}”?`)) { await api(`/api/conversations/${conversation.id}`, { method: "DELETE" }); if (state.currentConversation?.id === conversation.id) { state.currentConversation = null; renderConversation(); } await loadConversations(); toast("Conversation deleted.", "success"); }
    });
    row.append(button, menu); list.append(row);
  });
}

async function createConversation() {
  const conversation = await api("/api/conversations", { method: "POST", body: JSON.stringify({ title: "New conversation" }) });
  state.currentConversation = { ...conversation, messages: [], attachments: [] }; state.conversations.unshift(conversation);
  renderConversationList(); renderConversation(); input.focus(); return conversation;
}

async function openConversation(id) {
  state.currentConversation = await api(`/api/conversations/${id}`); state.pendingAttachments = [];
  renderConversationList(); renderAttachmentChips(); renderConversation(); $("#sidebar").classList.remove("open");
}

function isNearBottom() { return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 110; }
function scrollLatest(force = false) {
  if (force || state.pinnedToBottom || isNearBottom()) { messages.scrollTop = messages.scrollHeight; $("#jump-latest").classList.add("hidden"); }
  else $("#jump-latest").classList.remove("hidden");
}

function renderConversation() {
  messages.replaceChildren(); const hasMessages = Boolean(state.currentConversation?.messages?.length);
  welcome.classList.toggle("hidden", hasMessages); messages.classList.toggle("active", hasMessages);
  $("#conversation-title").textContent = state.currentConversation?.title || "New conversation";
  if (hasMessages) state.currentConversation.messages.forEach((message) => appendMessage(message.role, message.content, message.status, message.metadata));
  requestAnimationFrame(() => { state.pinnedToBottom = true; scrollLatest(true); });
}

function appendCard(container, card) {
  const article = document.createElement("article"); article.className = "capability-card";
  const result = card.result || {}, status = result.status || "complete";
  article.innerHTML = `<header><span>◇</span><strong></strong><em></em></header><div class="card-detail"></div>`;
  article.querySelector("strong").textContent = card.capability_id || "Capability"; article.querySelector("em").textContent = status;
  const detail = article.querySelector(".card-detail");
  const searchResults = result.results || result.items || [];
  if (Array.isArray(searchResults) && searchResults.length) {
    searchResults.slice(0, 5).forEach((item) => {
      const line = document.createElement(item.url ? "a" : "div"); line.className = "card-result";
      if (item.url) { line.href = item.url; line.target = "_blank"; line.rel = "noopener noreferrer"; }
      line.textContent = item.title || item.ro_number || [item.vehicle?.year, item.vehicle?.make, item.vehicle?.model].filter(Boolean).join(" ") || JSON.stringify(item).slice(0, 180); detail.append(line);
    });
  } else {
    const summary = result.message || (result.coverage ? JSON.stringify(result.coverage) : result.project?.name ? `Project: ${result.project.name}` : status);
    detail.textContent = summary;
  }
  container.append(article);
}

function appendMessage(role, content = "", status = "complete", metadata = {}) {
  const near = isNearBottom(); const wrapper = document.createElement("article"); wrapper.className = `message ${role} ${status}`;
  const body = document.createElement("div"); body.className = "message-body";
  if (role === "assistant") { const image = document.createElement("img"); image.className = "message-avatar"; image.src = "/assets/avatar/xoduz-512.png"; image.alt = "X"; wrapper.append(image); const name = document.createElement("p"); name.className = "message-name"; name.textContent = "X"; body.append(name); }
  const text = document.createElement("div"); text.className = "message-content"; text.textContent = content; body.append(text);
  const cards = document.createElement("div"); cards.className = "capability-cards"; (metadata.capability_cards || []).forEach((card) => appendCard(cards, card)); body.append(cards);
  wrapper.append(body); messages.append(wrapper); state.pinnedToBottom = near; scrollLatest(); return { wrapper, text, cards };
}

async function sendMessage(raw) {
  const text = raw.trim(); if (!text || state.sending) return; if (!state.currentConversation) await createConversation();
  input.value = ""; resizeInput(); welcome.classList.add("hidden"); messages.classList.add("active"); state.pinnedToBottom = true;
  appendMessage("user", text); const assistant = appendMessage("assistant", "", "complete"); assistant.text.classList.add("typing-cursor");
  state.sending = true; state.controller = new AbortController(); sendButton.textContent = "■"; composerStatus.className = "composer-status"; composerStatus.textContent = "X is thinking…";
  avatarStage.classList.add("speaking"); $("#presence-label").textContent = "Thinking";
  const ids = state.pendingAttachments.map((item) => item.id); state.pendingAttachments = []; renderAttachmentChips();
  try {
    const response = await fetch(`/api/conversations/${state.currentConversation.id}/stream`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: text, attachment_ids: ids }), signal: state.controller.signal });
    if (!response.ok) throw new Error((await response.json()).detail || `Request failed (${response.status})`);
    const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = "";
    while (true) {
      const { value, done } = await reader.read(); if (done) break; buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split("\n\n"); buffer = blocks.pop();
      for (const block of blocks) {
        const event = block.match(/^event:\s*(.+)$/m)?.[1], dataLine = block.match(/^data:\s*(.+)$/m)?.[1]; if (!event || !dataLine) continue;
        const data = JSON.parse(dataLine);
        if (event === "delta") { assistant.text.textContent += data.text; scrollLatest(); }
        if (event === "capability") {
          if (data.status === "running") { composerStatus.textContent = `Using ${data.capability_id}…`; $("#presence-label").textContent = "Working"; }
          if (data.status === "complete") { appendCard(assistant.cards, data); scrollLatest(); if (data.capability_id.startsWith("project.")) await loadProjects(); }
        }
        if (event === "error") throw new Error(data.message);
      }
    }
    assistant.text.classList.remove("typing-cursor"); composerStatus.textContent = "Response complete."; $("#presence-label").textContent = "Ready";
    await loadConversations();
  } catch (error) {
    assistant.text.classList.remove("typing-cursor");
    if (error.name === "AbortError") { assistant.wrapper.classList.add("interrupted"); composerStatus.textContent = "Response stopped."; }
    else { assistant.wrapper.classList.add("failed"); composerStatus.className = "composer-status error"; composerStatus.textContent = error.message; toast(error.message, "error"); }
  } finally { state.sending = false; state.controller = null; sendButton.textContent = "↑"; avatarStage.classList.remove("speaking"); $("#presence-label").textContent = "Ready"; input.focus(); }
}

async function uploadFile(file) {
  if (!file) return; const form = new FormData(); form.append("file", file); if (state.currentConversation) form.append("conversation_id", state.currentConversation.id);
  composerStatus.textContent = "Uploading attachment…";
  try { const item = await api("/api/attachments", { method: "POST", body: form }); state.pendingAttachments.push(item); renderAttachmentChips(); composerStatus.textContent = "Attachment ready."; toast(`${item.original_name} attached.`, "success"); }
  catch (error) { composerStatus.className = "composer-status error"; composerStatus.textContent = error.message; }
}

function renderAttachmentChips() {
  const chips = $("#attachment-chips"); chips.replaceChildren();
  state.pendingAttachments.forEach((item) => { const chip = document.createElement("span"); chip.className = "attachment-chip"; const label = document.createElement("span"); label.textContent = item.original_name; const remove = document.createElement("button"); remove.textContent = "×"; remove.ariaLabel = `Remove ${item.original_name}`; remove.addEventListener("click", async () => { await api(`/api/attachments/${item.id}`, { method: "DELETE" }); state.pendingAttachments = state.pendingAttachments.filter((candidate) => candidate.id !== item.id); renderAttachmentChips(); toast("Attachment removed."); }); chip.append(label, remove); chips.append(chip); });
}

async function loadProjects() { state.projects = await api("/api/projects"); $("#project-count").textContent = state.projects.length; const active = state.projects.find((item) => item.is_active); const chip = $("#active-project-chip"); chip.classList.toggle("hidden", !active); chip.textContent = active ? `▱ ${active.name} ×` : ""; }

async function showModal(kind) {
  const content = $("#modal-content");
  if (kind === "projects") {
    await loadProjects(); content.innerHTML = `<p class="eyebrow">OPTIONAL CONTEXT</p><h2>Projects</h2><p>Register a reference explicitly. XV12 stores the reference but never scans or executes it.</p><form id="project-form" class="project-form"><input name="name" required maxlength="120" placeholder="Project name"><input name="reference" maxlength="500" placeholder="Path or reference (optional)"><textarea name="description" maxlength="2000" placeholder="Short description"></textarea><button class="primary-button" type="submit">Register project</button></form><div id="project-list" class="project-list"></div>`;
    const list = content.querySelector("#project-list"); state.projects.forEach((project) => { const row = document.createElement("div"); row.className = `project-row${project.is_active ? " active" : ""}`; row.innerHTML = `<div><strong></strong><span></span></div><button class="secondary-button"></button>`; row.querySelector("strong").textContent = project.name; row.querySelector("span").textContent = project.reference || project.description || "Registered project"; const button = row.querySelector("button"); button.textContent = project.is_active ? "Detach" : "Activate"; button.addEventListener("click", async () => { if (project.is_active) await api("/api/projects/active", { method: "DELETE" }); else await api(`/api/projects/${project.id}/activate`, { method: "POST" }); await loadProjects(); await showModal("projects"); toast(project.is_active ? "Project detached." : "Project activated.", "success"); }); list.append(row); });
    content.querySelector("#project-form").addEventListener("submit", async (event) => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.target)); const project = await api("/api/projects", { method: "POST", body: JSON.stringify(data) }); await api(`/api/projects/${project.id}/activate`, { method: "POST" }); await loadProjects(); await showModal("projects"); toast("Project registered and activated.", "success"); });
  } else if (kind === "tools") {
    const listing = await api("/api/capabilities"); content.innerHTML = `<p class="eyebrow">AUTHORITATIVE REGISTRY</p><h2>Capabilities</h2><p>${listing.capabilities.length} capabilities are authorized for this account.</p><ul class="modal-list"></ul>`; const list = content.querySelector("ul"); listing.capabilities.forEach((item) => { const li = document.createElement("li"); li.innerHTML = `<strong></strong><span></span>`; li.querySelector("strong").textContent = item.id; li.querySelector("span").textContent = `Tier ${item.risk_tier} · ${item.health} · ${item.description}`; list.append(li); });
  } else { content.innerHTML = `<p class="eyebrow">XODUZ XV12</p><h2>Settings</h2><p>Your authenticated identity and user-scoped context are server-authoritative.</p><ul class="modal-list"><li><strong>Conversational identity</strong><span>${state.user.conversational_name} · ${state.user.role}</span></li><li><strong>Runtime</strong><span>Local Qwen3-Coder · 32K context</span></li><li><strong>Privacy</strong><span>XV12-owned local storage</span></li></ul>`; }
  if (!$("#modal").open) $("#modal").showModal();
}

function setupSpeech() {
  const NativeRecognition = window.SpeechRecognition || window.webkitSpeechRecognition, button = $("#mic-button"); let recognition = null;
  button.addEventListener("click", () => {
    if (recognition) { recognition.abort ? recognition.abort() : recognition.stop(); return; }
    const Recognition = window.__XV12_SPEECH_RECOGNITION__ || NativeRecognition;
    if (!Recognition) { composerStatus.className = "composer-status error"; composerStatus.textContent = "Dictation is not supported in this browser. Typed chat is ready."; return; }
    recognition = new Recognition(); recognition.lang = navigator.language || "en-US"; recognition.interimResults = true; recognition.continuous = false; const startingText = input.value.trim(); let finalText = "";
    recognition.onstart = () => { button.classList.add("listening"); button.setAttribute("aria-label", "Cancel dictation"); composerStatus.className = "composer-status"; composerStatus.textContent = "Listening… click the microphone to cancel."; };
    recognition.onresult = (event) => { let interim = ""; for (let index = event.resultIndex || 0; index < event.results.length; index += 1) { const text = event.results[index][0].transcript; if (event.results[index].isFinal) finalText += text; else interim += text; } input.value = [startingText, finalText + interim].filter(Boolean).join(startingText ? " " : ""); resizeInput(); };
    recognition.onerror = (event) => { composerStatus.className = "composer-status error"; composerStatus.textContent = event.error === "not-allowed" ? "Microphone access was denied." : event.error === "aborted" ? "Dictation canceled." : `Dictation stopped: ${event.error}.`; };
    recognition.onend = () => { recognition = null; button.classList.remove("listening"); button.setAttribute("aria-label", "Start dictation"); if (!composerStatus.classList.contains("error")) composerStatus.textContent = finalText ? "Dictation added to the composer." : "Dictation ended."; input.focus(); };
    try { recognition.start(); } catch (error) { recognition = null; composerStatus.className = "composer-status error"; composerStatus.textContent = `Dictation could not start: ${error.message}`; }
  });
}

function resizeInput() { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 160)}px`; }

$("#google-login").addEventListener("click", () => window.location.assign("/api/auth/google/start"));
document.querySelectorAll("[data-persona]").forEach((button) => button.addEventListener("click", async () => { $("#login-status").textContent = "Signing in…"; try { state.user = await api("/api/auth/test-login", { method: "POST", body: JSON.stringify({ persona: button.dataset.persona }) }); showApp(); await Promise.all([loadConversations(), loadProjects(), checkHealth()]); } catch (error) { $("#login-status").textContent = error.message; } }));
$("#logout").addEventListener("click", async () => { await api("/api/auth/logout", { method: "POST" }); window.location.reload(); });
$("#top-logout").addEventListener("click", async () => { await api("/api/auth/logout", { method: "POST" }); window.location.reload(); });
$("#new-chat").addEventListener("click", createConversation); $("#history-search").addEventListener("input", renderConversationList);
$("#composer").addEventListener("submit", (event) => { event.preventDefault(); if (state.sending) state.controller?.abort(); else sendMessage(input.value); });
input.addEventListener("input", resizeInput); input.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#composer").requestSubmit(); } });
$("#file-input").addEventListener("change", (event) => { uploadFile(event.target.files[0]); event.target.value = ""; });
document.querySelectorAll(".suggestions button").forEach((button) => button.addEventListener("click", () => { input.value = button.textContent; resizeInput(); input.focus(); }));
$("#projects-button").addEventListener("click", () => showModal("projects")); $("#active-project-chip").addEventListener("click", () => showModal("projects"));
$("#tools-button").addEventListener("click", () => showModal("tools")); $("#settings-button").addEventListener("click", () => showModal("settings")); $("#close-modal").addEventListener("click", () => $("#modal").close());
$("#collapse-sidebar").addEventListener("click", () => appView.classList.toggle("sidebar-collapsed")); $("#open-sidebar").addEventListener("click", () => $("#sidebar").classList.add("open"));
messages.addEventListener("scroll", () => { state.pinnedToBottom = isNearBottom(); $("#jump-latest").classList.toggle("hidden", state.pinnedToBottom); }); $("#jump-latest").addEventListener("click", () => { state.pinnedToBottom = true; scrollLatest(true); });
setupSpeech(); boot().catch((error) => { loginView.classList.remove("hidden"); $("#login-status").textContent = `XV12 could not start: ${error.message}`; });
