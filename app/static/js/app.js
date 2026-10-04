// Application shell: session bootstrap, hash router, navigation and profile switching.

import { api, clearCache, get, onDataChanged, onUnauthorized, post, put, unreachableMessage } from "./api.js";
import { markAllSeen, onAlertsChange, refreshAlerts, renderPanel, dismiss, unreadCount } from "./alerts.js";
import { $, closeAnimated, closeDialogs, html, icon, initials, mount, onAction, scrollToTop, toast, esc, wordmark } from "./ui.js";
import { renderOnboarding, renderPasswordGate, renderSetup, renderTermsGate } from "./onboarding.js";
import { termsCheckbox } from "./consent.js";
import { setUnitPreference } from "./units.js";

const ROUTES = {
  overview: { label: "Overview", icon: "home", load: () => import("./views/overview.js") },
  ask: { label: "Ask", icon: "sparkles", load: () => import("./views/ask.js") },
  timeline: { label: "Timeline", icon: "timeline", load: () => import("./views/timeline.js") },
  records: { label: "Records", icon: "records", load: () => import("./views/records.js") },
  trends: { label: "Trends", icon: "trends", load: () => import("./views/trends.js") },
  workouts: { label: "Workouts", icon: "activity", load: () => import("./views/workouts.js") },
  journal: { label: "Journal", icon: "book", load: () => import("./views/journal.js") },
  sources: { label: "Sources", icon: "sources", load: () => import("./views/sources.js") },
  report: { label: "Visit summary", icon: "printer", load: () => import("./views/report.js") },
  settings: { label: "Settings", icon: "settings", load: () => import("./views/settings.js") },
  // A single record as a page: the iPhone app's record screen (elsewhere records open in a drawer).
  _record: { label: "Record", icon: "records", load: () => import("./views/record-detail.js") },
};
// Sidebar sections. The visit summary is reached from Records and Settings rather than taking a place of its own
// (on phones and in the iPhone app too).
const NAV = [
  [null, ["overview", "ask"]],
  ["Your health", ["trends", "workouts", "journal", "timeline", "records"]],
  ["Manage", ["sources", "settings"]],
];
const SECTIONS = NAV.flatMap(([, keys]) => keys);
// The phone tab bar: the everyday places (checking in is a daily habit), the rest under More.
const MOBILE = ["overview", "trends", "journal", "ask"];
const MORE = ["records", "workouts", "timeline", "sources", "settings"];
// Old addresses (bookmarks, links from earlier versions) and where they live now.
const MOVED = { activity: "journal" };

// Running inside the iPhone app's Syntropy tab (see theme.js).
export const embedded = document.documentElement.hasAttribute("data-embedded");
// ...in its companion mode, which draws native navigation: a tab bar, title bars and back buttons. The page shows only
// its content, and going to another section opens it as a new screen in the app (see nativeShell below).
export const nativeNav = document.documentElement.hasAttribute("data-native-nav");

// Asks the iPhone app to do something the web view can't (e.g. print). Returns false outside the app.
export function appBridge(message) {
  const handler = window.webkit?.messageHandlers?.syntropy;
  if (!handler) return false;
  handler.postMessage(message);
  return true;
}

// Running in the Mac or Windows app (see theme.js), which draws the window: title bar, menus, appearance.
export const shell = document.documentElement.getAttribute("data-shell");

/** Tells the Mac app something (the theme in use, the page title, a window drag). Returns false elsewhere. */
export function desktopBridge(message) {
  const handler = window.webkit?.messageHandlers?.syntropyDesktop;
  if (!handler) return false;
  try { handler.postMessage(JSON.stringify(message)); } catch { return false; }
  return true;
}

// The desktop apps behave like apps rather than web pages: no browser context menu except where it's useful (text,
// links), and on a Mac the top of the window drags it (the title bar is part of the page there).
if (shell) {
  document.addEventListener("contextmenu", (e) => {
    if (!e.target.closest("input, textarea, [contenteditable], a[href], pre, code") && !String(getSelection())) e.preventDefault();
  });
}
if (shell === "mac") {
  const TITLEBAR = 52;
  const inTitlebar = (e) => e.clientY <= TITLEBAR && e.button === 0
    && !e.target.closest("a, button, input, select, textarea, label, summary, [role=button], [data-action], .popover, .overlay");
  document.addEventListener("mousedown", (e) => { if (inTitlebar(e)) { e.preventDefault(); desktopBridge({ type: "drag" }); } });
  document.addEventListener("dblclick", (e) => { if (inTitlebar(e)) desktopBridge({ type: "titlebar-double-click" }); });
  // The window's sidebar material and title bar follow the page's theme, which may differ from the system's.
  const sendTheme = () => desktopBridge({ type: "theme", theme: document.documentElement.getAttribute("data-theme") });
  new MutationObserver(sendTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  sendTheme();
  // Show the window once the page has drawn (it stays hidden until then, so it never flashes blank).
  requestAnimationFrame(() => requestAnimationFrame(() => desktopBridge({ type: "ready" })));
}

export const state = {
  status: null,
  profiles: [],
  profileId: null,
  get profile() { return this.profiles.find((p) => p.id === this.profileId) || this.profiles[0] || null; },
};

let cleanup = null;
let renderSeq = 0;
let shownRoute = null;       // the section on screen, so switching sections and switching tabs animate differently
let pageTransition = null;

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean);
  return { route: parts[0] || "overview", parts: parts.slice(1), params: new URLSearchParams(query) };
}

