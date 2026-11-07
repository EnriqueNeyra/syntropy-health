// Ask: chat about your health data with the AI chosen in Settings — a local model, your own key, or an agent on this
// computer (read-only tools, one person at a time).
import { del, get, patch as change, put, signedOut, unreachableMessage } from "../api.js";
import { appCan, appScroll, followAddress, setComposer, setToolbar, shell } from "../app.js";
import { $, confirmDialog, dropdown, emptyState, fmtAgo, html, icon, loading, markdown, modal, mount, plural, toast } from "../ui.js";
import { confirmAiProvider, confirmAiSummary } from "../consent.js";
import { logo, logoId } from "../ai-logos.js";
import { openModelPicker, sameChoice } from "../model-picker.js";
import { conversationList, renameInPlace } from "../ask-panel.js";

const SUGGESTIONS = [
  "How has my sleep changed over the last month?",
  "Summarize my latest lab results and anything out of range.",
  "How does my training load this week compare with my usual?",
  "What's my resting heart rate and HRV trend lately?",
  "Prepare questions for my next doctor's visit.",
];
const REDUCED_MOTION = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
// Where a keyboard's Command key is the one for shortcuts; elsewhere Control. (Control on a Mac moves the cursor in text.)
const MAC_KEYS = /Mac|iP(hone|ad|od)/.test(navigator.platform);
const keyName = (key, shifted) => (MAC_KEYS ? `⌘${shifted ? "⇧" : ""}${key}` : `Ctrl+${shifted ? "Shift+" : ""}${key}`);
// New chat: the Mac app's File menu has ⌘N and the Windows app leaves Ctrl+N to the page; a browser keeps both for a new
// window, and ⌘⇧O (Open in Browser in the Mac app's menu) is free there.
const NEW_KEY = shell === "mac" ? "⌘N" : shell === "windows" ? "Ctrl+N" : keyName("O", true);
const SIDE_CLOSED_KEY = "syntropy-ask-side-closed";
// Moving what's beside the panel, rather than resizing it, keeps a long conversation from being laid out again every frame.
const SLIDE = { duration: 240, easing: "cubic-bezier(.2, .8, .2, 1)" };

// Conversations are saved on the server (so they follow you to the desktop and iPhone apps), per person and account.
const newId = () => `c_${Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("")}`;
const titleOf = (messages) => {
  const first = messages.find((m) => m.role === "user")?.content || "New chat";
  return first.length > 70 ? `${first.slice(0, 67).trim()}…` : first;
};
// What the server accepts for one saved message, and for a question.
const MAX_SAVED_CHARS = 60000, MAX_SAVED_STEPS = 40, MAX_SAVED_MESSAGES = 200, MAX_QUESTION_CHARS = 40000, MAX_HISTORY = 200;
const stored = (messages) => messages.filter((m) => !m.pending && !m.error && (m.content || m.role === "user")).slice(-MAX_SAVED_MESSAGES)
  .map(({ role, content, steps, note, by, stopped, cut }) => ({ role, content: content.slice(0, MAX_SAVED_CHARS),
    ...(steps?.length ? { steps: steps.slice(-MAX_SAVED_STEPS) } : {}), ...(note ? { note } : {}), ...(by ? { by } : {}),
    ...(stopped ? { stopped } : {}), ...(cut ? { cut } : {}) }));
// The conversation so far, for the model: a stopped answer is said to be one, and a turn too long to send is left out.
const historyFor = (messages, question) => [...messages.filter((m) => !m.error && m.content)
  .map(({ role, content, stopped }) => ({ role, content: stopped ? `${content}\n\n[This answer was stopped before it finished.]` : content }))
  .filter((m) => m.content.length <= MAX_QUESTION_CHARS).slice(-(MAX_HISTORY - 1)), { role: "user", content: question }];
const errorDetail = (body, status) => (Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail) || `Request failed (${status})`;

/** Copies from a click. Without https a browser has no clipboard to ask, and the apps' windows can refuse: the old way still works. */
async function copyText(value) {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    const back = document.activeElement;
    const box = Object.assign(document.createElement("textarea"), { value });
    box.style.cssText = "position:fixed;top:0;left:0;opacity:0";
    document.body.appendChild(box);
    box.select();
    let done = false;
    try { done = document.execCommand("copy"); } catch { /* said below */ }
    box.remove();
    back?.focus?.({ preventScroll: true });
    return done;
  }
}

/** A table as tab-separated text, which pastes into a spreadsheet as a table and reads fine anywhere else. */
const tableText = (table) => [...table.rows].map((r) => [...r.cells].map((c) => c.textContent.trim()).join("\t")).join("\n");

