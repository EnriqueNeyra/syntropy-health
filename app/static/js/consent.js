// What the person agrees to, kept short: their records live on their own computer and are theirs to secure; an AI
// outside their network sees what it's asked about; none of it is medical advice. Full text on the website.
import { post } from "./api.js";
import { confirmDialog, html } from "./ui.js";

export const TERMS_URL = "https://health.syntropylabs.io/terms/";
export const PRIVACY_URL = "https://health.syntropylabs.io/privacy/";
// Under the AGPL, a changed version that others use over a network must offer them its source: point this at yours.
export const SOURCE_URL = "https://github.com/EnriqueNeyra/syntropy-health";

/** The agreement checkbox for setup and the password screen. */
export function termsCheckbox(id = "s-terms") {
  return html`<label class="checkbox small terms-check"><input type="checkbox" id="${id}" required>
    <span>I understand that my health records are stored on this computer, that keeping it secure and backed up is up to
      me, and that Syntropy Health isn't medical advice.
      <a href="${TERMS_URL}" target="_blank" rel="noopener">Terms</a> · <a href="${PRIVACY_URL}" target="_blank" rel="noopener">Privacy</a></span></label>`;
}

/** Asked once, before Ask first uses an AI outside the person's network. Resolves true once confirmed. */
export async function confirmAiProvider(name) {
  const ok = await confirmDialog(`Send your questions to ${name}?`, html`${name} will see your questions and the health records Ask looks
    up to answer them, and ${name}'s own terms and privacy policy apply to them, not Syntropy Health's. For answers that
    never leave your network, use a local model instead.`, { confirmLabel: "I understand" });
  if (ok) await post("/api/ai/acknowledge");
  return ok;
}