export function navigate(hash) {
  // In the iPhone app another section opens as its own screen, with a back button to this one.
  if (nativeNav && routeOf(hash) !== parseHash().route && appBridge({ type: "visit", hash })) return;
  if (location.hash !== hash) location.hash = hash; else route();
}

/** The section a "#/…" address belongs to. */
function routeOf(hash) { return hash.replace(/^#\/?/, "").split(/[/?]/)[0] || "overview"; }

/** Shows another address in place of this one: redirects from old addresses, which must not open a new screen. */
function redirect(hash) { history.replaceState(null, "", hash); route(); }

export function setProfile(id) {
  state.profileId = id;
  try { localStorage.setItem("syntropy-profile", id); } catch {}
  renderShell();
  route();
  refreshAlerts(state.profile, state.status);
}

export async function refreshProfiles() {
  const res = await get("/api/profiles");
  state.profiles = res.profiles;
  if (!state.profiles.some((p) => p.id === state.profileId)) state.profileId = state.profiles[0]?.id || null;
  renderShell();
}

/** "light", "dark" or "system" (follow the device). */
export function themePreference() {
  try { return localStorage.getItem("syntropy-theme") || "system"; } catch { return "system"; }
}

export function setTheme(pref) {
  try { if (pref === "system") localStorage.removeItem("syntropy-theme"); else localStorage.setItem("syntropy-theme", pref); } catch {}
  const dark = pref === "system" ? window.matchMedia("(prefers-color-scheme: dark)").matches : pref === "dark";
  // Charts and everything else take their colors from CSS variables: nothing needs drawing again, so the page stays
  // where it is and the colors cross-fade.
  themeTransition(() => {
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    renderShell();
  });
}

const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Changes colors (theme, accent) with a short cross-fade rather than a snap. */
export function themeTransition(apply) {
  if (reducedMotion()) return apply();
  if (document.startViewTransition) {
    document.documentElement.dataset.vt = "theme";
    const t = document.startViewTransition(apply);
    t.finished.finally(() => { if (document.documentElement.dataset.vt === "theme") delete document.documentElement.dataset.vt; });
    return;
  }
  const root = document.documentElement;
  root.classList.add("theme-anim");
  apply();
  clearTimeout(themeTransition.timer);
  themeTransition.timer = setTimeout(() => root.classList.remove("theme-anim"), 400);
}

// Accent colors: the swatches in Settings → General. The shades themselves are in app.css ([data-accent]); the
// iPhone app has the same list. "heart" is the logo's color and the default.
export const ACCENTS = [
  ["heart", "Heart", "#d6204a", "#ff6b8b"], ["orange", "Orange", "#c2410c", "#fb923c"], ["green", "Green", "#15803d", "#4ade80"],
  ["teal", "Teal", "#0f766e", "#2dd4bf"], ["blue", "Blue", "#2563eb", "#60a5fa"], ["indigo", "Indigo", "#4f46e5", "#818cf8"],
  ["purple", "Purple", "#7e22ce", "#c084fc"], ["graphite", "Graphite", "#4b5563", "#d1d5db"],
];

export function currentAccent() { return document.documentElement.getAttribute("data-accent") || "heart"; }

/** Shows an accent (from the server, or just chosen) and remembers it for the next first paint. */
export function applyAccent(id) {
  if (!ACCENTS.some(([k]) => k === id)) id = "heart";
  if (id === "heart") document.documentElement.removeAttribute("data-accent");
  else document.documentElement.setAttribute("data-accent", id);
  try { localStorage.setItem("syntropy-accent", id); } catch {}
  appBridge({ type: "accent", accent: id });          // the iPhone app tints its own screens to match
  desktopBridge({ type: "accent", accent: id });
}

/** Chooses the accent for every device: the web app, the desktop apps and the iPhone app. */
export async function setAccent(id) {
  themeTransition(() => applyAccent(id));
  await put("/api/preferences", { accent: id });
}

function toggleTheme() {
  setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
}

// ------------------------------------------------------------------ popovers (alerts, profile menu, mobile "More")
let openPopover = null;

function closePopover() {
  if (!openPopover) return;
  closeAnimated(openPopover.el);
  openPopover.button?.setAttribute("aria-expanded", "false");
  openPopover = null;
  document.removeEventListener("mousedown", onOutside, true);
  document.removeEventListener("keydown", onEscape);
}
function onOutside(e) {
  if (openPopover && !openPopover.el.contains(e.target) && !openPopover.button?.contains(e.target)) closePopover();
}
function onEscape(e) { if (e.key === "Escape") { const b = openPopover?.button; closePopover(); b?.focus(); } }

/** Opens a panel below `button` (a bottom sheet on phones). `fill(el)` draws its contents. */
function popover(name, button, fill, { align = "right", sheet = false, above = false } = {}) {
  const same = openPopover?.name === name;
  closePopover();
  if (same) return null;
  const el = document.createElement("div");
  el.className = `popover ${sheet ? "sheet" : ""} align-${align}`;
  el.setAttribute("role", "dialog");
  fill(el);
  document.body.appendChild(el);
  if (!sheet && button && (embedded || window.matchMedia("(min-width: 821px)").matches)) {
    const r = button.getBoundingClientRect();
    if (above) el.style.bottom = `${window.innerHeight - r.top + 8}px`; else el.style.top = `${r.bottom + 8}px`;
    if (align === "right") el.style.right = `${Math.max(12, window.innerWidth - r.right)}px`;
    else el.style.left = `${Math.max(12, r.left)}px`;
  }
  button?.setAttribute("aria-expanded", "true");
  openPopover = { name, el, button };
  el.addEventListener("click", (e) => {
    const dismissBtn = e.target.closest("[data-dismiss]");
    if (dismissBtn) { dismiss(dismissBtn.dataset.dismiss); fill(el); return; }
    if (e.target.closest("a[href], [data-close-popover]")) closePopover();
  });
  setTimeout(() => { document.addEventListener("mousedown", onOutside, true); document.addEventListener("keydown", onEscape); });
  return el;
}

function openAlerts(button) {
  const el = popover("alerts", button, renderPanel, { sheet: !button });
  if (el) markAllSeen();
}

const avatarOf = (p) => html`<span class="avatar" style="--c:${p.color || "var(--accent)"}">${initials(p.name)}</span>`;

function peopleMenu() {
  return html`<div class="popover-label">People</div>
    <div class="menu">${state.profiles.map((p) => html`
      <button class="menu-item ${p.id === state.profileId ? "active" : ""}" data-profile="${p.id}">
        ${avatarOf(p)}<span class="grow truncate">${p.name}</span>${p.access === "view" ? html`<span class="menu-note">View only</span>` : ""}${p.id === state.profileId ? icon("check") : ""}</button>`)}
      <a class="menu-item" href="#/settings/people">${icon("user")}<span>People and sharing</span></a>
    </div>`;
}

/** More than one person signs in here: "Sign out" rather than "Lock", and whose session this is. */
function household() { return (state.status?.account?.members || 0) > 1; }

function onPersonChosen(e) {
  const b = e.target.closest("[data-profile]");
  if (b) { closePopover(); if (b.dataset.profile !== state.profileId) setProfile(b.dataset.profile); }
}

function openProfileMenu(button, placement = {}) {
  popover("profile", button, (el) => mount(el, peopleMenu()), { align: "left", ...placement })?.addEventListener("click", onPersonChosen);
}

function openMore(button) {
  const { route: current } = parseHash();
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  const el = popover("more", button, (el) => mount(el, html`
    ${state.profiles.length ? peopleMenu() : ""}
    <div class="menu ${state.profiles.length ? "menu-sep" : ""}">${MORE.map((key) => html`<a class="menu-item ${key === current ? "active" : ""}" href="#/${key}">${icon(ROUTES[key].icon)}<span>${ROUTES[key].label}</span></a>`)}</div>
    <div class="menu menu-sep">
      <button class="menu-item" data-more="theme">${icon(dark ? "sun" : "moon")}<span>${dark ? "Light" : "Dark"} appearance</span></button>
      ${state.status?.auth_required ? html`<button class="menu-item" data-more="lock">${icon("lock")}<span>${household() ? `Sign out ${state.status.account.name}` : "Lock"}</span></button>` : ""}
    </div>`), { sheet: true });
  el?.addEventListener("click", onPersonChosen);
  el?.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-more]");
    if (!b) return;
    closePopover();
    if (b.dataset.more === "theme") toggleTheme();
    if (b.dataset.more === "lock") { await post("/api/auth/logout"); location.reload(); }
  });
}

