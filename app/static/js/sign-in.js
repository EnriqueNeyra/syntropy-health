// Leaving the app for another site's sign-in page. Going there is never a jump: a short, calm message fades in over the
// page first and stays until the other site has something to show. In the Mac app a native sheet takes over instead, and
// the page stays where it is. Coming back fades in too (data-arriving, set in theme.js before the first paint).
import { html, mount } from "./ui.js";

const FADE_MS = 220;      // the message's fade in app.css, so it's on screen before the page starts to go

/** Sends the page to url. restore() puts the page back as it was, if the person comes back without signing in. */
export function leaveForSignIn(url, restore = () => {}) {
  if (document.documentElement.getAttribute("data-shell") === "mac") {
    window.addEventListener("syntropy-sign-in-closed", () => restore(), { once: true });
    window.location.assign(url);
    return;
  }
  const there = new URL(url, location.href);
  const box = document.createElement("div");
  box.className = "leaving";
  box.setAttribute("role", "status");
  mount(box, html`<div class="leaving-words"><span class="spinner"></span>
    <p><b>${there.origin === location.origin ? "Taking you to sign in." : `Taking you to ${there.hostname} to sign in.`}</b></p>
    <p class="muted">You'll come back here when you're done.</p>
    <button class="btn btn-sm" type="button">Stay here</button></div>`);
  document.body.append(box);
  const back = () => {
    box.remove();
    window.removeEventListener("pageshow", cameBack);
    restore();
  };
  // The browser's Back button can bring this very page back, message and all.
  const cameBack = (e) => { if (e.persisted) back(); };
  window.addEventListener("pageshow", cameBack);
  // Only a slow site or a refused address leaves time to want this (the button fades in after a few seconds).
  box.querySelector("button").addEventListener("click", () => { window.stop(); back(); });
  setTimeout(() => window.location.assign(url), FADE_MS);
}
