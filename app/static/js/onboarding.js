// First run: welcome and name, a password, then (once the record exists) other devices, bringing in data and AI.
import { get, post } from "./api.js";
import { networkStatus, renderNetworkPanel } from "./network.js";
import { $, EASE_OUT, busy, html, icon, mount, onAction, prefersStill, toast, wordmark } from "./ui.js";
import { termsCheckbox } from "./consent.js";
import { restoreSteps } from "./backup.js";

const STEPS = 5;

function screen(step, content) {
  const before = $(".onboard-card")?.getBoundingClientRect().height;
  mount(document.getElementById("root"), html`
    <div class="auth-wrap" role="main"><div class="card auth-card onboard-card">
      <div class="onboard-top"><div class="brand">${wordmark()}</div>
        ${step ? html`<div class="onboard-dots" role="img" aria-label="Step ${step} of ${STEPS}">${Array.from({ length: STEPS }, (_, i) => html`<span class="${i < step ? "on" : ""}"></span>`)}</div>` : ""}</div>
      ${content}
    </div></div>`);
  // One card that changes from step to step: it eases to the new size and its contents fade over, rather than jumping.
  const card = $(".onboard-card");
  if (!before || !card || prefersStill()) return;
  card.style.overflow = "hidden";
  const settled = () => { card.style.overflow = ""; };
  card.animate([{ height: `${before}px` }, { height: `${card.getBoundingClientRect().height}px` }], { duration: 240, easing: EASE_OUT }).finished.then(settled, settled);
  [...card.children].slice(1).forEach((part) => part.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 200, easing: "ease-out" }));
}