export async function render({ el, state, params, navigate }) {
  const profile = state.profile;
  mount(el, loading(2));
  const cfg = await get("/api/ai/config", { chat: true }, { fresh: true });
  if (!cfg.configured) {
    mount(el, html`
      <div class="page-head"><div><h1>Ask</h1><p>Questions and trends from your own data, with the AI you choose.</p></div></div>
      <div class="card">${emptyState("Connect an AI to get started",
        "There are three ways. The assistant can only read your data.",
        html`<ul class="ai-routes">
            <li><b>A model on your computer</b><span>Nothing leaves your network. Ollama or LM Studio, for example.</span></li>
            <li><b>Your own API key</b><span>From Anthropic, OpenAI, Google and others.</span></li>
            <li><b>An app you're signed in to</b><span>Claude Code or Codex, already on this computer.</span></li>
          </ul>
          <a class="btn btn-primary" href="#/settings/ai">${icon("sparkles")} Connect an AI</a>`)}</div>`);
    return;
  }

  // In the iPhone app the conversation is the screen itself, like Messages: it scrolls with the screen, the message
  // field is the app's own (above the keyboard), and the model, past conversations and New chat are in its title bar.
  const native = appCan("composer") && appCan("toolbar");
  const q = { profile: profile.id };
  const chatPath = (id) => `/api/ai/chats/${encodeURIComponent(id)}?profile=${encodeURIComponent(profile.id)}`;
  let chats = (await get("/api/ai/chats", q, { fresh: true }).catch(() => ({ chats: [] }))).chats;
  let chatId = newId();
  let messages = [];
  // Pick up the conversation in the address or the latest one, unless a new one was asked for (File → New Chat in the
  // desktop app). An address from before switching person names a conversation that isn't theirs.
  if (params?.get("new")) history.replaceState(history.state, "", "#/ask");
  else {
    const load = (id) => get(`/api/ai/chats/${encodeURIComponent(id)}`, q, { fresh: true }).catch(() => null);
    const open = (params?.get("chat") && await load(params.get("chat"))) || (chats[0] && await load(chats[0].id));
    if (open) { chatId = open.id; messages = open.messages; }
  }
  let busy = false;
  let live = null;                  // the conversation being answered, which carries on if another is opened meanwhile
  let current = cfg;
  const unsaved = new Map();        // asked, but with no answer saved yet: listed so it doesn't appear from nowhere afterwards
  const chosenTitles = new Map();   // named before there was anything saved to name
  const drafts = new Map();         // what was being typed in each conversation
  let panel = null;
  const entryOf = (id) => chats.find((c) => c.id === id) || unsaved.get(id);

  const saveChat = async (id = chatId, thread = messages) => {
    const keep = stored(thread);
    if (!keep.some((m) => m.role === "assistant")) return;
    const chosen = chosenTitles.get(id);
    try {
      const saved = await put(chatPath(id), { title: chosen || titleOf(keep), messages: keep, ...(chosen ? { title_set: true } : {}) });
      chosenTitles.delete(id);
      if (native) return reloadChats();       // the app's Conversations menu lists it now
      const had = chats.find((c) => c.id === id);
      unsaved.delete(id);
      chats = [{ created_at: saved.updated_at, ...had, ...saved, messages: keep.length }, ...chats.filter((c) => c.id !== id)];
      panel?.draw();
      drawBar();
      showAddress();
    } catch (err) { toast(`Couldn't save this conversation: ${err.message}`, "bad"); }
  };

  // Set once the person has gone to another page: an answer finishing, or the model list arriving, after that must not
  // put Ask's message field and title bar back on the page they went to.
  let left = false;
  // Elsewhere the conversation fills the window and scrolls by itself: one scrollbar.
  el.classList.add(native ? "ask-native" : "page-fill");
  // The model menu's contents, fetched now so it opens at once (finding local models and AI apps takes a moment).
  let choices = null;
  const loadChoices = () => get("/api/ai/choices", null, { fresh: true }).then((res) => { choices = res; return res; });
  let choicesReady = loadChoices().catch(() => null);

  if (native) mount(el, html`
    <p class="small ask-privacy" id="ask-privacy"></p>
    <div class="chat-log" id="chat-log"></div>
    <div class="sr-only" id="ask-said" role="status"></div>
    <p class="tiny faint chat-foot">Read-only access to ${profile.name}'s records. Answers come from an AI and can be wrong. Syntropy Health organizes records and doesn't give medical advice.</p>`);
  else mount(el, html`
    <div class="page-head ask-head">
      <div><h1>Ask</h1><p class="small ask-privacy" id="ask-privacy"></p></div>
      <div class="row"><button class="model-btn" id="model-btn" aria-haspopup="true" aria-expanded="false"></button></div>
    </div>
    <div class="ask-split card" id="ask-split">
      <button class="btn btn-ghost btn-icon ask-toggle" id="ask-toggle" type="button" aria-controls="ask-side">${icon("sidebar")}</button>
      <div class="ask-scrim" id="ask-scrim"></div>
      <aside class="ask-side" id="ask-side" aria-label="Conversations">
        <div class="ask-side-head">
          <button class="btn ask-side-new" id="ask-new" type="button" title="New chat (${NEW_KEY})">${icon("plus")}<span>New chat</span></button>
          <button class="btn btn-ghost btn-icon ask-side-close" id="ask-side-close" type="button" aria-label="Close conversations" title="Close">${icon("x")}</button>
        </div>
        <div class="search ask-search">${icon("search")}<input class="input" id="ask-search" type="search" placeholder="Search conversations" aria-label="Search conversations" aria-keyshortcuts="${MAC_KEYS ? "Meta+K" : "Control+K"}" autocomplete="off"><kbd class="ask-kbd" aria-hidden="true">${keyName("K")}</kbd></div>
        <div class="ask-list" id="ask-list"></div>
      </aside>
      <section class="chat" id="ask-chat" aria-label="Conversation">
        <div class="ask-bar">
          <h2 class="ask-title" id="ask-title"><button class="ask-title-btn truncate" id="ask-title-btn" type="button"></button></h2>
          <button class="btn btn-ghost btn-icon ask-bar-new" id="ask-bar-new" type="button" aria-label="New chat" title="New chat (${NEW_KEY})">${icon("plus")}</button>
          <button class="btn btn-ghost btn-icon ask-bar-more" id="ask-bar-more" type="button" aria-label="More for this conversation" title="More" aria-haspopup="menu" aria-expanded="false">${icon("more")}</button>
        </div>
        <div class="chat-log" id="chat-log"><div class="chat-thread" id="chat-thread"></div></div>
        <div class="sr-only" id="ask-said" role="status"></div>
        <div class="chat-latest-wrap" id="chat-latest-wrap"><button class="btn btn-icon chat-latest" id="chat-latest" type="button" aria-label="Scroll to the latest message" title="Scroll to the latest message">${icon("arrowDown")}</button></div>
        <form class="chat-input" id="chat-form">
          <span class="chat-input-bg" id="chat-input-bg"></span>
          <textarea class="input" id="chat-text" rows="1" placeholder="Ask about your health data…" aria-label="Your question"></textarea>
          <button class="btn btn-primary btn-icon" type="submit" id="chat-send" aria-label="Send">${icon("send")}</button>
        </form>
      </section>
    </div>
    <p class="tiny faint chat-foot">Read-only access to ${profile.name}'s records. Answers come from an AI and can be wrong. Syntropy Health organizes records and doesn't give medical advice.</p>`);

  const modelBtn = $("#model-btn", el);
  const drawHeader = () => {
    const c = current;
    mount($("#ask-privacy", el), c.kind === "agent"
      ? html`${icon("globe")}<span>Runs through ${c.label} on your ${c.plan} plan. Your questions and what it looks up go to ${c.vendor}.</span>`
      : c.kind === "local"
        ? html`${icon(c.address?.private ? "shieldCheck" : "globe")}<span>${c.address?.private ? "Runs on your own hardware. Nothing leaves your network." : `Runs on ${c.label}, a server you chose.`}${c.tools === false ? " It answers from a summary of your data." : ""}</span>`
        : c.tools === false
          ? html`${icon("globe")}<span>This model can't look things up, so your questions and a summary of your data go to ${c.label}.</span>`
          : html`${icon("globe")}<span>Your questions and only the data it looks up go to ${c.label}.</span>`);
    $("#ask-privacy", el).classList.toggle("private", c.kind === "local" && !!c.address?.private);
    if (!modelBtn) return drawToolbar();
    mount(modelBtn, html`<span class="model-btn-kind">${logo(logoId({ provider: c.provider, agent: c.agent_id, label: c.label }), c.label, "", c.kind === "local" ? "monitor" : "globe")}</span>
      <span class="truncate">${modelName(c)} <span class="muted">${c.label}</span></span>${icon("chevronDown")}`);
    modelBtn.setAttribute("aria-label", `AI: ${byline(c)}. Choose another`);
  };

  // The model menu: every AI that's connected. Choosing one switches Ask to it. For the owner that's the default, as in
  // Settings; for anyone else it's only for their own questions.
  const pickModel = async (o, g) => {
    if (busy) return toast("Wait for the current answer first.");
    if ((g.kind !== "local" || !g.private) && !g.cloud_ack) {
      if (!(await confirmAiProvider(g.vendor_name, g.vendor_key))) return;
      g.cloud_ack = true;
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
  const thread = $("#chat-thread", el) || log;
  const text = $("#chat-text", el);
  const send = $("#chat-send", el);
  const split = $("#ask-split", el);
  const side = $("#ask-side", el);
  const pane = $("#ask-chat", el);
  const toggle = $("#ask-toggle", el);
  const titleBtn = $("#ask-title-btn", el);
  const barMore = $("#ask-bar-more", el);
  const latest = $("#chat-latest", el);
  // The conversation isn't a live region: an answer is drawn afresh every frame as it's written, and a whole thread at
  // once when one is opened, and a screen reader would queue each. A finished answer is read out here, once.
  const said = $("#ask-said", el);
  const say = (words) => {
    said.textContent = "";
    // Emptied first, so the same words twice are still read twice.
    setTimeout(() => { if (!left) said.textContent = words; }, 100);
  };
  const nodes = new Map();          // message → its element, so a streaming answer updates in place
  let controller = null;            // aborts the answer being written (the Stop button)
  const hasHover = () => window.matchMedia("(hover: hover)").matches;
  // A phone's keyboard shouldn't come up over the conversation just because one was opened.
  const focusText = () => { if (text && hasHover() && !left) text.focus({ preventScroll: true }); };

  // Follow the answer as it grows, unless the person has scrolled up to read something. In the app the screen itself
  // scrolls, and the app knows whether it's near the bottom.
  const nearBottom = () => native || log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  const toBottom = (smooth, follow = false) => (native ? appScroll("bottom", { animated: smooth && !REDUCED_MOTION(), follow })
    : log.scrollTo({ top: log.scrollHeight, behavior: smooth && !REDUCED_MOTION() ? "smooth" : "auto" }));
  const drawLatest = () => { latest?.classList.toggle("on", messages.length > 0 && log.scrollHeight - log.scrollTop - log.clientHeight > 120); };

  const toolButton = (tool, label, name) => html`<button class="btn btn-ghost btn-icon btn-sm" type="button" data-tool="${tool}" aria-label="${label}" title="${label}">${icon(name)}</button>`;

  const bubble = (m, enter) => {
    const node = document.createElement("div");
    if (m.role === "user" && native) {
      node.className = `msg msg-user${enter ? " enter" : ""}`;
      node.textContent = m.content;
    } else if (m.role === "user") {
      node.className = `msg-q${enter ? " enter" : ""}`;
      mount(node, html`<div class="msg msg-user"></div><div class="msg-tools">${toolButton("copy", "Copy question", "copy")}${toolButton("edit", "Edit and resend", "pencil")}</div>`);
      $(".msg-user", node).textContent = m.content;
    } else {
      node.className = `msg msg-ai${enter ? " enter" : ""}`;
      // The note comes under the answer: it can arrive as the answer ends, and above it would push all the text down.
      mount(node, html`<div class="msg-steps"></div><div class="msg-body"></div>
        <div class="typing" aria-hidden="true"><span></span><span></span><span></span></div>
        <div class="msg-note small muted"></div>
        <div class="msg-foot"><div class="msg-by"></div><div class="msg-tools">${toolButton("copy", "Copy answer", "copy")}${toolButton("regenerate", "Regenerate answer", "sync")}</div></div>`);
      patch(m, node, false);
    }
    nodes.set(m, node);
    return node;
  };

  /** A finished answer's code blocks and tables get a Copy button. Not while it's being written: they're redrawn every frame then. */
  const addCopyButtons = (body) => {
    for (const block of body.querySelectorAll(":scope > pre.md-code, :scope > .table-wrap")) {
      const box = document.createElement("div");
      box.className = "md-block";
      block.replaceWith(box);
      box.appendChild(block);
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "btn btn-icon btn-sm md-copy";
      copy.dataset.copyBlock = "";
      copy.title = block.matches("pre") ? "Copy code" : "Copy table";
      copy.setAttribute("aria-label", copy.title);
      mount(copy, icon("copy"));
      box.appendChild(copy);
    }
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
    if (m.error) {
      mount(body, html`<div class="banner bad small"><div class="grow">${m.error}</div>${m === messages.at(-1) && !m.stopped ? html`<button class="btn btn-sm" type="button" data-retry>Try again</button>` : ""}</div>`);
      delete body.dataset.shown;
    } else {
      // What's rendered, not the raw text: a table's last row is held back while it's being written, and must appear once it's finished.
      const source = m.pending ? partialMarkdown(shown) : shown;
      if (body.dataset.shown !== source) { mount(body, markdown(source)); body.dataset.shown = source; }
      if (!m.pending) addCopyButtons(body);
    }
    if (m.cut && !m.pending && !m.error && m === messages.at(-1) && !busy && !$("[data-continue]", node)) {
      const more = document.createElement("button");
      more.className = "btn btn-sm";
      more.type = "button";
      more.dataset.continue = "";
      more.textContent = "Continue";
      note.after(more);
    }
    node.classList.toggle("streaming", !!(m.pending && shown));
    // Still being written: a screen reader that reaches it waits until it's done.
    if (m.pending) node.setAttribute("aria-busy", "true"); else node.removeAttribute("aria-busy");
    $(".typing", node).hidden = !(m.pending && !shown);
    const by = $(".msg-by", node);
    by.textContent = m.pending ? "" : [m.stopped ? "Stopped" : "", m.by || ""].filter(Boolean).join(" · ");
    by.hidden = !by.textContent;
    // The row under an answer holds its place while it's written, so it doesn't push the page when it finishes.
    const row = $(".msg-foot", node);
    row.hidden = !!m.error;
    row.classList.toggle("ready", !m.pending && !!m.content);
  };

  /** Regenerate belongs to the last answer and Edit to the last question; neither while an answer is being written. */
  const drawTools = () => {
    const lastAsked = messages.findLast((m) => m.role === "user");
    for (const [m, node] of nodes) {
      node.classList.toggle("last", m === messages.at(-1) || m === lastAsked);
      const again = $('[data-tool="regenerate"]', node);
      if (again) again.hidden = busy || m !== messages.at(-1);
      const edit = $('[data-tool="edit"]', node);
      if (edit) edit.hidden = busy || m !== lastAsked;
    }
  };

  const drawAll = () => {
    nodes.clear();
    if (!messages.length) {
      mount(thread, html`<div class="chat-empty enter">
        <span class="chat-empty-icon">${icon("sparkles")}</span>
        <h3>What would you like to know?</h3>
        <div class="chips">${SUGGESTIONS.map((s) => html`<button class="chip" type="button" data-suggest="${s}">${s}</button>`)}</div></div>`);
      if (native) appScroll("top", { animated: false });
      drawLatest();
      return;
    }
    thread.replaceChildren(...messages.map((m) => bubble(m, false)));
    drawTools();
    toBottom(false);
    // The page is built before it's on screen: open at the latest message once it has a size.
    if (native) requestAnimationFrame(() => requestAnimationFrame(() => toBottom(false)));
    else if (!log.clientHeight) {
      const ro = new ResizeObserver(() => { if (log.clientHeight) { ro.disconnect(); toBottom(false); } });
      ro.observe(log);
    }
    drawLatest();
  };

  const append = (m) => {
    thread.querySelector(".chat-empty")?.remove();
    thread.appendChild(bubble(m, true));
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
      // Written out of sight when another conversation is open: it's all there on coming back.
      const follow = nodes.has(m) && nearBottom();
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

  const autosize = () => {
    if (!text) return;
    // The field growing takes from the conversation: what was at the bottom stays there.
    const stay = nearBottom();
    text.style.height = "auto";
    text.style.height = `${Math.min(text.scrollHeight, 200)}px`;
    if (stay) toBottom(false);
  };
  /** The button in the message field stops the answer on screen; another conversation's answer can't be stopped from here. */
  const drawSend = () => {
    if (native) return;
    const stopping = busy && live?.thread === messages;
    if (send.classList.contains("stop") === stopping && send.firstElementChild) return;
    send.classList.toggle("stop", stopping);
    mount(send, icon(stopping ? "stop" : "send"));
    send.setAttribute("aria-label", stopping ? "Stop" : "Send");
    send.title = stopping ? "Stop" : "";
    send.type = stopping ? "button" : "submit";
  };
  const setBusy = (on) => {
    busy = on;
    if (native) showComposer();
    else drawSend();
    drawTools();
  };

  // An AI outside this network: confirmed once for each (Settings asks too, when it's chosen there).
  const consented = async () => {
    if (!current.sends_elsewhere) return true;
    if (!current.cloud_ack) {
      if (!(await confirmAiProvider(current.vendor_name, current.vendor_key))) return false;
      current = { ...current, cloud_ack: true };
    }
    if (current.tools === false && !current.summary_ack) {
      if (!(await confirmAiSummary(current.vendor_name, current.vendor_key))) return false;
      current = { ...current, summary_ack: true };
    }
    return true;
  };
  // The AI this page says it's sending to: the server sends nothing if that's no longer the one in use.
  const expected = () => (current.kind === "agent"
    ? { provider: "agent", agent: current.agent_id, model: current.agent_model || "", vendor: current.vendor_key }
    : { provider: current.provider, model: current.model, vendor: current.vendor_key });

  /** Asks a question. With `again` the last question is asked afresh (as `question`, if edited) and its answer is replaced. */
  async function ask(question, { again = false } = {}) {
    if (!question.trim()) return;
    if (busy) {
      if (live?.thread !== messages) toast("Wait for the answer in your other conversation first.");
      return;
    }
    if (question.trim().length > MAX_QUESTION_CHARS) return toast(`That's too long to send: ${question.trim().length.toLocaleString()} characters, and the limit is ${MAX_QUESTION_CHARS.toLocaleString()}. Shorten it and send again.`, "bad");
    if (!(await consented())) return;
    const asked = question.trim(), held = messages, heldId = chatId;
    live = { id: heldId, thread: held };
    setBusy(true);
    log.querySelectorAll("[data-retry], [data-continue]").forEach((b) => b.remove());
    if (again && held.at(-1)?.role === "assistant") {
      const old = held.pop();
      nodes.get(old)?.remove();
      nodes.delete(old);
    }
    const mine = again ? held.at(-1) : { role: "user", content: asked };
    if (again && mine.content !== asked) {
      mine.content = asked;
      const shownQuestion = nodes.get(mine);
      if (shownQuestion) (shownQuestion.querySelector(".msg-user") || shownQuestion).textContent = asked;
    }
    const history = historyFor(again ? held.slice(0, -1) : held, asked);
    const reply = { role: "assistant", content: "", steps: [], pending: true };
    if (again) held.push(reply);
    else { held.push(mine, reply); append(mine); }
    startReveal(reply);
    append(reply);
    drawTools();
    if (!entryOf(heldId)) unsaved.set(heldId, { id: heldId, title: chosenTitles.get(heldId) || titleOf(held), updated_at: Date.now() / 1000, pinned: false, draft: true });
    panel?.draw();
    drawBar();
    if (text && (!again || text.value.trim() === asked)) { text.value = ""; autosize(); }
    toBottom(true);
    controller = new AbortController();
    let answer = null;
    try {
      const res = await fetch("/api/ai/chat", {
        method: "POST", credentials: "same-origin", signal: controller.signal,
        headers: { "Content-Type": "application/json", "X-Requested-With": "syntropy" },
        body: JSON.stringify({ profile_id: profile.id, messages: history, expect: expected() }),
      });
      if (res.status === 401) signedOut();
      if (!res.ok) throw new Error(errorDetail(await res.json().catch(() => ({})), res.status));
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
          if (reply.waiting && ev.type !== "retry") { reply.waiting = false; reply.note = ""; patch(reply); }
          if (ev.type === "delta") { draft += ev.text; setTarget(reply, draft.trimStart()); }
          else if (ev.type === "reset") { draft = ""; setTarget(reply, ""); }
          else if (ev.type === "tool") { reply.steps.push(ev.label); patch(reply); }
          else if (ev.type === "note") { reply.note = reply.note ? `${reply.note} ${ev.text}` : ev.text; patch(reply); }
          else if (ev.type === "retry") { reply.note = ev.text; reply.waiting = true; patch(reply); }
          else if (ev.type === "cut_short") { reply.note = ev.text; reply.cut = true; patch(reply); }
          else if (ev.type === "answer") { answer = ev.text; setTarget(reply, answer); }
          else if (ev.type === "error") {
            reply.error = ev.message;
            // Asked again when the person retries: the AI in use has changed, or they haven't agreed to it yet.
            if (ev.config) { current = ev.config; drawHeader(); choicesReady = loadChoices().then(() => drawToolbar(), () => null); }
            if (ev.code === "cloud_ack") current = { ...current, cloud_ack: false };
            if (ev.code === "summary_ack") current = { ...current, tools: false, summary_ack: false };
          } else if (ev.type === "done") reply.by = ev.by || byline(current);
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
      const gone = live?.gone;
      live = null;
      setBusy(false);
      const onScreen = nodes.has(reply);
      const follow = onScreen && nearBottom();
      patch(reply);
      if (follow) toBottom(true, true);
      if (onScreen && text && reply.error && !reply.stopped && !text.value.trim()) { text.value = asked; autosize(); }
      if (onScreen) {
        focusText();
        const body = $(".msg-body", nodes.get(reply));
        say(reply.error || `${reply.stopped ? "Stopped. " : ""}${body?.innerText.trim() || ""}`);
      }
      // Nobody's looking at this one: say so, and don't list a conversation there's nothing saved of.
      if (!onScreen && reply.error && !left && !gone) {
        toast(`The answer in your other conversation didn't finish: ${reply.error}`, "bad");
        unsaved.delete(heldId);
      }
      panel?.draw();
      // A failed second try leaves the answer already saved in place, for the next time this conversation is opened.
      if (!gone && !(again && reply.error)) saveChat(heldId, held);
    }
  }

  /** The Copy button says it worked, in place. */
  const copied = async (button, value) => {
    if (!(await copyText(value))) return toast("Couldn't copy that. Select the text and copy it instead.", "bad");
    const label = button.getAttribute("aria-label");
    mount(button, icon("check"));
    button.setAttribute("aria-label", "Copied");
    button.classList.add("did");
    setTimeout(() => { if (!button.isConnected) return; mount(button, icon("copy")); button.setAttribute("aria-label", label); button.classList.remove("did"); }, 1400);
  };

  /** Edit and resend: the question becomes a field where it is. Enter sends, Escape leaves it as it was. */
  const editQuestion = (m) => {
    const node = nodes.get(m);
    if (!node || busy || $(".msg-edit", node)) return;
    const form = document.createElement("form");
    form.className = "msg-edit";
    mount(form, html`<textarea class="input" rows="1" aria-label="Edit your question"></textarea>
      <div class="msg-edit-actions"><button class="btn btn-sm" type="button" data-cancel>Cancel</button><button class="btn btn-sm btn-primary" type="submit">Send</button></div>`);
    const field = $("textarea", form);
    field.value = m.content;
    node.classList.add("editing");
    node.appendChild(form);
    const fit = () => { field.style.height = "auto"; field.style.height = `${Math.min(field.scrollHeight, 240)}px`; };
    fit();
    field.focus({ preventScroll: true });
    field.setSelectionRange(field.value.length, field.value.length);
    form.scrollIntoView({ block: "nearest" });
    const close =() => { form.remove(); node.classList.remove("editing"); $('[data-tool="edit"]', node)?.focus({ preventScroll: true }); };
    field.addEventListener("input", fit);
    field.addEventListener("keydown", (e) => {
      if (e.isComposing) return;
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
    });
    $("[data-cancel]", form).addEventListener("click", close);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const value = field.value.trim();
      if (!value) return;
      form.remove();
      node.classList.remove("editing");
      ask(value, { again: true });
    });
  };

  log.addEventListener("click", (e) => {
    const chip = e.target.closest("[data-suggest]");
    if (chip) ask(chip.dataset.suggest);
    if (e.target.closest("[data-continue]")) ask("Please continue from where you stopped.");
    if (e.target.closest("[data-retry]")) {
      const asked = messages.findLast((m) => m.role === "user");
      if (asked && !busy) ask(asked.content, { again: true });
    }
    const block = e.target.closest("[data-copy-block]");
    if (block) {
      const pre = $("pre", block.parentElement);
      copied(block, pre ? pre.textContent : tableText($("table", block.parentElement)));
    }
    const tool = e.target.closest("[data-tool]");
    if (!tool) return;
    const m = [...nodes].find(([, node]) => node.contains(tool))?.[0];
    if (!m) return;
    if (tool.dataset.tool === "copy") copied(tool, m.content);
    else if (tool.dataset.tool === "edit") editQuestion(m);
    else if (tool.dataset.tool === "regenerate") {
      const asked = messages.findLast((x) => x.role === "user");
      if (asked) ask(asked.content, { again: true });
    }
  });

  // The AI can change in another window or on the phone: show the current one on coming back here.
  const refreshAi = async () => {
    if (left || document.hidden) return;
    if (!native) reloadChats();      // and any conversation had there
    if (busy) return;
    try { current = await get("/api/ai/config", { chat: true }, { fresh: true }); } catch { return; }
    if (!left && current.configured) drawHeader();
  };
  window.addEventListener("focus", refreshAi);
  document.addEventListener("visibilitychange", refreshAi);
  const stopRefreshing = () => { window.removeEventListener("focus", refreshAi); document.removeEventListener("visibilitychange", refreshAi); };

  // The address says which conversation is open, so a reload or Back returns to it. Replaced, not added to: Back leaves
  // Ask rather than going through every conversation looked at.
  // Closing the panel on a phone takes back the entry it added, which isn't finished until popstate: the address waits for that.
  let leavingPanelEntry = false, addressWaiting = false;
  const showAddress = () => {
    if (native || left) return;
    if (leavingPanelEntry) { addressWaiting = true; return; }
    history.replaceState(history.state, "", chats.some((c) => c.id === chatId) ? `#/ask?chat=${chatId}` : "#/ask");
  };

  /** Puts a conversation on screen: instantly, with what's there going straight to what's next. */
  let opening = 0, openingId = null;
  const show = (id, held) => {
    if (text) drafts.set(chatId, text.value);
    chatId = id;
    messages = held;
    openingId = null;
    // One that never got an answer goes, unless it's the one still being answered.
    for (const key of unsaved.keys()) if (key !== id && key !== live?.id) unsaved.delete(key);
    drawAll();
    if (!native && !REDUCED_MOTION()) thread.animate([{ opacity: .55 }, { opacity: 1 }], { duration: 140, easing: "ease-out" });
    panel?.draw({ glide: false });
    panel?.reveal();
    drawBar();
    showAddress();
    if (text) { text.value = drafts.get(id) || ""; autosize(); }
    drawSend();
    if (!native && narrow.matches) setSide(false);
    focusText();
    drawToolbar();
  };
  const newChat = () => {
    if (native && busy) return toast("Wait for the current answer first.");
    ++opening;
    openingId = null;
    if (!native && !messages.length) {
      if (narrow.matches) setSide(false);
      panel?.draw({ glide: false });
      showAddress();
      return focusText();
    }
    show(newId(), []);
  };
  const openChat = async (id) => {
    if (native && busy) return toast("Wait for the current answer first.");
    const seq = ++opening;
    if (id === chatId && !native) {
      openingId = null;
      panel?.draw({ glide: false });
      if (!native && narrow.matches) setSide(false);
      return focusText();
    }
    if (live?.id === id) return show(id, live.thread);
    // The list moves on the click; what's on screen stays until the conversation arrives (usually at once).
    openingId = id;
    panel?.draw({ glide: false });
    const chat = await get(`/api/ai/chats/${encodeURIComponent(id)}`, q, { fresh: true }).catch((err) => { toast(err.message, "bad"); return null; });
    if (seq !== opening || left) return;
    openingId = null;
    if (chat) show(chat.id, chat.messages);
    else { panel?.draw({ glide: false }); reloadChats(); }
  };
  const deleteChat = async (id) => {
    if (!(await confirmDialog("Delete this conversation?", "It's removed from this server for good.", { confirmLabel: "Delete", danger: true }))) return;
    try { await del(`/api/ai/chats/${encodeURIComponent(id)}`, q); } catch (err) { return toast(err.message, "bad"); }
    if (live?.id === id) { live.gone = true; controller?.abort(); }
    chats = chats.filter((c) => c.id !== id);
    panel?.forget(id);
    if (id === chatId) show(newId(), []);
    else panel?.draw();
    drawToolbar();
    toast("Conversation deleted");
  };
  const reloadChats = async () => {
    chats = (await get("/api/ai/chats", q, { fresh: true }).catch(() => ({ chats }))).chats;
    if (left) return;
    panel?.draw();
    drawBar();
    drawToolbar();
  };

  /** Puts a change on screen at once, and back if the server says no. */
  const changeChat = async (id, fields) => {
    const c = chats.find((x) => x.id === id);
    if (!c) return;
    const was = Object.fromEntries(Object.keys(fields).map((k) => [k, c[k]]));
    const apply = (values) => { Object.assign(c, values); panel?.change(id, values); panel?.draw(); drawBar(); drawToolbar(); };
    apply(fields);
    try { await change(chatPath(id), fields); } catch (err) { apply(was); toast(err.message, "bad"); }
  };
  const nameChat = (id, title) => {
    if (chats.some((c) => c.id === id)) return changeChat(id, { title });
    // Asked but not saved yet: it's named when it is.
    chosenTitles.set(id, title);
    const entry = unsaved.get(id);
    if (entry) entry.title = title;
    panel?.draw();
    drawBar();
  };
  const renameInBar = () => {
    const c = entryOf(chatId);
    if (!c || !titleBtn) return;
    const id = chatId;
    renameInPlace(titleBtn, c.title, { save: (title) => nameChat(id, title) });
  };
  const exportChat = (id) => {
    const link = Object.assign(document.createElement("a"), { href: `/api/ai/chats/${encodeURIComponent(id)}/export?profile=${encodeURIComponent(profile.id)}`, download: "" });
    document.body.appendChild(link);
    link.click();
    link.remove();
  };
  const chatMenu = (c, rename) => [
    { label: "Rename", icon: "pencil", run: rename },
    { label: c.pinned ? "Unpin" : "Pin", icon: "pin", run: () => changeChat(c.id, { pinned: !c.pinned }) },
    { label: "Export as Markdown", icon: "download", run: () => exportChat(c.id) },
    "-",
    { label: "Delete", icon: "trash", danger: true, run: () => deleteChat(c.id) },
  ];

  // ---- In the iPhone app: its message field and title bar
  function showComposer() {
    if (left) return;
    setComposer({ placeholder: "Ask about your health data", busy, onSend: (question) => ask(question), onStop: () => controller?.abort() });
  }

  /** The app's menus can't hold a text field, so renaming there is a small dialog. */
  const renameDialog = (id) => {
    const c = chats.find((x) => x.id === id);
    if (!c) return;
    const m = modal({ title: "Rename conversation", body: html`<input class="input" type="text" maxlength="120" aria-label="Conversation name" value="${c.title}">`,
                      footer: html`<button class="btn" type="button" data-no>Cancel</button><button class="btn btn-primary" type="button" data-yes>Save</button>` });
    const field = $("input", m.body);
    const save = () => {
      const title = field.value.trim();
      m.close();
      if (title && title !== c.title) changeChat(id, { title });
    };
    field.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); save(); } });
    $("[data-no]", m.el).addEventListener("click", () => m.close());
    $("[data-yes]", m.el).addEventListener("click", save);
  };

  function drawToolbar() {
    if (!native || left) return;
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
    const saved = chats.find((c) => c.id === chatId);
    const ordered = [...chats.filter((c) => c.pinned), ...chats.filter((c) => !c.pinned)];
    setToolbar([
      { id: "model", title: name.length > 16 ? `${name.slice(0, 15).trim()}…` : name, accessibilityLabel: `AI: ${byline(current)}. Choose another`,
        menu: choices ? [...modelSections, manage] : [{ items: [{ id: "model:loading", title: "Finding your models…", disabled: true }] }, manage] },
      { id: "history", title: "Conversations", symbol: "clock.arrow.circlepath", menu: [
        { title: chats.length ? `Conversations about ${profile.name}` : null, items: chats.length
          ? ordered.slice(0, 30).map((c) => ({ id: `chat:${c.id}`, title: c.title, subtitle: `${fmtAgo(c.updated_at)} · ${plural(c.messages, "message")}`,
                                               ...(c.pinned ? { symbol: "pin.fill" } : {}), checked: c.id === chatId, run: () => openChat(c.id) }))
          : [{ id: "chat:none", title: "No saved conversations yet", disabled: true }] },
        ...(saved ? [{ items: [
          { id: "chat:rename", title: "Rename Conversation", symbol: "pencil", run: () => renameDialog(chatId) },
          { id: "chat:pin", title: saved.pinned ? "Unpin Conversation" : "Pin Conversation", symbol: saved.pinned ? "pin.slash" : "pin", run: () => changeChat(chatId, { pinned: !saved.pinned }) },
          { id: "chat:delete", title: "Delete Conversation", symbol: "trash", destructive: true, run: () => deleteChat(chatId) },
        ] }] : []),
      ] },
      { id: "new", title: "New Chat", symbol: "square.and.pencil", run: newChat },
    ]);
  }

  if (native) {
    drawHeader();
    showComposer();
    drawAll();
    choicesReady.then(() => drawToolbar());
    return () => { left = true; stopRefreshing(); controller?.abort(); };
  }

  // ---- The conversation's title, and the panel of conversations beside it
  function drawBar() {
    if (!titleBtn) return;
    const c = entryOf(chatId);
    titleBtn.textContent = c?.title || "New chat";
    titleBtn.title = c ? "Rename" : "";
    titleBtn.disabled = !c;
    barMore.style.visibility = c && !c.draft ? "" : "hidden";
  }
  titleBtn.addEventListener("click", renameInBar);
  barMore.addEventListener("click", () => { const c = chats.find((x) => x.id === chatId); if (c) dropdown(barMore, chatMenu(c, renameInBar)); });

  panel = conversationList({
    list: $("#ask-list", el),
    searchBox: $("#ask-search", el),
    chats: () => [...unsaved.values(), ...chats],
    currentId: () => openingId ?? chatId,
    liveId: () => live?.id,
    onOpen: openChat,
    onMenu: (c, button, at) => dropdown(button, chatMenu(c, () => panel.rename(c.id)), at),
    onRename: nameChat,
    find: async (words) => (await get("/api/ai/chats", { ...q, q: words }, { fresh: true })).chats,
  });

  // On a wide window the panel sits beside the conversation and stays as it was left; on a narrow one it slides over.
  const narrow = window.matchMedia("(max-width: 1023px)");
  const rememberedClosed = () => { try { return localStorage.getItem(SIDE_CLOSED_KEY) === "1"; } catch { return false; } };
  let sideOpen = !narrow.matches && !rememberedClosed();
  const drawSide = () => {
    split.dataset.side = sideOpen ? "open" : "closed";
    side.inert = !sideOpen;
    pane.inert = sideOpen && narrow.matches;
    const label = sideOpen ? "Hide conversations" : "Show conversations";
    toggle.setAttribute("aria-label", label);
    toggle.title = `${label} (${keyName("B")})`;
    toggle.setAttribute("aria-expanded", String(sideOpen));
  };

  /** The paragraph at the top of the conversation, to stay there when the text takes a new width. */
  const readingPlace = () => {
    if (nearBottom()) return { atEnd: true };
    const top = log.getBoundingClientRect().top;
    const node = [...thread.querySelectorAll(".msg-user, .msg-body > *, .msg-foot")].find((n) => n.getBoundingClientRect().top >= top);
    return node ? { node, y: node.getBoundingClientRect().top } : null;
  };
  const keepPlace = (place) => {
    if (place?.atEnd) log.scrollTop = log.scrollHeight;
    else if (place) log.scrollTop += place.node.getBoundingClientRect().top - place.y;
  };

  /**
   * Opens or closes the panel. Beside the conversation, everything takes its final place at once and then glides there from
   * where it was: the panel slides, and each part of the conversation moves so that its edges carry on from where they were.
   */
  const slideParts = () => [
    [thread, "middle"], [$("#chat-latest-wrap", el), "middle"], [$("#chat-input-bg", el), "stretch"], [text, "start"], [send, "end"],
    [$("#ask-title", el), "start"],
  ];
  const setSide = (open, { remember = true } = {}) => {
    if (sideOpen === open) return;
    sideOpen = open;
    if (narrow.matches) {
      drawSide();
      if (open) { if (hasHover()) panel.focusSearch(); else $("#ask-side-close", el).focus({ preventScroll: true }); }
      else if (side.contains(document.activeElement)) toggle.focus({ preventScroll: true });
      // Over everything else, the panel is a screen of its own to the Back button: Back closes it, and doesn't leave Ask.
      if (open) history.pushState({ ...history.state, askPanel: true }, "");
      else if (history.state?.askPanel) { leavingPanelEntry = true; history.back(); }
      return;
    }
    if (remember) { try { localStorage.setItem(SIDE_CLOSED_KEY, open ? "0" : "1"); } catch { /* it just won't be remembered */ } }
    const glide = !REDUCED_MOTION() && el.isConnected;
    const parts = glide ? slideParts() : [];
    const before = parts.map(([part]) => part.getBoundingClientRect());
    const place = readingPlace();
    const sideWas = getComputedStyle(side).transform;
    for (const [part] of parts) part.getAnimations().forEach((a) => a.cancel());
    side.getAnimations().forEach((a) => a.cancel());
    split.classList.toggle("side-gliding", glide);
    split.classList.toggle("side-leaving", glide && !open);
    drawSide();
    keepPlace(place);
    if (!open && side.contains(document.activeElement)) toggle.focus({ preventScroll: true });
    if (!glide) { drawLatest(); return; }
    parts.forEach(([part, how], i) => {
      const was = before[i], now = part.getBoundingClientRect();
      if (!now.width) return;
      const shift = how === "middle" ? was.left + was.width / 2 - (now.left + now.width / 2) : how === "end" ? was.right - now.right : was.left - now.left;
      const from = how === "stretch" ? `translateX(${shift}px) scaleX(${was.width / now.width})` : `translateX(${shift}px)`;
      if (Math.abs(shift) < .5 && Math.abs(was.width - now.width) < .5) return;
      // Text that has taken a new width rises into place, rather than being seen to jump to its new lines.
      const rewrapped = part === thread && Math.abs(was.width - now.width) > 1;
      part.animate([{ transform: from, ...(rewrapped ? { opacity: .35 } : {}) }, { transform: "none", ...(rewrapped ? { opacity: 1 } : {}) }], SLIDE);
    });
    if (!open) $("#ask-bar-new", el).animate([{ opacity: 0 }, { opacity: 1 }], SLIDE);
    const hidden = "translateX(-100%)";
    const moving = side.animate([{ transform: sideWas !== "none" ? sideWas : open ? hidden : "none" }, { transform: open ? "none" : hidden }], SLIDE);
    moving.finished.then(() => { split.classList.remove("side-gliding", "side-leaving"); drawLatest(); }, () => {});
  };
  const onNarrowChange = () => { sideOpen = !narrow.matches && !rememberedClosed(); drawSide(); };
  narrow.addEventListener("change", onNarrowChange);
  const onBack = () => {
    if (leavingPanelEntry) {
      leavingPanelEntry = false;
      if (addressWaiting) { addressWaiting = false; showAddress(); }
    } else if (sideOpen && narrow.matches && !history.state?.askPanel) setSide(false);
  };
  window.addEventListener("popstate", onBack);
  if (history.state?.askPanel) history.replaceState({ ...history.state, askPanel: false }, "");    // back here from another page
  drawSide();
  toggle.addEventListener("click", () => setSide(!sideOpen));
  $("#ask-side-close", el).addEventListener("click", () => setSide(false));
  $("#ask-scrim", el).addEventListener("click", () => setSide(false));

  const onKeys = (e) => {
    if (left || document.querySelector(".overlay")) return;
    const key = e.key.toLowerCase();
    const mod = MAC_KEYS ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
    if (mod && !e.altKey && !e.shiftKey && key === "k") {
      e.preventDefault();
      setSide(true);
      panel.focusSearch();
    } else if (mod && !e.altKey && !e.shiftKey && key === "b") {
      e.preventDefault();
      setSide(!sideOpen);
    } else if ((mod && !e.altKey && e.shiftKey && key === "o" && shell !== "mac") || (shell === "windows" && mod && !e.altKey && !e.shiftKey && key === "n")) {
      e.preventDefault();
      newChat();
    } else if (e.key === "Escape" && sideOpen && narrow.matches) {
      setSide(false);
    } else if (e.key === "Tab" && sideOpen && narrow.matches) {
      // The panel is over everything else on a narrow window, so the keyboard stays in it.
      const stops = [...side.querySelectorAll("button, input, a[href]")].filter((n) => n.offsetParent && !n.disabled);
      const first = stops[0], last = stops.at(-1);
      if (!side.contains(document.activeElement)) { e.preventDefault(); first?.focus(); }
      else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
    }
  };
  document.addEventListener("keydown", onKeys);

  let latestQueued = false;
  log.addEventListener("scroll", () => {
    if (latestQueued) return;
    latestQueued = true;
    requestAnimationFrame(() => { latestQueued = false; drawLatest(); });
  }, { passive: true });
  const threadSize = new ResizeObserver(drawLatest);
  threadSize.observe(thread);
  latest.addEventListener("click", () => { toBottom(true); focusText(); });

  $("#chat-form", el).addEventListener("submit", (e) => { e.preventDefault(); ask(text.value); });
  send.addEventListener("click", (e) => { if (send.classList.contains("stop")) { e.preventDefault(); controller?.abort(); } });
  text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(text.value); }
  });
  text.addEventListener("input", autosize);
  $("#ask-new", el).addEventListener("click", newChat);
  $("#ask-bar-new", el).addEventListener("click", newChat);
  followAddress((route, where) => {
    if (route !== "ask") return false;
    if (where.get("chat")) openChat(where.get("chat")).then(showAddress);
    else {
      if (where.get("new")) newChat();
      showAddress();
    }
    return true;
  });

  drawHeader();
  drawAll();
  drawBar();
  panel.draw({ glide: false });
  showAddress();
  // The page isn't on screen until this returns: then the list can bring the open conversation into view.
  requestAnimationFrame(() => { panel.reveal(); if (!document.querySelector(".overlay")) focusText(); });
  return () => {
    left = true; stopRefreshing(); closeMenu?.(); controller?.abort(); threadSize.disconnect();
    document.removeEventListener("keydown", onKeys);
    narrow.removeEventListener("change", onNarrowChange);
    window.removeEventListener("popstate", onBack);
  };
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
