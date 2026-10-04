import { del, get, patch, post, put } from "../api.js";
import { ACCENTS, applyAccent, currentAccent, embedded, refreshProfiles, setAccent, setProfile, setTheme, state as appState, themePreference } from "../app.js";
import { regionSystem, setUnitPreference, unitPreference } from "../units.js";
import { renderAiTab } from "./settings-ai.js";
import { addressList, networkStatus, renderNetworkPanel } from "../network.js";
import { renderUpdates } from "../updates.js";
import { PRIVACY_URL, SOURCE_URL, TERMS_URL } from "../consent.js";
import {
  $, capitalize, confirmDialog, fmtDateTime, html, icon, initials, loading, modal, mount, onAction, plural, toast,
} from "../ui.js";

// Settings sections, in the order the side navigation lists them. Those marked owner change the server for everyone.
const ALL_SECTIONS = [
  ["Preferences", [["general", "General", "settings"], ["people", "People", "users"], ["wearables", "Wearables", "watch", "owner"]]],
  ["Integrations", [["ai", "AI", "sparkles"]]],
  ["Privacy", [["security", "Security", "shield"], ["data", "Your data", "download"], ["log", "Access log", "list"]]],
  ["Advanced", [["developer", "Developer", "code", "owner"]]],
];
/** The server's owner (or, with no accounts while developing, whoever opens it). */
const isOwner = () => appState.status?.account?.owner !== false;
const sectionsFor = () => ALL_SECTIONS.map(([g, tabs]) => [g, tabs.filter((t) => !t[3] || isOwner())]).filter(([, tabs]) => tabs.length);
// Earlier names of these sections (links, bookmarks).
const RENAMED = { profiles: "people", platforms: "developer", sync: "general", activity: "log" };
const MODE_TEXT = {
  simulated: "Built-in simulator — works today without vendor registration.",
  sandbox: "Vendor developer sandbox — test patients only; requires a sandbox client ID.",
  production: "Real patient portals — requires a production client ID approved by the vendor.",
};

/** A settings row: what it is on the left, the control on the right (stacked on phones). */
function row(title, hint, control) {
  return html`<div class="set-row"><div class="set-label"><div class="set-title">${title}</div>${hint ? html`<div class="set-hint">${hint}</div>` : ""}</div>
    <div class="set-control">${control}</div></div>`;
}
function section(title, hint, content, extra = "") {
  return html`<section class="card set-card ${extra}"><header><h3>${title}</h3>${hint ? html`<p>${hint}</p>` : ""}</header>${content}</section>`;
}

