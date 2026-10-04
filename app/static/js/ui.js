// UI primitives: escaped templating, formatting, icons, toasts, modals, drawers.

// ---------------------------------------------------------------- templating
const RAW = Symbol("raw");
export const raw = (s) => ({ [RAW]: true, value: String(s ?? "") });

export function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function render(value) {
  if (value === null || value === undefined || value === false) return "";
  if (Array.isArray(value)) return value.map(render).join("");
  if (typeof value === "object" && value[RAW]) return value.value;
  return esc(value);
}

/** Tagged template: interpolations are escaped unless wrapped in raw() or produced by html``. */
export function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) out += render(values[i]) + strings[i + 1];
  return raw(out);
}

export function mount(el, content) { if (el) el.innerHTML = render(content); return el; }
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Delegated click handling: <button data-action="name" data-id="..."> */
export function onAction(root, handlers) {
  const listener = async (ev) => {
    const target = ev.target.closest("[data-action]");
    if (!target || !root.contains(target)) return;
    const fn = handlers[target.dataset.action];
    if (!fn) return;
    ev.preventDefault();
    try { await fn(target.dataset, target, ev); } catch (err) { toast(err.message || String(err), "bad"); console.error(err); }
  };
  // Non-button elements with an action (cards, rows) work from the keyboard too.
  const keys = (ev) => {
    if ((ev.key === "Enter" || ev.key === " ") && ev.target.matches?.("[data-action][tabindex]")) listener(ev);
  };
  root.addEventListener("click", listener);
  root.addEventListener("keydown", keys);
  return () => { root.removeEventListener("click", listener); root.removeEventListener("keydown", keys); };
}

// ---------------------------------------------------------------- formatting
const DATE_FMT = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" });
const DATE_SHORT = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const DATETIME_FMT = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

