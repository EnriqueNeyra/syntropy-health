import { api, del, get, post } from "../api.js";
import {
  $, confirmDialog, debounce, dropdown, emptyState, esc, fmtAgo, fmtDate, fmtDateTime, html, icon, initials, loading, modal, modeBadge, mount,
  onAction, plural, toast,
} from "../ui.js";
import { embedded } from "../app.js";
import { networkStatus, renderNetworkPanel } from "../network.js";

const PLATFORM_COLORS = { epic: "#c2410c", cerner: "#b91c1c", athena: "#7c3aed", healow: "#0f766e", va: "#1d4ed8", "smart-health-it": "#0369a1", custom: "#4b5563" };
const PLATFORM_LABELS = { epic: "Epic MyChart", cerner: "Oracle Health", athena: "athenahealth", healow: "healow", va: "VA", "smart-health-it": "SMART Health IT", custom: "FHIR server" };
const WEARABLES = [
  ["oura", "Oura Ring", "Sleep stages, readiness, HRV, temperature and activity."],
  ["whoop", "WHOOP", "Recovery, strain, HRV and sleep performance."],
  ["google", "Google Health", "Fitbit and Pixel Watch: steps, sleep stages, HRV, resting heart rate and workouts."],
];
const WEARABLE_SHORT = { oura: "Oura", whoop: "WHOOP", google: "Google Health" };

function avatar(name, platform) {
  const c = PLATFORM_COLORS[platform] || "#4b5563";
  return html`<span class="src-avatar" style="color:${c};background:color-mix(in srgb, ${c} 10%, var(--surface))">${initials(name)}</span>`;
}
const iconAvatar = (name) => html`<span class="src-avatar">${icon(name)}</span>`;

/** Status at a glance: a coloured dot and a word. */
function statusLine(c, paired) {
  if (c.syncing) return html`<span class="src-status"><span class="spinner"></span> Syncing</span>`;
  if (c.kind === "device" && paired && c.metadata?.history && !c.metadata.history.complete) return historyStatus(c);
  if (c.kind === "device") return paired ? html`<span class="src-status good"><span class="dot good"></span> Paired</span>`
    : html`<span class="src-status"><span class="dot"></span> Not paired</span>`;
  // A one-time import (healow keeps no connection): nothing's wrong, signing in again brings in what's new.
  if (c.access_ended) return html`<span class="src-status"><span class="dot"></span> Imported</span>`;
  if (c.status === "needs_reauth") return html`<span class="src-status warn"><span class="dot warn"></span> Reconnect needed</span>`;
  if (c.status === "error") return html`<span class="src-status bad" title="${c.last_error || ""}"><span class="dot bad"></span> Sync failed</span>`;
  if (c.status === "disconnected") return html`<span class="src-status"><span class="dot"></span> Disconnected</span>`;
  if (c.last_sync_status === "partial") return html`<span class="src-status warn" title="Some record types could not be fetched"><span class="dot warn"></span> Partly synced</span>`;
  return html`<span class="src-status good"><span class="dot good"></span> Connected</span>`;
}

/** A phone still sending Health's history (the phone apps report it): moving right now, or waiting for the app. */
function historyStatus(c) {
  const android = c.metadata?.platform === "android";
  const h = c.metadata.history;
  const lastActive = Math.max(h.updated_at || 0, c.last_sync_at || 0);
  const types = h.types_total ? ` · ${h.types_done || 0} of ${h.types_total} types` : "";
  if (Date.now() / 1000 - lastActive < 180) {
    return html`<span class="src-status"><span class="spinner"></span> Sending Health history${types}</span>`;
  }
  const phone = android ? "phone" : "iPhone";
  const why = android ? "Android sends it a little at a time in the background." : "Health data can only be read while the iPhone is unlocked.";
  return html`<span class="src-status warn" title="${why} Open Syntropy Health on the ${phone} and keep it open to finish."><span class="dot warn"></span> History not finished</span>
    <span class="src-extra">open the app on the ${phone} to finish</span>`;
}

/** Where it comes from, in a few words (shown on wider screens). */
function origin(c) {
  if (c.kind === "ehr") return PLATFORM_LABELS[c.provider] || c.provider.toUpperCase();
  if (c.kind === "wearable") return c.mode === "simulated" ? "" : "Cloud sync";
  if (c.kind === "device") {
    return { "healthkit-export": "HealthKit export", android: "Syntropy Android app" }[c.metadata?.platform] || "Syntropy iPhone app";
  }
  return c.provider === "manual" ? "Lab results you entered" : `Imported ${fmtDate(c.created_at)}`;
}