export async function render({ el, parts, state, navigate }) {
  if (RENAMED[parts[0]]) return navigate(`#/settings/${RENAMED[parts[0]]}`);
  const SECTIONS = sectionsFor();
  const TABS = SECTIONS.flatMap(([, tabs]) => tabs);
  const tab = TABS.some(([k]) => k === parts[0]) ? parts[0] : "general";
  const label = TABS.find(([k]) => k === tab)[1];
  mount(el, html`
    <div class="page-head"><div><h1>Settings</h1><p>${isOwner() ? "Stored in this instance's own database, on this machine."
      : html`Your own settings. Settings that affect everyone here are changed by ${ownerName()}, the server's owner.`}</p></div></div>
    <div class="settings-layout">
      <nav class="settings-nav" aria-label="Settings sections">${SECTIONS.map(([group, tabs]) => html`<div class="settings-group">
        <div class="nav-label">${group}</div>
        ${tabs.map(([k, name, ic]) => html`<a class="${k === tab ? "active" : ""}" href="#/settings/${k}" ${k === tab ? html`aria-current="page"` : ""}>${icon(ic)}<span>${name}</span></a>`)}
      </div>`)}</nav>
      <div class="settings-body"><h2 class="settings-title">${label}</h2><div id="set-body">${loading(3)}</div></div>
    </div>`);
  // On a phone the sections are a row that scrolls sideways: bring the chosen one into view once the page is on screen
  // (it's drawn before it's shown, when there's nothing to scroll yet).
  const showChosen = (tries = 0) => {
    const nav = $(".settings-nav", el), chosen = $(".settings-nav a.active", el);
    if (!nav || !chosen) return;
    if (!el.isConnected) { if (tries < 120) requestAnimationFrame(() => showChosen(tries + 1)); return; }
    if (nav.scrollWidth > nav.clientWidth) nav.scrollLeft = chosen.offsetLeft - nav.offsetLeft - (nav.clientWidth - chosen.offsetWidth) / 2;
  };
  showChosen();
  const body = $("#set-body", el);
  const settings = await get("/api/settings");
  const redraw = () => navigate(`#/settings/${tab}`);

  if (tab === "ai") return renderAiTab(body, { state, redraw });

  if (tab === "general") {
    const { timezones } = await get("/api/settings/timezones");
    const pref = themePreference();
    const unitPref = unitPreference() || "";
    mount(body, html`<div class="stack">
      ${section("Appearance", "", html`
        ${embedded ? "" : row("Theme", "System follows your device's light or dark setting.",
          html`<div class="segmented" role="group" aria-label="Theme">${[["system", "System"], ["light", "Light"], ["dark", "Dark"]].map(([k, name]) =>
            html`<button class="${k === pref ? "active" : ""}" data-action="theme" data-theme="${k}" aria-pressed="${k === pref}">${name}</button>`)}</div>`)}
        ${row("Accent color", "Buttons, highlights and charts, on every device: here, the desktop app and the iPhone app.",
          html`<div class="swatches" role="radiogroup" aria-label="Accent color">${ACCENTS.map(([k, name, light, dark]) => html`
            <button class="swatch ${k === currentAccent() ? "active" : ""}" role="radio" aria-checked="${k === currentAccent()}" data-action="accent"
              data-accent="${k}" title="${name}" aria-label="${name}" style="--sw:${light};--sw-dark:${dark}"></button>`)}</div>`)}`)}
      ${section("Region", "", html`
        ${row("Units", html`How weight, height, distance, temperature and fluids are shown. Lab results keep the units the lab reported.`,
          html`<div class="segmented" role="group" aria-label="Units">${[["", `Automatic · ${regionSystem() === "us" ? "US" : "Metric"}`], ["metric", "Metric"], ["us", "US"]].map(([k, name]) =>
            html`<button class="${k === unitPref ? "active" : ""}" data-action="units" data-units="${k}" aria-pressed="${k === unitPref}">${name}</button>`)}</div>`)}
        ${row("Time zone", isOwner() ? "Wearable data is grouped into days in this time zone." : "Wearable data is grouped into days in this time zone, set by the server's owner.",
          isOwner() ? html`<select class="input" id="g-tz" data-save="timezone">${timezones.map((tz) => html`<option ${tz === settings.timezone ? "selected" : ""}>${tz}</option>`)}</select>`
            : html`<span>${settings.timezone}</span>`)}`)}
      ${isOwner() ? section("Background sync", settings.sync.scheduler_enabled ? "New data is fetched automatically while Syntropy Health is running."
        : html`<span style="color:var(--warn)">Background sync is off on this instance (SYNTROPY_SCHEDULER=false). Use Sync now on Sources.</span>`, html`
        ${row("Health records", "How often to check connected health systems.",
          html`<div class="input-unit"><input class="input" id="g-ehr" data-save="ehr_interval_hours" type="number" min="1" value="${settings.sync.ehr_interval_hours}"><span>hours</span></div>`)}
        ${row("Wearables", "How often to fetch from Oura, WHOOP and Google Health. The iPhone app sends its data on its own.",
          html`<div class="input-unit"><input class="input" id="g-wear" data-save="wearable_interval_hours" type="number" min="0.5" step="0.5" value="${settings.sync.wearable_interval_hours}"><span>hours</span></div>`)}`) : ""}
      ${isOwner() ? section("Updates", "", html`<div id="upd-panel" class="upd-panel"><div class="skeleton" style="height:64px"></div></div>`, "upd-card") : ""}
      ${section("About", "", html`
        ${row("Getting started", "Walk through connecting your other devices, your data and an AI again.",
          html`<button class="btn btn-sm" data-action="onboarding">Show the setup steps</button>`)}
        ${row("Version", "", html`<span class="mono">${settings.version}</span>`)}
        ${row("Source code", "Free and open source under the GNU AGPL-3.0.",
          html`<a href="${SOURCE_URL}" target="_blank" rel="noopener">View on GitHub</a>`)}
        ${row("Your responsibility", "Your records are stored only on this computer and in your backups, not with Syntropy Labs. Keeping them secure and backed up is up to you. Syntropy Health isn't medical advice.",
          html`<span class="row" style="gap:12px"><a href="${TERMS_URL}" target="_blank" rel="noopener">Terms</a><a href="${PRIVACY_URL}" target="_blank" rel="noopener">Privacy</a></span>`)}
        ${isOwner() ? row("Data folder", "Back this folder up, or download a backup from Your data.", html`<code>${settings.data_dir}</code>`) : ""}`)}
    </div>`);
    const updPanel = $("#upd-panel", body);
    if (updPanel) {
      // From the alert: once the page is shown (and scrolled to the top), bring the section into view.
      const reveal = (node, frames = 120) => {
        if (node.isConnected) requestAnimationFrame(() => node.scrollIntoView({ block: "start" }));
        else if (frames) requestAnimationFrame(() => reveal(node, frames - 1));
      };
      renderUpdates(updPanel).then(() => { if (parts[1] === "updates") reveal(updPanel.closest("section")); });
    }
    const save = async (e) => {
      const input = e.target.closest("[data-save]");
      if (!input) return;
      const key = input.dataset.save;
      const value = input.type === "number" ? Number(input.value) : input.value;
      if (input.type === "number" && !(value >= Number(input.min))) { toast(`Use at least ${input.min} hours.`, "bad"); return; }
      try { await put("/api/settings/general", { [key]: value }); toast("Saved"); }
      catch (err) { toast(err.message, "bad"); }
    };
    body.addEventListener("change", save);
    const off = onAction(body, {
      theme: ({ theme }) => {
        for (const b of body.querySelectorAll("[data-action=theme]")) {
          b.classList.toggle("active", b.dataset.theme === theme);
          b.setAttribute("aria-pressed", String(b.dataset.theme === theme));
        }
        setTheme(theme);
      },
      accent: async ({ accent }) => {
        const mark = (id) => body.querySelectorAll("[data-action=accent]").forEach((b) => {
          b.classList.toggle("active", b.dataset.accent === id);
          b.setAttribute("aria-checked", String(b.dataset.accent === id));
        });
        const before = currentAccent();
        mark(accent);
        try { await setAccent(accent); } catch (err) { toast(err.message, "bad"); applyAccent(before); mark(before); }
      },
      onboarding: async () => { await post("/api/onboarding/restart"); location.hash = "#/"; location.reload(); },
      units: async ({ units }) => {
        await put("/api/preferences", { units });
        setUnitPreference(units);
        if (appState.status) appState.status.preferences = { ...(appState.status.preferences || {}), units: units || null };
        toast(units ? `Showing ${units === "us" ? "US" : "metric"} units` : "Units follow your region");
        redraw();
      },
    });
    return () => { off(); body.removeEventListener("change", save); };
  }

  if (tab === "people") return renderPeople(body, { redraw, state });

  if (tab === "wearables") {
    const so = settings.source_order;
    const anyCreds = Object.values(settings.wearables).some((w) => w.client_id || w.client_secret_set);
    mount(body, html`<div class="stack">
      ${section("Device priority", "When an Apple Watch, iPhone and ring or strap measure the same thing on the same day, the first kind in each list is used. For sleep, a device that only caught part of the night is passed over for one that recorded all of it. Simulated data is only used when nothing real was recorded.", html`
        <div class="grid grid-3" style="padding:4px 18px 18px">${Object.entries(so.groups).map(([group, g]) => html`<div>
          <div class="label">${g.label}</div>
          <ol class="order-list">${so.orders[group].map((kind, i, all) => html`<li>
            <span class="grow">${so.kinds[kind]}</span>
            <button class="btn btn-ghost btn-icon btn-sm" data-action="move" data-group="${group}" data-i="${i}" data-d="-1"
              ${i === 0 ? "disabled" : ""} aria-label="Move ${so.kinds[kind]} up">${icon("arrowUp")}</button>
            <button class="btn btn-ghost btn-icon btn-sm" data-action="move" data-group="${group}" data-i="${i}" data-d="1"
              ${i === all.length - 1 ? "disabled" : ""} aria-label="Move ${so.kinds[kind]} down">${icon("arrowDown")}</button></li>`)}</ol>
          <div class="hint">${g.hint}</div></div>`)}</div>`)}
      ${section("Your own Oura, WHOOP or Google app", "Optional for Oura and WHOOP, whose client secret the Syntropy relay holds by default (it hands tokens straight back to this instance and keeps no copy). Required for Google Health for now: create an OAuth client in Google Cloud with the Google Health API enabled.", html`
        <details class="set-details" ${anyCreds ? "open" : ""}><summary>Use my own developer credentials</summary>
          <p class="small muted" style="margin:0 0 12px">Register an app with Oura, WHOOP or Google Cloud, enter its client ID and secret, and set the wearable
            redirect URI (Developer) to the one you registered: <code>${location.origin}/callback</code>, or <code>${settings.relay.default_redirect_uri}</code>
            if the provider only accepts https. Tokens then never touch the relay.</p>
          ${Object.entries(settings.wearables).map(([key, w]) => html`<div class="set-subcard">
            <h4>${w.label}</h4>
            <div class="grid grid-2">
              <div><label class="label" for="w-id-${key}">Client ID</label><input class="input" id="w-id-${key}" value="${w.client_id}"></div>
              <div><label class="label" for="w-secret-${key}">Client secret ${w.client_secret_set ? html`<span class="badge good">Saved</span>` : ""}</label>
                <input class="input" id="w-secret-${key}" type="password" autocomplete="off" placeholder="${w.client_secret_set ? "•••••••• (unchanged)" : "Only for your own app"}"></div>
            </div>
            <div style="margin-top:10px"><button class="btn btn-sm" data-action="save-wearable" data-key="${key}">Save ${w.label}</button></div></div>`)}
        </details>`)}
    </div>`);
    return onAction(body, {
      move: async ({ group, i, d }) => {
        const order = [...so.orders[group]];
        const a = Number(i), b = a + Number(d);
        [order[a], order[b]] = [order[b], order[a]];
        await put("/api/settings/source-order", { [group]: order });
        toast("Saved — charts use the new order right away"); redraw();
      },
      "save-wearable": async ({ key }) => {
        const secret = $(`#w-secret-${key}`, body).value;
        await put(`/api/settings/wearables/${key}`, { client_id: $(`#w-id-${key}`, body).value.trim(), ...(secret ? { client_secret: secret } : {}) });
        toast("Saved"); redraw();
      },
    });
  }

  if (tab === "security") {
    const { sessions } = await get("/api/auth/sessions");
    const on = settings.auth_required;
    mount(body, html`<div class="stack">
      ${section(household() ? "Your password" : "Password", on ? `Needed to open Syntropy Health${household() ? ` as ${appState.status.account.name}` : " in a browser"}. Changing it signs out your other browsers.`
        : html`<span style="color:var(--warn)">No password is set: anyone who can reach this server can open your records.</span>`, html`
        <form class="set-form" id="pw-form">
          ${on ? row("Current password", "", html`<input class="input" type="password" id="pw-cur" autocomplete="current-password">`) : ""}
          ${row(on ? "New password" : "Password", isOwner() ? "At least 8 characters. Keep it in your password manager: to reset it, you'd need the command line on this computer."
            : html`At least 8 characters. Keep it in your password manager. If you forget it, ${ownerName()} can reset it from the command line on the computer this server runs on.`,
            html`<input class="input" type="password" id="pw-new" minlength="8" autocomplete="new-password">`)}
          <div class="set-foot"><button class="btn btn-primary btn-sm" type="submit">${on ? "Change password" : "Set password"}</button>
            ${on && appState.status?.password_optional ? html`<button class="btn btn-ghost btn-danger btn-sm" type="button" data-action="remove-pass">Remove password (development)</button>` : ""}</div>
        </form>`)}
      ${section("Other devices", "Your iPhone, other computers, and your devices on Tailscale.", html`<div id="net-panel" class="set-pad"><div class="skeleton" style="height:64px"></div></div>`)}
      ${section(household() ? "Where you're signed in" : "Signed-in browsers", "", html`<div class="list">${sessions.map((s) => html`<div class="list-item small">
          <span class="ev-icon">${icon(s.device_name ? "phone" : "monitor")}</span><div class="grow truncate">${s.device_name ? `The iPhone app (${s.device_name})` : browserName(s.user_agent)}
          <div class="meta">${s.ip} · last active ${fmtDateTime(s.last_seen_at)}</div></div></div>`)}
          ${sessions.length ? "" : html`<div class="list-item small muted">No browser sessions.</div>`}</div>
        <div class="set-foot small muted">Phones and agents use their own tokens: see Sources → iPhone and Settings → AI.</div>`, "flush")}
      ${section("How your data is protected", "", html`<ul class="check-list">
          <li>Everything lives in <code>${settings.data_dir}</code> on this machine.</li>
          <li>Sign-in tokens and secrets are encrypted at rest (key in <code>secret.key</code> or <code>SYNTROPY_SECRET_KEY</code>).</li>
          <li>Health record connections use PKCE; the relay only ever sees a one-time code.</li>
          <li>Phones and agents use individual tokens you can revoke.</li>
          <li>Every sign-in, sync and export is recorded in the <a href="#/settings/log">access log</a>.</li></ul>`)}
    </div>`);
    networkStatus().then((net) => {
      const panel = $("#net-panel", body);
      if (!panel) return;
      if (isOwner()) renderNetworkPanel(panel, net, { onChange: () => redraw() });
      else mount(panel, net.on_network ? html`<p class="small muted" style="margin:0 0 10px">Open Syntropy Health from your other devices at:</p>${addressList(net)}`
        : html`<p class="small muted">Only the computer this server runs on can open it right now. ${ownerName()} can change that.</p>`);
    }).catch((err) => mount($("#net-panel", body), html`<p class="small muted">${err.message}</p>`));
    $("#pw-form", body).addEventListener("submit", async (e) => {
      e.preventDefault();
      const next = $("#pw-new", body).value;
      if (next.length < 8) return toast("Use at least 8 characters", "bad");
      try {
        await post("/api/auth/password", { current: $("#pw-cur", body)?.value, new: next });
        toast("Password updated", "good"); redraw();
      } catch (err) { toast(err.message, "bad"); }
    });
    return onAction(body, {
      "remove-pass": async () => {
        if (!(await confirmDialog("Remove password?", "Anyone who can reach this server will be able to open your records.", { confirmLabel: "Remove", danger: true }))) return;
        await post("/api/auth/password", { current: $("#pw-cur", body).value, new: null });
        toast("Password removed"); redraw();
      },
    });
  }

  if (tab === "data") {
    const p = state.profile;
    const dl = (href) => html`<a class="btn btn-sm" href="${href}" download>${icon("download")} Download</a>`;
    mount(body, html`<div class="stack">
      ${section(`Export ${p.name}'s data`, "Files download straight from this machine.", html`
        ${row("Visit summary", "A one-page summary to print or save as PDF for appointments.", html`<a class="btn btn-sm" href="#/report">${icon("printer")} Open</a>`)}
        ${row("FHIR R4 bundle", "Every clinical record in the standard format other apps and portals read.", dl(`/api/export/fhir?profile=${p.id}`))}
        ${row("Records (CSV)", "Conditions, medications, labs, visits and more, one row each.", dl(`/api/export/csv?kind=records&profile=${p.id}`))}
        ${row("Daily wearable data (CSV)", "One row per metric per day.", dl(`/api/export/csv?kind=daily&profile=${p.id}`))}
        ${row("Workouts (CSV)", "", dl(`/api/export/csv?kind=workouts&profile=${p.id}`))}
        ${row("Health journal (CSV)", "Symptoms, cycle tracking, mood and more.", dl(`/api/export/csv?kind=events&profile=${p.id}`))}`)}
      ${!isOwner() ? "" : section("Backup", "", row("Database backup", html`A complete copy of everything, for all people. Store it somewhere only you can reach: it holds your health records unencrypted (sign-in tokens in it stay encrypted; keep <code>secret.key</code> with it to restore).`,
        html`<a class="btn btn-sm btn-primary" href="/api/export/backup" download>${icon("download")} Download backup</a>`))}
      ${p.access !== "manage" ? "" : section("Delete data", "", row(`Delete all of ${p.name}'s data`, "Removes their sources, records, wearable data and paired devices from this machine. This can't be undone.",
        html`<button class="btn btn-sm btn-danger" data-action="wipe">Delete…</button>`), "danger")}
    </div>`);
    return onAction(body, {
      wipe: async () => {
        if (await confirmDialog(`Delete all of ${p.name}'s data?`, "Records, samples, sources and devices will be permanently deleted. Consider downloading a backup first.", { confirmLabel: "Delete permanently", danger: true })) {
          await del(`/api/profiles/${p.id}/data`); toast("Deleted"); redraw();
        }
      },
    });
  }

  if (tab === "log") {
    const { events } = await get("/api/audit", { limit: 200 });
    mount(body, html`<p class="small muted" style="margin:-4px 0 12px">${isOwner() ? "Every sign-in, sync, export and assistant lookup on this instance, newest first."
      : "Sign-ins, syncs, exports and assistant lookups for you and the people you can see, newest first."}</p>
      <div class="card"><div class="table-wrap"><table class="table"><thead><tr><th>When</th><th>Who</th><th>What</th><th>Details</th></tr></thead>
      <tbody>${events.map((e) => html`<tr><td class="small" style="white-space:nowrap">${fmtDateTime(e.at)}</td><td class="small">${e.who || e.actor}</td><td>${e.action}</td>
        <td class="small muted" style="max-width:420px;word-break:break-word">${auditDetail(e.detail)}</td></tr>`)}</tbody></table></div></div>`);
    return null;
  }

  if (tab === "developer") {
    const dev = settings.developer_mode;
    const cidField = (p, slot) => {
      const c = p.client_ids[slot];
      const using = c.source === "env" ? `Using ${c.client_id} from the environment (.env).`
        : c.source === "default" ? "Using Syntropy Health's registered client ID." : "";
      return html`<div class="cid-field" style="margin-top:10px"><label class="label" for="cid-${slot}-${p.platform}">${capitalize(slot)} client ID</label>
        <div class="row wrap"><input class="input input-sm grow" style="min-width:200px" id="cid-${slot}-${p.platform}" placeholder="${c.source && c.source !== "settings" ? c.client_id : "Client ID"}"
          value="${c.source === "settings" ? c.client_id : ""}">
          <button class="btn btn-sm" data-action="save-cid" data-platform="${p.platform}" data-slot="${slot}">Save</button>
          <button class="btn btn-sm btn-ghost" data-action="verify" data-platform="${p.platform}" data-slot="${slot}" ${c.client_id ? "" : "disabled"}>Verify</button></div>
        ${using ? html`<div class="hint">${using}</div>` : ""}<div class="small" id="verify-${slot}-${p.platform}" style="margin-top:6px"></div></div>`;
    };
    const status = (p) => {
      if (dev) return MODE_TEXT[p.mode];
      if (p.developer_only) return "A public test server, offered in developer mode only.";
      return p.available ? html`<span class="badge good">Connects for real</span> ${p.client_ids.production.source === "default" ? "with Syntropy Health's registered client ID." : ""}`
        : html`<span class="badge">Not available</span> Health systems on ${p.label} are listed but can't be connected without a production client ID.`;
    };
    mount(body, html`<div class="stack">
      ${section("Developer mode", "", html`<label class="set-row"><div class="set-label"><div class="set-title">Test with sandboxes and the simulator</div>
        <div class="set-hint">Off, health systems connect for real with production client IDs. On, each platform below uses the mode you
          choose: the built-in simulator, the vendor's developer sandbox with your sandbox client IDs, or production.</div></div>
        <div class="set-control"><span class="switch"><input type="checkbox" id="dev-mode" ${dev ? "checked" : ""}><span></span></span></div></label>`)}
      ${section("Health record platforms", dev ? "" : "Turn on developer mode to test with sandbox client IDs or the simulator.", html`${settings.platforms.map((p) => html`<div class="set-row platform">
        <div class="set-label"><div class="set-title">${p.label}</div><div class="set-hint">${status(p)}</div>
          ${dev ? html`<div class="segmented" style="margin-top:8px" role="group" aria-label="${p.label} mode">${["simulated", "sandbox", "production"].map((mode) => html`
            <button class="${p.mode === mode ? "active" : ""}" data-action="mode" data-platform="${p.platform}" data-mode="${mode}" aria-pressed="${p.mode === mode}">${capitalize(mode)}</button>`)}</div>` : ""}
          ${p.platform === "smart-health-it" ? (dev ? html`<div class="hint">Public test server — no registration needed. ${p.sandbox_hint}.</div>` : "")
            : html`${cidField(p, "production")}${dev ? cidField(p, "sandbox") : ""}
              ${dev ? html`<div class="hint">${p.sandbox_base ? html`Sandbox: <code>${p.sandbox_base}</code>` : `${p.sandbox_hint}.`}</div>` : ""}`}</div>
      </div>`)}`, "flush")}
      ${section("Redirect URIs", "Where health systems and wearables send you back after sign-in.", html`
        ${row("Health records", html`Registered with EHR vendors. Default <code>${settings.relay.default_redirect_uri}</code> forwards the one-time code to this instance; use <code>${location.origin}/callback</code> if you registered it directly.`,
          html`<input class="input" id="g-redirect" value="${settings.relay.redirect_uri}" aria-label="Health record redirect URI">`)}
        ${row("Wearables", html`Default <code>${settings.relay.default_wearable_redirect_uri}</code>.`,
          html`<input class="input" id="g-wredirect" value="${settings.relay.wearable_redirect_uri}" aria-label="Wearable redirect URI">`)}
        <div class="set-foot"><button class="btn btn-sm btn-primary" data-action="save-relay">Save redirect URIs</button></div>`)}
    </div>`);
    $("#dev-mode", body).addEventListener("change", async (ev) => {
      try { await put("/api/settings/developer", { enabled: ev.target.checked }); }
      catch (err) { toast(err.message, "bad"); ev.target.checked = !ev.target.checked; return; }
      toast(`Developer mode ${ev.target.checked ? "on" : "off"}`); redraw();
    });
    return onAction(body, {
      mode: async ({ platform, mode }) => { await put(`/api/settings/platforms/${platform}`, { mode }); toast(`${capitalize(mode)} mode`); redraw(); },
      "save-cid": async ({ platform, slot }) => {
        await put(`/api/settings/platforms/${platform}`, { [`${slot}_client_id`]: $(`#cid-${slot}-${platform}`, body).value.trim() });
        toast("Saved"); redraw();
      },
      verify: async ({ platform, slot }) => {
        const out = $(`#verify-${slot}-${platform}`, body);
        mount(out, html`<span class="spinner"></span>`);
        const r = await post(`/api/settings/platforms/${platform}/verify?mode=${slot}`);
        mount(out, html`<span class="badge ${r.ok ? "good" : r.ok === false ? "bad" : ""}">${r.status.replace("_", " ")}</span> <span class="muted">${r.detail}</span>`);
      },
      "save-relay": async () => {
        await put("/api/settings/general", { redirect_uri: $("#g-redirect", body).value.trim(), wearable_redirect_uri: $("#g-wredirect", body).value.trim() });
        toast("Saved"); redraw();
      },
    });
  }
  return null;
}

