// Backups in Settings, and restoring one, in Settings and on the first-run screen.
import { api, get } from "./api.js";
import { $, fmtDateTime, html, icon, modal, mount, plural, toast } from "./ui.js";

const fmtSize = (bytes) => (bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : `${Math.max(1, Math.round((bytes || 0) / 1024 ** 2))} MB`);

function passphraseFields(min, { optional = false } = {}) {
  return html`
    <div class="field"><label class="label" for="bk-pass">${optional ? "New passphrase (leave empty to keep the one you chose)" : "Passphrase"}</label>
      <input class="input" id="bk-pass" type="password" autocomplete="new-password" minlength="${min}" placeholder="At least ${min} characters"></div>
    <div class="field"><label class="label" for="bk-pass2">Passphrase again</label>
      <input class="input" id="bk-pass2" type="password" autocomplete="new-password"></div>`;
}

function readPassphrase(root, min, { optional = false } = {}) {
  const value = $("#bk-pass", root).value;
  if (!value && optional) return null;
  if (value.length < min) throw new Error(`Choose a passphrase of at least ${min} characters.`);
  if (value !== $("#bk-pass2", root).value) throw new Error("The passphrases don't match.");
  return value;
}

function busyButton(button, text) {
  button.disabled = true;
  button.dataset.label = button.textContent;
  button.textContent = text;
  return () => { button.disabled = false; button.textContent = button.dataset.label; };
}