function drawAlertBadge(reason) {
  const n = unreadCount();
  for (const b of document.querySelectorAll(".alerts-btn")) {
    const badge = b.querySelector(".count-badge");
    badge.textContent = n > 9 ? "9+" : String(n);
    badge.hidden = !n;
    b.setAttribute("aria-label", n ? `Alerts, ${n} new` : "Alerts");
  }
  if (nativeNav) appBridge({ type: "alerts", unread: n });
  // Opening the panel marks everything seen; keep showing which items were new until it's closed.
  if (openPopover?.name === "alerts" && reason !== "seen") renderPanel(openPopover.el);
  if (nativeNav && reason !== "seen" && parseHash().route === ALERTS) renderAlertsPage();
}
onAlertsChange(drawAlertBadge);

// ------------------------------------------------------------------ shell
function navLink(key, current) {
  const r = ROUTES[key];
  return html`<a href="#/${key}" class="${key === current ? "active" : ""}">${icon(r.icon)}<span>${r.label}</span></a>`;
}

function renderShell() {
  const root = document.getElementById("root");
  const { route: current } = parseHash();
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  const p = state.profile;
  if (!root.querySelector(".app")) {
    mount(root, html`
      <div class="app">
        <aside class="sidebar">
          <a class="brand" href="#/overview">${wordmark()}</a>
          <nav class="nav" id="nav" aria-label="Sections"></nav>
          <div class="sidebar-foot">
            <button class="profile-btn" id="profile-btn" data-action="profile" aria-haspopup="true" aria-expanded="false"></button>
            <div class="sidebar-footer" id="sidebar-footer"></div>
          </div>
        </aside>
        <div class="main">
          <header class="topbar">
            <a class="brand top-brand" href="#/overview">${wordmark()}</a>
            <div class="spacer"></div>
            <form class="search top-search" id="top-search" role="search">${icon("search")}
              <input class="input" name="q" placeholder="Search records" aria-label="Search records"></form>
            <button class="btn btn-ghost btn-icon alerts-btn" data-action="alerts" aria-haspopup="true" aria-expanded="false" aria-label="Alerts">
              ${icon("bell")}<span class="count-badge" hidden></span></button>
            <button class="btn btn-ghost btn-icon hide-mobile" data-action="theme" id="theme-btn" aria-label="Toggle appearance"></button>
            <button class="btn btn-ghost btn-icon hide-mobile" data-action="logout" id="logout-btn" aria-label="Lock" title="Lock">${icon("lock")}</button>
            <nav class="section-bar" id="section-bar" aria-label="Sections"></nav>
          </header>
          <div class="head-tools" id="head-tools"></div>
          <div class="view-only-note" id="view-only" role="note"></div>
          <main class="content" id="view"></main>
        </div>
        <nav class="mobile-nav" id="mobile-nav" aria-label="Sections"></nav>
      </div>`);
    onAction(root.querySelector(".topbar"), {
      theme: toggleTheme,
      logout: async () => { await post("/api/auth/logout"); location.reload(); },
      alerts: (_, btn) => openAlerts(btn),
    });
    onAction(root.querySelector(".sidebar"), { profile: (_, btn) => openProfileMenu(btn, { above: true }) });
    onAction($("#head-tools"), {
      "head-alerts": (_, btn) => openAlerts(btn),
      "head-profile": (_, btn) => openProfileMenu(btn, { align: "right" }),
    });
    onAction(root.querySelector("#mobile-nav"), { more: (_, btn) => openMore(btn) });
    $("#top-search").addEventListener("submit", (e) => {
      e.preventDefault();
      const q = e.target.q.value.trim();
      e.target.q.value = "";
      e.target.q.blur();
      navigate(q ? `#/records?q=${encodeURIComponent(q)}` : "#/records");
    });
  }
  mount($("#nav"), NAV.map(([label, keys]) => html`<div class="nav-group">
    ${label ? html`<div class="nav-label">${label}</div>` : ""}${keys.map((k) => navLink(k, current))}</div>`));
  const moreActive = MORE.includes(current);
  mount($("#mobile-nav"), html`${MOBILE.map((key) =>
    html`<a href="#/${key}" class="${key === current ? "active" : ""}">${icon(ROUTES[key].icon)}<span>${ROUTES[key].short || ROUTES[key].label}</span></a>`)}
    <button class="${moreActive ? "active" : ""}" data-action="more" aria-haspopup="true" aria-expanded="false">${icon("menu")}<span>More</span></button>`);
  if (embedded) {
    mount($("#section-bar"), SECTIONS.map((key) =>
      html`<a href="#/${key}" class="${key === current ? "active" : ""}">${icon(ROUTES[key].icon)}<span>${ROUTES[key].short || ROUTES[key].label}</span></a>`));
    centerSection(false);
    // The app has no room for a top bar of its own, so the bell and the person sit beside each page's title.
    mount($("#head-tools"), html`
      <button class="btn btn-ghost btn-icon alerts-btn" data-action="head-alerts" aria-haspopup="true" aria-expanded="false" aria-label="Alerts">
        ${icon("bell")}<span class="count-badge" hidden></span></button>
      ${p ? html`<button class="head-person" data-action="head-profile" aria-haspopup="true" aria-expanded="false"
        aria-label="Viewing ${p.name}. Switch person" title="${p.name}">${avatarOf(p)}</button>` : ""}`);
  }
  if (nativeNav) {
    appBridge({ type: "profiles", current: state.profileId,
                profiles: state.profiles.map(({ id, name, color }) => ({ id, name, color: color || null })) });
  }
  mount($("#profile-btn"), p ? html`${avatarOf(p)}<span class="grow truncate">${p.name}</span>${icon("chevronDown")}` : "");
  $("#profile-btn").setAttribute("aria-label", `Viewing ${p?.name || ""}. Switch person`);
  mount($("#theme-btn"), icon(dark ? "sun" : "moon"));
  $("#logout-btn").hidden = !state.status?.auth_required;
  const out = household() ? `Sign out ${state.status.account.name}` : "Lock";
  $("#logout-btn").setAttribute("aria-label", out);
  $("#logout-btn").title = out;
  mount($("#sidebar-footer"), household()
    ? html`${icon("user")}<span>Signed in as ${state.status.account.name}<br><span class="faint">v${state.status?.version || ""}</span></span>`
    : html`${icon("shieldCheck")}<span>Stored on this machine<br><span class="faint">v${state.status?.version || ""}</span></span>`);
  // Someone shared with you to view: the page says so, and controls that would change their data are hidden.
  document.documentElement.toggleAttribute("data-view-only", p?.access === "view");
  mount($("#view-only"), p?.access === "view"
    ? html`${icon("eye")}<span>You're viewing ${p.name}'s data. ${p.name} shared it with you to view, not to change.</span>` : "");
  drawAlertBadge();
}