export function parseDate(value) {
  if (!value) return null;
  if (typeof value === "number") return new Date(value * 1000);
  const s = String(value);
  const d = /^\d{4}-\d{2}-\d{2}$/.test(s) ? new Date(s + "T12:00:00") : new Date(s);
  return isNaN(d) ? null : d;
}
export function fmtDate(value) { const d = parseDate(value); return d ? DATE_FMT.format(d) : (value ? String(value) : "—"); }
export function fmtShortDate(value) { const d = parseDate(value); return d ? DATE_SHORT.format(d) : "—"; }
export function fmtDateTime(value) { const d = parseDate(value); return d ? DATETIME_FMT.format(d) : "—"; }
export function fmtAgo(value) {
  const d = parseDate(value);
  if (!d) return "never";
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 30) return `${Math.round(s / 86400)} d ago`;
  return fmtDate(d);
}
export function fmtNum(v, digits) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  const n = Number(v);
  const d = digits ?? (Math.abs(n) >= 100 ? 0 : Math.abs(n) >= 10 ? 1 : 2);
  return n.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 });
}
export function age(birthDate) {
  const d = parseDate(birthDate);
  if (!d) return null;
  const now = new Date();
  let a = now.getFullYear() - d.getFullYear();
  if (now < new Date(now.getFullYear(), d.getMonth(), d.getDate())) a--;
  return a;
}
export function initials(name) {
  return String(name || "?").split(/[\s\-–(]+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
}
export function capitalize(s) { s = String(s || ""); return s.charAt(0).toUpperCase() + s.slice(1); }
export function plural(n, word, pluralWord) { return `${fmtNum(n, 0)} ${n === 1 ? word : (pluralWord || word + "s")}`; }

export function flagBadge(interp) {
  if (!interp || interp === "normal") return "";
  const map = { high: ["High", "bad"], critical_high: ["Critical high", "bad"], low: ["Low", "info"],
                critical_low: ["Critical low", "info"], abnormal: ["Abnormal", "warn"] };
  const [label, cls] = map[interp] || [capitalize(interp), "warn"];
  return html`<span class="badge ${cls}">${label}</span>`;
}
export function statusBadge(status) {
  if (!status) return "";
  const s = String(status).toLowerCase();
  const cls = ["active", "final", "completed", "current", "finished", "in-progress"].includes(s) ? "good"
    : ["resolved", "inactive", "stopped", "completed", "cancelled", "entered-in-error"].includes(s) ? "" : "info";
  return html`<span class="badge ${cls}">${capitalize(s.replace(/-/g, " "))}</span>`;
}
export function modeBadge(mode) {
  if (mode === "simulated") return html`<span class="badge warn" title="Signs in through Syntropy's built-in EHR simulator">Simulated</span>`;
  if (mode === "sandbox") return html`<span class="badge info">Vendor sandbox</span>`;
  return "";
}

// ---------------------------------------------------------------- icons
const P = {
  home: '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
  timeline: '<path d="M12 8v4l3 2"/><circle cx="12" cy="12" r="9"/>',
  records: '<path d="M7 3h8l4 4v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>',
  trends: '<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  sources: '<path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1"/><path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1"/>',
  report: '<path d="M6 9V3h12v6"/><rect x="3" y="9" width="18" height="8" rx="2"/><path d="M6 14h12v7H6z"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  sync: '<path d="M21 12a9 9 0 0 1-15.5 6.3L3 16"/><path d="M3 12a9 9 0 0 1 15.5-6.3L21 8"/><path d="M21 3v5h-5M3 21v-5h5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
  heart: '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l1 1.1L12 21l7.8-7.5 1-1.1a5.5 5.5 0 0 0 0-7.8z"/>',
  pill: '<rect x="3" y="9" width="18" height="6" rx="3" transform="rotate(-45 12 12)"/><path d="m9 9 6 6"/>',
  flask: '<path d="M9 3h6M10 3v6L4.5 18.5A1.7 1.7 0 0 0 6 21h12a1.7 1.7 0 0 0 1.5-2.5L14 9V3"/><path d="M7 15h10"/>',
  alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
  syringe: '<path d="m18 2 4 4M17 7l3-3M19 9 8.7 19.3a1 1 0 0 1-1.4 0l-2.6-2.6a1 1 0 0 1 0-1.4L15 5M9 11l4 4M5 19l-3 3M14 4l6 6"/>',
  stethoscope: '<path d="M4.8 2.3A.3.3 0 1 0 5 2H4a2 2 0 0 0-2 2v5a6 6 0 0 0 6 6 6 6 0 0 0 6-6V4a2 2 0 0 0-2-2h-1a.2.2 0 1 0 .3.3"/><path d="M8 15v1a6 6 0 0 0 6 6 6 6 0 0 0 6-6v-4"/><circle cx="20" cy="10" r="2"/>',
  scissors: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.1 15.9M14.5 14.5 20 20M8.1 8.1 12 12"/>',
  book: '<path d="M2 4h6a4 4 0 0 1 4 4v13a3 3 0 0 0-3-3H2z"/><path d="M22 4h-6a4 4 0 0 0-4 4v13a3 3 0 0 1 3-3h7z"/>',
  note: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  phone: '<rect x="6" y="2" width="12" height="20" rx="2"/><path d="M11 18h2"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
  watch: '<circle cx="12" cy="12" r="6"/><path d="M12 10v2l1 1M16.1 7.5 15.5 2h-7l-.6 5.5M7.9 16.5l.6 5.5h7l.6-5.5"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14.2a6.5 6.5 0 0 1 3.5 5.8"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  ticket: '<path d="M3 8a2 2 0 0 0 2-2h14a2 2 0 0 0 2 2v2a2 2 0 0 0 0 4v2a2 2 0 0 0-2 2H5a2 2 0 0 0-2-2v-2a2 2 0 0 0 0-4z"/><path d="M13 6v2M13 11v2M13 16v2"/>',
  building: '<rect x="4" y="2" width="16" height="20" rx="2"/><path d="M9 22v-4h6v4M8 6h.01M16 6h.01M12 6h.01M12 10h.01M12 14h.01M16 10h.01M16 14h.01M8 10h.01M8 14h.01"/>',
  arrowUp: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  arrowDown: '<path d="M12 5v14M19 12l-7 7-7-7"/>',
  arrowRight: '<path d="M5 12h14M12 5l7 7-7 7"/>',
  printer: '<path d="M6 9V2h12v7M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  sparkles: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
  send: '<path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="2.5" fill="currentColor" stroke="none"/>',
  key: '<circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6M15.5 7.5l3 3L22 7l-3-3"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  trash: '<path d="M3 6h18M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>',
  cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/>',
  shieldCheck: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  chevronLeft: '<path d="m15 18-6-6 6-6"/>',
  chevronRight: '<path d="m9 18 6-6-6-6"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  lock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  monitor: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
  globe: '<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
  code: '<path d="m16 18 6-6-6-6M8 6l-6 6 6 6"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  compare: '<circle cx="6" cy="6" r="3"/><circle cx="18" cy="18" r="3"/><path d="M13 6h3a2 2 0 0 1 2 2v7M11 18H8a2 2 0 0 1-2-2V9"/>',
  lightbulb: '<path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.7V17h8v-2.3A7 7 0 0 0 12 2z"/>',
  trophy: '<path d="M6 9H4a2 2 0 0 1 0-4h2M18 9h2a2 2 0 0 0 0-4h-2M4 22h16M10 14.7V17c0 .6-.5 1-1 1.2C7.9 18.8 7 20.2 7 22M14 14.7V17c0 .6.5 1 1 1.2 1.2.6 2 2 2 3.8M18 2H6v7a6 6 0 0 0 12 0z"/>',
  run: '<circle cx="15" cy="4" r="2"/><path d="m5 21 3.5-5 3 2.5 1.5-6 4 3.5h3M8.5 11 11 8l4 1.5 2.5 3.5"/>',
  bike: '<circle cx="5.5" cy="17.5" r="3.5"/><circle cx="18.5" cy="17.5" r="3.5"/><circle cx="15" cy="5" r="1.5"/><path d="M12 17.5V14l-3-3 4-3 2 3h2"/>',
  walk: '<circle cx="13" cy="4" r="2"/><path d="m9 21 2.5-6.5L14 17v4M9.5 12l1.5-4 3.5 2 2.5 3M11 8l-3 3v3"/>',
  swim: '<path d="M2 18c1.5 1 3 1 4.5 0s3-1 4.5 0 3 1 4.5 0 3-1 4.5 0M2 14c1.5 1 3 1 4.5 0s3-1 4.5 0 3 1 4.5 0 3-1 4.5 0"/><circle cx="17" cy="6" r="2"/><path d="m6 11 4-4 3 3"/>',
  dumbbell: '<path d="M6.5 6.5v11M17.5 6.5v11M3 9v6M21 9v6M6.5 12h11"/>',
  yoga: '<circle cx="12" cy="4" r="2"/><path d="M4 20h16M12 6v7M6 9l6 4 6-4M8 20l4-7 4 7"/>',
  mountain: '<path d="m8 3 4 8 5-5 5 15H2z"/>',
  flame: '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.4-.5-2-1-3-1.1-2.1-.2-4.1 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.2.4-2.3 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>',
  bed: '<path d="M2 4v16M2 8h18a2 2 0 0 1 2 2v10M2 17h20M6 8v9"/>',
  sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
  grip: '<circle cx="9" cy="6" r="1"/><circle cx="15" cy="6" r="1"/><circle cx="9" cy="12" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="9" cy="18" r="1"/><circle cx="15" cy="18" r="1"/>',
  more: '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
  scale: '<path d="M16 16l3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1zM2 16l3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1zM7 21h10M12 3v18M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2"/>',
};
export function icon(name, cls = "") {
  return raw(`<svg class="ic ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || ""}</svg>`);
}
// The Syntropy Health wordmark: Satoshi Black outlined, with the brand S (small-size version) as its first letter
// and the pink heart behind it. Generated from the brand files, which live outside this repository: replace the paths
// rather than editing them.
const WORDMARK_PATH = "M3224 240V-500H3369L3377 -439C3405 -485 3468 -516 3539 -516C3678 -516 3775 -419 3775 -258C3775 -100 3689 14 3540 14C3471 14 3407 -12 3378 -50V240ZM4855 0H4693V-740H4855V-448H5141V-740H5303V0H5141V-298H4855ZM2584 -251C2584 -409 2700 -515 2860 -515C3019 -515 3135 -409 3135 -251C3135 -93 3019 12 2860 12C2700 12 2584 -93 2584 -251ZM36 -525C36 -659 149 -754 308 -754C467 -754 567 -666 567 -526H406C406 -578 367 -610 306 -610C240 -610 198 -580 198 -531C198 -486 221 -464 273 -453L384 -430C516 -403 579 -340 579 -223C579 -80 467 13 297 13C236.9 13 184.5 1.2 142.1 -20.6L247.2 -137.3C261.8 -133.1 278.7 -131 298 -131C371 -131 417 -160 417 -207C417 -248 398 -269 349 -279L236 -302C104 -329 36 -404 36 -525ZM7362 0H7208V-754H7363V-448C7394 -491 7451 -516 7516 -516C7635 -516 7704 -440 7704 -309V0H7550V-272C7550 -335 7515 -376 7462 -376C7401 -376 7362 -336 7362 -274ZM6159 13C6053 13 5987 -49 5987 -147C5987 -239 6052 -296 6172 -305L6312 -316V-324C6312 -373 6282 -399 6227 -399C6162 -399 6127 -374 6127 -329H5999C5999 -442 6092 -516 6235 -516C6380 -516 6462 -435 6462 -292V0H6326L6316 -66C6300 -20 6234 13 6159 13ZM5670 13C5519 13 5411 -97 5411 -251C5411 -407 5516 -516 5667 -516C5824 -516 5922 -413 5922 -250V-211L5559 -209C5568 -143 5605 -112 5673 -112C5731 -112 5772 -133 5783 -169H5924C5906 -58 5807 13 5670 13ZM627 240V111H712C764 111 786 95 806 41L815 17L614 -500H779L888 -179L1009 -500H1169L915 109C873 211 817 254 729 254C692 254 657 249 627 240ZM3804 240V111H3889C3941 111 3963 95 3983 41L3992 17L3791 -500H3956L4065 -179L4186 -500H4346L4092 109C4050 211 3994 254 3906 254C3869 254 3834 249 3804 240ZM1386 0H1232V-500H1377L1387 -448C1418 -491 1475 -516 1540 -516C1659 -516 1728 -440 1728 -309V0H1574V-272C1574 -335 1539 -376 1486 -376C1425 -376 1386 -336 1386 -274ZM2040 0H1886V-372H1791V-500H1886V-655H2040V-500H2135V-372H2040ZM7044 0H6890V-372H6795V-500H6890V-655H7044V-500H7139V-372H7044ZM6726 0H6572V-754H6726ZM2548 -500V-355H2499C2411 -355 2358 -316 2358 -217V0H2204V-499H2349L2357 -425C2378 -474 2422 -507 2489 -507C2507 -507 2527 -505 2548 -500ZM2739 -252C2739 -176 2788 -126 2860 -126C2931 -126 2980 -176 2980 -252C2980 -327 2931 -377 2860 -377C2788 -377 2739 -327 2739 -252ZM3379 -250C3379 -175 3429 -125 3501 -125C3574 -125 3620 -176 3620 -250C3620 -324 3574 -375 3501 -375C3429 -375 3379 -325 3379 -250ZM6213 -101C6272 -101 6313 -130 6313 -187V-214L6235 -207C6168 -201 6144 -186 6144 -154C6144 -118 6166 -101 6213 -101ZM5668 -391C5607 -391 5573 -364 5561 -304H5769C5769 -357 5730 -391 5668 -391ZM36.3 -147.1C25.5 -174.7 20.4 -205.4 21.5 -238.9L182.4 -233.3C182.1 -223.9 183 -215.2 185.1 -207.2ZM120.2 -44.1C98.7 -58.3 80.5 -75.6 66 -95.7L214.2 -164.8C216.3 -163.1 218.6 -161.4 220.9 -159.9Z";
const WORDMARK_HEART = "M625.4 -284.2 342.5 -567.1C329.8 -579.8 318.8 -594.2 309.9 -610C368.6 -608.6 406 -576.9 406 -526H567C567 -666 467 -754 308 -754C301.6 -754 295.3 -753.8 289 -753.5C309.5 -842.3 389 -908.5 483.9 -908.5C539.2 -908.5 589.2 -886.1 625.4 -849.9C661.6 -886.1 711.6 -908.5 766.8 -908.5C877.2 -908.5 966.8 -818.9 966.8 -708.5C966.8 -653.3 944.4 -603.3 908.2 -567.1L794.8 -453.6L779 -500H614L677.6 -336.4Z";
let wordmarkCount = 0;
export function wordmark() {
  // Each copy gets its own gradient id, so a hidden copy can't take the heart's colour with it.
  const id = `wordmark-heart-${++wordmarkCount}`;
  return raw(`<svg class="wordmark" viewBox="21.4 -908.5 7682.6 1162.5" role="img" aria-label="Syntropy Health"><defs><linearGradient id="${id}" gradientUnits="userSpaceOnUse" x1="0" y1="-908.5" x2="0" y2="-284.2"><stop offset="0" stop-color="#FF6B8B"/><stop offset="1" stop-color="#FF2D55"/></linearGradient></defs><path fill="url(#${id})" d="${WORDMARK_HEART}"/><path fill="currentColor" d="${WORDMARK_PATH}"/></svg>`);
}
export const CATEGORY_ICON = {
  conditions: "stethoscope", medications: "pill", allergies: "alert", labs: "flask", vitals: "heart",
  immunizations: "syringe", encounters: "building", procedures: "scissors", notes: "note", reports: "image",
  care_team: "user", care_plans: "records", goals: "check", devices: "watch", coverage: "shield", observations: "records",
};

// ---------------------------------------------------------------- markdown (assistant replies)
/** Small, safe Markdown subset: everything is escaped first, then headings, lists, tables, bold, italics,
 *  inline code and code blocks are formatted. Links are shown as text (no navigation from model output). */
export function markdown(text) {
  const lines = esc(text || "").split("\n");
  const inline = (s) => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const block = [];
      for (i++; i < lines.length && !/^```/.test(lines[i]); i++) block.push(lines[i]);
      out.push(`<pre class="md-code">${block.join("\n")}</pre>`); i++; continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1])) {
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      const head = cells(line);
      const rows = [];
      for (i += 2; i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i]); i++) rows.push(cells(lines[i]));
      out.push(`<div class="table-wrap"><table class="table md-table"><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead>`
        + `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) { out.push(`<h4 class="md-h">${inline(heading[2])}</h4>`); i++; continue; }
    if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]/.test(line);
      const items = [];
      for (; i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i]); i++) items.push(inline(lines[i].replace(/^\s*([-*•]|\d+[.)])\s+/, "")));
      out.push(`<${ordered ? "ol" : "ul"} class="md-list">${items.map((it) => `<li>${it}</li>`).join("")}</${ordered ? "ol" : "ul"}>`);
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const para = [];
    for (; i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|\s*([-*•]|\d+[.)])\s|\s*\|)/.test(lines[i]); i++) para.push(inline(lines[i]));
    if (!para.length) { para.push(inline(lines[i])); i++; }
    out.push(`<p>${para.join("<br>")}</p>`);
  }
  return raw(out.join(""));
}

