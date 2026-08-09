const state = {
  user: null,
  conversations: [],
  currentConversation: null,
  pendingAttachments: [],
  controller: null,
  sending: false,
};

const $ = (selector) => document.querySelector(selector);
const loginView = $("#login-view");
const appView = $("#app-view");
const messages = $("#messages");
const welcome = $("#welcome");
const input = $("#message-input");
const sendButton = $("#send-button");
const composerStatus = $("#composer-status");
const avatar = $("#x-avatar");

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: options.body instanceof FormData ? options.headers : { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

function showLogin(config) {
  state.user = null;
  appView.classList.add("hidden");
  loginView.classList.remove("hidden");
  $("#google-login").classList.toggle("hidden", config.mode !== "google");
  $("#test-login").classList.toggle("hidden", config.mode !== "test");
  if (config.mode === "google" && !config.google_ready) {
    $("#login-status").textContent = "Google OIDC needs operator configuration. See logs and config/.env.local.";
    $("#google-login").disabled = true;
  }
}

async function boot() {
  const config = await api("/api/auth/config");
  try {
    state.user = await api("/api/auth/me");
    showApp();
    await Promise.all([loadConversations(), checkHealth()]);
  } catch {
    showLogin(config);
  }
}

function showApp() {
  loginView.classList.add("hidden");
  appView.classList.remove("hidden");
  $("#user-name").textContent = state.user.display_name;
  $("#user-role").textContent = state.user.role;
  $("#user-initial").textContent = state.user.display_name.slice(0, 1).toUpperCase();
}

async function checkHealth() {
  const label = $("#health-label");
  try {
    const health = await api("/api/health");
    label.className = health.ok ? "healthy" : "unhealthy";
    label.innerHTML = `<i></i>${health.ok ? "Local model ready" : "Model runtime unavailable"}`;
  } catch {
    label.className = "unhealthy";
    label.innerHTML = "<i></i>XV12 health unavailable";
  }
}

async function loadConversations(selectId = null) {
  state.conversations = await api("/api/conversations");
  if (state.currentConversation) {
    const refreshed = state.conversations.find((item) => item.id === state.currentConversation.id);
    if (refreshed) {
      state.currentConversation = { ...state.currentConversation, ...refreshed };
      $("#conversation-title").textContent = refreshed.title;
    }
  }
  renderConversationList();
  if (selectId) await openConversation(selectId);
}

function renderConversationList() {
  const list = $("#conversation-list");
  list.replaceChildren();
  state.conversations.forEach((conversation) => {
    const button = document.createElement("button");
    button.className = `conversation-item${conversation.id === state.currentConversation?.id ? " active" : ""}`;
    button.textContent = conversation.title;
    button.title = conversation.title;
    button.addEventListener("click", () => openConversation(conversation.id));
    list.append(button);
  });
}

async function createConversation() {
  const conversation = await api("/api/conversations", { method: "POST", body: JSON.stringify({ title: "New conversation" }) });
  state.currentConversation = { ...conversation, messages: [], attachments: [] };
  state.conversations.unshift(conversation);
  renderConversationList();
  renderConversation();
  input.focus();
  return conversation;
}

async function openConversation(id) {
  state.currentConversation = await api(`/api/conversations/${id}`);
  renderConversationList();
  renderConversation();
  $("#sidebar").classList.remove("open");
}

function renderConversation() {
  const conversation = state.currentConversation;
  $("#conversation-title").textContent = conversation?.title || "New conversation";
  messages.replaceChildren();
  const hasMessages = Boolean(conversation?.messages?.length);
  welcome.classList.toggle("hidden", hasMessages);
  messages.classList.toggle("active", hasMessages);
  if (!hasMessages) return;
  conversation.messages.forEach((message) => appendMessage(message.role, message.content, message.status));
  scrollToLatest();
}

function appendMessage(role, content, status = "complete") {
  welcome.classList.add("hidden");
  messages.classList.add("active");
  const article = document.createElement("article");
  article.className = `message ${role} ${status || ""}`;
  if (role === "assistant") {
    const image = document.createElement("img");
    image.className = "message-avatar";
    image.src = "/assets/avatar/xoduz-512.png";
    image.alt = "X";
    article.append(image);
  }
  const body = document.createElement("div");
  body.className = "message-body";
  if (role === "assistant") {
    const name = document.createElement("p");
    name.className = "message-name";
    name.textContent = "XODUZ";
    body.append(name);
  }
  const text = document.createElement("div");
  text.className = "message-content";
  renderMessageText(text, content);
  body.append(text);
  article.append(body);
  messages.append(article);
  scrollToLatest();
  return { article, text };
}

function renderMessageText(target, content) {
  target.replaceChildren();
  const fragments = String(content).split(/(\*\*[^*]+\*\*)/g);
  for (const fragment of fragments) {
    if (fragment.startsWith("**") && fragment.endsWith("**") && fragment.length > 4) {
      const strong = document.createElement("strong");
      strong.textContent = fragment.slice(2, -2);
      target.append(strong);
    } else {
      target.append(document.createTextNode(fragment));
    }
  }
}

function scrollToLatest() {
  requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });
}