/** Scrolls the embedded section bar (not the page) so the current section sits in the middle. */
function centerSection(smooth) {
  const bar = $("#section-bar");
  const active = bar?.querySelector(".active");
  if (!active) return;
  bar.scrollTo({ left: active.offsetLeft - (bar.clientWidth - active.offsetWidth) / 2, behavior: smooth ? "smooth" : "auto" });
}

/** Moves the highlight to the current section without rebuilding the navigation. */
function markActive(current) {
  for (const a of document.querySelectorAll("#nav a, #mobile-nav a, #section-bar a")) {
    a.classList.toggle("active", a.getAttribute("href") === `#/${current}`);
  }
  $("#mobile-nav [data-action=more]")?.classList.toggle("active", MORE.includes(current));
  if (embedded) centerSection(true);
}

// The next page is built off-screen and swapped in once its content is ready, so switching sections doesn't flash a
// blank page and then a loading placeholder. Meanwhile the current page stays put with a thin progress bar at the top;
// only a page that takes much longer swaps in its placeholder.
const PROGRESS_AFTER_MS = 300;
const PLACEHOLDER_AFTER_MS = 700;

function setLoading(on) {
  let bar = document.getElementById("route-progress");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "route-progress";
    bar.setAttribute("aria-hidden", "true");
    document.body.appendChild(bar);
  }
  bar.classList.toggle("on", on);
}