// ---------------------------------------------------------------- scrolling
/** What scrolls the page: the document, or in the Mac app the main column (so the sidebar stays put, as in Mac apps). */
export function pageScroller() {
  return (document.documentElement.getAttribute("data-shell") === "mac" && document.querySelector(".main")) || document.scrollingElement;
}
export function scrollToTop() {
  // In the iPhone app's companion mode the page sits in the app's scroll view, below its title bar: the app scrolls it.
  const app = document.documentElement.hasAttribute("data-native-nav") && window.webkit?.messageHandlers?.syntropy;
  if (app) { app.postMessage({ type: "scrollTop" }); return; }
  window.scrollTo(0, 0);
  pageScroller().scrollTop = 0;
}

// ---------------------------------------------------------------- toasts
export function toast(message, kind = "") {
  const box = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  el.setAttribute("role", kind === "bad" ? "alert" : "status");
  box.appendChild(el);
  // Fades out (or goes when clicked) rather than vanishing.
  const leave = () => {
    if (el.classList.contains("leaving")) return;
    el.classList.add("leaving");
    el.addEventListener("animationend", () => el.remove(), { once: true });
    setTimeout(() => el.remove(), 400);
  };
  el.addEventListener("click", leave);
  setTimeout(leave, kind === "bad" ? 7000 : 4000);
}

