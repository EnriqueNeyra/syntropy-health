import { api, del, get, patch, post, put } from "../api.js";
import {
  ACCENTS, applyAccent, currentAccent, embedded, refreshProfiles, sendPreferences, setAccent, setProfile, setTheme, state as appState,
  themePreference,
} from "../app.js";
import { regionSystem, setUnitPreference, unitPreference } from "../units.js";
import { renderAiTab } from "./settings-ai.js";
import { addressList, networkStatus, renderNetworkPanel } from "../network.js";
import { renderUpdates } from "../updates.js";
import { importArchive, openBackupDialog, openRestoreDialog, openScheduleDialog, scheduleText } from "../backup.js";
import { PRIVACY_URL, SOURCE_URL, TERMS_URL } from "../consent.js";
import {
  $, busy, capitalize, confirmDialog, fmtDateTime, html, icon, initials, loading, modal, mount, onAction, plural, toast,
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

/** A phone's screens are signed in with its token, not a password: what can't be undone takes the password there. */
const confirmsWithPassword = () => Boolean(appState.status?.account?.confirms_with_password);

/** The password, on a phone's screens, before something that can't be undone: resolves to it, or to null if they
 * don't go ahead. Elsewhere it resolves to undefined straight away (signed in with the password already). */
function askPassword(title, message, { confirmLabel = "Continue", danger = false } = {}) {
  if (!confirmsWithPassword()) return Promise.resolve(undefined);
  return new Promise((resolve) => {
    let done = false;
    const m = modal({
      title, onClose: () => { if (!done) resolve(null); },
      body: html`<p>${message}</p>${phonePasswordField("ask-pass")}`,
      footer: html`<button class="btn" data-no>Cancel</button><button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-yes>${confirmLabel}</button>`,
    });
    const field = $("#ask-pass", m.el);
    const go = () => {
      if (!field.value) return toast("Enter your password.", "bad");
      done = true; m.close(); resolve(field.value);
    };
    m.el.querySelector("[data-no]").addEventListener("click", () => m.close());
    m.el.querySelector("[data-yes]").addEventListener("click", go);
    field.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
    field.focus();
  });
}

function phonePasswordField(id) {
  return html`<div class="field" style="margin-top:12px"><label class="label" for="${id}">Your password</label>
    <input class="input" id="${id}" type="password" autocomplete="current-password" required>
    <div class="hint">This phone opens Syntropy Health without your password, so this takes it.</div></div>`;
}

/** Asks whether to go ahead, and on a phone's screens for the password too: resolves to what to send, or null. */
async function confirmRemoval(title, message, options) {
  if (confirmsWithPassword()) {
    const password = await askPassword(title, message, options);
    return password == null ? null : { password };
  }
  return (await confirmDialog(title, message, options)) ? {} : null;
}

/** A settings row: what it is on the left, the control on the right (stacked on phones). */
function row(title, hint, control) {
  return html`<div class="set-row"><div class="set-label"><div class="set-title">${title}</div>${hint ? html`<div class="set-hint">${hint}</div>` : ""}</div>
    <div class="set-control">${control}</div></div>`;
}
function section(title, hint, content, extra = "") {
  return html`<section class="card set-card ${extra}"><header><h3>${title}</h3>${hint ? html`<p>${hint}</p>` : ""}</header>${content}</section>`;
}

function encryptionSection(state) {
  const recovery = state.password_recovery;
  return section("Your records on this computer", state.encrypted ? `Encrypted with ${state.cipher}.` : html`<span style="color:var(--warn)">Not encrypted on disk.</span>`, html`
    <div class="stack" style="gap:10px;padding:2px 18px 16px">
      ${state.encrypted ? html`<p class="small" style="margin:0">That covers the database and its log, the copies kept before upgrades, and files on their way in. Backups made with a passphrase are encrypted too. A backup you choose not to encrypt is not, and anyone who gets one can read it.</p>`
        : state.chosen ? html`<p class="small" style="margin:0"><b>Your records will be encrypted the next time Syntropy Health starts.</b> Quit it and open it again when you're ready. It takes about a minute for every few gigabytes, and shows its progress.</p>
          <div><button class="btn btn-sm" data-action="encrypt-choice" data-on="">Not yet</button></div>`
        : html`<p class="small" style="margin:0">${state.why_not || "They will be the next time Syntropy Health starts."}</p>
          ${state.can_choose ? html`<p class="small muted" style="margin:0">Make an encrypted backup first, and keep it somewhere else. Once your records are encrypted, losing the key and every backup means losing them.</p>
            <div class="row wrap" style="gap:8px"><a class="btn btn-sm" href="#/settings/data">Make a backup</a><button class="btn btn-sm btn-primary" data-action="encrypt-choice" data-on="1">Encrypt my records</button></div>` : ""}`}
      <p class="small" style="margin:0"><b>The key</b> is in ${state.key_where}.${state.key_file ? html` <span class="muted">${state.key_file}</span>` : ""}</p>
      ${state.key_beside_data ? html`<div class="banner warn">${icon("shield")}<div class="grow"><p>Anyone who copies this data folder gets the key along with it, so encryption protects little here. Use full-disk encryption (FileVault, BitLocker or LUKS) for a stolen disk. To keep the key apart from the data, set SYNTROPY_KEY_FILE to a file somewhere else, or SYNTROPY_SECRET_KEY.</p></div></div>` : ""}
      ${isOwner() ? html`<p class="small" style="margin:0"><b>If the key is ever lost,</b> ${recovery.applies ? "the password of an owner of this server can bring it back, once they've signed in since this began, and so can any backup made with a passphrase. Without those," : "only a backup made with a passphrase can bring it back. Without one,"} nobody can read your records, and nobody can help with that. <a href="#/settings/data">Keep a backup somewhere else.</a></p>` : ""}
      ${state.plain_copies?.length ? html`<p class="small" style="margin:0"><b>Still unencrypted:</b> ${plural(state.plain_copies.length, "copy", "copies")} kept from before, which Syntropy Health will encrypt or remove shortly. <span class="muted">${state.plain_copies.join(", ")}</span></p>` : ""}
      <p class="small muted" style="margin:0">This protects a copy of the files, such as a stolen disk or a backup drive. While Syntropy Health runs it holds the key, so that sources and phones can sync with nobody signed in. So it can't protect you from someone using this computer as you, or from malware. Copies made before encryption, in Time Machine for example, stay as they were. Turn on FileVault or BitLocker as well: it covers what an app can't.</p>
      ${household() ? html`<p class="small muted" style="margin:0">Everyone on this server shares one encrypted database. Passwords and what each person shares keep people apart, not the encryption, and whoever runs this computer can read everything.</p>` : ""}
    </div>`);
}

export async function render({ el, parts, state, navigate }) {
  if (RENAMED[parts[0]]) return navigate(`#/settings/${RENAMED[parts[0]]}`);
  const SECTIONS = sectionsFor();
  const TABS = SECTIONS.flatMap(([, tabs]) => tabs);
  const tab = TABS.some(([k]) => k === parts[0]) ? parts[0] : "general";
  const label = TABS.find(([k]) => k === tab)[1];
  mount(el, html`
    <div class="page-head"><div><h1>Settings</h1><p>${isOwner() ? "Saved on the computer that runs Syntropy Health."
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
        ${row("Accent color", "Buttons, highlights and charts, on every device: here, the desktop app and the phone apps.",
          html`<div class="swatches" role="radiogroup" aria-label="Accent color">${ACCENTS.map(([k, name, light, dark]) => html`
            <button class="swatch ${k === currentAccent() ? "active" : ""}" role="radio" aria-checked="${k === currentAccent()}" data-action="accent"
              data-accent="${k}" title="${name}" aria-label="${name}" style="--sw:${light};--sw-dark:${dark}"></button>`)}</div>`)}`)}
      ${section("Region", "", html`
        ${row("Units", html`How weight, height, distance, temperature and fluids are shown. Lab results keep the units the lab reported.`,
          html`<div class="segmented" role="group" aria-label="Units">${[["", `Automatic · ${regionSystem() === "us" ? "US" : "Metric"}`], ["metric", "Metric"], ["us", "US"]].map(([k, name]) =>
            html`<button class="${k === unitPref ? "active" : ""}" data-action="units" data-units="${k}" aria-pressed="${k === unitPref}">${name}</button>`)}</div>`)}
        ${row("Time zone", isOwner() ? "Wearable data is grouped into days in this time zone." : "Wearable data is grouped into days in this time zone, set by the server's owner.",
          isOwner() ? html`<select class="input" id="g-tz" data-save="timezone" aria-label="Time zone">${timezones.map((tz) => html`<option value="${tz}" ${tz === settings.timezone ? "selected" : ""}>${tz.replaceAll("_", " ")}</option>`)}</select>`
            : html`<span>${settings.timezone.replaceAll("_", " ")}</span>`)}`)}
      ${isOwner() ? section("Background sync", settings.sync.scheduler_enabled ? "New data is fetched automatically while Syntropy Health is running."
        : html`<span style="color:var(--warn)">Background sync is turned off where Syntropy Health was started (SYNTROPY_SCHEDULER=false). Use Sync now on Sources.</span>`, html`
        ${row("Health records", "How often to check connected health systems.",
          html`<div class="input-unit"><input class="input" id="g-ehr" aria-label="Hours between health record checks" data-save="ehr_interval_hours" type="number" min="1" value="${settings.sync.ehr_interval_hours}"><span>hours</span></div>`)}
        ${row("Wearables", "How often to fetch from Oura, WHOOP and Google Health. The phone apps send their data on their own.",
          html`<div class="input-unit"><input class="input" id="g-wear" aria-label="Hours between wearable fetches" data-save="wearable_interval_hours" type="number" min="0.5" step="0.5" value="${settings.sync.wearable_interval_hours}"><span>hours</span></div>`)}`) : ""}
      ${isOwner() ? section("Updates", "", html`<div id="upd-panel" class="upd-panel"><div class="skeleton" style="height:64px"></div></div>`, "upd-card") : ""}
      ${section("About", "", html`
        ${row("Getting started", "Walk through connecting your other devices, your data and an AI again.",
          html`<button class="btn btn-sm" data-action="onboarding">Show the setup steps</button>`)}
        ${row("Version", "", html`<span class="mono">${settings.version}</span>`)}
        ${row("Source code", "Free and open source under the GNU AGPL-3.0.",
          html`<a href="${SOURCE_URL}" target="_blank" rel="noopener">View on GitHub</a>`)}
        ${row(isOwner() ? "Your responsibility" : "Where your records are", isOwner()
          ? "Your records are stored only on this computer and in your backups, not with Syntropy Labs. Keeping them secure and backed up is up to you. Syntropy Health organizes records and doesn't give medical advice."
          : "Your records are stored on the computer that runs Syntropy Health and in its backups, not on this device and not with Syntropy Labs. Whoever runs that computer keeps it secure and backed up. Syntropy Health organizes records and doesn't give medical advice.",
          html`<span class="row" style="gap:12px"><a href="${TERMS_URL}" target="_blank" rel="noopener">Terms</a><a href="${PRIVACY_URL}" target="_blank" rel="noopener">Privacy</a></span>`)}
        ${isOwner() ? row("Data folder", "Make backups from Your data. A copy of this folder taken while Syntropy Health is running may not be complete.", html`<code>${settings.data_dir}</code>`) : ""}`)}
    </div>`);
    const updPanel = $("#upd-panel", body);
    if (updPanel) {
      // From the alert: once the page is shown (and scrolled to the top), bring the section into view.
      const reveal = (node, frames = 120) => {
        if (node.isConnected) requestAnimationFrame(() => node.scrollIntoView({ block: "start" }));
        else if (frames) requestAnimationFrame(() => reveal(node, frames - 1));
      };
      renderUpdates(updPanel, { askPassword: () => askPassword("Install the update?", "Syntropy Health restarts to install it.", { confirmLabel: "Install and restart" }) }).then(() => { if (parts[1] === "updates") reveal(updPanel.closest("section")); });
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
        sendPreferences();          // the iPhone app shows its Apple Health data in the same units
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
      ${section("Your own Oura, WHOOP or Google app", settings.relay.release_build
        ? "Optional for Oura and WHOOP, whose client secret the Syntropy relay holds by default (it hands tokens straight back to this instance and keeps no copy). Required for Google Health for now: create an OAuth client in Google Cloud with the Google Health API enabled."
        : "Required in this copy of Syntropy Health: it was built from the source code, so it can't use Syntropy Labs' Oura and WHOOP apps or its sign-in relay. The apps from health.syntropylabs.io include them.", html`
        <details class="set-details" ${anyCreds || !settings.relay.release_build ? "open" : ""}><summary>Use my own developer credentials</summary>
          <p class="small muted" style="margin:0 0 12px">Register an app with Oura, WHOOP or Google Cloud, with <code>${location.origin}/callback</code> as its redirect URI${settings.relay.release_build
              ? html`, or <code>${settings.relay.default_redirect_uri}</code> if the provider only accepts https` : ""}. Enter its client ID and secret below, and the redirect URI if it isn't this server's own. Tokens for that app then never touch the relay, and the others stay as they are.</p>
          ${Object.entries(settings.wearables).map(([key, w]) => html`<div class="set-subcard">
            <h4>${w.label}</h4>
            <div class="grid grid-2">
              <div><label class="label" for="w-id-${key}">Client ID</label><input class="input" id="w-id-${key}" value="${w.client_id}"></div>
              <div><label class="label" for="w-secret-${key}">Client secret ${w.client_secret_set ? html`<span class="badge good">Saved</span>` : ""}</label>
                <input class="input" id="w-secret-${key}" type="password" autocomplete="off" placeholder="${w.client_secret_set ? "•••••••• (unchanged)" : "Only for your own app"}"></div>
            </div>
            <div style="margin-top:10px"><label class="label" for="w-redirect-${key}">Redirect URI</label>
              <input class="input" id="w-redirect-${key}" value="${w.redirect_uri}" inputmode="url" placeholder="${location.origin}/callback"></div>
            ${"relay_url" in w ? html`<div style="margin-top:10px"><label class="label" for="w-relay-${key}">Your own relay</label>
              <input class="input" id="w-relay-${key}" value="${w.relay_url}" inputmode="url" placeholder="https://my-relay.example.workers.dev" aria-describedby="w-relay-hint-${key}">
              <div class="hint" id="w-relay-hint-${key}">Only if you run your own copy of the sign-in relay and keep this app's secret there instead of here. Your refresh token goes to this address each time access is renewed.</div></div>` : ""}
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
        const relay = $(`#w-relay-${key}`, body);
        await put(`/api/settings/wearables/${key}`, { client_id: $(`#w-id-${key}`, body).value.trim(), redirect_uri: $(`#w-redirect-${key}`, body).value.trim(),
          ...(relay ? { relay_url: relay.value.trim() } : {}), ...(secret ? { client_secret: secret } : {}) });
        toast("Saved"); redraw();
      },
    });
  }

  if (tab === "security") {
    const [{ sessions }, encryption] = await Promise.all([get("/api/auth/sessions"), get("/api/settings/encryption")]);
    const on = settings.auth_required;
    mount(body, html`<div class="stack">
      ${encryptionSection(encryption)}
      ${section(household() ? "Your password" : "Password", on ? `Needed to open Syntropy Health${household() ? ` as ${appState.status.account.name}` : " in a browser"}. Changing it signs out your other browsers.`
        : html`<span style="color:var(--warn)">No password is set: anyone who can reach this server can open your records.</span>`, html`
        <form class="set-form" id="pw-form">
          ${on ? row("Current password", "", html`<input class="input" type="password" id="pw-cur" autocomplete="current-password" aria-label="Current password">`) : ""}
          ${row(on ? "New password" : "Password", isOwner() ? "At least 8 characters. Keep it in your password manager. If you forget it, the sign-in screen shows the command that sets a new one, on the computer this server runs on."
            : html`At least 8 characters. Keep it in your password manager. If you forget it, ${ownerName()} can set a new one on the computer this server runs on.`,
            html`<input class="input" type="password" id="pw-new" minlength="8" autocomplete="new-password" aria-label="${on ? "New password" : "Password"}">`)}
          ${row(on ? "Confirm new password" : "Confirm password", "", html`<input class="input" type="password" id="pw-new2" autocomplete="new-password" aria-label="Confirm the password">`)}
          <div class="set-foot"><button class="btn btn-primary btn-sm" type="submit">${on ? "Change password" : "Set password"}</button>
            ${on && appState.status?.password_optional ? html`<button class="btn btn-ghost btn-danger btn-sm" type="button" data-action="remove-pass">Remove password (development)</button>` : ""}</div>
        </form>`)}
      ${section("Other devices", "Your phone, other computers, and your devices on Tailscale.", html`<div id="net-panel" class="set-pad"><div class="skeleton" style="height:64px"></div></div>`)}
      ${section(household() ? "Where you're signed in" : "Signed-in browsers", "", html`<div class="list">${sessions.map((s) => html`<div class="list-item small">
          <span class="ev-icon">${icon(s.device_name ? "phone" : "monitor")}</span><div class="grow truncate">${s.device_name ? `The ${s.device_platform === "android" ? "Android" : "iPhone"} app (${s.device_name})` : browserName(s.user_agent)}
          ${s.current ? html` <span class="badge">This browser</span>` : ""}
          <div class="meta">${s.ip} · last active ${fmtDateTime(s.last_seen_at)}</div></div>
          ${s.device_name || s.current ? "" : html`<button class="btn btn-sm btn-ghost" data-action="end-session" data-id="${s.id}">Sign out</button>`}</div>`)}
          ${sessions.length ? "" : html`<div class="list-item small muted">No browser sessions.</div>`}</div>
        <div class="set-foot small muted"><span class="grow">Phones and AI apps use their own tokens: see Sources → Phones & watches and Settings → AI. A browser has to sign in again after six months.</span>
          ${sessions.some((s) => !s.device_name && !s.current) ? html`<button class="btn btn-sm" data-action="end-others">Sign out everywhere else</button>` : ""}</div>`, "flush")}
      ${section("How your data is protected", "", html`<ul class="check-list">
          <li>Everything lives ${settings.data_dir ? html`in <code>${settings.data_dir}</code> on this machine` : "on the computer this server runs on"}.</li>
          ${isOwner() ? "" : html`<li>${ownerName()} runs this server, so can reach the file it's all kept in, and its backups. Your access log shows each backup.</li>`}
          <li>Sign-in tokens and secrets are encrypted a second time inside the database, so they stay locked in a backup you chose not to encrypt.</li>
          <li>Health record connections use PKCE; the relay only ever sees a one-time code.</li>
          <li>Phones and AI apps use individual tokens you can revoke.</li>
          <li>Every sign-in, sync and export is recorded in the <a href="#/settings/log">access log</a>.</li></ul>`)}
    </div>`);
    networkStatus().then((net) => {
      const panel = $("#net-panel", body);
      if (!panel) return;
      if (isOwner()) renderNetworkPanel(panel, net, { onChange: () => redraw(),
        askPassword: (enabled) => askPassword(enabled ? "Let other devices connect?" : "Only this computer?",
          enabled ? "Your phone and other devices at home will be able to reach Syntropy Health." : "Phones and other devices stop reaching Syntropy Health, this one included.") });
      else mount(panel, net.on_network ? html`<p class="small muted" style="margin:0 0 10px">Open Syntropy Health from your other devices at:</p>${addressList(net)}`
        : html`<p class="small muted">Only the computer this server runs on can open it right now. ${ownerName()} can change that.</p>`);
    }).catch((err) => mount($("#net-panel", body), html`<p class="small muted">${err.message}</p>`));
    $("#pw-form", body).addEventListener("submit", async (e) => {
      e.preventDefault();
      const next = $("#pw-new", body).value;
      if (next.length < 8) return toast("Choose a password of at least 8 characters.", "bad");
      // A typo here would lock you out until someone resets it on the server's computer.
      if (next !== $("#pw-new2", body).value) return toast("The passwords don't match.", "bad");
      await busy(e.submitter, async () => {
        try {
          await post("/api/auth/password", { current: $("#pw-cur", body)?.value, new: next });
          toast("Password updated", "good"); redraw();
        } catch (err) { toast(err.message, "bad"); }
      });
    });
    return onAction(body, {
      "encrypt-choice": async ({ on }) => { await put("/api/settings/encryption/at-next-start", { on: Boolean(on) }); redraw(); },
      "end-session": async ({ id }) => {
        try { await del(`/api/auth/sessions/${id}`); toast("Signed out"); redraw(); } catch (err) { toast(err.message, "bad"); }
      },
      "end-others": async () => {
        if (!(await confirmDialog("Sign out everywhere else?", "Your other browsers will need your password again. Phones and AI apps keep working.", { confirmLabel: "Sign out" }))) return;
        try { await post("/api/auth/sessions/end-others"); toast("Signed out everywhere else"); redraw(); } catch (err) { toast(err.message, "bad"); }
      },
      "remove-pass": async () => {
        if (!(await confirmDialog("Remove password?", "Anyone who can reach this server will be able to open your records.", { confirmLabel: "Remove", danger: true }))) return;
        await post("/api/auth/password", { current: $("#pw-cur", body).value, new: null });
        toast("Password removed"); redraw();
      },
    });
  }

  if (tab === "data") {
    const p = state.profile;
    const owner = isOwner();
    const dl = (href) => html`<a class="btn btn-sm" href="${href}" download>${icon("download")} Download</a>`;
    const backup = owner ? await get("/api/backups", undefined, { fresh: true }) : null;
    const last = backup?.last;
    const failing = backup?.schedule.state?.last_error;
    mount(body, html`<div class="stack">
      ${section(`Export ${p.name}'s data`, "Files download straight from this machine, and aren't encrypted.", html`
        ${row("Visit summary", "A summary to print or save as PDF for appointments.", html`<a class="btn btn-sm" href="#/report">${icon("printer")} Open</a>`)}
        ${p.access === "manage" ? row("Everything, in one file", html`Records, wearable samples, workouts with routes, the journal, goals and your conversations with the assistant. Another Syntropy Health can import it.`,
          dl(`/api/export/archive?profile=${p.id}`)) : ""}
        ${row("Patient summary (FHIR)", "An International Patient Summary: current conditions, medications, allergies and results, in the format clinicians' systems read.", dl(`/api/export/ips?profile=${p.id}`))}
        ${row("FHIR R4 bundle", "Every clinical record in the standard format other apps and portals read, with the source of each.", dl(`/api/export/fhir?profile=${p.id}`))}
        ${row("Records (CSV)", "Conditions, medications, labs, visits and more, one row each.", dl(`/api/export/csv?kind=records&profile=${p.id}`))}
        ${row("Daily wearable data (CSV)", "One row per metric per day.", dl(`/api/export/csv?kind=daily&profile=${p.id}`))}
        ${row("Workouts (CSV)", "", dl(`/api/export/csv?kind=workouts&profile=${p.id}`))}
        ${row("Health journal (CSV)", "Symptoms, cycle tracking, mood and more, with your notes.", dl(`/api/export/csv?kind=events&profile=${p.id}`))}
        ${p.access === "manage" ? html`${row("Import an archive", `Brings an "Everything" file from another Syntropy Health into ${p.name}'s data. What's already here is left as it is.`,
          html`<label class="btn btn-sm">${icon("upload")} Choose file<input type="file" id="ar-file" accept=".zip"></label>`)}<div id="ar-status" style="padding:0 16px"></div>` : ""}`)}
      ${!owner ? "" : section("Backup", "", html`
        ${failing ? html`<div class="banner bad" style="margin:12px 16px"><div class="grow"><p><b>The last scheduled backup failed.</b> ${failing}</p></div></div>` : ""}
        ${row("Download a backup", html`Everyone's data in one encrypted file, including anyone who hasn't shared with you, with the encryption key that opens it all, so it restores on any computer with only its passphrase. It's also how to get the key back if this computer loses it. ${household() ? "Everyone here sees it in their access log when one is made." : ""}
          ${last ? html`<br>Last backup: ${fmtDateTime(last.created_at)}${last.encrypted ? "" : " (not encrypted)"}.` : html`<br><b>No backup has been made yet.</b>`}`,
          html`<button class="btn btn-sm btn-primary" data-action="backup">${icon("download")} Back up…</button>`)}
        ${row("Scheduled backups", html`${scheduleText(backup.schedule)}
          ${backup.schedule.same_disk ? html`<br><b>This folder is on the same disk as your data, so a failed disk would take both.</b>` : ""}
          ${backup.schedule.scheduler_off ? html`<br><b>They can't run: the server's scheduler is turned off (SYNTROPY_SCHEDULER).</b>` : ""}`,
          html`<button class="btn btn-sm" data-action="schedule">${backup.schedule.enabled ? "Change…" : "Set up…"}</button>
            ${backup.schedule.enabled ? html`<button class="btn btn-sm btn-ghost" data-action="run-backup">Back up now</button>` : ""}`)}
        ${row("Restore from a backup", "Replaces everything here with what's in a backup, after checking it. What's here now is kept in a folder beside the data.",
          html`<button class="btn btn-sm" data-action="restore">Restore…</button>`)}
        ${row("Uninstalling", uninstallHint(), "")}`)}
      ${p.access !== "manage" || (p.signs_in && !p.is_self) || appState.status?.account?.limited ? "" : section("Delete data", "", row(`Delete all of ${p.name}'s data`, html`Removes their sources, records, wearable data, journal entries, everyone's conversations with the
          assistant about them, goals and paired devices from this machine. This can't be undone. Backups you've already made still contain it.`,
        html`<button class="btn btn-sm btn-danger" data-action="wipe">Delete…</button>`), "danger")}
    </div>`);
    $("#ar-file", body)?.addEventListener("change", (e) => {
      if (e.target.files[0]) importArchive(e.target.files[0], p.id, $("#ar-status", body), null);
    });
    return onAction(body, {
      backup: () => openBackupDialog(backup, redraw),
      schedule: () => openScheduleDialog(backup, redraw),
      "run-backup": async (_, button) => {
        const password = await askPassword("Back up now?", "A backup holds everyone's records.", { confirmLabel: "Back up now" });
        if (password === null) return;
        button.disabled = true;
        let after;
        try { after = await post("/api/backups/schedule/run", password ? { password } : {}); }
        catch (err) { button.disabled = false; return toast(err.message, "bad"); }
        toast(after.schedule.state.last_error ? `The backup failed: ${after.schedule.state.last_error}` : "Backed up", after.schedule.state.last_error ? "bad" : "good");
        redraw();
      },
      restore: () => openRestoreDialog({ needsPassword: backup.needs_password }),
      wipe: () => {
        const m = modal({ title: `Delete all of ${p.name}'s data?`,
          body: html`<p style="margin-bottom:10px">This permanently deletes their sources, records, wearable data, journal entries, goals and paired devices, and
              <b>everyone's conversations with the assistant about them</b>. ${p.name} stays listed here, with nothing in their record.</p>
            <p class="small muted" style="margin-bottom:12px">It's overwritten in the database file. Backups you've already made still contain it: delete those yourself if it has to go from them too.
              To keep a copy first, <a href="/api/export/archive?profile=${p.id}" download>download everything for ${p.name}</a>.</p>
            <div class="field"><label class="label" for="wipe-confirm">Type DELETE to go ahead</label><input class="input" id="wipe-confirm" autocomplete="off"></div>
            ${confirmsWithPassword() ? phonePasswordField("wipe-pass") : ""}`,
          footer: html`<button class="btn" data-cancel>Cancel</button><button class="btn btn-danger" data-yes>Delete permanently</button>` });
        m.el.querySelector("[data-cancel]").addEventListener("click", m.close);
        m.el.querySelector("[data-yes]").addEventListener("click", async (e) => {
          if ($("#wipe-confirm", m.el).value.trim() !== "DELETE") return toast("Type DELETE to go ahead.", "bad");
          const password = $("#wipe-pass", m.el)?.value;
          if (confirmsWithPassword() && !password) return toast("Enter your password.", "bad");
          const button = e.currentTarget;
          button.disabled = true;
          button.textContent = "Deleting…";
          try { await api(`/api/profiles/${p.id}/data`, { method: "DELETE", body: password ? { password } : undefined }); m.close(); toast("Deleted"); redraw(); }
          catch (err) {
            // A wrong password can be typed again; anything else closes the dialog.
            if (password && err.status === 403) { button.disabled = false; button.textContent = "Delete permanently"; return toast(err.message, "bad"); }
            m.close(); toast(err.message, "bad");
          }
        });
      },
    });
  }

  if (tab === "log") {
    const { events } = await get("/api/audit", { limit: 200 });
    mount(body, html`<p class="small muted" style="margin:-4px 0 12px">${isOwner() ? "Every sign-in, sync, export and assistant lookup on this instance, newest first."
      : "Sign-ins, syncs, exports and assistant lookups for you and the people you can see, newest first."}</p>
      <div class="card"><div class="table-wrap"><table class="table"><thead><tr><th>When</th><th>Who</th><th>What</th><th>Details</th></tr></thead>
      <tbody>${events.map((e) => html`<tr><td class="small" style="white-space:nowrap">${fmtDateTime(e.at)}</td><td class="small">${e.who || e.actor}</td><td title="${e.action}">${auditAction(e.action, e.actor)}</td>
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
      ${section("Redirect URI", "Where health systems send you back after sign-in. Your own wearable apps each have theirs under Wearables.", html`
        ${row("Health records", html`Registered with EHR vendors. Default <code>${settings.relay.default_redirect_uri}</code> forwards the one-time code to this instance; use <code>${location.origin}/callback</code> if you registered it directly.`,
          html`<input class="input" id="g-redirect" value="${settings.relay.redirect_uri}" aria-label="Health record redirect URI">`)}
        <div class="set-foot"><button class="btn btn-sm btn-primary" data-action="save-relay">Save redirect URI</button></div>`)}
      <div id="dev-upkeep"></div>
    </div>`);
    get("/api/health", undefined, { fresh: true }).then((health) => {
      const box = $("#dev-upkeep", body);
      if (!box?.isConnected) return;
      const fixes = health.data_fixes || [];
      const failed = fixes.filter((f) => f.state === "failed");
      const waiting = fixes.filter((f) => f.state === "pending" || f.state === "running").length;
      const copies = health.database?.pre_upgrade_copies || [];
      mount(box, section("Database upkeep", "What the server has done to keep stored data correct after upgrades.", html`
        ${row("Data corrections", failed.length ? html`Couldn't finish: ${failed.map((f) => f.name).join(", ")}. They're tried again when the server restarts.` : "",
          html`<span>${plural(fixes.length - waiting - failed.length, "finished", "finished")}, ${plural(waiting, "waiting", "waiting")}, ${plural(failed.length, "failed", "failed")}</span>`)}
        ${row("Copies from before upgrades", "Kept beside the database in case an upgrade has to be undone.", html`<span>${copies.length}</span>`)}`));
    }, () => {});
    $("#dev-mode", body).addEventListener("change", async (ev) => {
      try { await put("/api/settings/developer", { enabled: ev.target.checked }); }
      catch (err) { toast(err.message, "bad"); ev.target.checked = !ev.target.checked; return; }
      appState.status.developer_mode = ev.target.checked;
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
        await put("/api/settings/general", { redirect_uri: $("#g-redirect", body).value.trim() });
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

// How removing this install treats its data. The desktop apps ask; elsewhere the data folder stays until it's deleted.
function uninstallHint() {
  const shell = document.documentElement.dataset.shell;
  const kept = "Otherwise it stays in the data folder, and installing again picks up where you left off.";
  if (shell === "mac") return `Choose Syntropy Health → Uninstall Syntropy Health… in the menu bar. It asks whether to delete everyone's data from this Mac too. ${kept}`;
  if (shell === "windows") return `Uninstall Syntropy Health from Windows Settings → Apps. It asks whether to delete everyone's data from this PC too. ${kept}`;
  return html`Removing Syntropy Health keeps the data folder (and everyone's records in it) until you delete it. <a href="${SOURCE_URL}#uninstalling" target="_blank" rel="noopener">How to remove each kind of install</a>`;
}

// What each access-log entry was, in words (the log keeps a short code for each: "sync.completed").
const AUDIT_ACTIONS = {
  "setup.completed": "Finished setting up", "account.created": "Account created", "account.role": "Role changed",
  "auth.login": "Signed in", "auth.login_failed": "Sign-in refused", "auth.password_changed": "Password changed",
  "auth.password_reset": "Password reset", "auth.session_ended": "Signed a browser out", "auth.sessions_ended": "Signed out everywhere else",
  "terms.accepted": "Agreed to the terms", "profile.removed": "Removed someone",
  "access.granted": "Shared access", "access.revoked": "Stopped sharing", "invite.created": "Invitation made",
  "invite.accepted": "Joined by invitation", "connection.created": "Source added", "connection.disconnected": "Source disconnected",
  "connection.removed": "Source removed with its data", "sync.completed": "Synced", "device.paired": "Phone paired",
  "device.revoked": "Phone unpaired", "labs.entered": "Lab results entered", "labs.report_read": "Lab report read",
  "export.fhir": "Exported a FHIR file", "export.ips": "Exported a patient summary", "export.archive": "Exported everything for one person",
  "export.chat": "Exported a conversation",
  "export.backup": "Made a backup", "backup.restored": "Restored from a backup", "backup.schedule_set": "Scheduled backups changed",
  "backup.schedule_off": "Scheduled backups turned off", "profile.data_deleted": "Deleted all data",
  "network.enabled": "Other devices allowed", "network.disabled": "This computer only", "updates.settings": "Update settings changed",
  "updates.install": "Update installed", "ai.chat": "Asked the AI", "ai.provider_data_acknowledged": "Agreed to an online AI",
  "agent.token_created": "AI app access created", "agent.token_revoked": "AI app access revoked",
};
function auditAction(action, actor) {
  if (AUDIT_ACTIONS[action]) return AUDIT_ACTIONS[action];
  if (action?.startsWith("export.csv.")) return "Exported a CSV file";
  if (action?.startsWith("mcp.")) {
    const what = action.slice(4).replace(/^(get|list)_/, "").replaceAll("_", " ");
    return /^ask(:|$)/.test(actor || "") ? `Looked up ${what} to answer a question` : `AI app looked up ${what}`;
  }
  return action;
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
  const owner = isOwner();
  const [{ profiles }, signsIn, adult, { timezones }] = await Promise.all([
    get("/api/profiles", undefined, { fresh: true }),
    owner && appState.status?.account ? get("/api/accounts", undefined, { fresh: true }).then((r) => r.accounts) : [],
    owner ? get("/api/settings", undefined, { fresh: true }).then((r) => r.adult_age) : null,
    get("/api/settings/timezones"),
  ]);
  const me = profiles.find((p) => p.is_self);
  // Someone who signs in is removed from "Who signs in" below, which an owner sees whether or not they've shared anything.
  // The screens of a phone paired for someone else open that person's record, not the account's say over who's here.
  const limited = !!appState.status?.account?.limited;
  const canRemove = (p) => p.access === "manage" && !p.signs_in && !limited;
  const owners = signsIn.filter((a) => a.role === "owner");
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
          ${p.access === "manage" && !p.signs_in && !limited ? html`<button class="btn btn-sm" data-action="invite" data-id="${p.id}">${icon("ticket")} ${p.invited_until ? "Invitation" : "Invite"}</button>` : ""}
          ${p.access === "manage" && !p.is_self && !p.signs_in && !limited ? html`<button class="btn btn-sm" data-action="share" data-id="${p.id}">${icon("users")} Access</button>` : ""}
          ${p.access === "manage" ? html`<button class="btn btn-sm btn-ghost" data-action="edit-profile" data-id="${p.id}">Edit</button>` : ""}
          ${canRemove(p) ? html`<button class="btn btn-sm btn-ghost btn-danger" data-action="delete-profile" data-id="${p.id}">Remove</button>` : ""}
        </div></div>`)}</div>
      ${limited ? "" : html`<div class="set-foot"><button class="btn btn-sm btn-primary" data-action="add-profile">${icon("plus")} Add person</button>
        <span class="small muted">You'll look after them. Invite them if they should sign in themselves.</span></div>`}`, "flush")}
    ${signsIn.length ? section("Who signs in", "Only owners see this list: names and last sign-ins, not anyone's health data. Owners run the server: its settings, its backups and this list.", html`
      <div class="list">${signsIn.map((a) => html`<div class="list-item people-row">
        <span class="avatar" style="--c:${a.color || "var(--accent)"};width:34px;height:34px;font-size:12.5px">${initials(a.name)}</span>
        <div class="grow" style="min-width:0"><div class="row wrap" style="gap:6px"><b>${a.name}</b>${a.is_self ? html`<span class="badge">You</span>` : ""}${a.role === "owner" ? html`<span class="badge">Owner</span>` : ""}</div>
          <div class="meta">${a.last_login_at ? `Last signed in ${fmtDateTime(a.last_login_at)}` : "Hasn't signed in since joining"}</div></div>
        <div class="row wrap people-actions">
          ${a.role !== "owner" ? html`<button class="btn btn-sm" data-action="make-owner" data-id="${a.id}">Make owner</button>` : ""}
          ${a.role === "owner" && owners.length > 1 ? html`<button class="btn btn-sm btn-ghost" data-action="make-member" data-id="${a.id}">${a.is_self ? "Stop being an owner" : "Remove as owner"}</button>` : ""}
          ${a.is_self ? "" : html`<button class="btn btn-sm btn-ghost btn-danger" data-action="remove-account" data-id="${a.id}">Remove</button>`}
        </div></div>`)}</div>`, "flush") : ""}
    ${owner && signsIn.length ? section("Growing up", "", row("Age to suggest their own sign-in", "A reminder comes six months before someone you look after, marked as a child, turns this age. It varies by place. Nothing changes until you invite them.",
      html`<select class="input input-sm" id="adult-age" aria-label="Age to suggest their own sign-in">${[16, 17, 18, 19, 20, 21].map((n) => html`<option value="${n}" ${adult === n ? "selected" : ""}>${n}</option>`)}</select>`)) : ""}
    ${me ? section("Your data", "", row("Who can see it", html`Nobody here, in the app, unless you share it. Viewing lets someone read your data and ask about it. Changing also lets them add and remove sources, pair a phone and edit records. It doesn't let them share it with anyone else or delete all of it.
      ${owner ? "" : html` ${ownerName()} runs this server, so can reach the file it's kept in, and its backups; your access log shows each backup.`}`,
      html`<button class="btn btn-sm" data-action="share" data-id="${me.id}">${icon("users")} Share my data</button>`)) : ""}
    ${me ? section("Your account", "", row("Delete my account and data", html`Signs you out everywhere and permanently deletes your sources, records, wearable data and devices from this server. People only you look after pass to the server's owner. <a href="#/settings/data">Download your data</a> first if you want to keep it.`,
      html`<button class="btn btn-sm btn-danger" data-action="delete-me">Delete…</button>`), "danger") : ""}
  </div>`);
  $("#adult-age", body)?.addEventListener("change", async (e) => {
    try {
      await put("/api/settings/general", { adult_age: Number(e.target.value) });
      if (appState.status) appState.status.adult_age = Number(e.target.value);
      toast("Saved");
    } catch (err) { toast(err.message, "bad"); }
  });

  const profileForm = (p) => {
    p = p || { relationship: profiles.some((x) => x.relationship === "self") ? "other" : "self" };
    return html`
    <div class="field"><label class="label" for="p-name">Name</label><input class="input" id="p-name" value="${p.name || ""}" maxlength="80"></div>
    <div class="field"><label class="label" for="p-rel">Relationship</label><select class="input" id="p-rel">
      ${["self", "partner", "child", "parent", "sibling", "other"].map((r) => html`<option value="${r}" ${p.relationship === r ? "selected" : ""}>${capitalize(r)}</option>`)}</select></div>
    <div class="field"><label class="label" for="p-dob">Date of birth</label><input class="input" id="p-dob" type="date" value="${p.birth_date || ""}"></div>
    ${p.id ? html`<div class="field"><label class="label" for="p-tz">Home time zone</label><select class="input" id="p-tz">
      <option value="">Same as this server</option>
      ${timezones.map((tz) => html`<option value="${tz}" ${p.timezone === tz ? "selected" : ""}>${tz.replaceAll("_", " ")}</option>`)}</select>
      <div class="small muted">Which day a reading counts toward, when the device doesn't say where it was.</div></div>` : ""}`;
  };
  const openForm = (p) => {
    const m = modal({ title: p ? `Edit ${p.name}` : "Add person", dirty: true, body: profileForm(p),
      footer: html`<button class="btn" data-cancel>Cancel</button><button class="btn btn-primary" data-save>Save</button>` });
    m.el.querySelector("[data-cancel]").addEventListener("click", m.close);
    m.el.querySelector("[data-save]").addEventListener("click", (e) => busy(e.currentTarget, async () => {
      const data = { name: $("#p-name", m.el).value.trim(), relationship: $("#p-rel", m.el).value, birth_date: $("#p-dob", m.el).value || null };
      const zone = $("#p-tz", m.el);
      if (zone) data.timezone = zone.value || null;
      if (!data.name) return toast("Enter a name", "bad");
      try {
        if (p) await patch(`/api/profiles/${p.id}`, data);
        else { const created = await post("/api/profiles", data); m.close(); await refreshProfiles(); setProfile(created.id); toast(`Added ${created.name}`); return; }
        m.close(); await refreshProfiles(); toast("Saved"); redraw();
      } catch (err) { toast(err.message, "bad"); }
    }));
  };

  return onAction(body, {
    "add-profile": () => openForm(null),
    "edit-profile": ({ id }) => openForm(profiles.find((p) => p.id === id)),
    share: ({ id }) => openSharing(profiles.find((p) => p.id === id), redraw),
    invite: ({ id }) => openInvite(profiles.find((p) => p.id === id), redraw),
    "delete-profile": async ({ id }) => {
      const p = profiles.find((x) => x.id === id);
      const text = p.signs_in
        ? `${p.name} will be signed out everywhere, and their sources, records, wearable data, journal and devices are permanently deleted from this machine. Backups you've already made still contain them.`
        : "This permanently deletes them with all of their sources, records, wearable data, journal and devices. Backups you've already made still contain them.";
      const go = await confirmRemoval(`Remove ${p.name}?`, text, { confirmLabel: "Remove and delete their data", danger: true });
      if (go) {
        try { await api(`/api/profiles/${id}`, { method: "DELETE", body: go.password ? go : undefined }); await refreshProfiles(); toast(`Removed ${p.name}`); redraw(); }
        catch (err) { toast(err.message, "bad"); }
      }
    },
    "remove-account": async ({ id }) => {
      const a = signsIn.find((x) => x.id === id);
      const go = await confirmRemoval(`Remove ${a.name}?`, `${a.name} will be signed out everywhere, and their sources, records, wearable data and devices are permanently deleted from this server. You won't be able to see them first unless they've shared them with you.`, { confirmLabel: "Remove and delete their data", danger: true });
      if (go) {
        try { await api(`/api/profiles/${a.profile_id}`, { method: "DELETE", body: go.password ? go : undefined }); await refreshProfiles(); toast(`Removed ${a.name}`); redraw(); }
        catch (err) { toast(err.message, "bad"); }
      }
    },
    "make-owner": ({ id }) => openRoleChange(signsIn.find((a) => a.id === id), "owner", redraw),
    "make-member": ({ id }) => openRoleChange(signsIn.find((a) => a.id === id), "member", redraw),
    "delete-me": () => openDeleteAccount(me),
  });
}