// ------------------------------------------------------------------ before the record exists
export function renderSetup(status, onDone) {
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const draft = { name: "" };
  // The Mac app can join the household's server on another computer instead of starting a new one here.
  const mac = document.documentElement.getAttribute("data-shell") === "mac" && !!window.webkit?.messageHandlers?.syntropyDesktop;
  let servers = null;          // found on the network (null while looking)
  const looking = mac ? get("/api/desktop/discover", undefined, { fresh: true }).then((r) => r.servers, () => []) : Promise.resolve([]);
  looking.then((found) => { servers = found; });

  const choose = () => {
    screen(null, html`
      <h1>Welcome to Syntropy Health</h1>
      <p class="muted onboard-lede">Your household keeps its health records on one computer, the server. Each person signs in as
        themselves, from any computer or phone.</p>
      <div id="ob-found"></div>
      <div class="onboard-tiles">
        <button class="onboard-tile" type="button" data-action="join"><span class="ev-icon">${icon("users")}</span>
          <span class="grow"><b>Join your household's Syntropy Health</b><span class="small muted">It already runs on another computer
            at home. Sign in there as yourself.</span></span>${icon("chevronRight")}</button>
        <button class="onboard-tile" type="button" data-action="host"><span class="ev-icon">${icon("monitor")}</span>
          <span class="grow"><b>Set it up on this Mac</b><span class="small muted">This Mac becomes the server: it keeps everyone's
            records and should stay on for your phone to sync.</span></span>${icon("chevronRight")}</button>
        <button class="onboard-tile" type="button" data-action="restore"><span class="ev-icon">${icon("upload")}</span>
          <span class="grow"><b>Restore from a backup</b><span class="small muted">Bring back everything from a backup file you made
            with Syntropy Health.</span></span>${icon("chevronRight")}</button>
      </div>`);
    onAction($(".onboard-card"), { join, host: welcome, restore: () => restore(choose) });
    looking.then((found) => {
      const box = $("#ob-found");
      if (!box || !found.length) return;
      mount(box, html`<div class="banner info" style="margin-bottom:14px">${icon("users")}<div class="grow"><p>Found on your
        network: <b>${found.map((f) => f.name).join(", ")}</b>. Join it rather than setting up another one here.</p></div></div>`);
    });
  };

  const join = async () => {
    screen(null, html`
      <h1>Join your household</h1>
      <p class="muted onboard-lede">Choose the computer Syntropy Health runs on. You'll sign in there with your own password, or the
        invitation you were given.</p>
      <div id="ob-servers" class="stack">${servers ? "" : html`<p class="small muted">Looking on your network…</p>`}</div>
      <form id="ob-address" class="field" style="margin-top:14px"><label class="label" for="ob-url">Or enter its address</label>
        <div class="row" style="gap:8px"><input class="input grow" id="ob-url" placeholder="http://192.168.1.20:8000" autocapitalize="off" spellcheck="false">
          <button class="btn" type="submit">Connect</button></div>
        <div class="hint">On that computer: Settings → Security shows its addresses. Tailscale addresses work too.</div></form>
      <div class="onboard-foot"><button class="btn" data-action="back">Back</button></div>`);
    onAction($(".onboard-card"), {
      back: choose,
      pick: ({ i }) => connect(servers[Number(i)]),
    });
    $("#ob-address").addEventListener("submit", (e) => {
      e.preventDefault();
      const url = $("#ob-url").value.trim();
      if (url) connect({ url, name: null });
    });
    const found = await looking;
    const list = $("#ob-servers");
    if (!list) return;
    mount(list, found.length ? html`<div class="onboard-tiles">${found.map((f, i) => html`<button class="onboard-tile" type="button" data-action="pick" data-i="${i}">
        <span class="ev-icon">${icon("monitor")}</span><span class="grow"><b>${f.name}</b><span class="small muted">${f.url}</span></span>${icon("chevronRight")}</button>`)}</div>`
      : html`<p class="small muted">No Syntropy Health server found on this network. It may not let other devices connect yet (on that
          computer: Settings → Security), or this Mac may be on another network: enter its address below.</p>`);
  };

  const connect = async (server) => {
    try {
      const ok = await post("/api/desktop/probe", { url: server.url, address: server.address || null });
      mount($(".onboard-card"), html`<div class="brand">${wordmark()}</div><p class="muted" style="margin-top:14px">Connecting to ${server.name || ok.url}…</p>`);
      window.webkit.messageHandlers.syntropyDesktop.postMessage(JSON.stringify({ type: "join", url: ok.url, name: server.name }));
    } catch (err) { toast(err.message, "bad"); }
  };

  const restore = (back) => {
    screen(null, html`
      <h1>Restore from a backup</h1>
      <p class="muted onboard-lede">Everything comes back as it was when the backup was made: records, people, passwords and saved sign-ins.</p>
      <div id="ob-restore"></div>`);
    restoreSteps($("#ob-restore"), { onCancel: back, onRestored: () => { history.replaceState(null, "", "#/"); location.reload(); } });
  };

  const welcome = () => {
    screen(1, html`
      <h1>Welcome</h1>
      <p class="muted onboard-lede">Syntropy Health brings your health records, wearables and lab results together, in a private database
        on this computer.</p>
      <ul class="check-list onboard-points">
        <li>Your data stays here. Nothing is sent anywhere unless you connect it.</li>
        <li>Sync your iPhone or Android phone, your watch, Oura, WHOOP and patient portals like MyChart.</li>
        <li>Ask questions about it with an AI you choose, including one that runs on this computer.</li></ul>
      <form id="ob-name">
        <div class="field"><label class="label" for="s-name">Your first name</label>
          <input class="input" id="s-name" required maxlength="80" placeholder="e.g. Alex" autocomplete="given-name" value="${draft.name}" autofocus></div>
        <button class="btn btn-primary onboard-next" type="submit">Get started</button>
      </form>
      <p class="small muted" style="margin-top:14px;text-align:center">Moving from another computer?
        <button class="link-btn" type="button" data-action="restore">Restore from a backup</button></p>`);
    $("#s-name").focus();
    onAction($(".onboard-card"), { restore: () => restore(welcome) });
    $("#ob-name").addEventListener("submit", (e) => {
      e.preventDefault();
      draft.name = $("#s-name").value.trim();
      if (!draft.name) {
        toast("Enter your first name.", "bad");
        $("#s-name").focus();
        return;
      }
      password();
    });
  };

  const password = () => {
    screen(2, html`
      <h1>Protect your records</h1>
      <p class="muted onboard-lede">Your records are private, so Syntropy Health asks for this password whenever it's opened, on this
        computer or another device. Your phone gets its own sign-in when you pair it.</p>
      <form id="setup-form">
        <div class="field"><label class="label" for="s-pass">Password</label>
          <input class="input" id="s-pass" type="password" minlength="8" autocomplete="new-password" placeholder="At least 8 characters">
          <div class="hint">${RECOVERY_HINT}</div></div>
        <div class="field"><label class="label" for="s-pass2">Confirm password</label>
          <input class="input" id="s-pass2" type="password" autocomplete="new-password"></div>
        ${status?.setup_code_needed ? setupCodeField("s-code") : ""}
        ${status?.password_optional ? html`<label class="checkbox small muted field"><input type="checkbox" id="s-nopass">
          <span>Development mode: don't use a password.</span></label>` : ""}
        <div class="field">${termsCheckbox()}</div>
        <p class="tiny faint field">Time zone: ${tz}</p>
        <div class="onboard-foot"><button class="btn" type="button" data-action="back">Back</button><span class="grow"></span>
          <button class="btn btn-primary" type="submit">Create my health record</button></div>
      </form>`);
    const form = $("#setup-form");
    $("#s-pass").focus();
    onAction(form, { back: welcome });
    $("#s-nopass")?.addEventListener("change", (e) => { $("#s-pass").disabled = $("#s-pass2").disabled = e.target.checked; });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const skip = !!$("#s-nopass")?.checked;
      const pass = $("#s-pass").value;
      if (!skip && pass.length < 8) return toast("Choose a password of at least 8 characters.", "bad");
      if (!skip && pass !== $("#s-pass2").value) return toast("The passwords don't match.", "bad");
      await busy(e.submitter, async () => {
        try {
          await post("/api/setup", { profile_name: draft.name, password: skip ? null : pass, timezone: tz, accept_terms: true,
                                     setup_code: $("#s-code")?.value.trim() || undefined });
          onDone();
        } catch (err) {
          // A second submit that crossed the first: the record exists, so carry on.
          if (err.status === 409 && (await get("/api/status", undefined, { fresh: true }).catch(() => null))?.setup_complete) return onDone();
          toast(err.message, "bad");
        }
      });
    });
  };

  if (mac) choose(); else welcome();
}


const RECOVERY_HINT = "Keep it in your password manager. Syntropy Labs can't recover it, but someone at the computer this server runs on can set a new one.";

/** Setting up from somewhere other than this computer or the home network takes the code the server printed to its log. */
function setupCodeField(id) {
  return html`<div class="field"><label class="label" for="${id}">Setup code</label>
    <input class="input mono" id="${id}" autocomplete="one-time-code" autocapitalize="characters" spellcheck="false" required placeholder="ABCD-EFGH">
    <div class="hint">You're not on this server's computer or its home network, so it asks for proof it's yours. The code is in the server's log, printed when it started
      (for Docker: <code>docker logs syntropy-health</code>).</div></div>`;
}


// ------------------------------------------------------------------ set up before a password was required
/** Asks for the password an older setup doesn't have (and the agreement, if not given yet) before anything opens. */
export function renderPasswordGate(status, onDone) {
  mount(document.getElementById("root"), html`
    <div class="auth-wrap" role="main"><div class="card auth-card onboard-card">
      <div class="onboard-top"><div class="brand">${wordmark()}</div></div>
      <h1>Add a password</h1>
      <p class="muted onboard-lede">Syntropy Health now asks for a password, because it holds your health records. Everything else
        stays as it is.</p>
      <form id="gate-form">
        <div class="field"><label class="label" for="g-pass">Password</label>
          <input class="input" id="g-pass" type="password" minlength="8" autocomplete="new-password" placeholder="At least 8 characters" required>
          <div class="hint">${RECOVERY_HINT}</div></div>
        <div class="field"><label class="label" for="g-pass2">Confirm password</label>
          <input class="input" id="g-pass2" type="password" autocomplete="new-password" required></div>
        ${status.setup_code_needed ? setupCodeField("g-code") : ""}
        ${status.terms_accepted ? "" : html`<div class="field">${termsCheckbox("g-terms")}</div>`}
        <button class="btn btn-primary onboard-next" type="submit">Save password</button>
      </form></div></div>`);
  $("#g-pass").focus();
  $("#gate-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const pass = $("#g-pass").value;
    if (pass.length < 8) return toast("Choose a password of at least 8 characters.", "bad");
    if (pass !== $("#g-pass2").value) return toast("The passwords don't match.", "bad");
    await busy(e.submitter, async () => {
      try {
        await post("/api/auth/password", { current: null, new: pass, accept_terms: true, setup_code: $("#g-code")?.value.trim() || undefined });
        onDone();
      } catch (err) { toast(err.message, "bad"); }
    });
  });
}

/** Once, for instances set up before the agreement was asked for. */
export function renderTermsGate(onDone) {
  mount(document.getElementById("root"), html`
    <div class="auth-wrap" role="main"><div class="card auth-card onboard-card">
      <div class="onboard-top"><div class="brand">${wordmark()}</div></div>
      <h1>Before you continue</h1>
      <ul class="check-list onboard-points">
        <li>Your health records are stored on this computer, not with us. Keeping it secure, updated and backed up is up to you.</li>
        <li>If you connect an AI provider, it sees your questions and the records it looks up to answer them.</li>
        <li>Syntropy Health organizes your records and doesn't give medical advice.</li></ul>
      <form id="terms-form"><div class="field">${termsCheckbox("t-terms")}</div>
        <button class="btn btn-primary onboard-next" type="submit">Continue</button></form></div></div>`);
  $("#terms-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await busy(e.submitter, async () => {
      try { await post("/api/terms/accept"); onDone(); } catch (err) { toast(err.message, "bad"); }
    });
  });
}

// ------------------------------------------------------------------ after: devices, data, AI
/** Someone who joined by invitation starts at their data: the other devices and the AI are the owner's to set up. */
export function renderOnboarding(status, onDone) {
  const owner = status.account?.owner !== false;
  const finish = async (hash) => {
    try { await post("/api/onboarding/done"); } catch {}
    history.replaceState(null, "", hash);     // no hashchange: the app isn't drawn yet
    onDone();
  };

  const devices = async () => {
    screen(3, html`
      <h1>Your other devices</h1>
      <p class="muted onboard-lede">Syntropy Health runs on this computer. Your phone syncs to it, and you can open it from your other
        devices too.</p>
      <div id="ob-net">${html`<div class="skeleton" style="height:72px"></div>`}</div>
      <div class="onboard-foot"><span class="grow"></span><button class="btn btn-primary" data-action="next">Continue</button></div>`);
    onAction($(".onboard-card"), { next: data });
    try {
      const net = await networkStatus();
      renderNetworkPanel($("#ob-net"), net);
    } catch (err) { mount($("#ob-net"), html`<p class="small muted">${err.message}</p>`); }
  };

  const tile = (action, ic, title, text) => html`<button class="onboard-tile" type="button" data-action="${action}">
      <span class="ev-icon">${icon(ic)}</span><span class="grow"><b>${title}</b><span class="small muted">${text}</span></span>${icon("chevronRight")}</button>`;

  const data = () => {
    screen(owner ? 4 : null, html`
      <h1>${owner ? "Bring in your data" : `Welcome, ${status.account?.name || ""}`}</h1>
      <p class="muted onboard-lede">${owner ? "Start with whatever you have." : "Your health data is yours here: nobody sees it in the app unless you share it. Start with whatever you have."}
        You can add the rest any time from Sources.</p>
      <div class="onboard-tiles">
        ${tile("ehr", "records", "Doctors and hospitals", "MyChart and other patient portals: visits, labs, medications")}
        ${tile("iphone", "phone", "Your phone and watch", "Heart, sleep, workouts and more from Apple Health or Health Connect, with the Syntropy Health app")}
        ${tile("wearables", "watch", "Oura, WHOOP or Google Health", "Sign in once; new days sync on their own")}
        ${tile("import", "upload", "Files", "An Apple Health export, a portal health summary, a FHIR file or a lab report PDF")}
      </div>
      <div class="onboard-foot">${owner ? html`<button class="btn" data-action="back">Back</button>` : ""}<span class="grow"></span>
        <button class="btn" data-action="next">Skip for now</button></div>`);
    onAction($(".onboard-card"), {
      back: devices, next: owner ? ai : () => finish("#/overview"),
      iphone: () => finish("#/sources?add=iphone"),
      wearables: () => finish("#/sources?add=wearable"),
      ehr: () => finish("#/sources?add=ehr"),
      import: () => finish("#/sources?add=import"),
    });
  };

  const ai = async () => {
    screen(5, html`
      <h1>Ask about your health</h1>
      <p class="muted onboard-lede">Optional. Ask questions like “How has my sleep changed this month?” using an AI you choose. A model on this
        computer keeps every question here; an online AI sees what it looks up to answer.</p>
      <div id="ob-ai" class="stack small muted">Looking for AI on this computer…</div>
      <div class="onboard-foot"><button class="btn" data-action="back">Back</button><span class="grow"></span>
        <button class="btn" data-action="setup-ai">Set up AI</button>
        <button class="btn btn-primary" data-action="done">Finish</button></div>`);
    onAction($(".onboard-card"), {
      back: data, "setup-ai": () => finish("#/settings/ai"), done: () => finish("#/overview"),
    });
    const [local, agents] = await Promise.all([
      get("/api/ai/local/detect", undefined, { fresh: true }).catch(() => null),
      get("/api/ai/agents", undefined, { fresh: true }).catch(() => null),
    ]);
    const found = [
      ...(local?.servers || []).filter((s) => s.models?.length).map((s) => `${s.name} with ${s.models.length} model${s.models.length === 1 ? "" : "s"}`),
      ...(agents?.agents || []).filter((a) => a.installed).map((a) => a.label),
    ];
    const box = $("#ob-ai");
    if (box) mount(box, found.length
      ? html`<div class="banner info">${icon("check")}<div class="grow">Found on this computer: ${found.join(", ")}. Choose one in <b>Set up AI</b>.</div></div>`
      : html`<p>Nothing found on this computer yet. You can install a local model such as Ollama, use an API key, or connect Claude Code
          or Codex later in Settings → AI.</p>`);
  };

  if (owner) devices(); else data();
}