/** More than one person signs in here. */
function household() { return (appState.status?.account?.members || 0) > 1; }
function ownerName() { return appState.status?.account?.owner_name || "the server's owner"; }

// ------------------------------------------------------------------ People and sharing
const LEVEL_TEXT = { view: "Can view", manage: "Can view and change" };

function personBadges(p) {
  const b = [];
  if (p.is_self) b.push(html`<span class="badge">You</span>`);
  if (p.owner) b.push(html`<span class="badge">Owner</span>`);
  if (!p.is_self && p.signs_in) b.push(html`<span class="badge info">Signs in</span>`);
  if (p.access === "view") b.push(html`<span class="badge">View only</span>`);
  if (p.invited_until) b.push(html`<span class="badge warn">Invited</span>`);
  return b;
}

/** An access-log entry's details as "name: Sutter Health · status: success" rather than raw JSON. */
function auditDetail(detail) {
  if (!detail) return "";
  let value;
  try { value = JSON.parse(detail); } catch { return detail; }
  if (value === null || typeof value !== "object") return String(value);
  const text = (v) => (v && typeof v === "object" ? JSON.stringify(v) : String(v));
  return Object.entries(value).filter(([, v]) => v !== null && v !== "").map(([k, v]) => `${k.replace(/_/g, " ")}: ${text(v)}`).join(" · ");
}

