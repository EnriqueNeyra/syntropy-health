// Ask's list of conversations: pinned ones, then by when they were last talked in, or what a search found. Saving a
// conversation mustn't disturb it, so rows are kept and only what changed is touched: the list holds its scroll position,
// and rows that move glide to their new place.
import { debounce, esc, html, icon, mount, raw } from "./ui.js";

const REDUCED_MOTION = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const MOVE = { duration: 180, easing: "cubic-bezier(.2, .8, .2, 1)" };

/** Which heading a conversation goes under, by the day it was last talked in here. */
export function groupName(seconds, now = new Date()) {
  const when = new Date(seconds * 1000);
  const midnight = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const days = Math.round((midnight(now) - midnight(when)) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days <= 7) return "Previous 7 days";
  if (days <= 30) return "Previous 30 days";
  return when.toLocaleDateString(undefined, when.getFullYear() === now.getFullYear() ? { month: "long" } : { month: "long", year: "numeric" });
}

/** Replaces `shown` with a text field: Enter or leaving it saves, Escape doesn't. */
export function renameInPlace(shown, value, { save, done }) {
  const field = Object.assign(document.createElement("input"), { className: "input ask-rename", value, maxLength: 120, type: "text" });
  field.setAttribute("aria-label", "Conversation name");
  shown.hidden = true;
  shown.after(field);
  field.focus({ preventScroll: true });
  field.select();
  let finished = false;
  const finish = (keep, refocus) => {
    if (finished) return;
    finished = true;
    const next = field.value.trim();
    field.remove();
    shown.hidden = false;
    done?.();
    if (refocus) shown.focus({ preventScroll: true });
    if (keep && next && next !== value) save(next);
  };
  field.addEventListener("keydown", (e) => {
    if (e.isComposing) return;
    if (e.key === "Enter") { e.preventDefault(); finish(true, true); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false, true); }
  });
  field.addEventListener("blur", () => finish(true, false));
}

/**
 * `list` is the element to draw in, `searchBox` the search field. `chats()` gives the conversations (newest first),
 * `currentId()` the one open, `liveId()` the one being answered. `onOpen(id)`, `onMenu(chat, button, at)` act on a row and
 * `onRename(id, title)` and `find(words)` go to the server. Returns what the page asks of the list.
 */
