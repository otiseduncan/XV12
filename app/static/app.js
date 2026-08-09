const state = {
  user: null, conversations: [], projects: [], currentConversation: null,
  pendingAttachments: [], controller: null, sending: false, pinnedToBottom: true,
  voiceSettings: null, availableVoices: [], effectiveVoice: null, voiceInitialized: false, ttsSpeaking: false,
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

class ControlledSpeechSynthesisUtterance {
  constructor(text) { this.text = text; this.voice = null; this.volume = 1; this.lang = "en-US"; }
}

class ControlledSpeechSynthesis {
  constructor(shouldFail = false) {
    this.voices = [
      { name: "Google US English", lang: "en-US", default: true, localService: true },
      { name: "XV12 Test Alternate", lang: "en-US", default: false, localService: true },
    ];
    this.listeners = {};
    this.shouldFail = shouldFail;
  }
  getVoices() { return this.voices; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  speak(utterance) {
    window.__XV12_TTS_TEST_LAST__ = { text: utterance.text, voice_name: utterance.voice?.name || null, volume: utterance.volume };
    utterance.onstart?.(); setTimeout(() => this.shouldFail ? utterance.onerror?.({ error: "synthesis-failed" }) : utterance.onend?.(), 20);
  }
  cancel() { window.__XV12_TTS_TEST_CANCELLED__ = true; }
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
  if (config.mode === "test" && new URLSearchParams(location.search).get("voice_test") === "1") {
    const voiceTestFailure = new URLSearchParams(location.search).get("voice_fail") === "1";
    window.__XV12_SPEECH_RECOGNITION__ = ControlledSpeechRecognition;
    window.__XV12_SPEECH_SYNTHESIS__ = new ControlledSpeechSynthesis(voiceTestFailure);
    window.__XV12_SPEECH_UTTERANCE__ = ControlledSpeechSynthesisUtterance;
  }
  try { state.user = await api("/api/auth/me"); showApp(); await Promise.all([loadConversations(), loadProjects(), checkHealth(), loadVoiceSettings()]); }
  catch { showLogin(config); }
}

function showApp() {
  loginView.classList.add("hidden"); appView.classList.remove("hidden");
  const name = state.user.conversational_name || state.user.display_name;
  $("#user-name").textContent = name; $("#user-role").textContent = state.user.role;
  $("#top-user").textContent = `${name} · ${state.user.role}`;
  $("#user-initial").textContent = name.slice(0, 1).toUpperCase(); $("#welcome-name").textContent = `What are we working on, ${name}?`;
}

function speechEngine() { return window.__XV12_SPEECH_SYNTHESIS__ || window.speechSynthesis || null; }
function utteranceType() { return window.__XV12_SPEECH_UTTERANCE__ || window.SpeechSynthesisUtterance || null; }
function updateAvatarActivity() { avatarStage.classList.toggle("speaking", state.sending || state.ttsSpeaking); }
function isLikelyFemaleVoice(voice) { return /\b(zira|aria|jenny|samantha|ava|female)\b/i.test(voice.name); }

function resolveEffectiveVoice() {
  if (!state.voiceSettings) return null;
  const requested = state.voiceSettings.voice_name;
  state.effectiveVoice = state.availableVoices.find((voice) => voice.name === requested)
    || state.availableVoices.find((voice) => String(voice.lang).toLowerCase() === "en-us" && isLikelyFemaleVoice(voice))
    || state.availableVoices.find((voice) => String(voice.lang).toLowerCase() === "en-us")
    || (speechEngine() && utteranceType() ? { name: "Browser default en-US", lang: "en-US", runtimeDefault: true } : null)
    || null;
  refreshVoiceControls();
  return state.effectiveVoice;
}

function enumerateVoices() {
  const engine = speechEngine();
  state.availableVoices = engine?.getVoices ? engine.getVoices().slice().sort((a, b) => a.name.localeCompare(b.name)) : [];
  resolveEffectiveVoice();
}

function initializeVoiceOutput() {
  if (state.voiceInitialized) { enumerateVoices(); return; }
  state.voiceInitialized = true;
  const engine = speechEngine();
  if (!engine) { refreshVoiceControls(); return; }
  engine.addEventListener?.("voiceschanged", enumerateVoices);
  enumerateVoices();
}

async function loadVoiceSettings() {
  state.voiceSettings = await api("/api/settings/voice");
  initializeVoiceOutput(); renderQuickMute();
}

function voiceDiagnostic() {
  if (!speechEngine()) return "Speech output is unavailable in this browser. Text chat remains ready.";
  if (!state.effectiveVoice) return "No speech voice is currently exposed by this browser. Text chat remains ready.";
  if (state.effectiveVoice.runtimeDefault) return `Preferred ${state.voiceSettings.voice_name} is unavailable. The browser exposed no voice list; using its default en-US synthesis voice.`;
  if (state.effectiveVoice.name !== state.voiceSettings?.voice_name) return `Preferred ${state.voiceSettings.voice_name} is unavailable. Using en-US fallback: ${state.effectiveVoice.name}.`;
  return `Runtime voice: ${state.effectiveVoice.name} (${state.effectiveVoice.lang || "language unknown"}).`;
}

function renderQuickMute() {
  const button = $("#quick-mute");
  if (!button || !state.voiceSettings) return;
  const muted = state.voiceSettings.voice_muted;
  button.textContent = muted ? "🔇" : "🔊";
  button.title = muted ? "Unmute X" : "Mute X";
  button.setAttribute("aria-label", button.title);
  button.setAttribute("aria-pressed", String(muted));
}

function refreshVoiceControls() {
  const select = $("#voice-select");
  if (!select || !state.voiceSettings) return;
  select.replaceChildren();
  const requested = state.voiceSettings.voice_name;
  if (!state.availableVoices.some((voice) => voice.name === requested)) {
    const missing = document.createElement("option");
    missing.value = requested; missing.textContent = `${requested} (preferred, unavailable)`; select.append(missing);
  }
  state.availableVoices.forEach((voice) => {
    const option = document.createElement("option");
    option.value = voice.name; option.textContent = `${voice.name}${voice.lang ? ` · ${voice.lang}` : ""}`; select.append(option);
  });
  select.value = requested; select.disabled = state.availableVoices.length === 0;
  const diagnostic = $("#voice-runtime"); if (diagnostic) diagnostic.textContent = voiceDiagnostic();
  const volume = $("#voice-volume"); if (volume) volume.value = state.voiceSettings.voice_volume;
  const output = $("#voice-volume-value"); if (output) output.value = state.voiceSettings.voice_volume;
  const mute = $("#voice-muted"); if (mute) mute.checked = state.voiceSettings.voice_muted;
  const preview = $("#preview-voice"); if (preview) preview.disabled = state.voiceSettings.voice_muted || !state.effectiveVoice;
}

function applyVoiceSettings(settings) {
  const wasMuted = state.voiceSettings?.voice_muted;
  state.voiceSettings = settings;
  if (settings.voice_muted && !wasMuted) speechEngine()?.cancel?.();
  resolveEffectiveVoice(); renderQuickMute(); refreshVoiceControls();
}

async function saveVoiceSettings(changes) {
  const previous = state.voiceSettings;
  applyVoiceSettings({ ...state.voiceSettings, ...changes });
  try {
    const settings = await api("/api/settings/voice", { method: "PATCH", body: JSON.stringify(changes) });
    applyVoiceSettings(settings); return settings;
  } catch (error) {
    applyVoiceSettings(previous); toast(`Voice setting was not saved: ${error.message}`, "error"); return null;
  }
}

function speakX(text, { preview = false } = {}) {
  if (!state.voiceSettings || state.voiceSettings.voice_muted) {
    if (preview) toast("X is muted. Unmute to preview the voice.");
    return false;
  }
  const engine = speechEngine(), Utterance = utteranceType(), voice = resolveEffectiveVoice();
  if (!engine || !Utterance || !voice) {
    toast("X could not start speech output. Text chat remains available.", "error"); return false;
  }
  try {
    engine.cancel();
    const utterance = new Utterance(String(text).trim());
    if (!voice.runtimeDefault) utterance.voice = voice;
    utterance.lang = voice.lang || "en-US"; utterance.volume = state.voiceSettings.voice_volume / 100;
    utterance.onstart = () => { state.ttsSpeaking = true; updateAvatarActivity(); $("#presence-label").textContent = "Speaking"; };
    utterance.onend = () => { state.ttsSpeaking = false; updateAvatarActivity(); if (!state.sending) $("#presence-label").textContent = "Ready"; };
    utterance.onerror = () => { state.ttsSpeaking = false; updateAvatarActivity(); toast("X audio output failed. The text response is still available.", "error"); };
    engine.speak(utterance); return true;
  } catch {
    state.ttsSpeaking = false; updateAvatarActivity(); toast("X audio output failed. The text response is still available.", "error"); return false;
  }
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

function artifactUrl(artifact, download = false) {
  const base = (!download && artifact.type === "application" && artifact.metadata?.preview_url) || artifact.reference || artifact.preview?.url || ""; if (!base) return "";
  const page = artifact.preview?.page || artifact.metadata?.page; const query = download ? `${base}${base.includes("?") ? "&" : "?"}download=true` : base;
  return page && !download ? `${query}#page=${page}` : query;
}

async function copyArtifact(artifact) {
  try {
    if (artifact.type === "image" && artifactUrl(artifact) && window.ClipboardItem) {
      try {
        const response = await fetch(artifactUrl(artifact), { credentials: "same-origin" }); const blob = await response.blob();
        await navigator.clipboard.write([new ClipboardItem({ [blob.type]: blob })]);
      } catch { await writeClipboardText(new URL(artifactUrl(artifact), window.location.href).href); }
    } else {
      const text = artifact.data ? (typeof artifact.data === "string" ? artifact.data : JSON.stringify(artifact.data, null, 2))
        : await (await fetch(`/api/artifacts/${encodeURIComponent(artifact.id)}/text`, { credentials: "same-origin" })).text();
      await writeClipboardText(text);
    }
    toast("Copied to clipboard.", "success");
  } catch { toast("This artifact could not be copied in the current browser.", "error"); }
}

async function writeClipboardText(text) {
  try { await navigator.clipboard.writeText(text); return; } catch {}
  const field = document.createElement("textarea"); field.value = text; field.setAttribute("readonly", ""); field.style.cssText = "position:fixed;left:-9999px;top:0"; document.body.append(field); field.select();
  const copied = document.execCommand("copy"); field.remove(); if (!copied) throw new Error("Clipboard write was rejected.");
}

function appendArtifact(container, artifact) {
  const displayKey = artifact.display_key || [artifact.source_artifact_id || artifact.id, artifact.page_start, artifact.page_end, artifact.section_title].filter((value) => value !== null && value !== undefined).join(":");
  const duplicate = [...messages.querySelectorAll(".artifact-card")].find((item) => item.dataset.artifactKey === displayKey); if (duplicate) duplicate.remove();
  const article = document.createElement("article"); article.className = `artifact-card artifact-${String(artifact.type || "file").replace(/[^a-z_-]/gi, "")}`; article.dataset.artifactId = artifact.id || ""; article.dataset.artifactKey = displayKey;
  const pageStart = artifact.page_start ?? artifact.metadata?.page_start ?? artifact.metadata?.page; const pageEnd = artifact.page_end ?? artifact.metadata?.page_end ?? pageStart;
  const pageLabel = pageStart ? (pageEnd && pageEnd !== pageStart ? `Pages ${pageStart}–${pageEnd}` : `Page ${pageStart}`) : "";
  const section = [artifact.section_title || artifact.metadata?.section, artifact.subsection_title || artifact.metadata?.subsection].filter(Boolean).join(" — ");
  article.innerHTML = `<header><div class="artifact-mark">▧</div><div class="artifact-heading"><strong></strong><span></span></div></header><div class="artifact-preview"></div><div class="artifact-actions"></div>`;
  article.querySelector("strong").textContent = artifact.title || "Artifact";
  article.querySelector(".artifact-heading span").textContent = [artifact.source, pageLabel, section].filter(Boolean).join(" · ");
  const preview = article.querySelector(".artifact-preview"), url = artifactUrl(artifact);
  if (artifact.type === "image" && url) {
    const image = document.createElement("img"); image.src = url; image.alt = artifact.title || "Generated image"; image.loading = "lazy"; preview.append(image);
  } else if (artifact.type === "video" && url) {
    const video = document.createElement("video"); video.src = url; video.controls = true; video.preload = "metadata"; video.playsInline = true; preview.append(video);
  } else if (artifact.type === "application" && artifact.metadata?.preview_url) {
    const frame = document.createElement("iframe"); frame.src = artifact.metadata.preview_url; frame.title = `${artifact.title || "Application"} preview`; frame.loading = "lazy"; frame.setAttribute("sandbox", "allow-forms allow-modals allow-scripts");
    const fallback = document.createElement("img"); fallback.alt = `${artifact.title || "Application"} screenshot fallback`; fallback.className = "application-fallback hidden"; fallback.src = artifact.metadata?.screenshot?.reference || "";
    frame.addEventListener("error", () => { if (fallback.src) { frame.classList.add("hidden"); fallback.classList.remove("hidden"); } }); preview.append(frame, fallback);
  } else if (artifact.mime_type === "application/pdf" && url) {
    const frame = document.createElement("iframe"); frame.src = url; frame.title = `${artifact.title || "Document"} preview`; frame.loading = "lazy"; preview.append(frame);
  } else if (artifact.type === "document" && url) {
    const frame = document.createElement("iframe"); frame.src = url; frame.title = `${artifact.title || "Document"} preview`; frame.loading = "lazy"; preview.append(frame);
  } else if (artifact.type === "structured_data" && Array.isArray(artifact.data)) {
    const table = document.createElement("table"), columns = [...new Set(artifact.data.flatMap((row) => Object.keys(row || {})))];
    const head = document.createElement("thead"), header = document.createElement("tr"); columns.forEach((name) => { const cell = document.createElement("th"); cell.textContent = name; header.append(cell); }); head.append(header); table.append(head);
    const body = document.createElement("tbody"); artifact.data.forEach((row) => { const tr = document.createElement("tr"); columns.forEach((name) => { const cell = document.createElement("td"); cell.textContent = row?.[name] ?? ""; tr.append(cell); }); body.append(tr); }); table.append(body); preview.append(table);
  } else if (artifact.type === "receipt" && artifact.data) {
    const list = document.createElement("dl"); Object.entries(artifact.data).forEach(([key, value]) => { const term = document.createElement("dt"); term.textContent = key.replaceAll("_", " "); const detail = document.createElement("dd"); detail.textContent = value ?? ""; list.append(term, detail); }); preview.append(list);
  } else { preview.classList.add("hidden"); }
  const actions = article.querySelector(".artifact-actions");
  if (url) { const view = document.createElement("a"); view.href = url; view.target = "_blank"; view.rel = "noopener"; view.textContent = artifact.type === "application" ? "Open / Expand" : "View"; actions.append(view); }
  const scoped = artifact.metadata?.scope_kind && artifact.metadata.scope_kind !== "full";
  if (artifact.downloadable && artifact.reference) { const download = document.createElement("a"); download.href = artifactUrl(artifact, true); download.textContent = scoped ? (pageStart === pageEnd ? "Download Page" : "Download Section") : "Download"; actions.append(download); }
  if (artifact.type === "application" && artifact.metadata?.project_archive?.reference) { const project = document.createElement("a"); project.href = `${artifact.metadata.project_archive.reference}?download=true`; project.textContent = "Download Project"; actions.append(project); }
  if (artifact.printable && url) { const print = document.createElement("button"); print.type = "button"; print.textContent = scoped ? (pageStart === pageEnd ? "Print Page" : "Print Section") : "Print"; print.addEventListener("click", () => { const popup = window.open(url, "_blank"); if (popup) { popup.opener = null; popup.addEventListener("load", () => setTimeout(() => popup.print(), 700), { once: true }); } }); actions.append(print); }
  if (artifact.copyable) { const copy = document.createElement("button"); copy.type = "button"; copy.textContent = artifact.mime_type === "application/pdf" ? "Copy text" : "Copy"; copy.addEventListener("click", () => copyArtifact(artifact)); actions.append(copy); }
  if (artifact.full_document_reference) { const full = document.createElement("a"); full.href = artifact.full_document_reference; full.target = "_blank"; full.rel = "noopener"; full.textContent = "Full Document"; actions.append(full); }
  if (!preview.classList.contains("hidden")) { const collapse = document.createElement("button"); collapse.type = "button"; collapse.textContent = "Collapse"; collapse.addEventListener("click", () => { preview.classList.toggle("hidden"); collapse.textContent = preview.classList.contains("hidden") ? "Expand" : "Collapse"; }); actions.append(collapse); }
  container.append(article);
}

function appendCard(container, card) {
  const result = card.result || {}, artifacts = Array.isArray(result.artifacts) ? result.artifacts : [result.artifact, result.job?.result?.artifact].filter(Boolean);
  artifacts.forEach((artifact) => appendArtifact(container, artifact));
  if (result.job?.job_id) appendJobCard(container, result.job);
  const searchResults = result.results || result.items || [], links = Array.isArray(searchResults) ? searchResults.filter((item) => item?.url).slice(0, 5) : [];
  if (links.length) {
    const article = document.createElement("article"); article.className = "source-card"; article.innerHTML = `<header><strong>Sources</strong></header><div class="card-detail"></div>`; const detail = article.querySelector(".card-detail");
    links.forEach((item) => { const line = document.createElement("a"); line.className = "card-result"; line.href = item.url; line.target = "_blank"; line.rel = "noopener noreferrer"; line.textContent = item.title || item.url; const meta = document.createElement("small"); meta.textContent = [item.source, item.published_at].filter(Boolean).join(" · "); line.append(meta); detail.append(line); }); container.append(article);
  } else if (!artifacts.length && Array.isArray(searchResults) && searchResults.length) {
    const article = document.createElement("article"); article.className = "source-card"; article.innerHTML = `<header><strong>Results</strong></header><div class="card-detail"></div>`; const detail = article.querySelector(".card-detail");
    searchResults.slice(0, 5).forEach((item) => { const line = document.createElement("div"); line.className = "card-result"; line.textContent = item.title || item.ro_number || [item.vehicle?.year, item.vehicle?.make, item.vehicle?.model].filter(Boolean).join(" ") || "Result"; detail.append(line); }); container.append(article);
  } else if (!artifacts.length && (result.message || result.project?.name)) {
    const note = document.createElement("div"); note.className = "result-note"; note.textContent = result.message || `Project: ${result.project.name}`; container.append(note);
  }
}

function appendJobCard(container, job) {
  const article = document.createElement("article"); article.className = "job-card"; article.dataset.jobId = job.job_id;
  article.innerHTML = `<header><div class="artifact-mark">◷</div><div class="artifact-heading"><strong></strong><span></span></div></header><div class="job-progress"><progress max="100"></progress><span></span></div><div class="artifact-actions"></div>`;
  const title = article.querySelector("strong"), detail = article.querySelector(".artifact-heading span"), progress = article.querySelector("progress"), note = article.querySelector(".job-progress span"), actions = article.querySelector(".artifact-actions");
  title.textContent = job.title || String(job.job_type || "Creator job").replaceAll(".", " ");
  const paint = (current) => { if (current.result?.artifact?.title) title.textContent = current.result.artifact.title; detail.textContent = current.state || "queued"; progress.value = current.progress || 0; note.textContent = `${current.progress || 0}% · ${current.message || current.state || "Queued"}`; };
  paint(job);
  if (!["succeeded", "failed", "cancelled"].includes(job.state)) {
    const cancel = document.createElement("button"); cancel.type = "button"; cancel.textContent = "Cancel";
    cancel.addEventListener("click", async () => { const current = await api(`/api/creator/jobs/${encodeURIComponent(job.job_id)}/cancel`, { method: "POST" }); paint(current); cancel.disabled = true; }); actions.append(cancel);
    const poll = async () => { try { const current = await api(`/api/creator/jobs/${encodeURIComponent(job.job_id)}`); paint(current); if (["succeeded", "failed", "cancelled"].includes(current.state)) { cancel.remove(); if (current.result?.artifact) appendArtifact(container, current.result.artifact); return; } setTimeout(poll, 1200); } catch { detail.textContent = "status unavailable"; } };
    setTimeout(poll, 900);
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
  updateAvatarActivity(); $("#presence-label").textContent = "Thinking";
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
          if (data.status === "running") { composerStatus.textContent = "Checking authorized sources…"; $("#presence-label").textContent = "Working"; }
          if (data.status === "complete") {
            appendCard(assistant.cards, data); scrollLatest();
            if (data.capability_id.startsWith("project.")) await loadProjects();
            if (data.capability_id === "settings.voice.update" && data.result?.settings) applyVoiceSettings(data.result.settings);
          }
        }
        if (event === "error") throw new Error(data.message);
      }
    }
    assistant.text.classList.remove("typing-cursor"); composerStatus.textContent = "Response complete."; $("#presence-label").textContent = "Ready"; speakX(assistant.text.textContent);
    await loadConversations();
  } catch (error) {
    assistant.text.classList.remove("typing-cursor");
    if (error.name === "AbortError") { assistant.wrapper.classList.add("interrupted"); composerStatus.textContent = "Response stopped."; }
    else { assistant.wrapper.classList.add("failed"); composerStatus.className = "composer-status error"; composerStatus.textContent = error.message; toast(error.message, "error"); }
  } finally { state.sending = false; state.controller = null; sendButton.textContent = "↑"; updateAvatarActivity(); if (!state.ttsSpeaking) $("#presence-label").textContent = "Ready"; input.focus(); }
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

async function renderCapabilityAdmin(content) {
  const [users, catalog] = await Promise.all([api("/api/admin/capabilities/users"), api("/api/admin/capabilities/catalog")]);
  content.innerHTML = `<p class="eyebrow">ADMIN · REGISTRY ${catalog.registry_version}</p><h2>User capabilities</h2><p>Choose a normal user, grant only the scopes they need, then save. Changes are enforced by the gateway immediately.</p><label class="admin-user-label">Registered user<select id="capability-user"></select></label><form id="capability-grants" class="capability-grants"></form>`;
  const select = content.querySelector("#capability-user");
  users.forEach((user) => { const option = document.createElement("option"); option.value = user.id; option.textContent = `${user.display_name} · ${user.email} · ${user.role} · ${user.status}`; option.disabled = user.role === "admin"; select.append(option); });
  const normal = users.find((user) => user.role !== "admin");
  if (!normal) { content.querySelector("form").innerHTML = `<p class="setting-note">No normal users are registered yet.</p>`; select.disabled = true; return; }
  select.value = normal.id;
  const loadGrants = async () => {
    const current = await api(`/api/admin/capabilities/users/${select.value}/grants`); const form = content.querySelector("#capability-grants"); form.replaceChildren();
    catalog.families.forEach((family) => { const scopes = new Set(current.grants[family.family] || []); const card = document.createElement("fieldset"); card.className = "capability-grant"; card.dataset.family = family.family; card.disabled = family.health === "unavailable"; const legend = document.createElement("legend"); legend.textContent = family.label; const description = document.createElement("p"); description.textContent = `${family.description} · ${family.health}`; card.append(legend, description); family.allowed_scopes.forEach((scope) => { const label = document.createElement("label"); const box = document.createElement("input"); box.type = "checkbox"; box.name = scope; box.checked = scopes.has(scope); label.append(box, document.createTextNode(` ${scope}`)); card.append(label); }); form.append(card); });
    const actions = document.createElement("div"); actions.className = "settings-actions"; actions.innerHTML = `<button class="primary-button" type="submit">Save permissions</button><button id="revoke-capabilities" class="secondary-button" type="button">Revoke all access</button><span class="setting-note" id="permission-status"></span>`; form.append(actions);
    form.onsubmit = async (event) => { event.preventDefault(); const grants = [...form.querySelectorAll("fieldset")].map((field) => ({ family: field.dataset.family, scopes: [...field.querySelectorAll("input:checked")].map((box) => box.name) })).filter((item) => item.scopes.length); const result = await api(`/api/admin/capabilities/users/${select.value}/grants`, { method: "PUT", body: JSON.stringify({ grants }) }); form.querySelector("#permission-status").textContent = result.effective_immediately ? "Saved · effective now" : "Saved"; toast("Capability permissions updated.", "success"); };
    form.querySelector("#revoke-capabilities").onclick = async () => { await api(`/api/admin/capabilities/users/${select.value}/grants`, { method: "DELETE" }); toast("Capability access and active sessions revoked.", "success"); await loadGrants(); };
  };
  select.addEventListener("change", loadGrants); await loadGrants();
}

async function showModal(kind) {
  const content = $("#modal-content");
  if (kind === "projects") {
    await loadProjects(); content.innerHTML = `<p class="eyebrow">OPTIONAL CONTEXT</p><h2>Projects</h2><p>Register a reference explicitly. XV12 stores the reference but never scans or executes it.</p><form id="project-form" class="project-form"><input name="name" required maxlength="120" placeholder="Project name"><input name="reference" maxlength="500" placeholder="Path or reference (optional)"><textarea name="description" maxlength="2000" placeholder="Short description"></textarea><button class="primary-button" type="submit">Register project</button></form><div id="project-list" class="project-list"></div>`;
    const list = content.querySelector("#project-list"); state.projects.forEach((project) => { const row = document.createElement("div"); row.className = `project-row${project.is_active ? " active" : ""}`; row.innerHTML = `<div><strong></strong><span></span></div><button class="secondary-button"></button>`; row.querySelector("strong").textContent = project.name; row.querySelector("span").textContent = project.reference || project.description || "Registered project"; const button = row.querySelector("button"); button.textContent = project.is_active ? "Detach" : "Activate"; button.addEventListener("click", async () => { if (project.is_active) await api("/api/projects/active", { method: "DELETE" }); else await api(`/api/projects/${project.id}/activate`, { method: "POST" }); await loadProjects(); await showModal("projects"); toast(project.is_active ? "Project detached." : "Project activated.", "success"); }); list.append(row); });
    content.querySelector("#project-form").addEventListener("submit", async (event) => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.target)); const project = await api("/api/projects", { method: "POST", body: JSON.stringify(data) }); await api(`/api/projects/${project.id}/activate`, { method: "POST" }); await loadProjects(); await showModal("projects"); toast("Project registered and activated.", "success"); });
  } else if (kind === "tools") {
    const listing = await api("/api/capabilities"); content.innerHTML = `<p class="eyebrow">AUTHORITATIVE REGISTRY</p><h2>Capabilities</h2><p>${listing.capabilities.length} capabilities are authorized for this account.</p><ul class="modal-list"></ul>`; const list = content.querySelector("ul"); listing.capabilities.forEach((item) => { const li = document.createElement("li"); li.innerHTML = `<strong></strong><span></span>`; li.querySelector("strong").textContent = item.id; li.querySelector("span").textContent = `Tier ${item.risk_tier} · ${item.health} · ${item.description}`; list.append(li); });
  } else if (kind === "admin-capabilities") {
    await renderCapabilityAdmin(content);
  } else {
    content.innerHTML = `<p class="eyebrow">XODUZ XV12</p><h2>Settings</h2><p>Preferences are private to your authenticated account.</p><section class="settings-section"><h3>Appearance</h3><div class="setting-row"><span class="setting-label">Theme</span><span class="account-value">XODUZ Dark</span></div></section><section class="settings-section"><h3>Voice</h3><div class="setting-row"><label for="voice-select">XODUZ voice</label><select id="voice-select" aria-label="XODUZ voice"></select></div><p id="voice-runtime" class="setting-note"></p><div class="setting-row"><label for="voice-volume">Volume</label><div class="volume-control"><input id="voice-volume" type="range" min="0" max="100" step="1"><output id="voice-volume-value" for="voice-volume"></output></div></div><div class="setting-row"><span class="setting-label">Spoken output</span><label class="toggle-control"><input id="voice-muted" type="checkbox"> Mute X</label></div><div class="setting-row"><span class="setting-label">Test output</span><div class="settings-actions"><button id="preview-voice" class="secondary-button" type="button">Preview Voice</button></div></div><p id="voice-preview-status" class="setting-note" aria-live="polite"></p></section><section class="settings-section"><h3>Account</h3><div class="setting-row"><span class="setting-label">Signed in</span><span id="settings-account" class="account-value"></span></div><div class="setting-row"><span class="setting-label">Session</span><button id="settings-logout" class="secondary-button" type="button">Log out</button></div></section>`;
    if (state.user.role === "admin") content.insertAdjacentHTML("beforeend", `<section class="settings-section"><h3>Admin</h3><div class="setting-row"><span class="setting-label">User capability access</span><button id="admin-capability-settings" class="secondary-button" type="button">Manage permissions</button></div></section>`);
    $("#settings-account").textContent = `${state.user.conversational_name} · ${state.user.role}`;
    refreshVoiceControls();
    $("#voice-select").addEventListener("change", (event) => saveVoiceSettings({ voice_name: event.target.value }));
    $("#voice-volume").addEventListener("input", (event) => { $("#voice-volume-value").value = event.target.value; });
    $("#voice-volume").addEventListener("change", (event) => saveVoiceSettings({ voice_volume: Number(event.target.value) }));
    $("#voice-muted").addEventListener("change", (event) => saveVoiceSettings({ voice_muted: event.target.checked }));
    $("#preview-voice").addEventListener("click", () => {
      const started = speakX("Hello. I'm X. This is a preview of my current voice settings.", { preview: true });
      $("#voice-preview-status").textContent = started ? `Previewing ${state.effectiveVoice.name} at ${state.voiceSettings.voice_volume}%.` : "Voice preview did not start.";
    });
    $("#settings-logout").addEventListener("click", async () => { speechEngine()?.cancel?.(); await api("/api/auth/logout", { method: "POST" }); window.location.reload(); });
    $("#admin-capability-settings")?.addEventListener("click", () => showModal("admin-capabilities"));
  }
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
$("#logout").addEventListener("click", async () => { speechEngine()?.cancel?.(); await api("/api/auth/logout", { method: "POST" }); window.location.reload(); });
$("#top-logout").addEventListener("click", async () => { speechEngine()?.cancel?.(); await api("/api/auth/logout", { method: "POST" }); window.location.reload(); });
$("#quick-mute").addEventListener("click", () => { if (state.voiceSettings) saveVoiceSettings({ voice_muted: !state.voiceSettings.voice_muted }); });
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