/** Removes an element after its closing animation (`.closing` in app.css), or straight away without one. */
export function closeAnimated(el, done) {
  if (!el?.isConnected) { done?.(); return; }
  el.classList.add("closing");
  let finished = false;
  const finish = () => { if (finished) return; finished = true; el.remove(); done?.(); };
  const style = getComputedStyle(el);
  if (style.animationName === "none" || parseFloat(style.animationDuration) === 0) return finish();
  el.addEventListener("animationend", (e) => { if (e.target === el) finish(); });
  setTimeout(finish, 350);
}

// ---------------------------------------------------------------- modal & drawer
const openDialogs = new Set();

/** Closes every open dialog and drawer: they belong to the page they were opened from (see route in app.js). */
export function closeDialogs() { for (const close of [...openDialogs]) close(); }

export function modal({ title, body, footer = "", wide = false, drawer = false, onClose } = {}) {
  const overlay = document.createElement("div");
  overlay.className = `overlay${drawer ? " drawer-overlay" : ""}`;
  overlay.innerHTML = render(html`
    <div class="${drawer ? "drawer" : `modal${wide ? " wide" : ""}`}" role="dialog" aria-modal="true" aria-label="${title}">
      <div class="modal-head"><h2 class="grow truncate">${title}</h2>
        <button class="btn btn-ghost btn-icon" data-close aria-label="Close">${icon("x")}</button></div>
      <div class="modal-body">${body}</div>
      ${footer ? html`<div class="modal-foot">${footer}</div>` : ""}
    </div>`);
  const opener = document.activeElement;
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    openDialogs.delete(close);
    document.removeEventListener("keydown", onKey);
    closeAnimated(overlay);
    // Keyboard focus goes back where it was before the dialog opened.
    if (opener instanceof HTMLElement && opener.isConnected) opener.focus({ preventScroll: true });
    onClose && onClose();
  };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(); });
  overlay.querySelector("[data-close]").addEventListener("click", close);
  document.addEventListener("keydown", onKey);
  openDialogs.add(close);
  document.body.appendChild(overlay);
  const panel = overlay.firstElementChild;
  // Focus moves into the dialog: its first field, or the dialog itself (so Tab and Escape work from the keyboard).
  panel.setAttribute("tabindex", "-1");
  requestAnimationFrame(() => {
    if (!panel.contains(document.activeElement)) {
      (panel.querySelector(".modal-body :is(input:not([type=hidden]), textarea, select, [autofocus])") || panel).focus({ preventScroll: true });
    }
  });
  return { el: panel, body: panel.querySelector(".modal-body"), close,
           setBody: (content) => mount(panel.querySelector(".modal-body"), content) };
}

