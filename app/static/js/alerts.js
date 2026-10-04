// Alerts: things that need the person's attention (a source to reconnect, records from someone else, new out-of-range
// results), collected for the current profile and shown behind the bell in the top bar instead of across every page.

import { api, get } from "./api.js";
import { fmtDate, html, icon, mount, plural } from "./ui.js";

// Earlier versions kept what was seen and dismissed in each browser; the server keeps it now, so clearing alerts on
// the computer also clears them in the iPhone app. Anything still in this browser is handed over once.
const OLD_KEYS = { seen: "syntropy-alerts-seen", dismissed: "syntropy-alerts-dismissed" };

let alerts = [];
let profileId = null;
let listeners = [];
let seen = new Set();
let dismissed = new Set();

function takeOldIds(kind) {
  try {
    const ids = JSON.parse(localStorage.getItem(OLD_KEYS[kind]) || "[]");
    localStorage.removeItem(OLD_KEYS[kind]);
    return Array.isArray(ids) ? ids.filter((i) => typeof i === "string").slice(-400) : [];
  } catch { return []; }
}

async function loadState(id) {
  const old = { seen: takeOldIds("seen"), dismissed: takeOldIds("dismissed") };
  const res = old.seen.length || old.dismissed.length
    ? await api(`/api/profiles/${id}/alerts`, { method: "POST", body: old, keepCache: true })
    : await get(`/api/profiles/${id}/alerts`, {}, { fresh: true });
  return { seen: new Set(res.seen), dismissed: new Set(res.dismissed) };
}

function saveState(change) {
  if (profileId) api(`/api/profiles/${profileId}/alerts`, { method: "POST", body: change, keepCache: true }).catch(() => {});
}

const STALE_PHONE_DAYS = 3;
const UPDATE_REMINDER_DAYS = 30;

/** Builds the alert list from the profile's summary and connections. */
function collect({ summary, connections, status }) {
  const out = [];
  const conflict = summary?.identity_conflict || [];
  if (conflict.length) {
    out.push({
      id: `identity:${conflict.map((p) => p.full_name).join("|")}`, level: "warn", icon: "alert",
      title: "These records belong to different people",
      text: conflict.map((p) => `${p.full_name || "Unnamed patient"}${p.birth_date ? ` (born ${fmtDate(p.birth_date)})` : ""} from ${p.sources.join(", ")}`).join("; "),
      href: "#/sources", action: "Review sources",
    });
  }
  for (const c of connections || []) {
    const age = c.last_sync_at ? Math.floor((Date.now() / 1000 - c.last_sync_at) / 86400) : 0;
    if (c.access_ended) {
      // A one-time import (healow): a gentle nudge each month, never a warning. Dismissing it lasts until the next one.
      if (age >= UPDATE_REMINDER_DAYS) {
        out.push({ id: `conn:${c.id}:update:${Math.floor(c.last_sync_at)}:${Math.floor(age / UPDATE_REMINDER_DAYS)}`, level: "info", icon: "sources",
                   title: `Update ${c.display_name}?`, text: `Its records here are from ${fmtDate(c.last_sync_at)}. Sign in again to bring in anything new.`,
                   href: "#/sources", action: "Open Sources" });
      }
    } else if (c.status === "needs_reauth") {
      out.push({ id: `conn:${c.id}:reauth`, level: "warn", icon: "sources", title: `Reconnect ${c.display_name}`,
                 text: "Access expired, so new records aren't coming in.", href: "#/sources", action: "Reconnect" });
    } else if (c.status === "error") {
      out.push({ id: `conn:${c.id}:error:${c.last_sync_at || ""}`, level: "bad", icon: "sync", title: `${c.display_name} couldn't sync`,
                 text: c.last_error || "The last sync failed.", href: "#/sources", action: "Open Sources" });
    } else if (c.kind === "device" && c.status === "active" && c.last_sync_at && Date.now() / 1000 - c.last_sync_at > STALE_PHONE_DAYS * 86400) {
      // A paired phone that has gone quiet: usually the app hasn't been opened, or the server was unreachable from it.
      out.push({ id: `conn:${c.id}:stale:${Math.floor(c.last_sync_at / 86400)}`, level: "warn", icon: "phone",
                 title: `No Apple Health data since ${fmtDate(c.last_sync_at)}`,
                 text: `${c.display_name} hasn't sent anything in ${Math.floor((Date.now() / 1000 - c.last_sync_at) / 86400)} days. Open the Syntropy Health app on it to catch up.`,
                 href: "#/sources", action: "Open Sources" });
    }
  }
  if (status?.update) {
    out.push({ id: `update:${status.update.version}`, level: "info", icon: "download", title: `Syntropy Health ${status.update.version} is available`,
               text: status.update.installs ? "Install it from Settings; the app opens again by itself." : "Settings shows how to update this installation.",
               href: "#/settings/general/updates", action: "Update" });
  }
  for (const l of summary?.flagged_labs || []) {
    const word = /low/.test(l.interpretation) ? "low" : /high/.test(l.interpretation) ? "high" : "out of range";
    out.push({ id: `lab:${l.id}`, level: "lab", icon: "flask", title: `${l.title} is ${word}`,
               text: `${l.value_text}${l.ref_text ? ` (range ${l.ref_text})` : ""} · ${fmtDate(l.effective_at)}`,
               href: `#/trends/labs?code=${encodeURIComponent(l.code || "")}`, action: "See trend" });
  }
  return out;
}