/**
 * One source as a row: its name, then one line saying whether it's working and what it brought in, with the everyday
 * action (sync, reconnect) beside it and everything else in a menu.
 */
function sourceRow(c, devices = []) {
  const isEhr = c.kind === "ehr";
  const paired = devices.length > 0;
  const count = c.kind === "ehr" || (c.kind === "import" && c.record_count) ? plural(c.record_count || 0, "record")
    : plural(c.sample_count || 0, "sample");
  const live = c.status !== "disconnected";
  const ended = isEhr && c.access_ended;
  const canSync = (isEhr || c.kind === "wearable") && live && !ended;
  const canReconnect = isEhr || (c.kind === "wearable" && c.mode === "live");
  const needsAttention = !ended && (["needs_reauth", "error"].includes(c.status) || (c.status === "disconnected" && canReconnect));
  const ava = isEhr ? avatar(c.display_name, c.provider)
    : iconAvatar(c.kind === "wearable" ? "watch" : c.kind === "device" ? "phone" : c.provider === "manual" ? "flask" : "upload");
  const lastSeen = devices.map((d) => d.last_seen_at).filter(Boolean).sort().pop();
  const synced = c.kind === "import" ? null
    : html`<span class="src-word">synced </span>${fmtAgo(c.kind === "device" ? (c.last_sync_at || lastSeen) : c.last_sync_at)}`;
  // Only worth mentioning when access will run out.
  const access = ended ? "sign in again to update" : isEhr && c.token_expires_at && live && !c.has_refresh_token
    ? `access until ${(c.token_expires_at * 1000 - Date.now() < 86400000 ? fmtDateTime : fmtDate)(c.token_expires_at)}` : null;
  const where = origin(c);
  return html`<div class="src-row ${live || paired ? "" : "is-off"}" id="conn-${c.id}">
    ${ava}
    <div class="src-main">
      <div class="src-title"><span class="src-name" title="${c.display_name}${c.patient_ref ? ` · patient ${c.patient_ref}` : ""}">${c.display_name}</span>
        ${c.mode === "simulated" || c.mode === "sandbox" ? modeBadge(c.mode) : ""}</div>
      <div class="src-line">${statusLine(c, paired)}<span>${count}</span>${synced ? html`<span>${synced}</span>` : ""}
        ${where ? html`<span class="src-extra">${where}</span>` : ""}${access ? html`<span class="src-extra">${access}</span>` : ""}
        ${devices.length > 1 ? html`<span class="src-extra">${devices.length} devices</span>` : ""}</div>
    </div>
    <div class="src-actions">
      ${needsAttention && canReconnect ? html`<button class="btn btn-sm btn-primary" data-action="reconnect" data-id="${c.id}">Reconnect</button>` : ""}
      ${ended ? html`<button class="btn btn-sm" data-action="reconnect" data-id="${c.id}" title="Sign in again to bring in new records">${icon("sync")}<span class="btn-label">Update</span></button>` : ""}
      ${canSync ? html`<button class="btn btn-sm" data-action="sync" data-id="${c.id}" ${c.syncing ? "disabled" : ""} aria-label="Sync ${c.display_name} now" title="Sync now">
        ${icon("sync")}<span class="btn-label">Sync now</span></button>` : ""}
      ${c.kind === "device" && !paired && c.metadata?.platform !== "healthkit-export" ? html`<button class="btn btn-sm" data-action="pair">${icon("phone")}<span class="btn-label">Pair again</span></button>` : ""}
      <button class="btn btn-sm btn-ghost btn-icon" data-action="menu" data-id="${c.id}" aria-haspopup="menu" aria-expanded="false"
        aria-label="More for ${c.display_name}" title="More">${icon("more")}</button>
    </div>
    ${c.last_error && !ended && ["needs_reauth", "error"].includes(c.status) ? html`<div class="src-error">${icon("alert")} ${c.last_error}</div>` : ""}
  </div>`;
}