// In the iPhone app's companion mode, confirmations are the system's own alerts (with a red button for deleting).
const pendingConfirms = new Map();
let confirmSeq = 0;
if (document.documentElement.hasAttribute("data-native-nav")) {
  window.SyntropyConfirm = { resolve(id, ok) { pendingConfirms.get(id)?.(!!ok); pendingConfirms.delete(id); } };
}

export function confirmDialog(title, message, { confirmLabel = "Confirm", danger = false } = {}) {
  const app = document.documentElement.hasAttribute("data-native-nav") && window.webkit?.messageHandlers?.syntropy;
  if (app) {
    return new Promise((resolve) => {
      const id = ++confirmSeq;
      pendingConfirms.set(id, resolve);
      const text = document.createElement("div");
      text.innerHTML = render(message);   // an alert shows plain text
      app.postMessage({ type: "confirm", id, title, message: text.textContent.replace(/\s+/g, " ").trim(), confirmLabel, danger });
    });
  }
  return new Promise((resolve) => {
    let done = false;
    const m = modal({
      title, body: html`<p>${message}</p>`,
      footer: html`<button class="btn" data-no>Cancel</button><button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-yes>${confirmLabel}</button>`,
      onClose: () => { if (!done) resolve(false); },
    });
    m.el.querySelector("[data-no]").addEventListener("click", () => m.close());
    m.el.querySelector("[data-yes]").addEventListener("click", () => { done = true; m.close(); resolve(true); });
  });
}

