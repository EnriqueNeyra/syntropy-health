// What the person acknowledges, kept short: their records live on their own computer and are theirs to secure; an AI
// outside their network sees what it's asked about; none of it is medical advice. Full text on the website.
import { post } from "./api.js";
import { confirmDialog, html } from "./ui.js";

export const TERMS_URL = "https://health.syntropylabs.io/terms/";
export const PRIVACY_URL = "https://health.syntropylabs.io/privacy/";
// Under the AGPL, a changed version that others use over a network must offer them its source: point this at yours.
export const SOURCE_URL = "https://github.com/EnriqueNeyra/syntropy-health";

/** The acknowledgment checkbox for setup and the password screen. */
export function termsCheckbox(id = "s-terms") {
  return html`<label class="checkbox small terms-check"><input type="checkbox" id="${id}" required>
    <span>I understand that my health records are stored on this computer, that keeping it secure and backed up is up to
      me, and that Syntropy Health isn't medical advice. This is an acknowledgment, not a condition of the open-source
      license. The Terms cover Syntropy's website and sign-in relay.
      <span class="nowrap"><a href="${TERMS_URL}" target="_blank" rel="noopener">Terms</a> · <a href="${PRIVACY_URL}" target="_blank" rel="noopener">Privacy</a></span></span></label>`;
}

/** Who an AI's data goes to, as the server keys it: a company, or the server at an address someone typed. */
export function vendorKey(provider, agent, address) {
  if (provider === "agent") return `agent:${agent}`;
  if (provider !== "local" && provider !== "custom") return provider;
  try { return `host:${new URL(address.includes("://") ? address : `http://${address}`).hostname.replace(/^\[|\]$/g, "")}`; } catch { return "host:"; }
}

/** Asked once for each company or server, before Ask first uses it. Resolves true once confirmed. */
export async function confirmAiProvider(name, vendor) {
  const ok = await confirmDialog(`Send your questions to ${name}?`, html`${name} will see your questions, your name and birth date, and the health records Ask looks
    up to answer them, and ${name}'s own terms and privacy policy apply to them, not Syntropy Health's. For answers that
    never leave your network, use a local model instead. You can take this back in Settings → AI.`, { confirmLabel: "I understand" });
  if (ok) await post("/api/ai/acknowledge", { vendor, name });
  return ok;
}

/** Asked once for each company whose model can't look things up: it's sent a summary of the record with every question. */
export async function confirmAiSummary(name, vendor) {
  const ok = await confirmDialog(`Send a summary of the record to ${name}?`, html`This model can't look things up, so Ask would send ${name} a
    summary of the record with every question: conditions, medications, lab results, visits and more. To send less, choose
    a model that can look things up.`, { confirmLabel: "Send a summary" });
  if (ok) await post("/api/ai/acknowledge", { vendor, name, summary: true });
  return ok;
}