/** What the "⋯" menu offers for a source. `act` runs one of the page's actions. */
function sourceMenu(c, devices, act) {
  const paired = devices.length > 0;
  const items = [];
  if (c.kind === "ehr") items.push({ label: "View records", icon: "records", href: `#/records?connection=${c.id}` });
  if (c.kind === "wearable" && c.mode === "simulated") {
    items.push({ label: `Connect my ${WEARABLE_SHORT[c.provider] || c.provider}`, icon: "watch", run: () => act("wearable", { provider: c.provider, mode: "live" }) });
  }
  if (c.kind === "device" && paired) items.push({ label: "Unpair", icon: "phone", run: () => act("unpair", { id: c.id, name: c.display_name }) });
  if (items.length) items.push("-");
  items.push({ label: c.kind === "device" ? "Remove…" : "Disconnect or remove…", icon: "trash", danger: true,
               run: () => act("remove", { id: c.id, name: c.display_name, kind: c.kind }) });
  return items;
}

/** Something not connected yet (a wearable, the iPhone app), offered as a row in its section. */
function offerRow(iconName, title, text, actions) {
  return html`<div class="src-row src-offer">
    ${iconAvatar(iconName)}
    <div class="src-main"><div class="src-title"><span class="src-name">${title}</span></div><div class="src-line src-blurb">${text}</div></div>
    <div class="src-actions">${actions}</div>
  </div>`;
}

// ------------------------------------------------------------------ add health system
function institutionMeta(inst) {
  return [inst.platform_label, inst.portal, inst.location].filter(Boolean).join(" · ");
}

function institutionButton(inst) {
  return html`<button class="inst ${inst.available ? "" : "unavailable"}" data-action="choose" data-id="${inst.id}">${avatar(inst.name, inst.platform)}
    <div class="grow" style="min-width:0"><div class="inst-name">${inst.name}</div>
      <div class="small muted inst-meta">${institutionMeta(inst)}</div>
      ${!inst.available ? html`<div class="tiny faint">Not available yet</div>`
        : inst.sign_in_problem ? html`<div class="tiny faint">Sign-in not working right now</div>` : ""}</div></button>`;
}

// Why a health system's sign-in failed the weekly check (scripts/check_sign_in.py), in the user's words.
function signInProblem(inst) {
  const p = inst.sign_in_problem;
  const name = esc(inst.name);
  const portal = inst.portal || "patient portal";
  const since = p.since ? new Date(`${p.since}T12:00:00`).toLocaleDateString(undefined, { month: "long", day: "numeric" }) : "";
  const checked = since ? `when we checked on ${since}` : "when we last checked";
  const when = ` ${checked[0].toUpperCase()}${checked.slice(1)},`;
  const what = {
    client_unknown: html`<b>${name} hasn't finished setting up Syntropy Health yet.</b> ${inst.platform_label} has approved the connection, but ${checked},
      ${name}'s ${portal} didn't recognize Syntropy Health yet. This usually sorts itself out within days.`,
    unreachable: html`<b>${name}'s connection server isn't responding.</b>${when} it didn't answer, so signing in will probably fail.`,
    rejected: html`<b>${name} is turning down connections.</b>${when} its ${portal} refused Syntropy Health's sign-in request.`,
  }[p.status] || html`<b>${name}'s sign-in page isn't working.</b>${when} its ${portal} showed an error instead of a sign-in page,
      so signing in will probably fail until ${name} fixes it.`;
  return html`<div class="banner warn">${icon("alert")}<div class="grow"><p>${what}</p>
    ${p.instead.length ? html`<p>Try ${p.instead.map((alt, i) => html`${i ? " or " : ""}<button class="link-btn" data-action="choose" data-id="${alt.id}">${esc(alt.name)}</button>`)} instead: it's working.</p>` : ""}
    <p>You can also import a file downloaded from ${name}'s ${portal}, or try signing in anyway.</p></div></div>`;
}