function setSending(sending) {
  state.sending = sending;
  sendButton.textContent = sending ? "■" : "↑";
  sendButton.setAttribute("aria-label", sending ? "Stop response" : "Send message");
  input.disabled = sending;
}

async function sendMessage(text) {
  if (!text.trim() || state.sending) return;
  if (!state.currentConversation) await createConversation();
  const conversationId = state.currentConversation.id;
  const visibleText = text.trim();
  appendMessage("user", visibleText);
  state.currentConversation.messages.push({ role: "user", content: visibleText, status: "complete" });
  input.value = "";
  resizeInput();
  const pending = [...state.pendingAttachments];
  state.pendingAttachments = [];
  renderAttachmentChips();
  const assistant = appendMessage("assistant", "");
  assistant.text.classList.add("typing-cursor");
  setSending(true);
  composerStatus.className = "composer-status";
  composerStatus.textContent = "X is thinking locally…";
  state.controller = new AbortController();
  document.querySelector(".avatar-stage")?.classList.add("speaking");
  try {
    const response = await fetch(`/api/conversations/${conversationId}/stream`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: visibleText, attachment_ids: pending.map((item) => item.id) }),
      signal: state.controller.signal,
    });
    if (!response.ok || !response.body) throw new Error(`Chat failed (${response.status})`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let responseText = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames) {
        const event = frame.match(/^event: (.+)$/m)?.[1];
        const raw = frame.match(/^data: (.+)$/m)?.[1];
        if (!raw) continue;
        const data = JSON.parse(raw);
        if (event === "delta") {
          responseText += data.text;
          renderMessageText(assistant.text, responseText);
          scrollToLatest();
        } else if (event === "error") {
          throw new Error(data.message);
        }
      }
    }
    assistant.text.classList.remove("typing-cursor");
    state.currentConversation.messages.push({ role: "assistant", content: responseText, status: "complete" });
    composerStatus.textContent = "X can make mistakes. Verify important information.";
    await loadConversations();
  } catch (error) {
    assistant.text.classList.remove("typing-cursor");
    if (error.name === "AbortError") {
      if (!assistant.text.textContent) assistant.article.remove();
      else assistant.article.classList.add("interrupted");
      composerStatus.textContent = "Response stopped.";
    } else {
      if (!assistant.text.textContent) assistant.text.textContent = "I couldn't complete that response. The local model may not be ready.";
      assistant.article.classList.add("interrupted");
      composerStatus.className = "composer-status error";
      composerStatus.textContent = error.message;
    }
  } finally {
    setSending(false);
    state.controller = null;
    document.querySelector(".avatar-stage")?.classList.remove("speaking");
    input.focus();
  }
}

async function uploadFile(file) {
  if (!file) return;
  composerStatus.textContent = `Attaching ${file.name}…`;
  const form = new FormData();
  form.append("file", file);
  if (state.currentConversation) form.append("conversation_id", state.currentConversation.id);
  try {
    const item = await api("/api/attachments", { method: "POST", body: form });
    state.pendingAttachments.push(item);
    renderAttachmentChips();
    composerStatus.textContent = "Attachment ready. Baseline 1 sends metadata only.";
  } catch (error) {
    composerStatus.className = "composer-status error";
    composerStatus.textContent = error.message;
  }
}

function renderAttachmentChips() {
  const chips = $("#attachment-chips");
  chips.replaceChildren();
  state.pendingAttachments.forEach((item) => {
    const chip = document.createElement("span");
    chip.className = "attachment-chip";
    chip.textContent = `＋ ${item.original_name}`;
    chips.append(chip);
  });
}

