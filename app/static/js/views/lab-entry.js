// Add lab results: typed in, or read from a report (PDF or photo) on the server or with the person's AI provider key, then
// checked row by row beside the page they came from, and saved with the original kept.
import { api, get, post } from "../api.js";
import { $, $$, fieldsChanged, fmtDate, html, icon, modal, mount, toast } from "../ui.js";
import { confirmAiProvider } from "../consent.js";

const NUMBER = /^-?\d+(\.\d+)?$/;
const today = () => new Date().toLocaleDateString("en-CA");   // YYYY-MM-DD in local time
const MAX_BYTES = 15 * 1024 * 1024;
const FIELD_WORDS = { test: "the test's name", value: "the result", unit: "the unit", range: "the range" };

/** What a report can be. The extensions are for file dialogs that don't count a .heic or .tif as an image. */
export const REPORT_FILES = "application/pdf,image/*,.pdf,.heic,.heif,.tif,.tiff";

/** The Android app's web view has no file chooser yet, so a file box there would do nothing when tapped. It's to say "files" among its capabilities once it has. */
export const cannotPickFiles = () => /SyntropyAndroid\//.test(navigator.userAgent) && !(window.SyntropyApp?.capabilities || []).includes("files");

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

/** Whether a number is outside the range printed for it. Only that: what it means is for the person and their clinician. */
function outsideRange(r) {
  const value = String(r.value ?? "").trim();
  if (!NUMBER.test(value)) return "";
  const { bounds } = checkRow(r);
  if (bounds?.ref_high != null && Number(value) > bounds.ref_high) return "Above the printed range";
  if (bounds?.ref_low != null && Number(value) < bounds.ref_low) return "Below the printed range";
  return "";
}

const words = (list) => (list.length > 1 ? `${list.slice(0, -1).join(", ")} and ${list.at(-1)}` : list[0] || "");

/** The address of what was just added, where a save takes the person. `res` is what the server said, `results` what was sent. */
function addressOfAdded(res, results, collected) {
  const days = results.map((r) => r.date || collected).sort();
  const query = new URLSearchParams({ added: res.records, since: days[0], until: days.at(-1), connection: res.connection_id });
  if (res.report_id) query.set("report", res.report_id);
  return `#/records/labs?${query}`;
}

/**
 * The dialog. `file` is a report chosen before it opened (dropped on a box in Sources); `report` opens it ready for one.
 * `onSaved` is given the address of what was added.
 */