/** Draws the current address. `keepScroll` redraws in place (fresh data) without going back to the top. */
async function route(opts) {
  const keepScroll = opts?.keepScroll === true;
  const { route: name, parts, params } = parseHash();
  if (MOVED[name]) {
    // Activity & sleep used to hold the wearable charts; those are in Trends now.
    const metric = name === "activity" && params.get("metric");
    return redirect(metric ? `#/trends?metric=${encodeURIComponent(metric)}` : `#/${MOVED[name]}`);
  }
  // Workouts and the journal used to share a page, as its two tabs.
  if (name === "journal" && parts[0] === "workouts") return redirect("#/workouts");
  if (name === "journal" && parts[0] === "journal") return redirect("#/journal");
  // A screen the iPhone app keeps ready (booted, signed in) to show the next section instantly.
  if (nativeNav && name === IDLE) { mount($("#view"), ""); return; }
  // The app's alerts sheet: the list alone (the sheet has the title and the Done button).
  if (nativeNav && name === ALERTS) {
    ++renderSeq;
    renderAlertsPage();
    markAllSeen();
    appBridge({ type: "page", route: name, hash: location.hash, title: "Alerts", label: "Alerts" });
    return;
  }
  const def = ROUTES[name];
  if (!def) return redirect("#/overview");
  closePopover();
  // A dialog left open (Back, a link inside it, the address changed) would cover the next page.
  if (!keepScroll) closeDialogs();
  markActive(name);
  document.title = name === "overview" ? "Syntropy Health" : `${def.label} — Syntropy Health`;
  desktopBridge({ type: "title", title: document.title });
  const seq = ++renderSeq;
  if (cleanup) { try { cleanup(); } catch {} cleanup = null; }
  const next = document.createElement("main");
  next.className = "content";
  // Another section slides in; a tab or filter within the same page cross-fades; fresh data redraws in place.
  const motion = keepScroll || nativeNav || reducedMotion() ? null : name !== shownRoute ? "section" : "tab";
  // Fresh data drawn in place (keepScroll) doesn't replay the charts' intro.
  document.documentElement.classList.toggle("no-intro", keepScroll);
  shownRoute = name;
  let shown = false;
  const show = () => {
    if (shown || seq !== renderSeq) return;
    shown = true;
    const swap = () => {
      if (seq !== renderSeq) return;      // a later page replaced this one before its transition ran
      const current = $("#view");
      next.id = "view";
      current.replaceWith(next);
      if (!keepScroll) scrollToTop();
    };
    pageTransition?.skipTransition();
    if (motion && document.startViewTransition && !document.hidden) {
      document.documentElement.dataset.vt = motion;
      pageTransition = document.startViewTransition(swap);
      pageTransition.finished.finally(() => {
        pageTransition = null;
        if (document.documentElement.dataset.vt === motion) delete document.documentElement.dataset.vt;
      });
    } else swap();
    if (motion === "section" && !document.hidden) {     // hidden, the animation may never run and leave it see-through
      next.classList.add("page-enter");
      next.addEventListener("animationend", () => next.classList.remove("page-enter"), { once: true });
    }
  };
  const timer = setTimeout(show, PLACEHOLDER_AFTER_MS);
  const progress = setTimeout(() => { if (seq === renderSeq) setLoading(true); }, PROGRESS_AFTER_MS);
  try {
    const mod = await def.load();
    if (seq !== renderSeq) return;
    const done = (await mod.render({ el: next, parts, params, state, navigate })) || null;
    // Another section was opened meanwhile: this page is never shown, so release what it set up.
    if (seq !== renderSeq) { try { done?.(); } catch {} return; }
    cleanup = done;
  } catch (err) {
    console.error(err);
    if (seq === renderSeq) next.replaceChildren(pageError(err));
  } finally {
    clearTimeout(timer);
    clearTimeout(progress);
    if (seq === renderSeq) setLoading(false);
    show();
    // The app shows the page's heading as its title (the page hides its own; see [data-native-nav] in app.css).
    if (nativeNav && seq === renderSeq) {
      const title = next.dataset.appTitle || next.querySelector(".page-head h1")?.textContent.trim() || def.label;
      appBridge({ type: "page", route: name, hash: location.hash || "#/overview", title, label: def.label });
    }
  }
}