function modeExplainer(inst) {
  if (!inst.available) {
    return html`<div class="banner warn">${icon("alert")}<div class="grow"><p><b>Not available yet.</b> Syntropy Health isn't registered with
      ${inst.platform_label} yet, so ${esc(inst.name)} can't be connected directly.</p>
      <p>You can still bring in your records: download them from its ${inst.portal || "patient portal"} (look for a health summary or
      "download my record" option) and import the file.</p></div></div>`;
  }
  if (inst.mode === "simulated") {
    return html`<div class="banner warn">${icon("alert")}<div class="grow"><p><b>Simulated sign-in.</b> Syntropy Health isn't registered with ${inst.platform_label} yet,
      so this connection goes through Syntropy's built-in EHR simulator instead of ${esc(inst.name)}'s real ${inst.portal || "portal"}.</p>
      <p>You'll see a sign-in page with test patients (password <code>syntropy</code>). The full protocol — OAuth consent, PKCE token exchange,
      FHIR download, normalization — is exactly what runs for a real connection.</p></div></div>`;
  }
  if (inst.mode === "sandbox") {
    return html`<div class="banner info">${icon("shield")}<div class="grow"><p><b>${inst.platform_label} developer sandbox.</b> You'll sign in on ${inst.platform_label}'s
      sandbox with a test patient. Real patient data isn't available in this mode.</p>
      ${inst.sandbox_hint ? html`<p>${inst.sandbox_hint}</p>` : ""}</div></div>`;
  }
  return html`<div class="banner">${icon("shieldCheck")}<div class="grow"><p>You'll be taken to <b>${esc(inst.name)}</b>'s ${inst.portal || "patient portal"} to sign in and approve sharing.
    Syntropy never sees your password; records download directly to this machine.</p>
    ${inst.one_time ? html`<p><b>A one-time import.</b> ${inst.portal || inst.platform_label} doesn't let apps like Syntropy Health stay connected, so this
      brings in your records as they are today. To update them later, choose Update beside it in Sources and sign in again.</p>` : ""}</div></div>`;
}

async function openAddHealthSystem(profileId) {
  const m = modal({ title: "Connect a health system", wide: true, body: loading(3) });
  const featured = await get("/api/directory/featured");
  let current = featured.results;
  const byId = new Map(current.map((i) => [i.id, i]));

  const showList = (items, label) => {
    items.forEach((i) => byId.set(i.id, i));
    mount(m.body.querySelector("#dir-results"), items.length ? html`<div class="small muted" style="margin:4px 0 10px">${label}</div>
      <div class="inst-grid">${items.map(institutionButton)}</div>` : emptyState("No matches", "Try the organization's name, city, or portal name."));
  };
  m.setBody(html`
    <div class="search" style="margin-bottom:14px">${icon("search")}<input class="input" id="dir-q" placeholder="Search ${featured.total.toLocaleString()} health systems — e.g. Kaiser, Mayo, Cedars…" autofocus></div>
    <div id="dir-results"></div>
    <div class="divider"></div>
    <p class="small muted">Don't see yours? <button class="link-btn" data-action="custom">Connect any SMART on FHIR server</button> ·
      <button class="link-btn" data-action="import">import a record file</button></p>`);
  showList(current, "Popular");
  const input = m.body.querySelector("#dir-q");
  input.focus();
  input.addEventListener("input", debounce(async () => {
    const q = input.value.trim();
    if (!q) return showList(featured.results, "Popular");
    const res = await get("/api/directory", { q, limit: 30 });
    showList(res.results, `${res.total} match${res.total === 1 ? "" : "es"}`);
  }, 200));

  onAction(m.el, {
    choose: async ({ id }) => {
      if (!byId.has(id)) byId.set(id, await get(`/api/directory/${encodeURIComponent(id)}`));
      confirmInstitution(m, byId.get(id), profileId);
    },
    custom: () => customServer(m, profileId),
    import: () => { m.close(); location.hash = "#/sources?add=import"; },
    back: () => { m.close(); openAddHealthSystem(profileId); },
    go: async ({ id }, btn) => {
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> Preparing secure sign-in…';
      try {
        const res = await post("/api/connections/ehr", { institution_id: id, profile_id: profileId });
        window.location.assign(res.auth_url);
      } catch (err) { toast(err.message, "bad"); btn.disabled = false; btn.textContent = "Continue"; }
    },
    discover: async () => {
      const url = m.body.querySelector("#c-url").value.trim();
      const out = m.body.querySelector("#c-out");
      out.textContent = "Discovering…";
      try { const cfg = await post("/api/connections/discover", { fhir_base_url: url }); out.textContent = JSON.stringify(cfg, null, 2); }
      catch (err) { out.textContent = err.message; }
    },
    "custom-go": async (_, btn) => {
      const url = m.body.querySelector("#c-url").value.trim();
      const cid = m.body.querySelector("#c-client").value.trim();
      btn.disabled = true;
      try {
        const res = await post("/api/connections/ehr", { fhir_base_url: url, client_id: cid, profile_id: profileId });
        window.location.assign(res.auth_url);
      } catch (err) { toast(err.message, "bad"); btn.disabled = false; }
    },
  });
}