export function conversationList({ list, searchBox, chats, currentId, liveId, onOpen, onMenu, find, onRename }) {
  const rows = new Map();     // conversation id → its row, kept between draws
  const heads = new Map();    // heading → its element
  let words = "";             // what's being searched for, once the server has answered
  let found = null;
  let asked = 0;
  let renaming = null;
  let empty = null;

  const row = (id) => {
    if (rows.has(id)) return rows.get(id);
    const node = document.createElement("div");
    node.className = "ask-item";
    node.dataset.id = id;
    mount(node, html`<button class="ask-item-open" type="button"><span class="ask-item-title truncate"></span><span class="ask-item-snippet" hidden></span></button>
      <button class="btn btn-ghost btn-icon btn-sm ask-item-more" type="button" aria-haspopup="menu" aria-expanded="false">${icon("more")}</button>`);
    rows.set(id, node);
    return node;
  };

  const fill = (chat, snippet) => {
    const node = row(chat.id);
    const title = node.querySelector(".ask-item-title");
    const open = node.querySelector(".ask-item-open");
    const more = node.querySelector(".ask-item-more");
    if (title.textContent !== chat.title) {
      title.textContent = chat.title;
      open.title = chat.title;
      more.setAttribute("aria-label", `More for “${chat.title}”`);
      more.title = "More";
    }
    const current = chat.id === currentId();
    node.classList.toggle("current", current);
    if (current) open.setAttribute("aria-current", "true"); else open.removeAttribute("aria-current");
    node.classList.toggle("live", chat.id === liveId());
    more.hidden = !!chat.draft;       // not saved yet: there's nothing to rename, pin or delete on the server
    const line = node.querySelector(".ask-item-snippet");
    line.hidden = !snippet;
    if (snippet) mount(line, marked(snippet, words));
    return node;
  };

  const head = (label) => {
    if (!heads.has(label)) heads.set(label, Object.assign(document.createElement("div"), { className: "ask-group", textContent: label }));
    return heads.get(label);
  };

  /** Every element the list should hold, in order. */
  const wanted = () => {
    if (found) {
      if (!found.length) {
        empty ??= Object.assign(document.createElement("div"), { className: "ask-none small muted" });
        empty.textContent = "No conversations match.";
        return [empty];
      }
      return found.map((c) => fill(c, c.snippet));
    }
    const all = chats();
    if (!all.length) {
      empty ??= Object.assign(document.createElement("div"), { className: "ask-none small muted" });
      empty.textContent = "No saved conversations yet. They're kept here after each answer.";
      return [empty];
    }
    const out = [];
    const pinned = all.filter((c) => c.pinned);
    if (pinned.length) out.push(head("Pinned"), ...pinned.map((c) => fill(c)));
    let group = null;
    for (const c of all) {
      if (c.pinned) continue;
      const name = groupName(c.updated_at);
      if (name !== group) out.push(head(name));
      group = name;
      out.push(fill(c));
    }
    return out;
  };

  const draw = ({ glide = true } = {}) => {
    const next = wanted();
    const motion = glide && list.isConnected && !REDUCED_MOTION();
    const box = list.getBoundingClientRect();
    const seen = [...list.children].filter((n) => { const r = n.getBoundingClientRect(); return r.bottom > box.top && r.top < box.bottom; });
    const before = motion ? new Map(seen.map((n) => [n, n.getBoundingClientRect().top])) : null;
    const anchor = list.scrollTop ? seen.find((n) => next.includes(n)) : null;
    const anchorTop = anchor?.getBoundingClientRect().top;
    const focused = list.contains(document.activeElement) ? document.activeElement : null;
    // Only what's out of place moves: a row left alone keeps its focus and hover, and the list its scroll position.
    let at = list.firstChild;
    for (const node of next) {
      if (node === at) at = at.nextSibling;
      else list.insertBefore(node, at);
    }
    while (at) { const gone = at; at = at.nextSibling; gone.remove(); }
    if (anchor) list.scrollTop += anchor.getBoundingClientRect().top - anchorTop;
    if (focused && focused !== document.activeElement && focused.isConnected) focused.focus({ preventScroll: true });
    if (!motion) return;
    const view = list.getBoundingClientRect();
    for (const node of next) {
      const r = node.getBoundingClientRect();
      if (r.bottom < view.top || r.top > view.bottom) continue;
      const was = before.get(node);
      if (was === undefined) node.animate([{ opacity: 0 }, { opacity: 1 }], MOVE);
      else if (Math.abs(was - r.top) > 1) node.animate([{ transform: `translateY(${was - r.top}px)` }, { transform: "none" }], MOVE);
    }
  };

  const search = debounce(async () => {
    const q = searchBox.value.trim();
    const seq = ++asked;
    if (!q) { found = null; words = ""; list.scrollTop = 0; return draw({ glide: false }); }
    let res = null;
    try { res = await find(q); } catch { /* what's shown stays; the next letter asks again */ }
    if (seq !== asked || !res) return;
    words = q;
    found = res;
    list.scrollTop = 0;
    draw({ glide: false });
  }, 160);

  const clearSearch = () => { searchBox.value = ""; ++asked; found = null; words = ""; draw({ glide: false }); };
  const onInput = () => { if (!searchBox.value.trim()) { clearSearch(); } else search(); };
  const rowsOnScreen = () => [...list.querySelectorAll(".ask-item-open")].filter((b) => b.offsetParent);
  const onSearchKey = (e) => {
    if (e.isComposing) return;
    if (e.key === "Escape" && searchBox.value) { e.preventDefault(); e.stopPropagation(); clearSearch(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); rowsOnScreen()[0]?.focus(); }
    else if (e.key === "Enter") { e.preventDefault(); rowsOnScreen()[0]?.click(); }
  };
  const onListKey = (e) => {
    if ((e.key !== "ArrowDown" && e.key !== "ArrowUp") || !e.target.classList.contains("ask-item-open")) return;
    e.preventDefault();
    const all = rowsOnScreen();
    const to = all[all.indexOf(e.target) + (e.key === "ArrowDown" ? 1 : -1)];
    if (to) to.focus();
    else if (e.key === "ArrowUp") searchBox.focus();
  };
  const chatOf = (id) => found?.find((c) => c.id === id) || chats().find((c) => c.id === id);
  const onClick = (e) => {
    const node = e.target.closest(".ask-item");
    if (!node) return;
    if (e.target.closest(".ask-item-more")) onMenu(chatOf(node.dataset.id), node.querySelector(".ask-item-more"));
    else if (e.target.closest(".ask-item-open")) onOpen(node.dataset.id);
  };
  const onContext = (e) => {
    const node = e.target.closest(".ask-item");
    if (!node || node.classList.contains("renaming") || node.querySelector(".ask-item-more").hidden) return;
    e.preventDefault();
    onMenu(chatOf(node.dataset.id), node.querySelector(".ask-item-more"), { x: e.clientX, y: e.clientY });
  };
  searchBox.addEventListener("input", onInput);
  searchBox.addEventListener("keydown", onSearchKey);
  list.addEventListener("keydown", onListKey);
  list.addEventListener("click", onClick);
  list.addEventListener("contextmenu", onContext);

  /** Scrolls the list, and only the list, far enough to show a row. */
  const show = (node) => {
    if (!node?.isConnected) return;
    const r = node.getBoundingClientRect(), box = list.getBoundingClientRect();
    if (r.top < box.top) list.scrollTop -= box.top - r.top + 8;
    else if (r.bottom > box.bottom) list.scrollTop += r.bottom - box.bottom + 8;
  };

  const rename = (id) => {
    const node = rows.get(id);
    const chat = chatOf(id);
    if (!node?.isConnected || !chat || renaming === id) return false;
    renaming = id;
    node.classList.add("renaming");
    show(node);
    renameInPlace(node.querySelector(".ask-item-open"), chat.title, {
      save: (title) => onRename(id, title),
      done: () => { renaming = null; node.classList.remove("renaming"); },
    });
    return true;
  };

  return {
    draw, rename, clearSearch,
    reveal: () => show(rows.get(currentId())),
    focusSearch: () => { searchBox.focus(); searchBox.select(); },
    /** What a search shows follows a rename or a pin straight away too. */
    change(id, fields) { const c = found?.find((x) => x.id === id); if (c) Object.assign(c, fields); },
    forget(id) { if (found) found = found.filter((c) => c.id !== id); rows.delete(id); },
  };
}

/** A snippet with the words searched for marked, everything else escaped. */
function marked(text, words) {
  const pattern = words.split(/\s+/).filter(Boolean).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  if (!pattern) return esc(text);
  return raw(text.split(new RegExp(`(${pattern})`, "gi")).map((part, i) => (i % 2 ? `<mark>${esc(part)}</mark>` : esc(part))).join(""));
}