function passwordField(id) {
  return appState.status?.auth_required ? html`<div class="field" style="margin-top:12px"><label class="label" for="${id}">Your password</label>
    <input class="input" id="${id}" type="password" autocomplete="current-password" required></div>` : "";
}

/** Who runs the server: everything an owner can do, handed over with the owner's own password. */
function openRoleChange(a, role, redraw) {
  const promoting = role === "owner";
  const m = modal({ title: promoting ? `Make ${a.name} an owner` : a.is_self ? "Stop being an owner" : `Remove ${a.name} as owner`, body: html`
    <p>${promoting ? html`${a.name} will be able to change every server setting, download a backup holding everyone's records, and see who signs in.`
      : a.is_self ? "You'll keep your own data and what's shared with you, but stop running the server's settings and backups."
      : `${a.name} keeps their own data and what's shared with them, but stops running the server's settings and backups.`}</p>
    ${promoting ? html`<label class="checkbox small" style="margin-top:12px"><input type="checkbox" id="role-step-down">
      <span>I'm handing the server over: stop being an owner myself.</span></label>` : ""}
    ${passwordField("role-pass")}`,
    footer: html`<button class="btn" data-cancel>Cancel</button><button class="btn btn-primary" data-save>${promoting ? "Make owner" : "Confirm"}</button>` });
  m.el.querySelector("[data-cancel]").addEventListener("click", m.close);
  m.el.querySelector("[data-save]").addEventListener("click", async () => {
    const stepDown = !!$("#role-step-down", m.el)?.checked;
    try {
      await put(`/api/accounts/${a.id}/role`, { role, step_down: stepDown, password: $("#role-pass", m.el)?.value });
      m.close();
      if (a.is_self || stepDown) location.reload();     // what this person may see and change has just changed
      else { toast("Saved"); redraw(); }
    } catch (err) { toast(err.message, "bad"); }
  });
}

/** Leaving: only with their own password, and not by accident. */
function openDeleteAccount(me) {
  const m = modal({ title: "Delete your account and data?", body: html`
    <p>Your sources, records, wearable data, conversations and devices are permanently deleted from this server, and you're signed out everywhere. This can't be undone.</p>
    <p class="small muted" style="margin-top:8px"><a href="#/settings/data">Download your data</a> first if you want to keep it. Copies in backups already made stay until those are deleted.</p>
    ${passwordField("del-pass")}`,
    footer: html`<button class="btn" data-cancel>Cancel</button><button class="btn btn-danger" data-save>Delete ${me.name}'s account and data</button>` });
  m.el.querySelector("[data-cancel]").addEventListener("click", m.close);
  m.el.querySelector("[data-save]").addEventListener("click", async () => {
    try {
      await post("/api/accounts/me/delete", { password: $("#del-pass", m.el)?.value });
      try { localStorage.removeItem("syntropy-account"); localStorage.removeItem("syntropy-profile"); } catch {}
      history.replaceState(null, "", "#/");
      location.reload();
    } catch (err) { toast(err.message, "bad"); }
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
      <p class="muted" style="margin-bottom:14px">${p.is_self ? "Nobody here sees your data in the app unless you share it here." : html`${p.name} doesn't sign in; these people can see them too.`}
        Viewing lets someone read ${whose} data and ask about it. Changing also lets them add and remove sources, pair a phone and edit records.
        ${p.is_self ? "It doesn't let them share it with anyone else or delete all of it." : `It also lets them share ${whose} data with others, and delete it.`}
        If you lower or end someone's access, any phone they paired for ${p.is_self ? "you" : p.name} stops and has to be paired again, and their conversations with an AI about ${p.is_self ? "you" : p.name} end along with their access.</p>
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
      a phone app.</p>
    <div class="banner info">${icon("users")}<div class="grow"><p><b>Once ${p.name} joins, their data is theirs.</b> You'll stop
      seeing it unless they share it with you. Phones you paired for them stop until they pair them again with their own sign-in, and your
      conversations with an AI about them are deleted. Sources you connected keep running, and they're shown what's there.
      Whoever runs this server can still reach the file it's kept in, and its backups.
      ${p.relationship === "child" ? html`For a young child, you may prefer to keep looking after them without an invitation.` : ""}</p></div></div>`;
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
      try { await del(`/api/profiles/${p.id}/invite`); } catch (err) { toast(err.message, "bad"); return; }
      toast("Invitation cancelled"); m.close(); redraw();
    }
  });
}
