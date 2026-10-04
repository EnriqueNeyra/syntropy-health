// Ask: chat about your health data with the AI chosen in Settings — a local model, your own key, or an agent on this
// computer (read-only tools, one person at a time).
import { del, get, put, unreachableMessage } from "../api.js";
import { appCan, appScroll, setComposer, setToolbar } from "../app.js";
import { $, confirmDialog, emptyState, fmtAgo, html, icon, loading, markdown, mount, toast } from "../ui.js";
import { confirmAiProvider } from "../consent.js";
import { logo, logoId } from "../ai-logos.js";
import { openModelPicker, sameChoice } from "../model-picker.js";

const SUGGESTIONS = [
  "How has my sleep changed over the last month?",
  "Summarize my latest lab results and anything out of range.",
  "Coach me on my training load this week.",
  "What's my resting heart rate and HRV trend lately?",
  "Prepare questions for my next doctor's visit.",
];
const REDUCED_MOTION = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Conversations are saved on the server (so they follow you to the desktop and iPhone apps), per person and account.
const newId = () => `c_${Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("")}`;
const titleOf = (messages) => {
  const first = messages.find((m) => m.role === "user")?.content || "New chat";
  return first.length > 70 ? `${first.slice(0, 67).trim()}…` : first;
};
const stored = (messages) => messages.filter((m) => !m.pending && !m.error && (m.content || m.role === "user"))
  .map(({ role, content, steps, note, by }) => ({ role, content, ...(steps?.length ? { steps } : {}), ...(note ? { note } : {}), ...(by ? { by } : {}) }));