function confirmInstitution(m, inst, profileId) {
  m.setBody(html`
    <div class="stack">
      <div class="row">${avatar(inst.name, inst.platform)}<div><h2>${inst.name}</h2>
        <div class="small muted">${institutionMeta(inst)}</div></div></div>
      ${inst.available && inst.sign_in_problem ? signInProblem(inst) : modeExplainer(inst)}
      ${inst.available ? html`<div class="small muted"><b>What gets shared (read-only):</b> demographics, conditions, medications, allergies, lab results, vitals,
        immunizations, visits, procedures, clinical notes, imaging reports, care team, care plans, devices and insurance.</div>` : ""}
      <div class="row between"><button class="btn" data-action="back">Back</button>
        ${!inst.available ? html`<button class="btn btn-primary" data-action="import">Import a record file</button>`
          : inst.sign_in_problem ? html`<div class="row"><button class="btn" data-action="go" data-id="${inst.id}">Try anyway</button>
              <button class="btn btn-primary" data-action="import">Import a record file</button></div>`
          : html`<button class="btn btn-primary" data-action="go" data-id="${inst.id}">Continue</button>`}</div>
      ${inst.mode === "simulated" ? html`<p class="tiny faint">Change how ${inst.platform_label} connects in Settings → Developer.</p>` : ""}
    </div>`);
}

function customServer(m) {
  m.setBody(html`
    <div class="stack">
      <p class="muted">Connect directly to any SMART on FHIR (R4) patient-access endpoint. You need a client ID registered with that server,
        with this redirect URI: <code>${location.origin}/callback</code> (or your relay URL from Settings).</p>
      <div><label class="label">FHIR base URL</label><input class="input" id="c-url" placeholder="https://fhir.example.org/r4"></div>
      <div><label class="label">Client ID</label><input class="input" id="c-client"></div>
      <div class="row"><button class="btn" data-action="discover">Test discovery</button><span class="grow"></span>
        <button class="btn" data-action="back">Back</button><button class="btn btn-primary" data-action="custom-go">Connect</button></div>
      <pre class="raw" id="c-out" style="min-height:40px"></pre>
    </div>`);
}