/** "Safari on Mac", "the Windows app"... from a browser's user agent, for the list of where you're signed in. */
function browserName(ua) {
  if (!ua) return "Unknown browser";
  const app = /SyntropyHealthDesktop\/[\d.]+ \((Mac|Windows)\)/.exec(ua);
  if (app) return `The ${app[1]} app`;
  const os = /iPhone|iPad/.test(ua) ? (/iPad/.test(ua) ? "iPad" : "iPhone") : /Android/.test(ua) ? "Android"
    : /Mac OS X|Macintosh/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : /CrOS/.test(ua) ? "ChromeOS" : /Linux/.test(ua) ? "Linux" : "";
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\/|FxiOS/.test(ua) ? "Firefox" : /OPR\//.test(ua) ? "Opera"
    : /Chrome\/|CriOS/.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "";
  return browser ? `${browser}${os ? ` on ${os}` : ""}` : os || ua.slice(0, 60);
}

async function renderPeople(body, { redraw }) {
  const { profiles } = await get("/api/profiles", undefined, { fresh: true });
  const me = profiles.find((p) => p.is_self);
  const owner = isOwner();
  const canRemove = (p) => p.access === "manage" && !p.is_self && (!p.signs_in || owner);
  mount(body, html`<div class="stack">
    ${appState.status && !appState.status.auth_required ? html`<div class="banner warn">${icon("shield")}<div class="grow"><p><b>No password is set.</b>
      Anyone who opens Syntropy Health is signed in as you, so invitations are off until you
      <a href="#/settings/security">set a password</a>.</p></div></div>` : ""}
    ${section("Everyone here", html`Everyone here has their own sources, records and devices. People who sign in see only their own data and
      what's shared with them; people who don't (a child, a parent you care for) are looked after by whoever manages them.`, html`
      <div class="list">${profiles.map((p) => html`<div class="list-item people-row">
        <span class="avatar" style="--c:${p.color || "var(--accent)"};width:34px;height:34px;font-size:12.5px">${initials(p.name)}</span>
        <div class="grow" style="min-width:0"><div class="row wrap" style="gap:6px"><b>${p.name}</b>${personBadges(p)}</div>
          <div class="meta">${capitalize(p.relationship)} · ${plural(p.connection_count, "source")} · ${plural(p.record_count, "record")}</div></div>
        <div class="row wrap people-actions">
          ${p.access === "manage" && !p.signs_in ? html`<button class="btn btn-sm" data-action="invite" data-id="${p.id}">${icon("ticket")} ${p.invited_until ? "Invitation" : "Invite"}</button>` : ""}
          ${p.access === "manage" && !p.is_self ? html`<button class="btn btn-sm" data-action="share" data-id="${p.id}">${icon("users")} Access</button>` : ""}
          ${p.access === "manage" ? html`<button class="btn btn-sm btn-ghost" data-action="edit-profile" data-id="${p.id}">Edit</button>` : ""}
          ${canRemove(p) ? html`<button class="btn btn-sm btn-ghost btn-danger" data-action="delete-profile" data-id="${p.id}">Remove</button>` : ""}
        </div></div>`)}</div>
      <div class="set-foot"><button class="btn btn-sm btn-primary" data-action="add-profile">${icon("plus")} Add person</button>
        <span class="small muted">You'll look after them. Invite them if they should sign in themselves.</span></div>`, "flush")}
    ${me ? section("Your data", "", row("Who can see it", "Only you, unless you share it. Sharing to view lets someone read your data; to change, also add sources, pair a phone and edit.",
      html`<button class="btn btn-sm" data-action="share" data-id="${me.id}">${icon("users")} Share my data</button>`)) : ""}
  </div>`);

  const profileForm = (p) => {
    p = p || { relationship: profiles.some((x) => x.relationship === "self") ? "other" : "self" };
    return html`
    <div class="field"><label class="label" for="p-name">Name</label><input class="input" id="p-name" value="${p.name || ""}" maxlength="80"></div>
    <div class="field"><label class="label" for="p-rel">Relationship</label><select class="input" id="p-rel">
      ${["self", "partner", "child", "parent", "sibling", "other"].map((r) => html`<option value="${r}" ${p.relationship === r ? "selected" : ""}>${capitalize(r)}</option>`)}</select></div>
    <div class="field"><label class="label" for="p-dob">Date of birth</label><input class="input" id="p-dob" type="date" value="${p.birth_date || ""}"></div>`;
  };
  const openForm = (p) => {
    const m = modal({ title: p ? `Edit ${p.name}` : "Add person", body: profileForm(p),
      footer: html`<button class="btn" data-cancel>Cancel</button><button class="btn btn-primary" data-save>Save</button>` });
    m.el.querySelector("[data-cancel]").addEventListener("click", m.close);
    m.el.querySelector("[data-save]").addEventListener("click", async () => {
      const data = { name: $("#p-name", m.el).value.trim(), relationship: $("#p-rel", m.el).value, birth_date: $("#p-dob", m.el).value || null };
      if (!data.name) return toast("Enter a name", "bad");
      try {
        if (p) await patch(`/api/profiles/${p.id}`, data);
        else { const created = await post("/api/profiles", data); m.close(); await refreshProfiles(); setProfile(created.id); toast(`Added ${created.name}`); return; }
        m.close(); await refreshProfiles(); redraw();
      } catch (err) { toast(err.message, "bad"); }
    });
  };

  return onAction(body, {
    "add-profile": () => openForm(null),
    "edit-profile": ({ id }) => openForm(profiles.find((p) => p.id === id)),
    share: ({ id }) => openSharing(profiles.find((p) => p.id === id), redraw),
    invite: ({ id }) => openInvite(profiles.find((p) => p.id === id), redraw),
    "delete-profile": async ({ id }) => {
      const p = profiles.find((x) => x.id === id);
      const text = p.signs_in
        ? `${p.name} will be signed out everywhere, and their sources, records, wearable data and devices are permanently deleted from this machine.`
        : "This permanently deletes them with all of their sources, records, wearable data and devices.";
      if (await confirmDialog(`Remove ${p.name}?`, text, { confirmLabel: "Remove and delete their data", danger: true })) {
        try { await del(`/api/profiles/${id}`); await refreshProfiles(); redraw(); }
        catch (err) { toast(err.message, "bad"); }
      }
    },
  });
}

/** Who else can see a person, and how much: none, view, or view and change. */
async function openSharing(p, redraw) {
  const m = modal({ title: p.is_self ? "Share your data" : `Who can see ${p.name}`, body: loading(2),
    footer: html`<button class="btn btn-primary" data-done>Done</button>` });
  m.el.querySelector("[data-done]").addEventListener("click", () => { m.close(); redraw(); });
  const draw = (info) => {
    const level = (id) => info.access.find((a) => a.account_id === id)?.level || "";
    const whose = p.is_self ? "your" : `${p.name}'s`;
    m.setBody(info.accounts.length ? html`
      <p class="muted" style="margin-bottom:14px">${p.is_self ? "Only you see your data unless you share it here." : html`${p.name} ${p.signs_in ? "signs in themselves" : "doesn't sign in"}; these people can see them too.`}
        Viewing lets someone read ${whose} data and ask about it; changing also lets them add sources, pair a phone and edit.</p>
      <div class="list share-list">${info.accounts.map((a) => html`<div class="list-item">
        <span class="avatar" style="--c:${a.color || "var(--accent)"}">${initials(a.name)}</span><b class="grow truncate">${a.name}</b>
        <div class="segmented" role="group" aria-label="${a.name}'s access">${[["", "None"], ["view", "View"], ["manage", "Change"]].map(([k, label]) => html`
          <button class="${level(a.account_id) === k ? "active" : ""}" data-account="${a.account_id}" data-level="${k}" aria-pressed="${level(a.account_id) === k}">${label}</button>`)}</div>
      </div>`)}</div>`
      : html`<p class="muted">No one else signs in here yet. Add someone under People and invite them, then you can share with them.</p>`);
  };
  let info;
  try { info = await get(`/api/profiles/${p.id}/access`, undefined, { fresh: true }); draw(info); }
  catch (err) { m.setBody(html`<p class="muted">${err.message}</p>`); return; }
  m.body.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-account]");
    if (!b) return;
    try {
      info = await put(`/api/profiles/${p.id}/access`, { account_id: b.dataset.account, level: b.dataset.level || null });
      draw(info);
      const who = info.accounts.find((a) => a.account_id === b.dataset.account)?.name;
      toast(b.dataset.level ? `${who}: ${LEVEL_TEXT[b.dataset.level].toLowerCase()}` : `${who} can no longer see ${p.is_self ? "your" : `${p.name}'s`} data`);
    } catch (err) { toast(err.message, "bad"); }
  });
}