export async function render({ el, state, params, navigate }) {
  const profile = state.profile;
  mount(el, loading(2));
  const cfg = await get("/api/ai/config");
  if (!cfg.configured) {
    mount(el, html`
      <div class="page-head"><div><h1>Ask</h1><p>Questions, trends and coaching from your own data, with the AI you choose.</p></div></div>
      ${emptyState("Connect an AI to get started",
        "Run a model on your own computer (Ollama, LM Studio) so nothing leaves your network, use your own API key (Anthropic, OpenAI, Google and others), or use Claude Code or Codex already signed in on this computer. The assistant can only read your data.",
        html`<a class="btn btn-primary" href="#/settings/ai">${icon("sparkles")} Connect an AI</a>`)}`);
    return;
  }

  const q = { profile: profile.id };
  let chats = (await get("/api/ai/chats", q, { fresh: true }).catch(() => ({ chats: [] }))).chats;
  let chatId = newId();
  let messages = [];
  // Pick up the latest conversation, unless a new one was asked for (File → New Chat in the desktop app).
  if (params?.get("new")) history.replaceState(null, "", "#/ask");
  else if (params?.get("chat") || chats[0]) {
    const open = await get(`/api/ai/chats/${params?.get("chat") || chats[0].id}`, q, { fresh: true }).catch(() => null);
    if (open) { chatId = open.id; messages = open.messages; }
  }
  let busy = false;
  let current = cfg;
  const saveChat = async () => {
    const keep = stored(messages);
    if (!keep.some((m) => m.role === "assistant")) return;
    try {
      await put(`/api/ai/chats/${chatId}?profile=${encodeURIComponent(profile.id)}`, { title: titleOf(keep), messages: keep });
      if (native) reloadChats();       // the app's Conversations menu lists it now
    } catch (err) { toast(`Couldn't save this conversation: ${err.message}`, "bad"); }
  };

  // In the iPhone app the conversation is the screen itself, like Messages: it scrolls with the screen, the message
  // field is the app's own (above the keyboard), and the model, past conversations and New chat are in its title bar.
  const native = appCan("composer") && appCan("toolbar");
  // Elsewhere the conversation fills the window and scrolls by itself: one scrollbar.
  el.classList.add(native ? "ask-native" : "page-fill");
  // The model menu's contents, fetched now so it opens at once (finding local models and AI apps takes a moment).
  let choices = null;
  const loadChoices = () => get("/api/ai/choices", null, { fresh: true }).then((res) => { choices = res; return res; });
  let choicesReady = loadChoices().catch(() => null);

  if (native) mount(el, html`
    <p class="small ask-privacy" id="ask-privacy"></p>
    <div class="chat-log" id="chat-log" aria-live="polite"></div>
    <p class="tiny faint chat-foot">Read-only access to ${profile.name}'s records. Not medical advice; check anything important with your care team.</p>`);
  else mount(el, html`
    <div class="page-head">
      <div><h1>Ask</h1><p class="small ask-privacy" id="ask-privacy"></p></div>
      <div class="row"><button class="model-btn" id="model-btn" aria-haspopup="true" aria-expanded="false"></button>
        <button class="btn btn-sm" id="ask-history" aria-haspopup="true" aria-expanded="false" title="Past conversations">${icon("history")}<span class="hide-narrow"> History</span></button>
        <button class="btn btn-sm" id="ask-new">${icon("plus")}<span class="hide-narrow"> New chat</span></button></div>
    </div>
    <div class="chat card">
      <div class="chat-log" id="chat-log" aria-live="polite"></div>
      <form class="chat-input" id="chat-form">
        <textarea class="input" id="chat-text" rows="1" placeholder="Ask about your health data…" aria-label="Your question"></textarea>
        <button class="btn btn-primary btn-icon" type="submit" id="chat-send" aria-label="Send">${icon("send")}</button>
      </form>
    </div>
    <p class="tiny faint chat-foot">Read-only access to ${profile.name}'s records. Not medical advice; check anything important with your care team.</p>`);

  const modelBtn = $("#model-btn", el);
  const drawHeader = () => {
    const c = current;
    mount($("#ask-privacy", el), c.kind === "agent"
      ? html`${icon("globe")}<span>Runs through ${c.label} on your ${c.plan} plan. Your questions and what it looks up go to ${c.vendor}.</span>`
      : c.kind === "local"
        ? html`${icon(c.address?.private ? "shieldCheck" : "globe")}<span>${c.address?.private ? "Runs on your own hardware. Nothing leaves your network." : `Runs on ${c.label}, a server you chose.`}${c.tools === false ? " It answers from a summary of your data." : ""}</span>`
        : html`${icon("globe")}<span>Your questions and only the data it looks up go to ${c.label}.</span>`);
    $("#ask-privacy", el).classList.toggle("private", c.kind === "local" && !!c.address?.private);
    if (!modelBtn) return drawToolbar();
    mount(modelBtn, html`<span class="model-btn-kind">${logo(logoId({ provider: c.provider, agent: c.agent_id, label: c.label }), c.label, "", c.kind === "local" ? "monitor" : "globe")}</span>
      <span class="truncate">${modelName(c)} <span class="muted">${c.label}</span></span>${icon("chevronDown")}`);
    modelBtn.setAttribute("aria-label", `AI: ${byline(c)}. Choose another`);
  };

  // The model menu: every AI that's connected. Choosing one switches Ask to it (and makes it the default, as in Settings).
  const pickModel = async (o, g) => {
    if (busy) return toast("Wait for the current answer first.");
    if ((g.kind !== "local" || !g.private) && !current.cloud_ack) {
      if (!(await confirmAiProvider(g.kind === "agent" ? g.vendor : g.label))) return;
      current = { ...current, cloud_ack: true };
    }
    try {
      current = await put("/api/ai/config", o.provider === "agent" ? { provider: "agent", agent: o.agent, model: o.model }
        : { provider: o.provider, model: o.model });
      if (choices) choices.current = { provider: o.provider, agent: o.agent || null, model: o.model };
      drawHeader();
      toast(`Now using ${byline(current)}`);
      choicesReady = loadChoices().then(() => drawToolbar(), () => null);    // the recently used list changed
    } catch (err) { toast(err.message, "bad"); }
  };
  let closeMenu = null;
  const openMenu = () => {
    if (closeMenu && document.querySelector(".model-menu:not(.closing)")) { closeMenu(); closeMenu = null; return; }
    closeMenu = openModelPicker(modelBtn, { onPick: pickModel, choices: choices || choicesReady });
  };
  modelBtn?.addEventListener("click", openMenu);

  const log = $("#chat-log", el);
  const text = $("#chat-text", el);
  const send = $("#chat-send", el);
  const nodes = new Map();          // message → its element, so a streaming answer updates in place
  let controller = null;            // aborts the answer being written (the Stop button)

  // Follow the answer as it grows, unless the person has scrolled up to read something. In the app the screen itself
  // scrolls, and the app knows whether it's near the bottom.
  const nearBottom = () => native || log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  const toBottom = (smooth, follow = false) => (native ? appScroll("bottom", { animated: smooth && !REDUCED_MOTION(), follow })
    : log.scrollTo({ top: log.scrollHeight, behavior: smooth && !REDUCED_MOTION() ? "smooth" : "auto" }));

  const bubble = (m, enter) => {
    const node = document.createElement("div");
    node.className = `msg ${m.role === "user" ? "msg-user" : "msg-ai"}${enter ? " enter" : ""}`;
    if (m.role === "user") node.textContent = m.content;
    else {
      mount(node, html`<div class="msg-note small muted"></div><div class="msg-steps"></div><div class="msg-body"></div>
        <div class="typing" aria-hidden="true"><span></span><span></span><span></span></div><div class="msg-by"></div>`);
      patch(m, node, false);
    }
    nodes.set(m, node);
    return node;
  };

  /** Brings an answer's element up to date: lookups, the text shown so far, the typing dots, who answered. */
  const patch = (m, node = nodes.get(m), animate = true) => {
    if (!node) return;
    const note = $(".msg-note", node);
    note.textContent = m.note || "";
    note.hidden = !m.note;
    const steps = $(".msg-steps", node);
    const have = steps.children.length;
    (m.steps || []).slice(have).forEach((label) => {
      const chip = document.createElement("span");
      if (animate) chip.className = "enter";
      mount(chip, html`${icon("check")} ${label}`);
      steps.appendChild(chip);
    });
    steps.hidden = !(m.steps || []).length;
    const body = $(".msg-body", node);
    const shown = m.pending ? (m.reveal?.text || "") : m.content;
    if (m.error) mount(body, html`<div class="banner bad small"><div class="grow">${m.error}</div></div>`);
    else if (body.dataset.shown !== shown) { mount(body, markdown(m.pending ? partialMarkdown(shown) : shown)); body.dataset.shown = shown; }
    node.classList.toggle("streaming", !!(m.pending && shown));
    $(".typing", node).hidden = !(m.pending && !shown);
    const by = $(".msg-by", node);
    by.textContent = m.pending ? "" : [m.stopped ? "Stopped" : "", m.by || ""].filter(Boolean).join(" · ");
    by.hidden = !by.textContent;
  };

  const drawAll = () => {
    nodes.clear();
    if (!messages.length) {
      mount(log, html`<div class="chat-empty enter">
        <span class="chat-empty-icon">${icon("sparkles")}</span>
        <h3>What would you like to know?</h3>
        <div class="chips">${SUGGESTIONS.map((s) => html`<button class="chip" type="button" data-suggest="${s}">${s}</button>`)}</div></div>`);
      if (native) appScroll("top", { animated: false });
      return;
    }
    log.replaceChildren(...messages.map((m) => bubble(m, false)));
    toBottom(false);
    // The page is built before it's on screen: open at the latest message once it has a size.
    if (native) requestAnimationFrame(() => requestAnimationFrame(() => toBottom(false)));
    else if (!log.clientHeight) {
      const ro = new ResizeObserver(() => { if (log.clientHeight) { ro.disconnect(); toBottom(false); } });
      ro.observe(log);
    }
  };

  const append = (m) => {
    log.querySelector(".chat-empty")?.remove();
    log.appendChild(bubble(m, true));
  };

  // The answer appears as if written, at a pace that keeps up with the model: a quick sweep when it arrives all at once,
  // steady when it streams in. Always whole words.
  const startReveal = (m) => {
    m.reveal = { target: "", text: "", raf: 0 };
    const step = () => {
      const r = m.reveal;
      if (!r) return;
      r.raf = 0;
      if (r.text === r.target) return;
      if (!r.target.startsWith(r.text)) {
        // The finished answer differs from the draft (whitespace, a reworded end): carry on from what they share.
        let same = 0;
        while (same < r.text.length && r.text[same] === r.target[same]) same++;
        r.text = r.target.slice(0, same);
      }
      const backlog = r.target.length - r.text.length;
      let end = r.text.length + (REDUCED_MOTION() ? backlog : Math.max(3, Math.ceil(backlog / 14)));
      const space = r.target.slice(end).search(/\s/);
      end = space < 0 ? r.target.length : Math.min(r.target.length, end + Math.min(space, 16));
      const follow = nearBottom();
      r.text = r.target.slice(0, end);
      patch(m);
      if (follow) toBottom(false, true);
      if (r.text !== r.target) r.raf = requestAnimationFrame(step);
      else if (r.onDone) r.onDone();
    };
    m.reveal.kick = () => { if (!m.reveal.raf) m.reveal.raf = requestAnimationFrame(step); };
  };
  const setTarget = (m, value) => { m.reveal.target = value; m.reveal.kick(); };
  // Resolves once everything received is on screen (the finished answer then replaces the streamed draft).
  const revealed = (m) => new Promise((resolve) => {
    const r = m.reveal;
    if (!r || r.text === r.target || document.hidden) return resolve();
    r.onDone = resolve;
    r.kick();
  });

  const autosize = () => { if (text) { text.style.height = "auto"; text.style.height = `${Math.min(text.scrollHeight, 200)}px`; } };
  const setBusy = (on) => {
    busy = on;
    if (native) return showComposer();
    send.classList.toggle("stop", on);
    mount(send, icon(on ? "stop" : "send"));
    send.setAttribute("aria-label", on ? "Stop" : "Send");
    send.title = on ? "Stop" : "";
    send.type = on ? "button" : "submit";
  };

  // An AI outside this network: confirmed once (Settings asks too, when it's chosen there).
  const consented = async () => {
    if (!current.sends_elsewhere || current.cloud_ack) return true;
    if (!(await confirmAiProvider(current.kind === "agent" ? current.vendor : current.label))) return false;
    current = { ...current, cloud_ack: true };
    return true;
  };

  async function ask(question) {
    if (busy || !question.trim()) return;
    if (!(await consented())) return;
    setBusy(true);
    const history = [...messages.filter((m) => !m.error && m.content), { role: "user", content: question.trim() }]
      .map(({ role, content }) => ({ role, content }));
    const mine = { role: "user", content: question.trim() };
    const reply = { role: "assistant", content: "", steps: [], pending: true };
    messages.push(mine, reply);
    append(mine);
    startReveal(reply);
    append(reply);
    if (text) { text.value = ""; autosize(); }
    toBottom(true);
    controller = new AbortController();
    let answer = null;
    try {
      const res = await fetch("/api/ai/chat", {
        method: "POST", credentials: "same-origin", signal: controller.signal,
        headers: { "Content-Type": "application/json", "X-Requested-With": "syntropy" },
        body: JSON.stringify({ profile_id: profile.id, messages: history }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Request failed (${res.status})`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "", draft = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          const ev = JSON.parse(line);
          if (ev.type === "delta") { draft += ev.text; setTarget(reply, draft.trimStart()); }
          else if (ev.type === "reset") { draft = ""; setTarget(reply, ""); }
          else if (ev.type === "tool") { reply.steps.push(ev.label); patch(reply); }
          else if (ev.type === "note") { reply.note = ev.text; patch(reply); }
          else if (ev.type === "answer") { answer = ev.text || "(No answer.)"; setTarget(reply, answer); }
          else if (ev.type === "error") reply.error = ev.message;
          else if (ev.type === "done") reply.by = byline(current);
        }
      }
    } catch (err) {
      if (err.name === "AbortError") reply.stopped = true;
      else reply.error = err instanceof TypeError ? unreachableMessage() : (err.message || String(err));
    } finally {
      controller = null;
      if (!reply.error && !reply.stopped) await revealed(reply);
      reply.content = answer ?? (reply.reveal?.text || "");
      reply.pending = false;
      if (reply.reveal?.raf) cancelAnimationFrame(reply.reveal.raf);
      delete reply.reveal;
      if (!reply.content && !reply.error) reply.error = reply.stopped ? "Stopped before an answer." : "No answer came back. Try again.";
      if (reply.stopped && !reply.by) reply.by = byline(current);
      setBusy(false);
      const follow = nearBottom();
      patch(reply);
      if (follow) toBottom(true, true);
      text?.focus();
      saveChat();
    }
  }

  log.addEventListener("click", (e) => {
    const chip = e.target.closest("[data-suggest]");
    if (chip) ask(chip.dataset.suggest);
  });
  const newChat = () => {
    if (busy) return toast("Wait for the current answer first.");
    chatId = newId(); messages = []; drawAll(); text?.focus();
    drawToolbar();
  };
  const openChat = async (id) => {
    if (busy) return toast("Wait for the current answer first.");
    const chat = await get(`/api/ai/chats/${id}`, q, { fresh: true }).catch((err) => { toast(err.message, "bad"); return null; });
    if (chat) { chatId = chat.id; messages = chat.messages; drawAll(); text?.focus(); drawToolbar(); }
  };
  const deleteChat = async (id) => {
    if (!(await confirmDialog("Delete this conversation?", "It's removed from this server for good.", { confirmLabel: "Delete", danger: true }))) return;
    try { await del(`/api/ai/chats/${id}`, q); } catch (err) { return toast(err.message, "bad"); }
    chats = chats.filter((c) => c.id !== id);
    if (id === chatId) { chatId = newId(); messages = []; drawAll(); }
    drawToolbar();
    toast("Conversation deleted");
  };
  const reloadChats = async () => {
    chats = (await get("/api/ai/chats", q, { fresh: true }).catch(() => ({ chats }))).chats;
    drawToolbar();
  };

  // ---- In the iPhone app: its message field and title bar
  function showComposer() {
    setComposer({ placeholder: "Ask about your health data", busy, onSend: (question) => ask(question), onStop: () => controller?.abort() });
  }

  function drawToolbar() {
    if (!native) return;
    const modelSections = (choices?.groups || []).map((g, gi) => {
      const option = (o, oi) => ({
        id: `model:${gi}:${oi}`, title: o.name, checked: sameChoice(o, choices.current),
        subtitle: o.tools === false ? "Answers from a summary of your data"
          : o.model && o.name.toLowerCase() !== o.model.toLowerCase() ? o.model : null,
        run: () => pickModel(o, g),
      });
      const all = g.options.map(option);
      const pinned = g.options.map((o, i) => (o.pinned || sameChoice(o, choices.current) ? i : -1)).filter((i) => i >= 0);
      const first = all.length <= 6 ? all : [...new Set([...pinned, 0, 1, 2, 3, 4])].slice(0, Math.max(5, pinned.length)).sort((a, b) => a - b).map((i) => all[i]);
      const items = g.kind === "local" && !g.reachable ? [{ id: `model:${gi}:off`, title: "Not reachable right now", disabled: true }] : first;
      if (first.length < all.length) items.push({ id: `model:${gi}:all`, title: `All ${all.length} Models`, symbol: "ellipsis", menu: [{ items: all }] });
      return { title: g.label, items };
    });
    const manage = { items: [{ id: "manage", title: "Manage AI Connections", symbol: "gearshape", run: () => navigate("#/settings/ai") }] };
    const name = modelName(current);
    const saved = chats.some((c) => c.id === chatId);
    setToolbar([
      { id: "model", title: name.length > 16 ? `${name.slice(0, 15).trim()}…` : name, accessibilityLabel: `AI: ${byline(current)}. Choose another`,
        menu: choices ? [...modelSections, manage] : [{ items: [{ id: "model:loading", title: "Finding your models…", disabled: true }] }, manage] },
      { id: "history", title: "Conversations", symbol: "clock.arrow.circlepath", menu: [
        { title: chats.length ? `Conversations about ${profile.name}` : null, items: chats.length
          ? chats.slice(0, 30).map((c) => ({ id: `chat:${c.id}`, title: c.title, subtitle: `${fmtAgo(c.updated_at)} · ${c.messages} messages`,
                                            checked: c.id === chatId, run: () => openChat(c.id) }))
          : [{ id: "chat:none", title: "No saved conversations yet", disabled: true }] },
        ...(saved ? [{ items: [{ id: "chat:delete", title: "Delete This Conversation", symbol: "trash", destructive: true, run: () => deleteChat(chatId) }] }] : []),
      ] },
      { id: "new", title: "New Chat", symbol: "square.and.pencil", run: newChat },
    ]);
  }

  if (native) {
    drawHeader();
    showComposer();
    drawAll();
    choicesReady.then(() => drawToolbar());
    return () => controller?.abort();
  }

  $("#chat-form", el).addEventListener("submit", (e) => { e.preventDefault(); ask(text.value); });
  send.addEventListener("click", (e) => { if (busy) { e.preventDefault(); controller?.abort(); } });
  text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(text.value); }
  });
  text.addEventListener("input", autosize);
  $("#ask-new", el).addEventListener("click", newChat);

  // Past conversations: open one to carry on, or delete it.
  const historyBtn = $("#ask-history", el);
  let closeHistory = null;
  const openHistory = async () => {
    if (closeHistory) { closeHistory(); return; }
    const list = (await get("/api/ai/chats", q, { fresh: true }).catch(() => ({ chats: [] }))).chats;
    const menu = document.createElement("div");
    menu.className = "popover chat-history";
    menu.setAttribute("role", "dialog");
    menu.setAttribute("aria-label", "Past conversations");
    mount(menu, html`<div class="chat-history-head"><b>Conversations</b><span class="tiny faint">about ${profile.name}</span></div>
      ${list.length ? html`<div class="chat-history-list">${list.map((c) => html`<div class="chat-history-item ${c.id === chatId ? "current" : ""}">
        <button class="chat-history-open" data-open="${c.id}"><span class="truncate">${c.title}</span><span class="tiny faint">${fmtAgo(c.updated_at)} · ${c.messages} messages</span></button>
        <button class="btn btn-ghost btn-icon btn-sm" data-delete="${c.id}" aria-label="Delete “${c.title}”" title="Delete">${icon("trash")}</button></div>`)}</div>`
        : html`<div class="small muted" style="padding:12px 14px">No saved conversations yet. They're kept here after each answer.</div>`}`);
    document.body.appendChild(menu);
    const r = historyBtn.getBoundingClientRect();
    const width = Math.min(360, window.innerWidth - 24);
    menu.style.width = `${width}px`;
    menu.style.top = `${r.bottom + 6}px`;
    menu.style.left = `${Math.max(12, Math.min(r.right - width, window.innerWidth - width - 12))}px`;
    historyBtn.setAttribute("aria-expanded", "true");
    const outside = (e) => { if (!menu.contains(e.target) && !historyBtn.contains(e.target)) closeHistory(); };
    const onKey = (e) => { if (e.key === "Escape") { closeHistory(); historyBtn.focus(); } };
    closeHistory = () => {
      menu.remove(); historyBtn.setAttribute("aria-expanded", "false");
      document.removeEventListener("mousedown", outside, true); document.removeEventListener("keydown", onKey, true);
      closeHistory = null;
    };
    setTimeout(() => document.addEventListener("mousedown", outside, true));
    document.addEventListener("keydown", onKey, true);
    menu.addEventListener("click", async (e) => {
      const open = e.target.closest("[data-open]"), gone = e.target.closest("[data-delete]");
      if (open) { closeHistory(); openChat(open.dataset.open); }
      else if (gone) { closeHistory(); deleteChat(gone.dataset.delete); }
    });
  };
  historyBtn.addEventListener("click", openHistory);
  drawHeader();
  drawAll();
  text.focus();
  return () => { closeMenu?.(); closeHistory?.(); controller?.abort(); };
}

/** The model's readable name ("Claude Sonnet 4.5", "qwen3:8b", "Default model" for an app's own default). */
function modelName(c) {
  if (c.kind === "agent") return c.agent_model ? c.agent_model[0].toUpperCase() + c.agent_model.slice(1) : "Default model";
  return c.model_name || c.model;
}

/** "Claude Sonnet 4.5 · Anthropic (Claude)", "qwen3:8b · Ollama": which AI answered. */
function byline(c) {
  return `${modelName(c)} · ${c.label}`;
}

/** Markdown for an answer that is still being written: a table waits until its header row and divider have arrived
 * (and each row until it's complete), and bold, code and code blocks that are open are closed for now, so the text
 * doesn't flash raw symbols before they're finished. */
function partialMarkdown(text) {
  const lines = text.split("\n");
  const isRow = (l) => /^\s*\|/.test(l);
  let end = lines.length;
  if (isRow(lines[end - 1] ?? "")) {
    let start = end;
    while (start > 0 && isRow(lines[start - 1])) start--;
    const divided = end - start >= 2 && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[start + 1]) && /-/.test(lines[start + 1]);
    end = divided ? end - 1 : start;           // the row being written, or the whole table so far
    if (divided && end - start < 2) end = start;
  }
  let out = lines.slice(0, end).join("\n");
  if ((out.match(/^```/gm) || []).length % 2) return `${out}\n\`\`\``;
  const last = out.slice(out.lastIndexOf("\n") + 1);
  if ((last.match(/\*\*/g) || []).length % 2) out += "**";
  if ((last.replace(/```/g, "").match(/`/g) || []).length % 2) out += "`";
  return out;
}