/** A page that couldn't be drawn: what happened, in words, and a way to try again. */
function pageError(err) {
  // The page's code didn't load: the server was updated (a reload fetches the new version) or can't be reached.
  const code = err instanceof TypeError && /module|import/i.test(err.message || "");
  const message = code ? `${unreachableMessage().replace(/ then try again\.$/, "")} If Syntropy Health was just updated, reload the page.`
    : (err?.message || String(err));
  // The iPhone app shows its own "can't reach your server" screen, like its other screens, over this one.
  if (nativeNav && (code || err?.status === 0)) appBridge({ type: "unreachable", message: unreachableMessage() });
  const box = mount(document.createElement("div"), html`<div class="empty page-error" role="alert">${icon("alert")}<h3>This page couldn't be shown</h3>
    <p>${message}</p><button class="btn" type="button">${icon("sync")}<span>${code ? "Reload" : "Try again"}</span></button></div>`);
  box.querySelector("button").addEventListener("click", () => (code ? location.reload() : route({ keepScroll: true })));
  return box.firstElementChild;
}

// ------------------------------------------------------------------ the iPhone app's companion mode
const IDLE = "_idle";
const ALERTS = "_alerts";

function renderAlertsPage() {
  const view = $("#view");
  if (!view) return;
  view.classList.add("alerts-page");
  renderPanel(view);
}

// Links to another section become new screens in the app. Links within this section (a tab, a filter) stay here.
if (nativeNav) {
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href^='#/'], a[href='#']");
    if (!a || e.defaultPrevented || e.metaKey || e.ctrlKey) return;
    const hash = a.getAttribute("href") === "#" ? "#/overview" : a.getAttribute("href");
    if (routeOf(hash) === parseHash().route) return;
    e.preventDefault();
    closePopover();
    appBridge({ type: "visit", hash });
  });
  // Saved changes may show on the app's other screens (a new lab result on Overview); it refreshes them.
  onDataChanged(() => appBridge({ type: "changed" }));
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".alerts-page [data-dismiss]");
    if (b) { dismiss(b.dataset.dismiss); renderAlertsPage(); }
  });
}

/** What the app calls on the page (evaluateJavaScript). */
window.SyntropyShell = nativeNav ? {
  /** Shows a section in this page, which the app took from its ready screens. */
  visit(hash) { closePopover(); redirect(hash); },
  /** Another screen switched person, or the app's person menu did. */
  setProfile(id) { if (id !== state.profileId && state.profiles.some((p) => p.id === id)) setProfile(id); },
  /** Something changed elsewhere: draw this section again from fresh data. */
  refresh() { clearCache(); if (state.status) { route({ keepScroll: true }); refreshAlerts(state.profile, state.status); } },
  openAlerts() { openAlerts(null); },
  /** The accent was chosen on another screen or in the app's own Settings. */
  setAccent(id) { if (id !== currentAccent()) themeTransition(() => applyAccent(id)); },
} : undefined;

// ------------------------------------------------------------------ auth screens
function authScreen(content) {
  mount(document.getElementById("root"), html`
    <div class="auth-wrap"><div class="card auth-card">
      <div class="brand">${wordmark()}</div>${content}
    </div></div>`);
}

