// Settings → General → Updates (for the server's owner): whether a newer release is out, how this installation gets
// it, and the choices to check for updates and to install them automatically. See app/services/updates.py.

import { get, post, put } from "./api.js";
import { fmtDateTime, html, icon, mount, toast } from "./ui.js";

const PROGRESS = {
  downloading: "Downloading the update…",
  waiting: "Downloaded. It installs when you close the window.",
  installing: "Installing… Syntropy Health opens again in a moment.",
};

const HOW = {
  app: "Download the new version of the app and open it to install.",
  docker: "Run this where your docker-compose.yml is:",
  linux: "Run this on the server:",
  package: "Run this on the computer running Syntropy Health:",
  source: "In your clone of the repository:",
};

function statusLine(u) {
  if (u.available) return html`<b>Syntropy Health ${u.latest.version} is available.</b> You have ${u.current}.`;
  if (u.error && !u.checked_at) return html`Couldn't check for updates: ${u.error}`;
  if (!u.checked_at) return u.check ? "Not checked yet." : "Checking for updates is off.";
  return html`You have the latest version, ${u.current}.`;
}

function switchRow(id, title, hint, on, disabled = false) {
  return html`<label class="set-row"><div class="set-label"><div class="set-title">${title}</div><div class="set-hint">${hint}</div></div>
    <div class="set-control"><span class="switch"><input type="checkbox" id="${id}" ${on ? "checked" : ""} ${disabled ? "disabled" : ""}><span></span></span></div></label>`;
}

export async function renderUpdates(el) {
  let u;
  try { u = await get("/api/system/updates", {}, { fresh: true }); }
  catch (err) { mount(el, html`<p class="small muted upd-how">${err.message}</p>`); return; }
  draw(el, u);
}

function draw(el, u) {
  const m = u.method;
  const busy = u.progress && u.progress.state !== "failed";
  const checked = u.checked_at ? html` Checked ${fmtDateTime(u.checked_at)}${u.error ? html` (the last check failed: ${u.error})` : ""}.` : "";
  const notes = u.available && u.latest.url ? html` <a href="${u.latest.url}" target="_blank" rel="noopener">What's new</a>` : "";
  let action = html`<button class="btn btn-sm" data-upd="check" ${busy || !u.check_allowed ? "disabled" : ""}>${icon("sync")} Check now</button>`;
  if (u.available && m.installs) {
    action = html`<button class="btn btn-sm btn-primary" data-upd="install" ${busy ? "disabled" : ""}>${icon("download")} Install and restart</button>`;
  } else if (u.available && m.kind === "app") {
    action = html`<a class="btn btn-sm btn-primary" href="${u.latest.url}" target="_blank" rel="noopener">${icon("download")} Download</a>`;
  }
  const autoHint = m.kind === "linux"
    ? "The installer's daily timer installs new versions and restarts the service, overnight."
    : "New versions download in the background and install while the window is closed; the app then opens again in the background.";
  mount(el, html`
    <div class="set-row"><div class="set-label"><div class="set-title">${statusLine(u)}</div>
      <div class="set-hint">${checked}${notes}</div></div><div class="set-control">${action}</div></div>
    ${u.progress ? html`<div class="banner upd-note ${u.progress.state === "failed" ? "bad" : "info"}">${icon(u.progress.state === "failed" ? "alert" : "download")}<div class="grow">
      ${u.progress.state === "failed" ? html`Couldn't install the update: ${u.progress.detail}` : PROGRESS[u.progress.state] || u.progress.state}</div></div>` : ""}
    ${u.available && m.command ? html`<div class="upd-how"><p class="small muted">${HOW[m.kind]}</p><pre class="raw upd-cmd">${m.command}</pre></div>` : ""}
    ${u.check_allowed ? switchRow("upd-check", "Check for updates", "Once a day, asks GitHub whether a new version is out. Nothing about you or your records is sent.", u.check)
      : html`<p class="small muted upd-how">Checking for updates is turned off where this server is started (SYNTROPY_UPDATE_CHECK).</p>`}
    ${m.automatic && u.check_allowed ? switchRow("upd-auto", "Install updates automatically", autoHint, u.auto, !u.check) : ""}`);

  el.onclick = async (e) => {
    const b = e.target.closest("[data-upd]");
    if (!b) return;
    b.disabled = true;
    try {
      if (b.dataset.upd === "check") {
        const next = await post("/api/system/updates/check");
        toast(next.available ? `Syntropy Health ${next.latest.version} is available` : next.error ? next.error : "You have the latest version", next.error && !next.available ? "bad" : undefined);
        draw(el, next);
      } else {
        draw(el, await post("/api/system/updates/install"));
        follow(el);
      }
    } catch (err) { toast(err.message, "bad"); b.disabled = false; }
  };
  el.onchange = async (e) => {
    const box = e.target;
    const key = box.id === "upd-check" ? "check" : box.id === "upd-auto" ? "auto" : null;
    if (!key) return;
    try {
      const next = await put("/api/system/updates", { [key]: box.checked });
      toast(key === "check" ? (box.checked ? "Checking for updates daily" : "Update checks are off")
        : (box.checked ? "Updates install automatically" : "Updates wait for you"));
      draw(el, next);
    } catch (err) { toast(err.message, "bad"); box.checked = !box.checked; }
  };
}

/** While the app downloads and installs: show its progress until it closes to install. */
function follow(el) {
  const poll = async () => {
    if (!el.isConnected) return;
    let u;
    try { u = await get("/api/system/updates", {}, { fresh: true }); }
    catch { return; }     // the app is closing to install; it opens again with the new version
    draw(el, u);
    if (u.progress && u.progress.state !== "failed") setTimeout(poll, 1500);
  };
  setTimeout(poll, 1500);
}