/** A one-time code (and link) for someone to choose their own password and sign in as themselves. */
async function openInvite(p, redraw) {
  const m = modal({ title: `Invite ${p.name}`, body: "", footer: html`<button class="btn" data-close-invite>Close</button>` });
  m.el.querySelector("[data-close-invite]").addEventListener("click", () => { m.close(); redraw(); });
  const intro = () => html`
    <p style="margin-bottom:10px">${p.name} will choose their own password and sign in as themselves, on this computer, another one, or
      the iPhone app.</p>
    <div class="banner info">${icon("users")}<div class="grow"><p><b>Once ${p.name} joins, their data is theirs.</b> You'll stop
      seeing it unless they share it with you. ${p.relationship === "child" ? html`For a young child, you may prefer to keep looking after them without an invitation.` : ""}</p></div></div>`;
  const show = (inv) => {
    const link = `${inv.server_url || location.origin}/#/join/${inv.code}`;
    m.setBody(html`${intro()}
      <div class="invite-code"><span class="mono">${inv.code}</span></div>
      <div class="field"><div class="label">Or send this link</div>
        <div class="row"><code class="grow truncate">${link}</code><button class="btn btn-sm" data-copy="${link}">${icon("copy")} Copy</button></div>
        <div class="hint">Works once, until ${fmtDateTime(inv.expires_at)}. Anyone with it could join as ${p.name}, so send it only to them.</div></div>
      <div class="row" style="margin-top:14px;gap:8px"><button class="btn btn-sm" data-new>New code</button>
        <button class="btn btn-sm btn-ghost btn-danger" data-cancel-invite>Cancel invitation</button></div>`);
  };
  const make = async () => {
    try { show(await post(`/api/profiles/${p.id}/invite`)); } catch (err) { toast(err.message, "bad"); m.close(); }
  };
  if (p.invited_until) {
    m.setBody(html`${intro()}<p class="muted" style="margin:14px 0">An invitation is waiting until ${fmtDateTime(p.invited_until)}. Codes are shown
      only once; make a new one to send again (the old one stops working).</p>
      <div class="row" style="gap:8px"><button class="btn btn-primary btn-sm" data-new>New code</button>
        <button class="btn btn-sm btn-ghost btn-danger" data-cancel-invite>Cancel invitation</button></div>`);
  } else {
    m.setBody(html`${intro()}<div class="row" style="margin-top:14px"><button class="btn btn-primary" data-new>${icon("ticket")} Create invitation</button></div>`);
  }
  m.body.addEventListener("click", async (e) => {
    if (e.target.closest("[data-new]")) return make();
    const copy = e.target.closest("[data-copy]");
    if (copy) {
      try { await navigator.clipboard.writeText(copy.dataset.copy); toast("Copied", "good"); } catch { toast(copy.dataset.copy); }
      return;
    }
    if (e.target.closest("[data-cancel-invite]")) {
      await del(`/api/profiles/${p.id}/invite`);
      toast("Invitation cancelled"); m.close(); redraw();
    }
  });
}