function renderLogin() {
  // The app signs its web view in with the device's own token; ask it to do that again rather than show the password.
  if (embedded && appBridge({ type: "signin" })) {
    authScreen(html`<p class="muted" style="margin-top:10px">Signing in…</p>`);
    return;
  }
  // Nearby (this computer, the home network) the server lists who signs in: tap your name. From farther away, or when
  // only one person signs in, it's a name field or just the password.
  const people = state.status?.accounts || [];
  let who = people.length === 1 ? people[0] : null;
  let saved = null;
  try { saved = localStorage.getItem("syntropy-account"); } catch {}
  if (!who && people.length > 1) who = people.find((a) => a.id === saved) || null;

  const draw = () => {
    if (people.length > 1 && !who) {
      authScreen(html`
        <h1 style="margin-bottom:6px">Who's signing in?</h1>
        <p class="muted" style="margin-bottom:18px">Each person has their own password and sees their own health data.</p>
        <div class="who-list">${people.map((a) => html`<button class="who" type="button" data-who="${a.id}">
          <span class="avatar" style="--c:${a.color || "var(--accent)"}">${initials(a.name)}</span><span class="grow truncate">${a.name}</span>${icon("chevronRight")}</button>`)}</div>
        <p class="small muted auth-alt"><button class="link-btn" type="button" data-join>I have an invitation</button></p>`);
      document.querySelector(".who-list").addEventListener("click", (e) => {
        const b = e.target.closest("[data-who]");
        if (b) { who = people.find((a) => a.id === b.dataset.who); draw(); }
      });
      document.querySelector("[data-join]").addEventListener("click", () => renderJoin());
      return;
    }
    const typed = !people.length;    // from farther away: type your name
    authScreen(html`
      ${who ? html`<div class="who-current"><span class="avatar" style="--c:${who.color || "var(--accent)"}">${initials(who.name)}</span>
        <div class="grow"><h1>${who.name}</h1></div>
        ${people.length > 1 ? html`<button class="btn btn-ghost btn-sm" type="button" data-switch>Not ${who.name}?</button>` : ""}</div>`
        : html`<h1 style="margin-bottom:6px">Unlock</h1>`}
      <p class="muted" style="margin-bottom:18px">Enter your password to open your health record.</p>
      <form id="login-form">
        ${typed ? html`<div class="field"><input class="input" id="l-name" autocomplete="username" placeholder="Your name" aria-label="Your name"></div>` : ""}
        <div class="field"><input class="input" id="l-pass" type="password" autocomplete="current-password" placeholder="Password" required aria-label="Password"></div>
        <button class="btn btn-primary" style="width:100%" type="submit">Unlock</button>
      </form>
      <p class="small muted auth-alt"><button class="link-btn" type="button" data-join>I have an invitation</button></p>`);
    (typed ? $("#l-name") : $("#l-pass")).focus();
    document.querySelector("[data-switch]")?.addEventListener("click", () => { who = null; draw(); });
    document.querySelector("[data-join]").addEventListener("click", () => renderJoin());
    $("#login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const account = who?.id || $("#l-name")?.value.trim() || undefined;
      try {
        await post("/api/auth/login", { password: $("#l-pass").value, account });
        try { if (who) localStorage.setItem("syntropy-account", who.id); } catch {}
        boot();
      } catch (err) { toast(err.message, "bad"); $("#l-pass").select(); }
    });
  };
  draw();
}

/** Joining with an invitation: whom it's for, then a password of their own. */
function renderJoin(code = "") {
  const ask = () => {
    authScreen(html`
      <h1 style="margin-bottom:6px">Join Syntropy Health</h1>
      <p class="muted" style="margin-bottom:18px">Enter the invitation code you were given. It works once, for a week.</p>
      <form id="join-code">
        <div class="field"><input class="input mono" id="j-code" placeholder="ABCD-EFGH" autocomplete="one-time-code" autocapitalize="characters"
          spellcheck="false" required aria-label="Invitation code" value="${code}"></div>
        <button class="btn btn-primary" style="width:100%" type="submit">Continue</button>
      </form>
      <p class="small muted auth-alt"><button class="link-btn" type="button" data-back>Back to sign in</button></p>`);
    $("#j-code").focus();
    document.querySelector("[data-back]").addEventListener("click", () => { history.replaceState(null, "", "#/"); renderLogin(); });
    $("#join-code").addEventListener("submit", async (e) => {
      e.preventDefault();
      code = $("#j-code").value.trim();
      try { choose(await post("/api/invites/check", { code })); }
      catch (err) { toast(err.message, "bad"); $("#j-code").select(); }
    });
  };
  const choose = (who) => {
    authScreen(html`
      <div class="who-current"><span class="avatar" style="--c:${who.color || "var(--accent)"}">${initials(who.name)}</span>
        <div class="grow"><h1>Welcome, ${who.name}</h1></div></div>
      <p class="muted" style="margin-bottom:18px">Choose your own password. From now on your health data is yours: only you see it,
        unless you choose to share it with someone else here.</p>
      <form id="join-form">
        <div class="field"><label class="label" for="j-pass">Password</label>
          <input class="input" id="j-pass" type="password" minlength="8" autocomplete="new-password" placeholder="At least 8 characters" required>
          <div class="hint">Keep it in your password manager.</div></div>
        <div class="field"><label class="label" for="j-pass2">Confirm password</label>
          <input class="input" id="j-pass2" type="password" autocomplete="new-password" required></div>
        <div class="field">${termsCheckbox("j-terms")}</div>
        <button class="btn btn-primary" style="width:100%" type="submit">Join</button>
      </form>`);
    $("#j-pass").focus();
    $("#join-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const pass = $("#j-pass").value;
      if (pass.length < 8) return toast("Choose a password of at least 8 characters.", "bad");
      if (pass !== $("#j-pass2").value) return toast("The passwords don't match.", "bad");
      try {
        const res = await post("/api/invites/accept", { code, password: pass, accept_terms: true });
        try { localStorage.setItem("syntropy-account", res.account.id); localStorage.removeItem("syntropy-profile"); } catch {}
        history.replaceState(null, "", "#/");
        boot();
      } catch (err) { toast(err.message, "bad"); }
    });
  };
  // From an invitation link the code is already known: go straight to whom it's for.
  if (code) post("/api/invites/check", { code }).then(choose, (err) => { ask(); toast(err.message, "bad"); });
  else ask();
}