function setupSpeech() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const button = $("#mic-button");
  if (!Recognition) {
    button.addEventListener("click", () => {
      composerStatus.className = "composer-status error";
      composerStatus.textContent = "Dictation is not supported in this browser. Typed chat still works.";
    });
    return;
  }
  let recognition = null;
  button.addEventListener("click", () => {
    if (recognition) {
      recognition.stop();
      return;
    }
    recognition = new Recognition();
    recognition.lang = navigator.language || "en-US";
    recognition.interimResults = true;
    recognition.continuous = false;
    const startingText = input.value.trim();
    recognition.onstart = () => {
      button.classList.add("listening");
      button.setAttribute("aria-label", "Stop dictation");
      composerStatus.textContent = "Listening…";
    };
    recognition.onresult = (event) => {
      const transcript = Array.from(event.results).map((result) => result[0].transcript).join("");
      input.value = [startingText, transcript].filter(Boolean).join(startingText ? " " : "");
      resizeInput();
    };
    recognition.onerror = (event) => {
      composerStatus.className = "composer-status error";
      composerStatus.textContent = event.error === "not-allowed" ? "Microphone access was denied." : `Dictation stopped: ${event.error}.`;
    };
    recognition.onend = () => {
      recognition = null;
      button.classList.remove("listening");
      button.setAttribute("aria-label", "Start dictation");
      if (!composerStatus.classList.contains("error")) composerStatus.textContent = "Dictation added to the composer.";
      input.focus();
    };
    recognition.start();
  });
}

function resizeInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

function showModal(kind) {
  const content = $("#modal-content");
  if (kind === "tools") {
    const available = state.user.role === "admin" ? "System health and registry inspection are available to the owner." : "Only safe Tier 0 capabilities are visible to this user.";
    content.innerHTML = `<p class="eyebrow">CAPABILITY FOUNDATION</p><h2>Tools</h2><p>Baseline 1 intentionally keeps the capability surface small. ${available}</p><ul class="modal-list"><li><strong>System health</strong><span>Tier 0 · local · read-only</span></li>${state.user.role === "admin" ? '<li><strong>Capability inspection</strong><span>Tier 2 · administrator only · read-only</span></li>' : ""}</ul>`;
  } else {
    content.innerHTML = `<p class="eyebrow">XODUZ XV12</p><h2>Settings</h2><p>Your session is server-authoritative and your conversations, attachments, active subject, and rolling context are isolated under your internal user identity.</p><ul class="modal-list"><li><strong>Identity</strong><span>${state.user.display_name} · ${state.user.role}</span></li><li><strong>Runtime</strong><span>Local Qwen3-Coder · approximately 32K context</span></li><li><strong>Privacy</strong><span>XV12-owned local storage</span></li></ul>`;
  }
  $("#modal").showModal();
}

$("#google-login").addEventListener("click", () => { window.location.assign("/api/auth/google/start"); });
document.querySelectorAll("[data-persona]").forEach((button) => button.addEventListener("click", async () => {
  $("#login-status").textContent = "Signing in…";
  try {
    state.user = await api("/api/auth/test-login", { method: "POST", body: JSON.stringify({ persona: button.dataset.persona }) });
    showApp();
    await Promise.all([loadConversations(), checkHealth()]);
  } catch (error) { $("#login-status").textContent = error.message; }
}));
$("#logout").addEventListener("click", async () => { await api("/api/auth/logout", { method: "POST" }); window.location.reload(); });
$("#new-chat").addEventListener("click", createConversation);
$("#composer").addEventListener("submit", (event) => {
  event.preventDefault();
  if (state.sending) state.controller?.abort(); else sendMessage(input.value);
});
input.addEventListener("input", resizeInput);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#composer").requestSubmit(); }
});
$("#file-input").addEventListener("change", (event) => { uploadFile(event.target.files[0]); event.target.value = ""; });
document.querySelectorAll(".suggestions button").forEach((button) => button.addEventListener("click", () => { input.value = button.textContent; resizeInput(); input.focus(); }));
$("#tools-button").addEventListener("click", () => showModal("tools"));
$("#settings-button").addEventListener("click", () => showModal("settings"));
$("#close-modal").addEventListener("click", () => $("#modal").close());
$("#open-sidebar").addEventListener("click", () => $("#sidebar").classList.add("open"));
$("#close-sidebar").addEventListener("click", () => $("#sidebar").classList.remove("open"));

setupSpeech();
boot().catch((error) => {
  loginView.classList.remove("hidden");
  $("#login-status").textContent = `XV12 could not start the interface: ${error.message}`;
});