export async function openLabEntry(profile, onSaved, { file: given = null, report = false } = {}) {
  let rows = [{}, {}, {}];
  let mode = given || report ? "report" : "type";
  let collected = today();
  let lab = "";
  let reading = "";         // "local" | "ai" while a report is being read
  let readAs = null;        // how the rows were read: "text", "ocr", or an AI's "image" and "document"
  let file = null;          // the report: read again another way, and kept with the results
  let pages = [];           // its pages, to check the rows against
  let notes = [];           // what the reader wants said about the file
  let failure = "";         // why the last file couldn't be read
  let readSeq = 0;
  let catalog = { common: [], yours: [] };
  let ai = { configured: false };
  let reader = { ocr: true };
  const known = new Map();

  const m = modal({ title: "Add lab results", wide: true, body: "",
    dirty: () => fieldsChanged(m.el) || rows.some((r) => r.test || r.value),
    footer: html`<span class="small muted grow" id="lab-foot-note"></span><button class="btn" data-cancel>Cancel</button><button class="btn btn-primary" data-save>Save results</button>` });
  m.el.classList.add("lab-modal");
  m.el.querySelector("[data-cancel]").addEventListener("click", m.close);

  // ---- Drawing. The frame stays put; only what's inside a pane changes, so nothing jumps as parts load or as the tabs change.
  m.setBody(html`<div class="lab-entry">
    <div class="chips lab-tabs" role="group" aria-label="How to add results">
      <button class="chip" data-mode="type">Type them in</button>
      <button class="chip" data-mode="report">${icon("upload")} From a report<span class="lab-long"> (PDF or photo)</span></button></div>
    <div class="lab-panes">
      <section class="lab-source" id="lab-source" aria-label="The report"></section>
      <section class="lab-review" id="lab-review"></section>
    </div></div>`);
  const panes = $(".lab-panes", m.el);
  const source = $("#lab-source", m.el);
  const review = $("#lab-review", m.el);

  const readRows = () => {
    collected = $("#lab-date", m.el)?.value || collected;
    lab = $("#lab-name", m.el)?.value ?? lab;
    $$(".lab-row", m.el).forEach((tr) => {
      const row = rows[Number(tr.dataset.i)];
      if (!row) return;
      for (const k of ["test", "value", "unit", "ref_low", "ref_high"]) row[k] = tr.querySelector(`[data-k="${k}"]`).value.trim();
    });
  };

  const noteFor = (r, i) => {
    const hard = (r.unsure || []).map((k) => FIELD_WORDS[k]).filter(Boolean);
    const file_ = r.onFile;
    return html`
      ${r.matched ? html`<div class="small muted" data-matched-note>Filed as ${r.matched}</div>` : ""}
      ${hard.length ? html`<div class="small lab-hard">${icon("alert")} Hard to read: check ${words(hard)}.</div>` : ""}
      ${file_ ? html`<div class="small lab-on-file" data-on-file>
        ${file_.same ? html`Already on file for this day: ${file_.value}${file_.source ? ` (${file_.source})` : ""}. ${r.skip ? "Left out, so it isn't saved twice." : ""}`
          : html`On file for this day with another result: ${file_.value}${file_.source ? ` (${file_.source})` : ""}.`}
        <button class="btn btn-sm btn-ghost" data-keep="${i}">${r.skip ? "Add anyway" : "Leave out"}</button></div>` : ""}
      <div class="small muted" data-outside>${outsideRange(r)}</div>
      <div class="small lab-problem" data-row-problem role="alert"></div>`;
  };

  const drawRows = () => {
    const keep = review.scrollTop;
    const hard = rows.filter((r) => r.unsure?.length).length;
    const already = rows.filter((r) => r.skip).length;
    mount(review, html`<div class="stack">
      ${readAs ? html`<div class="banner warn small"><div class="grow">${readAs === "text" ? "Check each row against the page before saving."
          : "Reading a photo or scan makes mistakes. Compare each row with the page before saving."}
          ${hard ? ` ${hard === 1 ? "1 row was" : `${hard} rows were`} hard to read and ${hard === 1 ? "is" : "are"} marked.` : ""}</div></div>` : ""}
      ${notes.map((n) => html`<div class="banner info small"><div class="grow">${n}</div></div>`)}
      <div class="grid grid-2 lab-when">
        <div class="field"><label class="label" for="lab-date">Date collected</label><input class="input" type="date" id="lab-date" value="${collected}" max="${today()}"></div>
        <div class="field"><label class="label" for="lab-name">Lab or clinic (optional)</label><input class="input" id="lab-name" value="${lab}" placeholder="e.g. Quest Diagnostics"></div>
      </div>
      <datalist id="lab-tests">${[...known.values()].map((t) => html`<option value="${t.name}">`)}</datalist>
      <div class="table-wrap"><table class="table lab-table">
        <thead><tr><th>Test</th><th>Result</th><th>Unit</th><th>Range low</th><th>Range high</th><th aria-label="Remove row"></th></tr></thead>
        <tbody>${rows.map((r, i) => html`<tr class="lab-row ${r.skip ? "lab-left-out" : ""}" data-i="${i}" data-loinc="${r.loinc || ""}" data-flag="${r.flag || ""}" data-date="${r.date || ""}" data-matched="${r.matched || ""}">
          <td class="lab-cell-test"><input class="input ${r.unsure?.includes("test") ? "lab-unsure" : ""}" data-k="test" list="lab-tests" value="${r.test || ""}" placeholder="e.g. Ferritin" aria-label="Test">
            ${noteFor(r, i)}</td>
          <td class="lab-cell-value" data-label="Result"><input class="input ${r.unsure?.includes("value") ? "lab-unsure" : ""}" data-k="value" value="${r.value ?? ""}" placeholder="48" aria-label="Result" inputmode="decimal"></td>
          <td class="lab-cell-unit" data-label="Unit"><input class="input ${r.unsure?.includes("unit") ? "lab-unsure" : ""}" data-k="unit" value="${r.unit || ""}" placeholder="ng/mL" aria-label="Unit"></td>
          <td class="lab-cell-low" data-label="Range low"><input class="input ${r.unsure?.includes("range") ? "lab-unsure" : ""}" data-k="ref_low" value="${r.ref_low ?? ""}" aria-label="Range low"></td>
          <td class="lab-cell-high" data-label="Range high"><input class="input ${r.unsure?.includes("range") ? "lab-unsure" : ""}" data-k="ref_high" value="${r.ref_high ?? ""}" aria-label="Range high"></td>
          <td class="lab-cell-remove"><button class="btn btn-ghost btn-icon btn-sm" data-remove="${i}" aria-label="Remove row">${icon("x")}</button></td></tr>`)}</tbody>
      </table></div>
      <div><button class="btn btn-sm" data-add>${icon("plus")} Add another test</button></div>
      <p class="hint">A result like "&lt;0.5" is kept as a limit, and one like "Negative" as text. Ranges are optional and can be typed the way
        labs print them ("&lt;200" or "3.5-5.0"); with them, out-of-range results are flagged on your Overview.</p>
    </div>`);
    review.scrollTop = keep;
    const count = rows.filter((r) => !r.skip && r.test && String(r.value ?? "").trim()).length;
    const save = m.el.querySelector("[data-save]");
    save.textContent = count ? `Save ${count} result${count === 1 ? "" : "s"}` : "Save results";
    $("#lab-foot-note", m.el).textContent = already ? `${already} already on file ${already === 1 ? "is" : "are"} left out.` : "";
  };

  const dropBox = () => {
    const touch = matchMedia("(pointer: coarse)").matches;
    if (cannotPickFiles()) {
      return html`<div class="lab-drop-area"><div class="banner info"><div class="grow">The Android app can't open files yet. To add a report, open
        <b>${location.origin}</b> in your phone's browser or on a computer, and sign in. You can type results in here.</div></div></div>`;
    }
    return html`<div class="lab-drop-area">
      ${failure ? html`<div class="banner bad small" role="alert"><div class="grow">${failure}</div>
        ${file && ai.configured ? html`<button class="btn btn-sm" data-read="ai">${icon("sparkles")} Read with ${ai.label}</button>` : ""}</div>` : ""}
      <label class="drop-zone lab-drop" data-lab-drop>
        ${icon("upload")}<div><b>${touch ? "Choose a lab report" : "Drop a lab report here, or click to choose one"}</b>
          <div class="small muted">A PDF from your lab's website, or a photo or scan of a paper report. Up to 15 MB.</div></div>
        <input type="file" id="lab-file" accept="${REPORT_FILES}" aria-label="Choose a lab report"></label>
      ${touch ? html`<label class="btn lab-camera">${icon("image")} Take a photo of a report
        <input type="file" id="lab-camera" accept="image/*" capture="environment" aria-label="Take a photo of a report"></label>` : ""}
      <p class="hint">Read privately: your Syntropy server reads the file and it goes nowhere else. Nothing is saved until you've checked it.
        ${reader.ocr ? "" : html`Photos and scans need a reader on the computer running Syntropy Health, such as a free program called Tesseract.
          ${reader.install} PDFs from lab websites work without it.`}
        ${ai.configured ? html`${ai.label} (${ai.model}) is usually better with blurry or crooked photos. You can choose that after it's read; it sends the file to ${ai.label} with your key.`
          : html`For hard-to-read photos you can also <a href="#/settings/ai">add your own AI key</a>.`}</p></div>`;
  };

  const drawSource = () => {
    if (reading) {
      mount(source, html`<div class="lab-drop-area" role="status"><div class="lab-reading">
        <b>Reading ${file?.name || "the report"}…</b>
        <div class="progress working"><div></div></div>
        <p class="small muted">${reading === "ai" ? `With ${ai.label}.` : "On this computer. A photo or scan takes a few seconds a page."}</p></div></div>`);
      return;
    }
    if (!file || !pages.length) {
      mount(source, dropBox());
      return;
    }
    mount(source, html`<div class="lab-pages-head">
        <span class="small truncate grow" title="${file.name}"><b>${file.name}</b>${pages.length > 1 ? ` · ${pages.length} pages` : ""}</span>
        <button class="btn btn-sm btn-ghost" data-zoom aria-pressed="false">Zoom in</button>
        ${ai.configured ? html`<button class="btn btn-sm btn-ghost" data-read="ai">${icon("sparkles")} Read with ${ai.label}</button>` : ""}
        <label class="btn btn-sm btn-ghost">Choose another<input type="file" id="lab-file" accept="${REPORT_FILES}" aria-label="Choose another report"></label></div>
      <div class="lab-pages" tabindex="0" aria-label="The original report">${pages.map((p, i) => html`
        <div class="lab-page" data-page="${i}" style="aspect-ratio:${p.width}/${p.height}"><img src="${p.image}" alt="Page ${i + 1} of ${pages.length}" width="${p.width}" height="${p.height}"></div>`)}</div>`);
  };

  const layout = () => {
    const both = mode === "report" && !reading && file && (readAs || pages.length);
    panes.classList.toggle("lab-both", Boolean(both));
    source.hidden = mode !== "report";
    review.hidden = mode === "report" && !both;
    for (const chip of $$("[data-mode]", m.el)) chip.classList.toggle("active", chip.dataset.mode === mode);
    m.el.querySelector("[data-save]").hidden = review.hidden;
  };

  const draw = () => { drawSource(); drawRows(); layout(); };

  /** Which rows the record already has for that test and day. Rows the person chose to keep stay kept. */
  const checkOnFile = async () => {
    const asked = rows.map((r, i) => ({ r, i })).filter(({ r }) => r.test);
    if (!asked.length) return;
    let out;
    try {
      out = await api("/api/labs/on-file", { method: "POST", keepCache: true, body: { profile_id: profile.id, collected, results: asked.map(({ r }) => ({
        test: r.matched || r.test, loinc: r.loinc || known.get(String(r.test).toLowerCase())?.loinc || null, value: String(r.value ?? ""), date: r.date || null })) } });
    } catch { return; }    // a convenience: being unable to check doesn't stop a save
    asked.forEach(({ r }, n) => {
      r.onFile = out.results[n] || null;
      if (!r.kept) r.skip = Boolean(r.onFile?.same);
    });
  };

  // ---- Reading a report

  const useFile = (chosen) => {
    if (!chosen) return;
    if (chosen.size > MAX_BYTES) { failure = "That file is larger than 15 MB."; file = null; pages = []; drawSource(); layout(); return; }
    file = chosen;
    failure = "";
    readFile("local");
  };

  const readFile = async (how) => {
    const seq = ++readSeq;
    readRows();
    mode = "report";
    reading = how;
    drawSource(); layout();
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("method", how);
      form.append("profile_id", profile.id);
      const out = await api("/api/labs/extract", { method: "POST", form, keepCache: true });
      if (seq !== readSeq) return;
      pages = out.pages || [];
      notes = out.notes || [];
      if (!out.results.length) {
        rows = [{}, {}, {}];
        readAs = null;
        notes = [...notes, how === "local" && ai.configured ? `No results were found in this file. You can type them in here while looking at the page, or read it with ${ai.label}.`
          : "No results were found in this file. You can type them in here while looking at the page, or try a sharper photo taken straight on, in good light."];
      } else {
        rows = out.results.map((r) => ({ test: r.test, value: r.value ?? r.value_text ?? "", unit: r.unit, ref_low: r.ref_low, ref_high: r.ref_high, ref_text: r.ref_text,
          loinc: r.loinc, flag: r.flag, date: r.date, matched: r.matched_test || "", unsure: r.unsure || [], where: r.where || null }));
        if (out.collected) collected = out.collected;
        if (out.lab) lab = out.lab;
        readAs = out.read_as || "text";
        await checkOnFile();
        if (seq !== readSeq) return;
      }
    } catch (err) {
      if (seq !== readSeq) return;
      // A file that couldn't be read here may still be readable with the person's AI, if they choose that.
      if (how === "local" || !pages.length) { failure = err.message; pages = []; readAs = null; }
      else toast(err.message, "bad");
    }
    reading = "";
    draw();
  };

  // ---- What the person does

  /** Shows which line of the page a row came from, so the eye doesn't have to find it. */
  const pointAt = (where) => {
    $$(".lab-mark", source).forEach((x) => x.remove());
    const page = where && source.querySelector(`.lab-page[data-page="${where.page}"]`);
    if (!page) return;
    const mark = document.createElement("div");
    mark.className = "lab-mark";
    const margin = 0.004;
    mark.style.top = `${(where.top - margin) * 100}%`;
    mark.style.height = `${(where.bottom - where.top + margin * 2) * 100}%`;
    page.append(mark);
    const box = $(".lab-pages", source);
    const top = page.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop + page.offsetHeight * where.top - box.clientHeight * 0.35;
    box.scrollTo({ top: Math.max(0, top), behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  };

  m.el.addEventListener("focusin", (e) => {
    const tr = e.target.closest(".lab-row");
    if (tr && rows[Number(tr.dataset.i)]?.where) pointAt(rows[Number(tr.dataset.i)].where);
  });
  m.el.addEventListener("click", async (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    if (t.dataset.mode) { readRows(); mode = t.dataset.mode; failure = ""; drawRows(); layout(); }
    else if (t.hasAttribute("data-add")) { readRows(); rows.push({}); drawRows(); $$(".lab-row [data-k=test]", m.el).at(-1).focus(); }
    else if (t.dataset.remove !== undefined) { readRows(); rows.splice(Number(t.dataset.remove), 1); if (!rows.length) rows.push({}); drawRows(); }
    else if (t.dataset.keep !== undefined) { readRows(); const r = rows[Number(t.dataset.keep)]; r.skip = !r.skip; r.kept = !r.skip; drawRows(); }
    else if (t.hasAttribute("data-zoom")) {
      const zoomed = $(".lab-pages", source).classList.toggle("zoomed");
      t.textContent = zoomed ? "Fit the page" : "Zoom in";
      t.setAttribute("aria-pressed", String(zoomed));
    }
    else if (t.dataset.read) {
      if (!file) return toast("Choose a PDF or photo first.");
      if (ai.sends_elsewhere && !ai.cloud_ack) {
        if (!(await confirmAiProvider(ai.vendor_name, ai.vendor_key))) return;
        ai.cloud_ack = true;
      }
      readFile(t.dataset.read);
    }
  });
  // The value is cleared each time, so choosing the same file again after a failure counts as a choice.
  m.el.addEventListener("change", (e) => {
    if (e.target.id === "lab-file" || e.target.id === "lab-camera") { const chosen = e.target.files[0]; e.target.value = ""; useFile(chosen); }
  });
  m.el.addEventListener("dragover", (e) => {
    if (!e.dataTransfer?.types?.includes("Files") || cannotPickFiles()) return;
    e.preventDefault();
    $("[data-lab-drop]", source)?.classList.add("over");
  });
  m.el.addEventListener("dragleave", (e) => { if (e.target === m.el) $("[data-lab-drop]", source)?.classList.remove("over"); });
  m.el.addEventListener("drop", (e) => {
    const dropped = e.dataTransfer?.files?.[0];
    if (!dropped || cannotPickFiles()) return;
    e.preventDefault();
    useFile(dropped);
  });
  // Picking a known test fills in its unit and LOINC code (so results join the existing trend). What the page says about a corrected value no longer applies.
  m.el.addEventListener("change", async (e) => {
    const k = e.target.dataset?.k;
    if (e.target.id === "lab-date") { readRows(); await checkOnFile(); drawRows(); return; }
    if (!k) return;
    const tr = e.target.closest(".lab-row");
    const row = rows[Number(tr.dataset.i)];
    if (k === "test") {
      const hit = known.get(e.target.value.trim().toLowerCase());
      row.loinc = hit?.loinc || "";
      row.matched = "";
      tr.querySelector("[data-matched-note]")?.remove();
      const unit = tr.querySelector("[data-k=unit]");
      if (hit?.unit && !unit.value) unit.value = hit.unit.replace(/\{[^}]*\}/g, "");
    }
    if (k === "value") row.flag = "";
    if (k === "ref_low" || k === "ref_high") row.ref_text = "";
    if (row.unsure) {
      row.unsure = row.unsure.filter((u) => u !== k && !(u === "range" && (k === "ref_low" || k === "ref_high")));
      e.target.classList.remove("lab-unsure");
    }
    if (k === "test" || k === "value") { readRows(); await checkOnFile(); drawRows(); }
  });
  m.el.addEventListener("input", (e) => {
    const tr = e.target.closest?.(".lab-row");
    if (!tr) return;
    const get_ = (k) => tr.querySelector(`[data-k="${k}"]`).value;
    tr.querySelector("[data-outside]").textContent = outsideRange({ value: get_("value"), ref_low: get_("ref_low"), ref_high: get_("ref_high") });
  });
  m.el.querySelector("[data-save]").addEventListener("click", async (e) => {
    readRows();
    await ready;
    const dateBox = $("#lab-date", m.el);
    if (dateBox && !dateBox.reportValidity()) return;
    // A half-filled row or an unreadable range is pointed out where it is. It's never left out with a message that sounds like success.
    const checks = rows.map(checkRow);
    let blocked = false;
    $$(".lab-row", m.el).forEach((tr) => {
      const i = Number(tr.dataset.i);
      const problems = rows[i].skip ? [] : checks[i].problems || [];
      tr.querySelector("[data-row-problem]").textContent = problems.join(" ");
      blocked = blocked || problems.length > 0;
    });
    if (blocked) return toast("Some rows need fixing before they can be saved. Fix them, or remove them.", "bad");
    const results = rows.flatMap((r, i) => {
      if (checks[i].empty || r.skip) return [];
      const v = String(r.value).trim();
      return [{ test: r.matched || r.test, loinc: r.loinc || known.get(r.test.toLowerCase())?.loinc || null,
                ...(NUMBER.test(v) ? { value: Number(v) } : { value_text: v }), unit: r.unit || null,
                ...checks[i].bounds, ref_text: r.ref_text || null, flag: r.flag || null, date: r.date || null }];
    });
    if (!results.length) {
      return toast(rows.some((r) => r.skip) ? "Every result here is already on file, so there's nothing new to save." : "Enter at least one test and its result.", "bad");
    }
    e.target.disabled = true;
    try {
      const entry = { profile_id: profile.id, collected, lab: lab || null, results };
      let res;
      if (file && readAs) {
        const form = new FormData();
        form.append("file", file);
        form.append("entry", JSON.stringify(entry));
        res = await api("/api/labs/report", { method: "POST", form });
      } else {
        res = await post("/api/labs", entry);
      }
      toast(`Saved ${res.records} result${res.records === 1 ? "" : "s"}`, "good");
      m.close();
      if (onSaved) onSaved(addressOfAdded(res, results, collected));
    } catch (err) { toast(err.message, "bad"); e.target.disabled = false; }
  });

  draw();
  // What the dialog needs to know loads behind it, and fills in without moving anything.
  const ready = Promise.all([get("/api/labs/catalog", { profile: profile.id }), get("/api/ai/config"), get("/api/labs/reader")]).then(([c, a, r]) => {
    catalog = c; ai = a; reader = r;
    for (const t of [...catalog.common, ...catalog.yours]) known.set(t.name.toLowerCase(), t);
    const list = $("#lab-tests", m.el);
    if (list) mount(list, [...known.values()].map((t) => html`<option value="${t.name}">`));
    if (!reading && !pages.length) drawSource();
  }).catch((err) => { m.close(); toast(err.message, "bad"); });
  if (given) { await ready; useFile(given); }
}