// ------------------------------------------------------------------ a backup to download
export function openBackupDialog(info, onDone) {
  let finished = false;
  const m = modal({
    title: "Download a backup",
    onClose: () => { if (finished) onDone?.(); },      // redrawing the page closes dialogs, and this one has a link in it
    body: html`
      <p class="muted" style="margin-bottom:14px">One file with everything here, for everyone: records, wearable data, saved sign-ins, and the
        encryption key that opens them, so it restores on any computer. It's encrypted with a passphrase you choose.
        ${info.key_source === "setting" ? html`<b>Your encryption key comes from the SYNTROPY_SECRET_KEY setting, so it isn't in the file: keep that value safe too.</b>` : ""}</p>
      <div id="bk-fields">${passphraseFields(info.min_passphrase)}</div>
      <div class="hint" style="margin-bottom:14px">Keep the passphrase somewhere other than the backup. If it's lost, nobody can open the backup.</div>
      <label class="checkbox small field"><input type="checkbox" id="bk-plain"><span>Don't encrypt it.</span></label>
      <div class="banner warn" id="bk-warn" hidden>${icon("shield")}<div class="grow"><p><b>Anyone who gets an unencrypted backup can read every record and use every saved
        sign-in, for everyone here.</b> A synced folder, an email or a lost drive would give all of that away.</p></div></div>
      <div id="bk-result"></div>`,
    footer: html`<button class="btn" data-cancel>Cancel</button><button class="btn btn-primary" data-make>Make the backup</button>`,
  });
  const plain = $("#bk-plain", m.el);
  plain.addEventListener("change", () => {
    $("#bk-warn", m.el).hidden = !plain.checked;
    $("#bk-fields", m.el).hidden = plain.checked;
    $("[data-make]", m.el).textContent = plain.checked ? "Make it without encryption" : "Make the backup";
  });
  $("[data-cancel]", m.el).addEventListener("click", m.close);
  $("[data-make]", m.el).addEventListener("click", async (e) => {
    let done;
    try {
      const body = plain.checked ? { unencrypted: true } : { passphrase: readPassphrase(m.el, info.min_passphrase) };
      done = busyButton(e.currentTarget, "Making it…");
      const made = await api("/api/backups", { method: "POST", body, keepCache: true });
      m.setBody(html`<div class="banner info">${icon("check")}<div class="grow"><p>Your backup is ready: ${fmtSize(made.size_bytes)}, holding ${made.holds}.</p></div></div>
        ${made.key === "setting" ? html`<p class="small" style="margin-top:12px">The encryption key isn't in it. Keep your SYNTROPY_SECRET_KEY value with your backups.</p>` : ""}
        <p class="small muted" style="margin-top:12px">Store it somewhere other than this computer. It keeps anything you delete later, so remove older backups you no longer need.</p>`);
      mount($(".modal-foot", m.el), html`<button class="btn" data-cancel>Close</button>
        <a class="btn btn-primary" href="${made.download}" download="${made.filename}">${icon("download")} Download ${made.filename}</a>`);
      $("[data-cancel]", m.el).addEventListener("click", m.close);
      finished = true;
    } catch (err) { toast(err.message, "bad"); done?.(); }
  });
}

// ------------------------------------------------------------------ scheduled backups
export function scheduleText(s) {
  if (!s.enabled) return "Off. A copy kept on this computer alone won't survive a failed disk, so a schedule to another drive is worth setting up.";
  const when = s.every_hours === 24 ? "Every day" : "Every week";
  const last = s.state.last_success_at ? `Last: ${fmtDateTime(s.state.last_success_at)}, ${fmtSize(s.state.last_size_bytes)}.` : "None made yet.";
  return `${when} to ${s.folder}, keeping the last ${s.keep}. ${last}`;
}

export function openScheduleDialog(info, onDone) {
  const s = info.schedule;
  const m = modal({
    title: "Scheduled backups",
    body: html`
      <div class="field"><label class="label" for="sc-folder">Folder on the computer this server runs on</label>
        <input class="input" id="sc-folder" value="${s.folder || ""}" placeholder="/Volumes/Backup/Syntropy" autocapitalize="off" spellcheck="false">
        <div class="hint">Another drive is best: a folder on the same disk as ${info.data_dir} is lost with it. Files named syntropy-backup-… in this folder count towards what's kept.</div></div>
      <div class="row wrap" style="gap:12px">
        <div class="field grow"><label class="label" for="sc-every">How often</label><select class="input" id="sc-every">
          <option value="24" ${s.every_hours === 24 ? "selected" : ""}>Every day</option>
          <option value="168" ${s.every_hours === 168 ? "selected" : ""}>Every week</option></select></div>
        <div class="field grow"><label class="label" for="sc-keep">Keep the last</label>
          <input class="input" id="sc-keep" type="number" min="1" max="100" value="${s.keep}"></div></div>
      ${passphraseFields(info.min_passphrase, { optional: s.enabled })}
      <div class="banner info">${icon("shield")}<div class="grow"><p><b>What the passphrase protects.</b> It isn't saved. A key made from it is kept in this server's
        database, protected by this server's encryption key, so backups can run with nobody here. These backups are safe if the drive or folder they're in is lost or shared.
        They aren't safe from someone who has this computer's data folder, who could open them without the passphrase. To restore one you'll need the passphrase, so keep it somewhere else.</p></div></div>`,
    footer: html`${s.enabled ? html`<button class="btn btn-danger" data-off>Turn off</button><span class="grow"></span>` : ""}
      <button class="btn" data-cancel>Cancel</button><button class="btn btn-primary" data-save>Save and back up now</button>`,
  });
  $("[data-cancel]", m.el).addEventListener("click", m.close);
  $("[data-off]", m.el)?.addEventListener("click", async () => {
    try {
      await api("/api/backups/schedule", { method: "DELETE", keepCache: true });
      m.close(); toast("Scheduled backups are off. Those already made are still in the folder."); onDone();
    } catch (err) { toast(err.message, "bad"); }
  });
  $("[data-save]", m.el).addEventListener("click", async (e) => {
    let done;
    try {
      const body = { folder: $("#sc-folder", m.el).value.trim(), every_hours: Number($("#sc-every", m.el).value), keep: Number($("#sc-keep", m.el).value),
                     passphrase: readPassphrase(m.el, info.min_passphrase, { optional: s.enabled }) };
      done = busyButton(e.currentTarget, "Backing up…");
      await api("/api/backups/schedule", { method: "PUT", body, keepCache: true });
      const after = await api("/api/backups/schedule/run", { method: "POST", keepCache: true });
      m.close();
      const failed = after.schedule.state.last_error;
      toast(failed ? `Saved, but the backup failed: ${failed}` : "Saved, and a backup was made", failed ? "bad" : "good");
      onDone();
    } catch (err) { toast(err.message, "bad"); done?.(); }
  });
}

