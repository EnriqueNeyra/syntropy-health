// Add lab results: typed in, or read from a report (PDF or photo) on the server or with the person's AI provider key,
// then reviewed and saved.
import { api, get, post } from "../api.js";
import { $, $$, fieldsChanged, html, icon, loading, modal, toast } from "../ui.js";
import { confirmAiProvider } from "../consent.js";

const NUMBER = /^-?\d+(\.\d+)?$/;
const today = () => new Date().toLocaleDateString("en-CA");   // YYYY-MM-DD in local time

/**
 * What's typed in a range box, the way labs print it: "200", "<200", "<=200", ">40", "3.5-5.0". Returns the bounds it gives, or
 * null when it can't be read. A one-sided bound fills its own side whichever box it was typed in.
 */
export function parseRange(text) {
  const t = String(text ?? "").trim().replace(/≤/g, "<=").replace(/≥/g, ">=").replace(/[–—]/g, "-");
  if (!t) return {};
  if (NUMBER.test(t)) return { value: Number(t) };
  let m = t.match(/^<=?\s*(\d+(?:\.\d+)?)$/);
  if (m) return { high: Number(m[1]) };
  m = t.match(/^>=?\s*(\d+(?:\.\d+)?)$/);
  if (m) return { low: Number(m[1]) };
  m = t.match(/^(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)$/i);
  if (m) return { low: Number(m[1]), high: Number(m[2]) };
  return null;
}

/** The server's form of one typed row, and what's wrong with it. A row with nothing in it isn't a row. */
export function checkRow(r) {
  const value = String(r.value ?? "").trim();
  const filled = [r.test, value, r.ref_low, r.ref_high].some((x) => String(x ?? "").trim());
  if (!filled) return { empty: true };
  const problems = [];
  if (!String(r.test ?? "").trim()) problems.push("Add the test's name.");
  if (!value) problems.push("Add the result.");
  const low = parseRange(r.ref_low);
  const high = parseRange(r.ref_high);
  if (low === null || high === null) problems.push("Range: use a number, or a form like <200 or 3.5-5.0.");
  const bounds = { ref_low: low?.low ?? low?.value ?? high?.low ?? null, ref_high: high?.high ?? high?.value ?? low?.high ?? null };
  if (bounds.ref_low != null && bounds.ref_high != null && bounds.ref_low > bounds.ref_high) problems.push("Range: the low end is above the high end.");
  return { problems, bounds };
}