export function loading(lines = 3) {
  return html`<div class="stack">${Array.from({ length: lines }, () => html`<div class="skeleton" style="height:64px"></div>`)}</div>`;
}

export function emptyState(title, text, action = "") {
  return html`<div class="empty"><h3>${title}</h3><p>${text}</p>${action}</div>`;
}

export function debounce(fn, ms = 250) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// In the iPhone app a menu is the phone's own (an action sheet), when the app can show one (see appCan in app.js).
let appMenuItems = null;
let appMenuSeq = 0;

function appMenu(items, button) {
  const handler = window.webkit?.messageHandlers?.syntropy;
  if (!handler || !document.documentElement.hasAttribute("data-native-nav") || !(window.SyntropyApp?.capabilities || []).includes("menu")) return false;
  const entries = items.filter((it) => it !== "-");
  appMenuItems = { id: ++appMenuSeq, entries };
  const r = button?.getBoundingClientRect();       // where the menu points from
  handler.postMessage({ type: "menu", id: appMenuItems.id, items: entries.map((it) => ({ title: it.label, destructive: !!it.danger })),
                        rect: r ? { x: r.left, y: r.top, width: r.width, height: r.height } : null });
  return true;
}

/** The app's menu was answered: the chosen item's index, or -1 when it was cancelled. */
export function appMenuChosen(id, index) {
  if (appMenuItems?.id !== id) return;
  const item = appMenuItems.entries[index];
  appMenuItems = null;
  if (!item) return;
  if (item.href) {
    // Through a link, so a link to another section opens as a new screen.
    const a = Object.assign(document.createElement("a"), { href: item.href });
    document.body.appendChild(a);
    a.click();
    a.remove();
  } else item.run?.();
}