/** An invitation link opened while someone is signed in (it's meant for another person, on their own device). */
function renderJoinWhileSignedIn(code) {
  const name = state.status?.account?.name || "someone";
  authScreen(html`
    <h1 style="margin-bottom:6px">This invitation is for someone else</h1>
    <p class="muted" style="margin-bottom:18px">You're signed in as ${name}. Invitations are for the person joining, on their own
      device. To join here instead, sign out first.</p>
    <div class="row" style="gap:10px"><button class="btn" type="button" data-cancel>Stay signed in</button>
      <button class="btn btn-primary grow" type="button" data-out>Sign out and join</button></div>`);
  document.querySelector("[data-cancel]").addEventListener("click", () => { history.replaceState(null, "", "#/"); boot(); });
  document.querySelector("[data-out]").addEventListener("click", async () => {
    await post("/api/auth/logout");
    state.status = null;
    renderJoin(code);
  });
}

// ------------------------------------------------------------------ boot
async function boot() {
  try {
    state.status = await get("/api/status");
  } catch (err) {
    authScreen(html`<h1 style="margin:14px 0 6px">Can't open Syntropy Health</h1>
      <p class="muted" style="margin-bottom:18px">${err.status ? err.message : unreachableMessage()}</p>
      <button class="btn btn-primary" style="width:100%" type="button" data-retry>Try again</button>`);
    document.querySelector("[data-retry]").addEventListener("click", boot);
    // Back on the network (the laptop woke up, the server came back): try again by itself.
    window.addEventListener("online", boot, { once: true });
    return;
  }
  if (!state.status.setup_complete) return renderSetup(state.status, () => { history.replaceState(null, "", "#/"); boot(); });
  // An invitation link: #/join/ABCD-EFGH
  const { route: first, parts: joinParts } = parseHash();
  if (first === "join" && !embedded) {
    return state.status.authenticated && state.status.auth_required ? renderJoinWhileSignedIn(joinParts[0] || "") : renderJoin(joinParts[0] || "");
  }
  if (!state.status.authenticated) return renderLogin();
  // A step to finish before anything else (setting a password, accepting the terms): in the iPhone app the screen is
  // shown as is, rather than left behind the app's spinner waiting for a page.
  const gate = () => nativeNav && appBridge({ type: "page", route: "_gate", hash: location.hash || "#/", title: "Syntropy Health", label: "Syntropy Health" });
  if (state.status.password_missing) { renderPasswordGate(state.status, boot); gate(); return; }
  if (!state.status.terms_accepted) { renderTermsGate(boot); gate(); return; }
  // First run (other devices, bringing in data, AI) is for the browser: the iPhone app has its own, and in companion mode
  // each of its screens would show it. It stays waiting for the first visit from a browser.
  if (state.status.onboarding && !nativeNav) return renderOnboarding(state.status, boot);
  state.profiles = state.status.profiles || [];
  setUnitPreference(state.status.preferences?.units);
  if (state.status.preferences?.accent && state.status.preferences.accent !== currentAccent()) applyAccent(state.status.preferences.accent);
  let saved = null;
  try { saved = localStorage.getItem("syntropy-profile"); } catch {}
  state.profileId = state.profiles.some((p) => p.id === saved) ? saved : (state.profiles.find((p) => p.is_default) || state.profiles[0])?.id;
  document.getElementById("root").innerHTML = "";
  renderShell();
  route();
  if (nativeNav) appBridge({ type: "booted" });
  // Overview reports its own alerts once loaded; every other page asks for them here.
  if (parseHash().route !== "overview") refreshAlerts(state.profile, state.status);
  // Signing in again (after locking, or a session that ran out) boots again: these are set up only once.
  if (started) return;
  started = true;
  setInterval(() => { if (!document.hidden && state.status?.authenticated) refreshAlerts(state.profile, state.status); }, 5 * 60 * 1000);
  // Alerts cleared on another device (the computer, or the iPhone app) disappear here when this page is looked at again.
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.status?.authenticated) refreshAlerts(state.profile, state.status); });
  // Fetch the other sections' code in the background so the first visit to each doesn't wait for it.
  setTimeout(() => Object.values(ROUTES).forEach((r) => r.load().catch(() => {})), 1500);
}
let started = false;

onUnauthorized(() => { state.status = null; renderLogin(); });
window.addEventListener("hashchange", () => {
  if (parseHash().route === "join" && !embedded) boot();     // an invitation link opened in this tab
  else if (state.status?.authenticated) route();
});
boot();

export { api, esc };