// ------------------------------------------------------------------ restoring
const KIND_TEXT = { "older-zip": "It's from an older version of Syntropy Health.", "database-only": "It's an older backup, holding only the database." };

/** Draws the steps inside ``root``. ``firstRun``: there's nothing here yet to replace. */
export function restoreSteps(root, { onCancel, onRestored }) {
  let file = null;
  let keyFile = null;
  let passphrase = "";

  const start = (message) => {
    mount(root, html`
      ${message ? html`<div class="banner bad" style="margin-bottom:12px"><div class="grow"><p>${message}</p></div></div>` : ""}
      <div class="field"><label class="label" for="rs-file">Backup file</label>
        <input class="input" id="rs-file" type="file">
        <div class="hint">A .syntropy file, or a .zip or .db from an older version. It's checked first, and nothing changes until you say so.</div></div>
      <div class="field"><label class="label" for="rs-pass">Passphrase, if it has one</label>
        <input class="input" id="rs-pass" type="password" autocomplete="off" value=""></div>
      <div class="row" style="gap:8px;justify-content:flex-end"><button class="btn" data-cancel type="button">Cancel</button>
        <button class="btn btn-primary" data-check type="button">Check this backup</button></div>`);
    $("[data-cancel]", root).addEventListener("click", onCancel);
    $("[data-check]", root).addEventListener("click", () => {
      file = $("#rs-file", root).files[0];
      passphrase = $("#rs-pass", root).value;
      keyFile = null;
      if (!file) return toast("Choose the backup file first.", "bad");
      check();
    });
  };

  const check = async () => {
    mount(root, html`<p class="small"><span class="spinner"></span> Checking ${file.name}. A large backup can take a few minutes.</p>`);
    const form = new FormData();
    form.append("file", file);
    if (passphrase) form.append("passphrase", passphrase);
    if (keyFile) form.append("key_file", keyFile);
    try {
      report(await api("/api/restore/check", { method: "POST", form, keepCache: true }));
    } catch (err) { start(err.message); }
  };

  const report = (r) => {
    const made = r.created_at ? fmtDateTime(r.created_at) : "an unknown date";
    mount(root, html`
      <div class="banner info" style="margin-bottom:12px">${icon("check")}<div class="grow"><p><b>This backup is complete and undamaged.</b> Made ${made}${r.app_version ? ` by version ${r.app_version}` : ""}.
        It holds ${r.holds}${r.counts.workouts ? `, ${plural(r.counts.workouts, "workout")}` : ""}${r.counts.journal ? `, ${plural(r.counts.journal, "journal entry", "journal entries")}` : ""}.</p>
        ${r.people.length ? html`<p class="small" style="margin-top:6px">People: ${r.people.join(", ")}</p>` : ""}
        ${KIND_TEXT[r.kind] ? html`<p class="small" style="margin-top:6px">${KIND_TEXT[r.kind]}</p>` : ""}
        ${r.upgrade_needed ? html`<p class="small" style="margin-top:6px">It's upgraded to this version when restored.</p>` : ""}</div></div>
      <div class="banner ${r.key.ok ? "" : "warn"}" style="margin-bottom:12px">${icon("shield")}<div class="grow"><p>${r.key.message}</p>
        ${r.key.ok ? "" : html`<div class="field" style="margin-top:10px"><label class="label" for="rs-key">The secret.key file</label>
          <input class="input" id="rs-key" type="file"><button class="btn btn-sm" data-recheck type="button" style="margin-top:8px">Check again with this key</button></div>
          <label class="checkbox small field"><input type="checkbox" id="rs-nokey"><span>Restore anyway. Nobody will be able to sign in or use saved sign-ins until the key is sorted out, from this computer's command line.</span></label>`}</div></div>
      ${r.replaces ? html`<div class="banner warn" style="margin-bottom:12px"><div class="grow"><p><b>This replaces everything here now</b>: ${r.replaces.holds}, for everyone.
        Anything added since this backup was made is left out. What's here now is kept in a folder beside the data, so this can be undone by hand.</p></div></div>
        <div class="field"><label class="label" for="rs-type">Type RESTORE to go ahead</label><input class="input" id="rs-type" autocomplete="off"></div>` : ""}
      <p class="small" style="margin-bottom:12px">${r.sign_in}</p>
      <div class="row" style="gap:8px;justify-content:flex-end"><button class="btn" data-cancel type="button">Cancel</button>
        <button class="btn ${r.replaces ? "btn-danger" : "btn-primary"}" data-apply type="button">Restore this backup</button></div>`);
    $("[data-cancel]", root).addEventListener("click", () => {
      api(`/api/restore/${encodeURIComponent(r.id)}`, { method: "DELETE", keepCache: true }).catch(() => {});
      onCancel();
    });
    $("[data-recheck]", root)?.addEventListener("click", () => {
      keyFile = $("#rs-key", root).files[0];
      if (!keyFile) return toast("Choose the secret.key file first.", "bad");
      api(`/api/restore/${encodeURIComponent(r.id)}`, { method: "DELETE", keepCache: true }).catch(() => {});
      check();
    });
    $("[data-apply]", root).addEventListener("click", async (e) => {
      const accept = !r.key.ok && $("#rs-nokey", root).checked;
      if (!r.key.ok && !accept) return toast("Add the key file, or choose to restore anyway.", "bad");
      if (r.replaces && $("#rs-type", root).value.trim() !== "RESTORE") return toast("Type RESTORE to go ahead.", "bad");
      const done = busyButton(e.currentTarget, "Restoring…");
      try {
        const out = await api("/api/restore/apply", { method: "POST", body: { id: r.id, accept_key_problem: accept }, keepCache: true });
        mount(root, html`<div class="banner info" style="margin-bottom:12px">${icon("check")}<div class="grow"><p><b>Restored: ${out.holds}.</b></p></div></div>
          <p style="margin-bottom:10px"><b>${out.sign_in}</b></p>
          <p class="small muted" style="margin-bottom:10px">Sources may ask you to sign in again if their sign-ins changed since the backup, and phones paired since then need pairing again.</p>
          ${out.safety_copy ? html`<p class="small muted" style="margin-bottom:12px">What was here before is kept in <code>${out.safety_copy}</code>.</p>` : ""}
          <div class="row" style="justify-content:flex-end"><button class="btn btn-primary" data-finish type="button">Continue to sign in</button></div>`);
        $("[data-finish]", root).addEventListener("click", onRestored);
      } catch (err) { toast(err.message, "bad"); done(); }
    });
  };

  start();
}

export function openRestoreDialog() {
  const m = modal({ title: "Restore from a backup", body: html`<div id="rs-steps"></div>` });
  restoreSteps($("#rs-steps", m.el), { onCancel: m.close, onRestored: () => location.reload() });
}

// ------------------------------------------------------------------ an archive coming in
export async function importArchive(file, profileId, status, onDone) {
  const form = new FormData();
  form.append("file", file);
  form.append("profile_id", profileId);
  mount(status, html`<p class="small" style="margin-top:8px"><span class="spinner"></span> Importing ${file.name}…</p>`);
  try {
    const { job_id: job } = await api("/api/imports/archive", { method: "POST", form });
    for (;;) {
      await new Promise((r) => setTimeout(r, 800));
      const state = await get(`/api/imports/${job}`, undefined, { fresh: true });
      if (state.status === "error") throw new Error(state.error);
      if (state.status === "completed") {
        const r = state.result;
        const records = r.clinical?.inserted || 0;
        const kept = (r.clinical?.kept_both || 0);
        mount(status, html`<div class="banner info" style="margin-top:8px">${icon("check")}<div class="grow"><p>Added ${plural(records, "record")}, ${plural(r.samples, "sample")},
          ${plural(r.workouts, "workout")}, ${plural(r.journal, "journal entry", "journal entries")}, ${plural(r.goals, "goal")} and ${plural(r.chats, "conversation")}.
          What was already here was left as it was.${kept ? ` ${plural(kept, "record")} differed from what's here, so both were kept.` : ""}</p></div></div>`);
        onDone?.();
        return;
      }
    }
  } catch (err) { mount(status, html`<div class="banner bad" style="margin-top:8px"><div class="grow"><p>${err.message}</p></div></div>`); }
}