export async function openLabEntry(profile, onSaved) {
  // The dialog opens at once; what it needs to know loads inside it.
  let rows = [{}, {}, {}];
  const m = modal({ title: "Add lab results", wide: true, body: loading(3),
    dirty: () => fieldsChanged(m.el) || rows.some((r) => r.test || r.value),
    footer: html`<button class="btn" data-cancel>Cancel</button><button class="btn btn-primary" data-save disabled>Save results</button>` });
  m.el.querySelector("[data-cancel]").addEventListener("click", m.close);
  let catalog, ai, reader;
  try {
    [catalog, ai, reader] = await Promise.all([get("/api/labs/catalog", { profile: profile.id }), get("/api/ai/config"),
      get("/api/labs/reader")]);
  } catch (err) { m.close(); toast(err.message, "bad"); return; }
  m.el.querySelector("[data-save]").disabled = false;
  const known = new Map();
  for (const t of [...catalog.common, ...catalog.yours]) known.set(t.name.toLowerCase(), t);
  let mode = "type";
  let collected = today();
  let lab = "";
  let reading = "";         // "local" | "ai" while a report is being read
  let fromReport = null;    // how the rows in the table were read
  let file = null;          // kept across redraws, so a report can be read again another way


  const readRows = () => {
    collected = $("#lab-date", m.el)?.value || collected;
    lab = $("#lab-name", m.el)?.value ?? lab;
    rows = $$(".lab-row", m.el).map((tr) => {
      const get = (k) => tr.querySelector(`[data-k="${k}"]`).value.trim();
      return { test: get("test"), value: get("value"), unit: get("unit"), ref_low: get("ref_low"), ref_high: get("ref_high"),
               loinc: tr.dataset.loinc || "", flag: tr.dataset.flag || "", date: tr.dataset.date || "", matched: tr.dataset.matched || "" };
    });
  };

  const draw = () => {
    m.setBody(html`<div class="stack">
      <div class="chips">
        <button class="chip ${mode === "type" ? "active" : ""}" data-mode="type">Type them in</button>
        <button class="chip ${mode === "report" ? "active" : ""}" data-mode="report">${icon("upload")} From a report (PDF or photo)</button></div>
      ${mode === "report" ? html`<div class="card card-pad stack">
          <p class="small muted">A PDF from your lab's website, or a photo or scan of a paper report.</p>
          <input type="file" id="lab-file" accept="application/pdf,image/*" class="input" style="max-width:360px">
          <div class="row wrap">
            <button class="btn btn-primary btn-sm" data-read="local" ${reading ? "disabled" : ""}>${reading === "local" ? "Reading…" : "Read privately"}</button>
            ${ai.configured ? html`<button class="btn btn-sm" data-read="ai" ${reading ? "disabled" : ""}>${icon("sparkles")} ${reading === "ai" ? "Reading…" : `Read with ${ai.label}`}</button>` : ""}</div>
          <p class="hint">Read privately: your Syntropy server reads the file and it goes nowhere else.
            ${reader.ocr ? "" : html`Photos and scans need a free program called Tesseract on the computer running Syntropy Health.
              ${reader.install} PDFs from lab websites work without it.`}
            ${ai.configured ? html`${ai.label} (${ai.model}) is usually better with blurry or crooked photos; that sends the file to ${ai.label} with your key.`
              : html`For hard-to-read photos you can also <a href="#/settings/ai">add your own AI key</a>.`}</p></div>` : ""}
      ${fromReport ? html`<div class="banner warn small"><div class="grow">${fromReport === "ocr"
          ? "Read from a photo. Photo reading makes mistakes: compare every name, value, unit and range with the paper before saving."
          : "Check every value, unit and range against the report before saving."}</div></div>` : ""}
      <div class="grid grid-2">
        <div class="field"><label class="label" for="lab-date">Date collected</label><input class="input" type="date" id="lab-date" value="${collected}" max="${today()}"></div>
        <div class="field"><label class="label" for="lab-name">Lab or clinic (optional)</label><input class="input" id="lab-name" value="${lab}" placeholder="e.g. Quest Diagnostics"></div>
      </div>
      <datalist id="lab-tests">${[...known.values()].map((t) => html`<option value="${t.name}">`)}</datalist>
      <div class="table-wrap"><table class="table lab-table">
        <thead><tr><th>Test</th><th>Result</th><th>Unit</th><th>Range low</th><th>Range high</th><th aria-label="Remove row"></th></tr></thead>
        <tbody>${rows.map((r, i) => html`<tr class="lab-row" data-loinc="${r.loinc || ""}" data-flag="${r.flag || ""}" data-date="${r.date || ""}" data-matched="${r.matched || ""}">
          <td><input class="input" data-k="test" list="lab-tests" value="${r.test || ""}" placeholder="e.g. Ferritin" aria-label="Test">
            ${r.matched ? html`<div class="small muted" data-matched-note>Filed as ${r.matched}</div>` : ""}
            <div class="small" style="color:var(--bad)" data-row-problem role="alert"></div></td>
          <td><input class="input" data-k="value" value="${r.value ?? ""}" placeholder="48" aria-label="Result" inputmode="decimal"></td>
          <td><input class="input" data-k="unit" value="${r.unit || ""}" placeholder="ng/mL" aria-label="Unit"></td>
          <td><input class="input" data-k="ref_low" value="${r.ref_low ?? ""}" aria-label="Range low"></td>
          <td><input class="input" data-k="ref_high" value="${r.ref_high ?? ""}" aria-label="Range high"></td>
          <td><button class="btn btn-ghost btn-icon btn-sm" data-remove="${i}" aria-label="Remove row">${icon("x")}</button></td></tr>`)}</tbody>
      </table></div>
      <div><button class="btn btn-sm" data-add>${icon("plus")} Add another test</button></div>
      <p class="hint">A result like "&lt;0.5" is kept as a limit, and one like "Negative" as text. Ranges are optional and can be typed the way
        labs print them ("&lt;200" or "3.5-5.0"); with them, out-of-range results are flagged on your Overview.</p>
    </div>`);
    const input = $("#lab-file", m.el);
    if (input && file) { const dt = new DataTransfer(); dt.items.add(file); input.files = dt.files; }
  };

  m.el.addEventListener("click", async (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    if (t.dataset.mode) { readRows(); mode = t.dataset.mode; draw(); }
    else if (t.hasAttribute("data-add")) { readRows(); rows.push({}); draw(); $$(".lab-row [data-k=test]", m.el).at(-1).focus(); }
    else if (t.dataset.remove !== undefined) { readRows(); rows.splice(Number(t.dataset.remove), 1); if (!rows.length) rows.push({}); draw(); }
    else if (t.dataset.read) {
      if (!file) return toast("Choose a PDF or photo first.");
      if (t.dataset.read === "ai" && ai.sends_elsewhere && !ai.cloud_ack) {
        if (!(await confirmAiProvider(ai.vendor_name, ai.vendor_key))) return;
        ai.cloud_ack = true;
      }
      readRows(); reading = t.dataset.read; draw();
      try {
        const form = new FormData();
        form.append("file", file);
        form.append("method", reading);
        form.append("profile_id", profile.id);
        const out = await api("/api/labs/extract", { method: "POST", form });
        if (!out.results.length) {
          toast(reading === "local" && ai.configured ? `No results found. Try a sharper photo taken straight on, or Read with ${ai.label}.`
            : "No results found. Try a sharper photo taken straight on, in good light.", "bad");
        } else {
          rows = out.results.map((r) => ({ test: r.test, value: r.value ?? r.value_text ?? "", unit: r.unit, ref_low: r.ref_low,
            ref_high: r.ref_high, loinc: r.loinc, flag: r.flag, date: r.date, matched: r.matched_test || "" }));
          if (out.collected) collected = out.collected;
          if (out.lab) lab = out.lab;
          fromReport = out.read_as === "ocr" ? "ocr" : "report"; mode = "type";
          toast(`Found ${rows.length} results — please check them`, "good");
        }
      } catch (err) { toast(err.message, "bad"); }
      reading = ""; draw();
    }
  });
  // The chosen report survives redraws (switching tabs, reading), so it can be read again the other way.
  m.el.addEventListener("change", (e) => { if (e.target.id === "lab-file") file = e.target.files[0] || null; });
  // Picking a known test fills in its unit and LOINC code (so results join the existing trend).
  m.el.addEventListener("change", (e) => {
    if (e.target.dataset.k !== "test") return;
    const hit = known.get(e.target.value.trim().toLowerCase());
    const tr = e.target.closest(".lab-row");
    tr.dataset.loinc = hit?.loinc || "";
    tr.dataset.matched = "";
    tr.querySelector("[data-matched-note]")?.remove();
    const unit = tr.querySelector("[data-k=unit]");
    if (hit?.unit && !unit.value) unit.value = hit.unit.replace(/\{[^}]*\}/g, "");
  });
  m.el.querySelector("[data-save]").addEventListener("click", async (e) => {
    readRows();
    const dateBox = $("#lab-date", m.el);
    if (dateBox && !dateBox.reportValidity()) return;
    // A half-filled row or an unreadable range is pointed out where it is. It's never left out with a message that sounds like success.
    const checks = rows.map(checkRow);
    let blocked = false;
    $$(".lab-row", m.el).forEach((tr, i) => {
      const problems = checks[i].problems || [];
      tr.querySelector("[data-row-problem]").textContent = problems.join(" ");
      blocked = blocked || problems.length > 0;
    });
    if (blocked) return toast("Some rows need fixing before they can be saved. Fix them, or remove them.", "bad");
    const results = rows.flatMap((r, i) => {
      if (checks[i].empty) return [];
      const v = String(r.value).trim();
      return [{ test: r.matched || r.test, loinc: r.loinc || known.get(r.test.toLowerCase())?.loinc || null,
                ...(NUMBER.test(v) ? { value: Number(v) } : { value_text: v }), unit: r.unit || null,
                ...checks[i].bounds, flag: r.flag || null, date: r.date || null }];
    });
    if (!results.length) return toast("Enter at least one test and its result.", "bad");
    e.target.disabled = true;
    try {
      const res = await post("/api/labs", { profile_id: profile.id, collected, lab: lab || null, results });
      toast(`Saved ${res.records} result${res.records === 1 ? "" : "s"}`, "good");
      m.close(); onSaved && onSaved();
    } catch (err) { toast(err.message, "bad"); e.target.disabled = false; }
  });
  draw();
}
