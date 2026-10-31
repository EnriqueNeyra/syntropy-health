// Getting ready for a visit, without AI: what's on record since a visit or a date, and the person's own questions.
import { get, put } from "../api.js";
import { appBridge, nativeNav, setToolbar } from "../app.js";
import { recordValue } from "../units.js";
import { $, capitalize, emptyState, flagBadge, fmtDate, html, icon, loading, mount, plural, toast } from "../ui.js";

// Said on the row only when it changes how the row reads: a visit that was cancelled, an allergy that's resolved.
const UNREMARKABLE = new Set(["", "active", "completed", "finished", "final", "amended", "corrected", "current"]);
const statusWord = (r) => (UNREMARKABLE.has(String(r.status || "").toLowerCase()) ? null : capitalize(r.status.replace(/-/g, " ")));

const row = (r, extra = "") => html`<div class="list-item"><div class="grow" style="min-width:0"><div class="title">${r.title}</div>
  <div class="meta">${[statusWord(r), r.category_label, fmtDate(r.effective_at), r.source_name].filter(Boolean).join(" · ")}</div></div>${extra}</div>`;

const visitRow = (v) => row(v, v.clinician ? html`<span class="small muted">${v.clinician}</span>` : "");
const resultRow = (r) => html`<div class="list-item"><div class="grow" style="min-width:0"><div class="title">${r.title}</div>
  <div class="meta">${[fmtDate(r.effective_at), r.source_name, r.count > 1 ? plural(r.count, "result") : null].filter(Boolean).join(" · ")}</div>
  <div class="meta">${r.before ? `Before this period: ${recordValue(r.before)} on ${fmtDate(r.before.effective_at)}` : "No earlier result on record"}${r.ref_text ? ` · Range ${r.ref_text}` : ""}</div></div>
  <div class="value">${r.value_num != null || r.value_text ? recordValue(r) : ""}</div>${flagBadge(r.interpretation)}</div>`;

function block(title, count, items, draw) {
  if (!items.length) return "";
  return html`<section class="prepare-block"><h2>${title} <span class="badge">${count}</span></h2><div class="list">${items.map(draw)}</div>
    ${count > items.length ? html`<p class="small muted">and ${count - items.length} more, in Records.</p>` : ""}</section>`;
}

function digest(d) {
  const n = d.counts;
  const total = Object.values(n).reduce((a, b) => a + b, 0);
  if (!total) return emptyState("Nothing since then", `Nothing in the records or the journal is dated on or after ${fmtDate(d.since)}.`);
  return html`
    ${block("Visits", n.visits, d.visits, visitRow)}
    ${block("Upcoming visits", n.upcoming, d.upcoming, visitRow)}
    ${block("Allergies", n.allergies, d.allergies, (a) => row(a))}
    ${block("Results", n.results, d.results, resultRow)}
    ${block("Vital signs", n.vitals, d.vitals, resultRow)}
    ${block("Medications first ordered", n.medications_started, d.medications_started, (m) => row(m))}
    ${block("Medications marked stopped", n.medications_stopped, d.medications_stopped, (m) => row(m))}
    ${block("Diagnoses", n.diagnoses, d.diagnoses, (c) => row(c))}
    ${block("Procedures", n.procedures, d.procedures, (p) => row(p))}
    ${block("Procedures not marked completed", n.procedures_unfinished, d.procedures_unfinished, (p) => row(p))}
    ${block("Imaging", n.imaging, d.imaging, (p) => row(p))}
    ${block("Other reports", n.reports, d.reports, (p) => row(p))}
    ${block("Notes", n.notes, d.notes, (p) => row(p))}
    ${block("Immunizations", n.immunizations, d.immunizations, (p) => row(p))}
    ${block("Other records", n.other, d.other, (p) => row(p))}
    ${block("Logged in the journal", n.journal, d.journal, (j) => html`<div class="list-item"><div class="grow"><div class="title">${j.name}</div>
      <div class="meta">${plural(j.count, "time")} · last ${fmtDate(j.last_at)}</div></div></div>`)}`;
}

