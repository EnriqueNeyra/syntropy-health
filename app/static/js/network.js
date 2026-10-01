// Who can reach this server: only this computer, or the iPhone and other devices at home and on Tailscale too.
// Used by Settings → Security, onboarding and the iPhone pairing dialog.
import { get, post, put } from "./api.js";
import { $, html, icon, mount, onAction, toast } from "./ui.js";

export const networkStatus = () => get("/api/system/network", undefined, { fresh: true });

/** Wait for the server to answer again after it restarts on its new address. */
async function backOnline() {
  await new Promise((r) => setTimeout(r, 1200));
  for (let i = 0; i < 40; i++) {
    try { if ((await fetch("/health", { cache: "no-store" })).ok) return true; } catch {}
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

export async function switchNetwork(enabled, { withoutPassword = false } = {}) {
  const res = await put("/api/system/network", { enabled, without_password: withoutPassword });
  if (res.restarting && !(await backOnline())) throw new Error("Syntropy Health didn't come back. Quit and reopen it.");
  return networkStatus();
}

/** The addresses other devices can use, each with a copy button. */
export function addressList(net) {
  if (!net.on_network) return "";
  return html`<div class="addr-list">${net.addresses.map((a) => html`
    <div class="addr-row"><span class="addr-kind">${a.label}</span><code class="grow">${a.url}</code>
      <button class="btn btn-ghost btn-sm btn-icon" type="button" data-action="copy-addr" data-url="${a.url}" aria-label="Copy ${a.url}">${icon("copy")}</button></div>`)}
    ${net.addresses.length ? "" : html`<div class="small muted">No network connection found. Connect this computer to Wi-Fi or Ethernet.</div>`}
  </div>`;
}

function explain(net) {
  if (!net.managed) {
    return net.on_network
      ? "This server listens on your network. Open it from your other devices at:"
      : html`This server only accepts connections from this computer. To change that, start it with <code>--host 0.0.0.0</code>.`;
  }
  return net.on_network
    ? html`Your iPhone and other devices at home${net.tailscale ? " and on Tailscale" : ""} can connect. Open it from them at:`
    : html`Only this computer can open Syntropy Health. Turn this on to sync your iPhone and open it from your other devices at home${net.tailscale ? " or over Tailscale" : ""}.`;
}

/**
 * The on/off switch (Mac and Windows apps) or the addresses (servers), with the password requirement.
 * `onChange(net)` runs after a switch, when the server answers on its new address.
 */
export function renderNetworkPanel(el, net, { onChange, waited = 0 } = {}) {
  // syntropyhealth.local is announced a few seconds after the server starts listening; show it once it is.
  if (net.on_network && !net.local_name && waited < 3) {
    setTimeout(async () => {
      if (!el.isConnected) return;
      try { renderNetworkPanel(el, await networkStatus(), { onChange, waited: waited + 1 }); } catch {}
    }, 3000);
  }
  const needsChoice = net.managed && !net.on_network && !net.password_set;
  mount(el, html`<div class="stack net-panel">
    ${net.managed ? html`<label class="set-row net-switch"><div class="set-label"><div class="set-title">Let your iPhone and other devices connect</div>
        <div class="set-hint">${explain(net)}</div></div>
        <div class="set-control"><span class="switch"><input type="checkbox" id="net-on" ${net.on_network ? "checked" : ""}><span></span></span></div></label>`
      : html`<p class="small muted">${explain(net)}</p>`}
    ${needsChoice ? html`<div class="banner warn net-needs-pass" hidden>${icon("shield")}<div class="grow">
        <p><b>Set a password first.</b> Otherwise anyone on your Wi-Fi could open your records. Your iPhone gets its own sign-in either way.</p>
        <form class="net-pass" id="net-pass">
          <input class="input" type="password" id="net-pass1" minlength="8" autocomplete="new-password" placeholder="Password (8 or more characters)" aria-label="Password">
          <input class="input" type="password" id="net-pass2" autocomplete="new-password" placeholder="Confirm password" aria-label="Confirm password">
          <button class="btn btn-primary btn-sm" type="submit">Set password and turn on</button></form>
        ${net.password_optional ? html`<p class="small"><button class="link-btn" type="button" data-action="no-pass">Development mode: turn on without a password</button></p>` : ""}</div></div>` : ""}
    ${addressList(net)}
    ${net.on_network && !net.password_set ? html`<div class="banner warn">${icon("shield")}<div class="grow">No password is set: anyone who can reach these addresses can open your records.</div></div>` : ""}
  </div>`);

  const apply = async (enabled, withoutPassword = false) => {
    const box = $("#net-on", el);
    if (box) box.disabled = true;
    try {
      const next = await switchNetwork(enabled, { withoutPassword });
      toast(enabled ? "Your other devices can connect now" : "Only this computer can connect now", "good");
      renderNetworkPanel(el, next, { onChange });
      onChange && onChange(next);
    } catch (err) {
      toast(err.message, "bad");
      renderNetworkPanel(el, net, { onChange });
    }
  };
  $("#net-on", el)?.addEventListener("change", (e) => {
    if (e.target.checked && needsChoice) {
      e.target.checked = false;
      $(".net-needs-pass", el).hidden = false;
      return;
    }
    apply(e.target.checked);
  });
  $("#net-pass", el)?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const pass = $("#net-pass1", el).value;
    if (pass.length < 8) return toast("Choose a password of at least 8 characters.", "bad");
    if (pass !== $("#net-pass2", el).value) return toast("The passwords don't match.", "bad");
    try { await post("/api/auth/password", { current: null, new: pass }); }
    catch (err) { return toast(err.message, "bad"); }
    net = { ...net, password_set: true };
    apply(true);
  });
  onAction(el, {
    "no-pass": () => apply(true, true),
    "copy-addr": async ({ url }) => {
      try { await navigator.clipboard.writeText(url); toast("Copied", "good"); }
      catch { toast(url); }
    },
  });
}