/** Recomputes the alerts. Pass data a page already fetched to avoid asking for it twice. */
export async function refreshAlerts(profile, status, data = null) {
  if (!profile) return;
  profileId = profile.id;
  try {
    const [summary, connections, marks] = await Promise.all([
      data ? data.summary : get("/api/summary", { profile: profile.id }),
      data ? data.connections : get("/api/connections", { profile: profile.id }).then((r) => r.connections),
      loadState(profile.id),
    ]);
    if (profileId !== profile.id) return;   // switched profile while loading
    ({ seen, dismissed } = marks);
    alerts = collect({ summary, connections, status }).filter((a) => !dismissed.has(a.id));
  } catch { return; }
  listeners.forEach((fn) => fn());
}

export function onAlertsChange(fn) { listeners.push(fn); return () => { listeners = listeners.filter((f) => f !== fn); }; }

export function unreadCount() {
  return alerts.filter((a) => !seen.has(a.id)).length;
}

export function markAllSeen() {
  const fresh = alerts.filter((a) => !seen.has(a.id)).map((a) => a.id);
  if (!fresh.length) return;
  fresh.forEach((id) => seen.add(id));
  saveState({ seen: fresh });
  listeners.forEach((fn) => fn("seen"));
}

export function dismiss(id) {
  dismissed.add(id);
  saveState({ dismissed: [id] });
  alerts = alerts.filter((a) => a.id !== id);
  listeners.forEach((fn) => fn());
}

/** The panel's contents. Unread alerts are marked, so opening the panel shows what's new before it's cleared. */
export function renderPanel(el) {
  const labs = alerts.filter((a) => a.level === "lab");
  const other = alerts.filter((a) => a.level !== "lab");
  const item = (a) => html`<div class="alert-item ${seen.has(a.id) ? "" : "unread"}">
      <span class="alert-icon ${a.level}">${icon(a.icon)}</span>
      <a class="grow" href="${a.href}" data-close-alerts>
        <div class="alert-title">${a.title}</div><div class="alert-text">${a.text}</div></a>
      <button class="btn btn-ghost btn-icon btn-sm" data-dismiss="${a.id}" aria-label="Dismiss" title="Dismiss">${icon("x")}</button>
    </div>`;
  mount(el, html`
    <div class="popover-head"><b>Alerts</b><span class="small muted">${alerts.length ? plural(alerts.length, "item") : ""}</span></div>
    <div class="popover-body">
      ${alerts.length ? "" : html`<div class="empty small"><span class="alert-icon good" style="margin:0 auto 8px">${icon("check")}</span>
        <b>You're all caught up.</b><p>Sources that need attention and new out-of-range results will show up here.</p></div>`}
      ${other.map(item)}
      ${labs.length ? html`<div class="popover-label">Out-of-range results</div>${labs.map(item)}` : ""}
    </div>`);
}