export async function render({ el, state }) {
  mount(el, loading(3));
  const pid = state.profile.id;
  const [visits, prefs] = await Promise.all([get("/api/records", { profile: pid, category: "encounters", limit: 100 }), get("/api/report/preferences", { profile: pid })]);
  const today = new Date().toISOString().slice(0, 10);
  const past = visits.items.filter((v) => v.effective_at && v.effective_at.slice(0, 10) <= today && String(v.status).toLowerCase() !== "planned");
  let questions = [...(prefs.questions || [])];
  const appPrint = setToolbar([{ id: "print", title: "Print or Save PDF", symbol: "printer", run: () => appBridge({ type: "print" }) }]);
  mount(el, html`
    <div class="page-head no-print"><div><h1>Prepare for a visit</h1>
      <p>What's on ${state.profile.name}'s record since a visit or a date, and your own questions.</p></div>
      <div class="row wrap">${nativeNav ? "" : html`<a class="btn btn-ghost" href="#/report">${icon("chevronLeft")} Visit summary</a>`}
        ${appPrint ? "" : html`<button class="btn" id="prep-print">${icon("printer")} Print</button>`}</div></div>
    <div class="card card-pad no-print stack" style="margin-bottom:12px">
      <div class="row wrap" style="gap:12px">
        <label class="stack" style="gap:4px;flex:1;min-width:220px"><span class="label">Since a visit</span>
          <select class="input" id="prep-visit"><option value="">Choose a visit</option>
            ${past.map((v) => html`<option value="${v.effective_at.slice(0, 10)}">${fmtDate(v.effective_at)} · ${v.title}${v.details?.clinicians?.[0] ? ` · ${v.details.clinicians[0]}` : ""}</option>`)}</select></label>
        <label class="stack" style="gap:4px"><span class="label">Or since a date</span><input class="input" type="date" id="prep-date" max="${today}"></label>
      </div></div>
    <div class="report card card-pad">
      <section><h2>My questions</h2>
        <ol class="report-questions" id="prep-questions"></ol>
        <form class="row no-print" id="prep-form" style="margin-top:8px"><input class="input grow" id="prep-question" maxlength="500" aria-label="A question to ask" placeholder="Add a question to ask">
          <button class="btn" type="submit">${icon("plus")} Add</button></form>
        <p class="small muted no-print" style="margin-top:6px">These print on the visit summary too.</p></section>
      <div id="prep-digest"><p class="muted small">Choose a visit or a date to see what's on record since then.</p></div>
    </div>`);

  const drawQuestions = () => mount($("#prep-questions", el), questions.map((q, i) => html`<li><span>${q}</span>
    <button class="btn btn-ghost btn-icon btn-sm no-print" data-remove-question="${i}" aria-label="Remove this question" title="Remove">${icon("x")}</button></li>`));
  const saveQuestions = async () => {
    try { await put("/api/report/preferences", { profile_id: pid, questions }); }
    catch (err) { toast(`Couldn't save that: ${err.message}`, "bad"); }
  };
  drawQuestions();
  $("#prep-form", el).addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#prep-question", el);
    if (!input.value.trim()) return;
    questions = [...questions, input.value.trim()];
    input.value = "";
    drawQuestions();
    await saveQuestions();
  });
  $("#prep-questions", el).addEventListener("click", async (e) => {
    const b = e.target.closest("[data-remove-question]");
    if (!b) return;
    questions = questions.filter((_, i) => i !== Number(b.dataset.removeQuestion));
    drawQuestions();
    await saveQuestions();
  });

  const box = $("#prep-digest", el);
  const show = async (since) => {
    if (!since) return;
    mount(box, loading(3));
    try {
      const d = await get("/api/visit-prep", { profile: pid, since });
      const undated = d.undated.map((u) => `${u.label} ${u.count}`).join(", ");
      mount(box, html`<p class="small muted" style="margin:12px 0">Records and journal entries dated ${fmtDate(d.since)} or later. Measures from wearables and apps are in Trends.
        ${undated ? `Records with no date can't be placed before or after this, so they aren't listed here. They're in Records: ${undated}.` : ""}</p>${digest(d)}`);
    } catch (err) { mount(box, html`<div class="banner bad"><div class="grow">${err.message}</div></div>`); }
  };
  $("#prep-visit", el).addEventListener("change", (e) => { $("#prep-date", el).value = ""; show(e.target.value); });
  $("#prep-date", el).addEventListener("change", (e) => { $("#prep-visit", el).value = ""; show(e.target.value); });
  $("#prep-print", el)?.addEventListener("click", () => appBridge({ type: "print" }) || window.print());
}
