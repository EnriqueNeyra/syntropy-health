// First run: welcome and name, a password, then (once the record exists) other devices, bringing in data and AI.
import { get, post } from "./api.js";
import { networkStatus, renderNetworkPanel } from "./network.js";
import { $, html, icon, mount, onAction, toast, wordmark } from "./ui.js";
import { termsCheckbox } from "./consent.js";

const STEPS = 5;

function screen(step, content) {
  mount(document.getElementById("root"), html`
    <div class="auth-wrap"><div class="card auth-card onboard-card">
      <div class="onboard-top"><div class="brand">${wordmark()}</div>
        ${step ? html`<div class="onboard-dots" aria-label="Step ${step} of ${STEPS}">${Array.from({ length: STEPS }, (_, i) => html`<span class="${i < step ? "on" : ""}"></span>`)}</div>` : ""}</div>
      ${content}
    </div></div>`);
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
        themselves, from any computer or iPhone.</p>
      <div id="ob-found"></div>
      <div class="onboard-tiles">
        <button class="onboard-tile" type="button" data-action="join"><span class="ev-icon">${icon("users")}</span>
          <span class="grow"><b>Join your household's Syntropy Health</b><span class="small muted">It already runs on another computer
            at home. Sign in there as yourself.</span></span>${icon("chevronRight")}</button>
        <button class="onboard-tile" type="button" data-action="host"><span class="ev-icon">${icon("monitor")}</span>
          <span class="grow"><b>Set it up on this Mac</b><span class="small muted">This Mac becomes the server: it keeps everyone's
            records and should stay on for your iPhone to sync.</span></span>${icon("chevronRight")}</button>
      </div>`);
    onAction($(".onboard-card"), { join, host: welcome });
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

  const welcome = () => {
    screen(1, html`
      <h1>Welcome</h1>
      <p class="muted onboard-lede">Syntropy Health brings your health records, wearables and lab results together, in a private database
        on this computer.</p>
      <ul class="check-list onboard-points">
        <li>Your data stays here. Nothing is sent anywhere unless you connect it.</li>
        <li>Sync your iPhone and Apple Watch, Oura, WHOOP and patient portals like MyChart.</li>
        <li>Ask questions about it with an AI you choose, including one that runs on this computer.</li></ul>
      <form id="ob-name">
        <div class="field"><label class="label" for="s-name">Your first name</label>
          <input class="input" id="s-name" required maxlength="80" placeholder="e.g. Alex" autocomplete="given-name" value="${draft.name}" autofocus></div>
        <button class="btn btn-primary onboard-next" type="submit">Get started</button>
      </form>`);
    $("#s-name").focus();
    $("#ob-name").addEventListener("submit", (e) => {
      e.preventDefault();
      draft.name = $("#s-name").value.trim();
      password();
    });
  };

  const password = () => {
    screen(2, html`
      <h1>Protect your records</h1>
      <p class="muted onboard-lede">Your records are private, so Syntropy Health asks for this password whenever it's opened, on this
        computer or another device. Your iPhone gets its own sign-in when you pair it.</p>
      <form id="setup-form">
        <div class="field"><label class="label" for="s-pass">Password</label>
          <input class="input" id="s-pass" type="password" minlength="8" autocomplete="new-password" placeholder="At least 8 characters">
          <div class="hint">There is no recovery, so keep it in your password manager.</div></div>
        <div class="field"><label class="label" for="s-pass2">Confirm password</label>
          <input class="input" id="s-pass2" type="password" autocomplete="new-password"></div>
        ${status?.password_optional ? html`<label class="checkbox small muted field"><input type="checkbox" id="s-nopass">
          <span>Development mode: don't use a password.</span></label>` : ""}
        <div class="field">${termsCheckbox()}</div>
        <p class="tiny faint field">Time zone: ${tz}</p>
        <div class="onboard-foot"><button class="btn" type="button" data-action="back">Back</button>
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
      try {
        await post("/api/setup", { profile_name: draft.name || "Me", password: skip ? null : pass, timezone: tz, accept_terms: true });
        onDone();
      } catch (err) { toast(err.message, "bad"); }
    });
  };

  if (mac) choose(); else welcome();
}