/**
 * A small menu under `button`: items are { label, icon, run, href, danger } or "-" for a divider. Closes on a choice,
 * a click elsewhere, Escape or scrolling the page. Opening it again from the same button closes it.
 */
let openMenu = null;
export function dropdown(button, items) {
  if (appMenu(items, button)) return;
  const same = openMenu?.button === button;
  openMenu?.close();
  if (same) return;
  const el = document.createElement("div");
  el.className = "popover dropdown";
  el.setAttribute("role", "menu");
  el.innerHTML = render(html`<div class="menu">${items.map((it, i) => it === "-" ? html`<div class="menu-sep"></div>`
    : it.href ? html`<a class="menu-item ${it.danger ? "danger" : ""}" role="menuitem" href="${it.href}">${it.icon ? icon(it.icon) : ""}<span>${it.label}</span></a>`
    : html`<button class="menu-item ${it.danger ? "danger" : ""}" role="menuitem" data-i="${i}">${it.icon ? icon(it.icon) : ""}<span>${it.label}</span></button>`)}</div>`);
  document.body.appendChild(el);
  const r = button.getBoundingClientRect();
  const below = r.bottom + 6 + el.offsetHeight < window.innerHeight - 8;
  el.style.top = below ? `${r.bottom + 6}px` : "";
  el.style.bottom = below ? "" : `${window.innerHeight - r.top + 6}px`;
  el.style.right = `${Math.max(8, window.innerWidth - r.right)}px`;
  el.style.left = "auto";
  // Right-aligned to the button, unless that pushes it off the left edge (a button on the left of a phone screen).
  if (el.getBoundingClientRect().left < 8) {
    el.style.left = `${Math.max(8, r.left)}px`;
    el.style.right = "auto";
  }
  button.setAttribute("aria-expanded", "true");
  const scroller = pageScroller();
  const startY = scroller.scrollTop;
  const close = () => {
    closeAnimated(el);
    button.setAttribute("aria-expanded", "false");
    document.removeEventListener("mousedown", outside, true);
    document.removeEventListener("keydown", key);
    document.removeEventListener("scroll", scrolled, true);
    if (openMenu?.el === el) openMenu = null;
  };
  const outside = (e) => { if (!el.contains(e.target) && !button.contains(e.target)) close(); };
  const key = (e) => { if (e.key === "Escape") { close(); button.focus(); } };
  // The menu is pinned to the screen, so it goes once the page has really moved (not a nudge while tapping).
  const scrolled = () => { if (Math.abs(scroller.scrollTop - startY) > 30) close(); };
  el.addEventListener("click", (e) => {
    const b = e.target.closest("[data-i], a");
    if (!b) return;
    close();
    if (b.dataset.i != null) items[Number(b.dataset.i)].run?.();
  });
  setTimeout(() => {
    document.addEventListener("mousedown", outside, true);
    document.addEventListener("keydown", key);
    document.addEventListener("scroll", scrolled, { passive: true, capture: true });
  });
  openMenu = { el, button, close };
}