// ------------------------------------------------------------------ pairing & imports
async function openPairing(profile) {
  const m = modal({ title: "Pair a phone", body: loading(2) });
  const [res, net] = await Promise.all([
    post("/api/devices/pairing-code", { profile_id: profile.id }),
    networkStatus().catch(() => null),
  ]);
  const link = `syntropyhealth://pair?server=${encodeURIComponent(res.server_url)}&code=${encodeURIComponent(res.code)}`;
  // The Mac and Windows apps start out reachable only from this computer; the phone can't connect until that changes.
  const blocked = net && !net.on_network && /^https?:\/\/(localhost|127\.0\.0\.1)(:|\/|$)/.test(res.server_url);
  const away = net?.addresses.find((a) => a.kind === "tailscale");
  m.setBody(html`
    <div class="stack">
      ${blocked ? html`<div class="banner warn">${icon("phone")}<div class="grow"><p><b>Your phone can't reach this computer yet.</b></p>
          <div id="pair-net"></div></div></div>` : ""}
      <p class="muted">The Syntropy Health apps send your phone's health data straight to this server over your network, with no
        cloud in between: Apple Health from the iPhone app (heart, HRV, sleep stages, steps, workouts with routes, symptoms,
        cycle tracking, ECG and more), and Health Connect from the Android app (Fitbit, Pixel Watch, Samsung Health, Oura and
        other apps that write to it).</p>
      <ol class="small" style="margin:0;padding-left:18px;line-height:1.8">
        <li>In the app, open <b>Settings</b> → <b>Connect to my server</b>.</li>
        <li>Server address: <code>${res.server_url}</code></li>
        <li>Sign in with your password, or choose to use a pairing code and enter:</li></ol>
      <div class="code-box">${res.code}</div>
      ${away ? html`<p class="small muted">Away from home: in the app's <b>Settings</b> → <b>Addresses</b> → <b>Away from home</b>, enter <code>${away.url}</code> to sync over Tailscale.</p>` : ""}
      ${embedded ? "" : html`<a class="btn" href="${link}" style="align-self:center">${icon("phone")} Open in the app (when viewing this page on the phone)</a>`}
      <p class="small muted" style="text-align:center">Data is saved to <b>${profile.name}</b>. The code expires in 10 minutes and works once.
        Pairing the same phone again picks up where it left off.</p>
    </div>`);
  const panel = m.body.querySelector("#pair-net");
  if (panel && net) renderNetworkPanel(panel, net, { onChange: () => { m.close(); openPairing(profile); } });
}

function importZones() {
  return html`<div class="src-drops" id="import-card">
      <label class="drop-zone" data-drop="apple">
        ${icon("upload")}<div><b>Apple Health export</b><div class="small muted">Health app → your photo → Export All Health Data → <code>export.zip</code>. Includes Health Records if present.</div></div>
        <input type="file" id="imp-apple" accept=".zip,.xml" hidden></label>
      <label class="drop-zone" data-drop="fhir">
        ${icon("records")}<div><b>FHIR record file</b><div class="small muted">A FHIR R4 Bundle (.json) or bulk-data NDJSON downloaded from a portal or app.</div></div>
        <input type="file" id="imp-fhir" accept=".json,.ndjson,application/json" hidden></label>
    </div>
    <div id="imp-status" class="src-drop-status"></div>`;
}

async function uploadApple(file, profile, statusEl, refresh) {
  const form = new FormData();
  form.append("file", file);
  form.append("profile_id", profile.id);
  mount(statusEl, html`<div class="small">Uploading ${file.name}…</div><div class="progress"><div style="width:15%"></div></div>`);
  const { job_id } = await api("/api/imports/apple-health", { method: "POST", form });
  for (;;) {
    await new Promise((r) => setTimeout(r, 800));
    const job = await get(`/api/imports/${job_id}`, undefined, { fresh: true });
    if (job.status === "error") { mount(statusEl, html`<div class="banner bad">${job.error}</div>`); return; }
    if (job.status === "completed") {
      const r = job.result || {};
      mount(statusEl, html`<div class="banner info">${icon("check")}<div class="grow">Imported ${plural(r.inserted || 0, "new sample")} (${plural(r.samples || 0, "sample")} read)
        ${r.clinical ? `and ${plural(r.clinical.records, "clinical record")}` : ""}.</div></div>`);
      refresh();
      return;
    }
    mount(statusEl, html`<div class="small">Importing… ${plural(job.processed, "sample")} processed</div><div class="progress"><div style="width:60%"></div></div>`);
  }
}

// ------------------------------------------------------------------ view
/** A section of Sources: one panel with a heading (and its action) over a list of rows. */
function group(title, hint, action, body) {
  return html`<section class="card src-group">
    <header class="src-group-head"><div><h2>${title}</h2>${hint ? html`<p>${hint}</p>` : ""}</div>${action}</header>
    ${body}
  </section>`;
}

export async function render({ el, params, state }) {
  const profile = state.profile;
  mount(el, loading(4));
  let pollTimer = null;
  let shown = { all: [], devices: [] };   // what the page is showing, for the "⋯" menus

  const refresh = async (fresh = false) => {
    const [conns, devs] = await Promise.all([
      get("/api/connections", { profile: profile.id, include_disconnected: true }, { fresh }),
      get("/api/devices", { profile: profile.id }, { fresh }),
    ]);
    const all = conns.connections;
    const devicesFor = (c) => devs.devices.filter((d) => d.connection_id === c.id);
    shown = { all, devices: devs.devices };
    // Sources removed with "keep data" stay listed (muted) so their data can still be found and deleted.
    const ehr = all.filter((c) => c.kind === "ehr");
    const wear = all.filter((c) => c.kind === "wearable");
    const phones = all.filter((c) => c.kind === "device");
    const files = all.filter((c) => c.kind === "import");
    const active = all.filter((c) => c.status !== "disconnected" || devicesFor(c).length);
    const attention = active.filter((c) => !c.access_ended && ["needs_reauth", "error"].includes(c.status));
    const lastSync = active.map((c) => c.last_sync_at).filter(Boolean).sort().pop();

    mount(el, html`
      <div class="page-head"><div><h1>Sources</h1><p>Where ${profile.name}'s data comes from. Everything syncs into the database on this machine.</p></div>
        <button class="btn btn-primary" data-action="add" aria-haspopup="menu" aria-expanded="false">${icon("plus")} Add a source</button></div>
      ${all.length ? html`<div class="src-summary">
        <span><b>${active.length}</b> ${active.length === 1 ? "source" : "sources"} connected</span>
        ${attention.length ? html`<span class="warn"><span class="dot warn"></span> ${plural(attention.length, "needs", "need")} attention</span>` : ""}
        ${lastSync ? html`<span>Last sync ${fmtAgo(lastSync)}</span>` : ""}
      </div>` : ""}
      <div id="src-banner"></div>

      ${group("Phones & watches", "Apple Health and Health Connect data from the Syntropy phone apps, sent over your own network.",
        phones.length ? html`<button class="btn btn-sm" data-action="pair">${icon("phone")} Pair phone</button>` : "",
        html`<div class="src-rows">${phones.length ? phones.map((c) => sourceRow(c, devicesFor(c))) : html`
          ${offerRow("phone", "Syntropy Health iPhone app", "Apple Health from iPhone and Apple Watch: heart, HRV, sleep stages, steps, workouts with routes, symptoms, cycle tracking, ECG and more, synced in the background.", html`
            <button class="btn btn-sm btn-primary" data-action="pair">${icon("phone")} Pair iPhone</button>
            <a class="btn btn-sm btn-ghost" href="https://health.syntropylabs.io/setup/" target="_blank" rel="noopener">Get the app</a>`)}
          ${offerRow("phone", "Syntropy Health Android app", "Health Connect from Fitbit, Pixel Watch, Samsung Health, Oura and other apps: heart, HRV, sleep stages, steps, workouts and more, synced in the background.", html`
            <button class="btn btn-sm btn-primary" data-action="pair">${icon("phone")} Pair Android phone</button>`)}`}</div>`)}

      ${group("Wearables", "Oura, WHOOP and Google Health (Fitbit, Pixel Watch) sync from their cloud every few hours. Signing in passes through Syntropy Labs' sign-in relay, which completes the sign-in and keeps nothing.", "",
        html`<div class="src-rows">${WEARABLES.map(([provider, label, blurb]) => {
          const mine = wear.filter((c) => c.provider === provider);
          const connected = mine.some((c) => c.status !== "disconnected");
          const simulated = mine.some((c) => c.mode === "simulated");
          return html`${mine.map((c) => sourceRow(c))}${connected ? "" : offerRow("watch", label, blurb, html`
            <button class="btn btn-sm btn-primary" data-action="wearable" data-provider="${provider}" data-mode="live">Connect ${label}</button>
            ${simulated ? "" : html`<button class="btn btn-sm btn-ghost" data-action="wearable" data-provider="${provider}" data-mode="simulated">Try simulated data</button>`}`)}`;
        })}</div>`)}

      ${group("Medical records", "Your history from hospitals and clinics, through their patient portals.",
        html`<button class="btn btn-sm" data-action="add-ehr">${icon("plus")} Connect a health system</button>`,
        ehr.length ? html`<div class="src-rows">${ehr.map((c) => sourceRow(c))}</div>`
          : html`<div class="src-empty">No health systems connected yet. Search 20,000+ US health systems and practices that offer patient access.</div>`)}

      ${group("Files", "One-off imports of an Apple Health export or a record file from a portal. Drop a file on a box, or click to choose one.", "",
        html`${importZones()}${files.length ? html`<div class="src-rows">${files.map((c) => sourceRow(c))}</div>` : ""}`)}
    `);
    wireUploads();
    renderBanner();
    clearTimeout(pollTimer);
    if (all.some((c) => c.syncing)) pollTimer = setTimeout(() => refresh(true), 1500);
  };

  let bannerShown = false;
  const renderBanner = () => {
    if (bannerShown) return;
    const box = $("#src-banner", el);
    if (params.get("error")) {
      mount(box, html`<div class="banner bad">${icon("alert")}<div class="grow"><b>The connection didn't complete.</b><p>${params.get("error")}</p></div></div>`);
      bannerShown = true;
    } else if (params.get("connected")) {
      toast("Connected. Downloading records…", "good");
      bannerShown = true;
    }
    history.replaceState(null, "", "#/sources");
  };

  const importFhir = async (file, status) => {
    const form = new FormData();
    form.append("file", file);
    form.append("profile_id", profile.id);
    mount(status, html`<div class="small"><span class="spinner"></span> Importing ${file.name}…</div>`);
    try {
      const r = await api("/api/imports/fhir", { method: "POST", form });
      toast(`Imported ${plural(r.records, "record")}`, "good");
      await refresh();
    } catch (err) { mount(status, html`<div class="banner bad">${err.message}</div>`); }
  };
  const importApple = async (file, status) => {
    try { await uploadApple(file, profile, status, refresh); } catch (err) { mount(status, html`<div class="banner bad">${err.message}</div>`); }
  };

  const wireUploads = () => {
    const status = $("#imp-status", el);
    $("#imp-apple", el)?.addEventListener("change", (e) => { if (e.target.files[0]) importApple(e.target.files[0], status); });
    $("#imp-fhir", el)?.addEventListener("change", (e) => { if (e.target.files[0]) importFhir(e.target.files[0], status); });
    // Drag a file onto either box.
    for (const zone of el.querySelectorAll("[data-drop]")) {
      zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
      zone.addEventListener("dragleave", () => zone.classList.remove("over"));
      zone.addEventListener("drop", (e) => {
        e.preventDefault();
        zone.classList.remove("over");
        const file = e.dataTransfer.files[0];
        if (file) (zone.dataset.drop === "apple" ? importApple : importFhir)(file, status);
      });
    }
  };

  await refresh();
  if (params.get("add") === "ehr") openAddHealthSystem(profile.id);
  if (params.get("add") === "iphone" || params.get("add") === "phone") openPairing(profile);
  if (params.get("add") === "import") $("#import-card", el)?.scrollIntoView({ behavior: "smooth" });

  const handlers = {
    "add-ehr": () => openAddHealthSystem(profile.id),
    add: (_, btn) => dropdown(btn, [
      { label: "Phone & watch", icon: "phone", run: () => handlers.pair() },
      ...WEARABLES.map(([provider, label]) => ({ label, icon: "watch", run: () => handlers.wearable({ provider, mode: "live" }) })),
      "-",
      { label: "Hospital or clinic", icon: "building", run: () => openAddHealthSystem(profile.id) },
      { label: "Import a file", icon: "upload", run: () => $("#import-card", el)?.scrollIntoView({ behavior: "smooth" }) },
    ]),
    menu: ({ id }, btn) => {
      const c = shown.all.find((x) => x.id === id);
      if (c) dropdown(btn, sourceMenu(c, shown.devices.filter((d) => d.connection_id === id), (name, data) => handlers[name](data, btn)));
    },
    sync: async ({ id }, btn) => {
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> Syncing…';
      const res = await post(`/api/connections/${id}/sync`);
      if (res.status === "error") toast(res.error, "bad");
      else if (res.status === "skipped") toast(res.reason);
      else toast(`Synced · ${plural(res.inserted || 0, "new item")}`, "good");
      await refresh();
    },
    reconnect: async ({ id }) => {
      const res = await post(`/api/connections/${id}/reconnect`);
      window.location.assign(res.auth_url);
    },
    remove: async ({ id, name, kind }) => {
      const phone = kind === "device";
      const m = modal({
        title: `Remove ${name}?`,
        body: html`<p>${phone ? "Unpairing stops the phone sending data" : "Disconnecting stops syncing"} and keeps what it already brought in;
          the source stays listed so you can reconnect or delete it later. Removing it also deletes its data from this machine.</p>`,
        footer: html`<button class="btn" data-keep>${phone ? "Unpair, keep data" : "Disconnect, keep data"}</button><button class="btn btn-danger" data-wipe>Remove and delete data</button>`,
      });
      m.el.querySelector("[data-keep]").addEventListener("click", async () => { m.close(); await del(`/api/connections/${id}`); toast(phone ? "Unpaired. Its data is kept." : "Disconnected. Its data is kept."); refresh(); });
      m.el.querySelector("[data-wipe]").addEventListener("click", async () => { m.close(); await del(`/api/connections/${id}`, { delete_data: true }); toast("Removed with its data"); refresh(); });
    },
    wearable: async ({ provider, mode }) => {
      const res = await post("/api/connections/wearable", { provider, mode, profile_id: profile.id });
      if (res.auth_url) window.location.assign(res.auth_url);
      else { toast("Simulated wearable connected — generating 90 days of data…", "good"); refresh(); }
    },
    pair: async () => { await openPairing(profile); },
    unpair: async ({ id, name }) => {
      if (!(await confirmDialog(`Unpair ${name}?`, "The phone stops sending data until it's paired again. What it already sent is kept.", { confirmLabel: "Unpair", danger: true }))) return;
      const devices = (await get("/api/devices", { profile: profile.id }, { fresh: true })).devices.filter((d) => d.connection_id === id);
      await Promise.all(devices.map((d) => del(`/api/devices/${d.id}`)));
      toast("Unpaired");
      refresh();
    },
  };
  const off = onAction(el, handlers);
  return () => { off(); clearTimeout(pollTimer); };
}