// ------------------------------------------------------------------ set up before a password was required
/** Asks for the password an older setup doesn't have (and the agreement, if not given yet) before anything opens. */
export function renderPasswordGate(status, onDone) {
  mount(document.getElementById("root"), html`
    <div class="auth-wrap"><div class="card auth-card onboard-card">
      <div class="onboard-top"><div class="brand">${wordmark()}</div></div>
      <h1>Add a password</h1>
      <p class="muted onboard-lede">Syntropy Health now asks for a password, because it holds your health records. Everything else
        stays as it is.</p>
      <form id="gate-form">
        <div class="field"><label class="label" for="g-pass">Password</label>
          <input class="input" id="g-pass" type="password" minlength="8" autocomplete="new-password" placeholder="At least 8 characters" required>
          <div class="hint">There is no recovery, so keep it in your password manager.</div></div>
        <div class="field"><label class="label" for="g-pass2">Confirm password</label>
          <input class="input" id="g-pass2" type="password" autocomplete="new-password" required></div>
        ${status.terms_accepted ? "" : html`<div class="field">${termsCheckbox("g-terms")}</div>`}
        <button class="btn btn-primary onboard-next" type="submit">Save password</button>
      </form></div></div>`);
  $("#g-pass").focus();
  $("#gate-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const pass = $("#g-pass").value;
    if (pass.length < 8) return toast("Choose a password of at least 8 characters.", "bad");
    if (pass !== $("#g-pass2").value) return toast("The passwords don't match.", "bad");
    try {
      await post("/api/auth/password", { current: null, new: pass, accept_terms: true });
      onDone();
    } catch (err) { toast(err.message, "bad"); }
  });
}

/** Once, for instances set up before the agreement was asked for. */
export function renderTermsGate(onDone) {
  mount(document.getElementById("root"), html`
    <div class="auth-wrap"><div class="card auth-card onboard-card">
      <div class="onboard-top"><div class="brand">${wordmark()}</div></div>
      <h1>Before you continue</h1>
      <ul class="check-list onboard-points">
        <li>Your health records are stored on this computer, not with us. Keeping it secure, updated and backed up is up to you.</li>
        <li>If you connect an AI provider, it sees your questions and the records it looks up to answer them.</li>
        <li>Syntropy Health organizes your information; it isn't medical advice. Check anything important with your care team.</li></ul>
      <form id="terms-form"><div class="field">${termsCheckbox("t-terms")}</div>
        <button class="btn btn-primary onboard-next" type="submit">Continue</button></form></div></div>`);
  $("#terms-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await post("/api/terms/accept"); onDone(); } catch (err) { toast(err.message, "bad"); }
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
      <p class="muted onboard-lede">Syntropy Health runs on this computer. Your iPhone syncs to it, and you can open it from your other
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
      <p class="muted onboard-lede">${owner ? "Start with whatever you have." : "Your health data is yours here: only you see it, unless you share it. Start with whatever you have."}
        You can add the rest any time from Sources.</p>
      <div class="onboard-tiles">
        ${tile("iphone", "phone", "iPhone and Apple Watch", "Heart, sleep, workouts and more from Apple Health, with the Syntropy Health app")}
        ${tile("wearables", "watch", "Oura, WHOOP or Google Health", "Sign in once; new days sync on their own")}
        ${tile("ehr", "records", "Doctors and hospitals", "MyChart and other patient portals: visits, labs, medications")}
        ${tile("import", "upload", "Files", "An Apple Health export, a FHIR file or a lab report PDF")}
      </div>
      <div class="onboard-foot">${owner ? html`<button class="btn" data-action="back">Back</button>` : ""}<span class="grow"></span>
        <button class="btn" data-action="next">Skip for now</button></div>`);
    onAction($(".onboard-card"), {
      back: devices, next: owner ? ai : () => finish("#/overview"),
      iphone: () => finish("#/sources?add=iphone"),
      wearables: () => finish("#/sources"),
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
