import { get } from "../api.js";
import { html, icon, mount, toast } from "../ui.js";
import { appBridge } from "../app.js";

// A letter asking a clinic for someone's records, for the clinics Syntropy Health can't connect to. It's made here on the
// page from what's typed: nothing is saved or sent anywhere.

const DELIVERY = {
  portal: "through my patient portal or a secure download link",
  email: "by email, to the address above. I understand ordinary email may not be secure, and I'm asking for it anyway",
  mail: "on a CD or USB drive, or on paper if you can't do that, by mail to the address above",
};

function longDate(value) {
  return value ? new Date(`${value}T12:00:00`).toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" }) : "";
}

/** The letter, as plain text: what's previewed, copied and printed. What 45 CFR 164.524 gives a person, in their words. */
export function letterText(f) {
  const today = new Date().toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
  const span = f.from || f.to
    ? `from ${longDate(f.from) || "the earliest you have"} to ${longDate(f.to) || "today"}`
    : "for all dates you have";
  const lines = [
    f.name || "[Your full name]",
    ...(f.address || "[Your mailing address]").split("\n"),
    f.contact || "[Your phone number or email]",
    "",
    today,
    "",
    "Medical Records / Health Information Management",
    f.facility || "[Name of the hospital or clinic]",
    ...(f.facilityAddress ? f.facilityAddress.split("\n") : []),
    "",
    "Re: Request for my medical records under HIPAA's right of access",
    `Patient: ${f.name || "[Your full name]"}`,
    `Date of birth: ${longDate(f.dob) || "[Your date of birth]"}`,
    "",
    "To the Medical Records Department:",
    "",
    "Under my right of access in the HIPAA Privacy Rule (45 CFR 164.524), I am asking for a copy of my health information "
      + `${span}. Please include my complete designated record set: office and hospital notes, test and lab results, `
      + "imaging reports, medications, allergies, immunizations, problem lists, procedures and discharge summaries.",
    "",
    "Please give it to me in electronic form if you can. A standard health data file (C-CDA or FHIR) is best, along with a PDF. "
      + `Please send it ${DELIVERY[f.delivery] || DELIVERY.portal}.`,
    "",
    "The rule asks you to act on this request within 30 days of receiving it. It allows only a reasonable, cost-based fee, so "
      + "please tell me the amount before charging one. If you can't give me some or all of this, please tell me why in writing.",
    "",
    "If you need to confirm who I am or need anything else from me, please contact me using the details above.",
    "",
    "Thank you,",
    "",
    "",
    "______________________________",
    f.name || "[Your full name]",
  ];
  return lines.join("\n");
}

/** Done when the dialog closes too: the iPhone app's printing doesn't tell the page when it's finished, and a report printed
 * later must not come out as this letter. */
export function clearPrintedLetter() {
  document.querySelector(".print-letter")?.remove();
  document.body.classList.remove("printing-letter");
}

export function printLetter(text) {
  clearPrintedLetter();
  const sheet = document.createElement("div");
  sheet.className = "print-letter";
  sheet.textContent = text;
  document.body.append(sheet);
  document.body.classList.add("printing-letter");
  window.addEventListener("afterprint", clearPrintedLetter, { once: true });
  // In the iPhone app, the app prints the page; web views can't open the print dialog.
  appBridge({ type: "print" }) || window.print();
}

async function copyLetter(text, area) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Letter copied");
  } catch {
    // Reached over the home network, a browser doesn't offer the clipboard to the page.
    const range = document.createRange();
    range.selectNodeContents(area);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    toast("The letter is selected. Copy it with your keyboard or browser menu");
  }
}

export async function openRecordsRequest(m, { facility = "", profileId } = {}) {
  let profile = null;
  try { profile = (await get("/api/profiles")).profiles.find((p) => p.id === profileId) || null; }
  catch { /* the form starts empty, which is fine */ }
  m.setBody(html`
    <div class="stack">
      <p class="muted">You have a legal right to a copy of your records, even when a clinic can't connect to an app. Fill this in and we'll
        write the letter. Print and sign it, then hand it in or mail it to the clinic's medical records office. Nothing here is saved or sent.</p>
      <div class="grid-2">
        <div class="field"><label class="label" for="rr-facility">Hospital or clinic</label><input class="input" id="rr-facility" value="${facility}" autocomplete="off"></div>
        <div class="field"><label class="label" for="rr-facility-address">Its address (optional)</label><input class="input" id="rr-facility-address" autocomplete="off"></div>
        <div class="field"><label class="label" for="rr-name">Your full name</label><input class="input" id="rr-name" value="${profile?.name || ""}" autocomplete="name"></div>
        <div class="field"><label class="label" for="rr-dob">Date of birth</label><input class="input" id="rr-dob" type="date" value="${profile?.birth_date || ""}"></div>
        <div class="field"><label class="label" for="rr-address">Your mailing address</label><input class="input" id="rr-address" autocomplete="street-address"></div>
        <div class="field"><label class="label" for="rr-contact">Your phone or email</label><input class="input" id="rr-contact" autocomplete="email"></div>
        <div class="field"><label class="label" for="rr-from">Records from (leave empty for all)</label><input class="input" id="rr-from" type="date"></div>
        <div class="field"><label class="label" for="rr-to">Records to</label><input class="input" id="rr-to" type="date"></div>
        <div class="field"><label class="label" for="rr-delivery">How to get them</label><select class="input" id="rr-delivery">
          <option value="portal">Patient portal or secure download</option><option value="email">Email</option><option value="mail">By mail</option></select></div>
      </div>
      <div><div class="label">Your letter</div><div class="letter" id="rr-letter" tabindex="0"></div></div>
      <p class="small muted">The clinic may also ask you to fill in its own form or show ID. When your records arrive as a file, bring them in with
        <button class="link-btn" data-action="import">Import a record file</button>. This isn't legal advice.</p>
      <div class="row between wrap"><button class="btn" data-action="back">Back</button>
        <div class="row"><button class="btn" data-action="copy-letter">Copy</button>
          <button class="btn btn-primary" data-action="print-letter">${icon("printer")} Print</button></div></div>
    </div>`);
  const value = (id) => m.body.querySelector(`#rr-${id}`).value.trim();
  const current = () => letterText({
    facility: value("facility"), facilityAddress: value("facility-address"), name: value("name"), dob: value("dob"), address: value("address"),
    contact: value("contact"), from: value("from"), to: value("to"), delivery: value("delivery"),
  });
  const area = m.body.querySelector("#rr-letter");
  const show = () => { mount(area, current()); };
  m.body.querySelector(".grid-2").addEventListener("input", show);
  show();
  m.letter = { print: () => printLetter(current()), copy: () => copyLetter(current(), area) };
}
