"use strict";

const $ = (s) => document.querySelector(s);
const state = { selected: null, historyFor: null, labels: {}, lastSlots: {}, refreshTimer: null, attemptTimer: null };

const KIND = {
  lead_captured: "Lead captured",
  gate_decision: "Compliance gate",
  call_dialing: "Dialing via Teams Phone",
  call_status: "Call status",
  call_connected: "Agent connected",
  browser_session_armed: "Browser session armed",
  answers_saved: "Answers captured",
  slots_offered: "Meeting times offered",
  slot_held: "Time held",
  meeting_booked: "Teams meeting booked (Graph)",
  transfer_requested: "Live transfer approved",
  callback_scheduled: "Callback scheduled",
  opted_out: "Opted out - do not call",
  call_summarized: "Call summarized",
  call_ended: "Call ended",
  call_failed: "Call failed",
  dataverse_synced: "Saved to Dataverse",
  rep_notified: "Rep notified in Teams (Power Automate)",
  rep_notify_skipped: "Rep notification not needed",
  delivery_retry: "Delivery retry",
  delivery_failed: "Delivery failed",
  browser_session_expired: "Browser session expired",
  demo_reset: "Demo reset",
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let body = null;
  try { body = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const e = body && body.error;
    const detail = e && e.details ? ": " + e.details.map((d) => `${d.field} ${d.message}`).join("; ") : "";
    throw new Error((e ? e.message : `HTTP ${res.status}`) + detail);
  }
  return body;
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}

const money = (v) => (v === null || v === undefined || v === "" ? "-" :
  Number(v).toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }));
const time = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });

function fmtSlot(name, value) {
  if (value === null || value === undefined) return "-";
  if (Array.isArray(value)) return value.join(", ") || "-";
  if (name === "decision_role") return String(value).replace(/_/g, " ");
  if (name === "contract_months_remaining" || name === "timeline_months") return `${value} months`;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return value.toLocaleString("en-US");
  return String(value);
}

function describe(ev) {
  const d = ev.detail || {};
  switch (ev.kind) {
    case "gate_decision": return `${d.verdict} - ${d.reason}`;
    case "answers_saved": return Object.entries(d.slots || {}).map(([k, v]) => `${state.labels[k] || k}: ${fmtSlot(k, v)}`).join(" · ") + (d.score !== undefined ? ` → score ${d.score} (${d.band})` : "");
    case "slots_offered": return (d.slots || []).map((s) => new Date(s.start).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }) + " with " + s.rep).join(" | ");
    case "meeting_booked": return `${new Date(d.start).toLocaleString()} · ${d.calendar === "graph" ? "Teams meeting created" : "calendar not connected"}`;
    case "call_dialing": case "call_status": case "call_ended": return [d.status, d.reason].filter(Boolean).join(" - ");
    case "call_failed": case "delivery_failed": case "delivery_retry": return d.reason || d.error || "";
    case "call_summarized": return d.outcome;
    case "lead_captured": return d.company;
    default: return "";
  }
}

function renderIntegrations(integ) {
  const box = $("#integrations");
  box.replaceChildren();
  const names = { teams_phone: "Teams Phone", graph: "Microsoft Graph", dataverse: "Dataverse", power_automate: "Power Automate", escalation_agent: "MAF agent" };
  for (const [k, label] of Object.entries(names)) {
    const p = el("span", "pill" + (integ[k] ? " on" : ""), label);
    p.title = integ[k] ? "Connected" : "Not configured on this server";
    box.append(p);
  }
}

function renderLeads(leads) {
  const ul = $("#leads");
  ul.replaceChildren();
  if (!leads.length) { ul.append(el("li", "li-co", "No leads yet.")); return; }
  const tpl = $("#lead-item");
  for (const l of leads) {
    const li = tpl.content.firstElementChild.cloneNode(true);
    li.querySelector(".li-name").textContent = `${l.first_name} ${l.last_name || ""}`.trim();
    li.querySelector(".li-co").textContent = `${l.company} · ${l.phone_e164}${l.status === "opted_out" ? " · OPTED OUT" : ""}`;
    const callBtn = li.querySelector(".call-btn");
    const brBtn = li.querySelector(".browser-btn");
    if (l.status === "opted_out") { callBtn.disabled = true; brBtn.disabled = true; }
    callBtn.addEventListener("click", () => act(callBtn, `/api/leads/${l.id}/call`));
    brBtn.addEventListener("click", () => act(brBtn, `/api/leads/${l.id}/browser-session`));
    ul.append(li);
  }
}

function renderCalls(attempts) {
  const ul = $("#calls");
  ul.replaceChildren();
  for (const a of attempts) {
    const li = el("li");
    const left = el("div");
    left.append(el("b", null, `${a.first_name} · ${a.company}`), el("div", "li-co", a.channel === "browser" ? "Browser preview" : "Teams Phone"));
    const right = el("div", "cs", `${a.status.replace("_", " ")}${a.score !== null ? " · " + a.score : ""}`);
    right.append(el("div", null, new Date(a.created_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })));
    li.append(left, right);
    li.addEventListener("click", () => select(a.id));
    ul.append(li);
  }
}

async function act(btn, path) {
  btn.disabled = true;
  try {
    const r = await api(path, { method: "POST" });
    if (r.status === "blocked") alert(`Compliance gate blocked this call: ${r.reason}`);
    if (r.attempt_id) select(r.attempt_id);
    scheduleRefresh();
  } catch (e) {
    alert(e.message);
  } finally {
    btn.disabled = false;
  }
}

async function refresh() {
  const s = await api("/api/state");
  state.labels = s.slot_labels;
  $("#company").textContent = s.company_name;
  $("#reset-btn").hidden = !s.demo_mode;
  renderIntegrations(s.integrations);
  renderLeads(s.leads);
  renderCalls(s.attempts);
  if (!state.selected && s.attempts.length) select(s.attempts[0].id, false);
}

function scheduleRefresh() {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(() => refresh().catch(console.error), 250);
}

function select(id, push = true) {
  if (state.selected !== id) state.historyFor = null;
  state.selected = id;
  state.lastSlots = {};
  if (push) history.replaceState(null, "", `#/attempt/${id}`);
  loadAttempt().catch(console.error);
}

function scheduleAttempt() {
  clearTimeout(state.attemptTimer);
  state.attemptTimer = setTimeout(() => loadAttempt().catch(console.error), 150);
}

async function loadAttempt() {
  if (!state.selected) return;
  const d = await api(`/api/attempts/${state.selected}`);
  const b = d.briefing;
  $("#call-empty").hidden = true;
  $("#call-view").hidden = false;
  $("#cv-name").textContent = b.contact_name;
  $("#cv-company").textContent = `${b.company} · ${b.phone} · ${b.channel === "browser" ? "Browser preview" : "Teams Phone"}`;
  const st = $("#cv-status");
  st.textContent = (d.attempt.status || "").replace("_", " ");
  st.className = "status-chip " + d.attempt.status;

  const box = $("#cv-score-box");
  box.className = "score " + (b.band || "");
  $("#cv-score").textContent = b.score ?? "--";
  $("#cv-band").textContent = b.band || "incomplete";
  $("#cv-value").textContent = money(b.contract_value);
  const mt = $("#cv-meeting");
  mt.replaceChildren();
  if (b.meeting_when) {
    mt.append(document.createTextNode(`${b.meeting_when}${b.rep_name ? " with " + b.rep_name : ""} `));
    if (b.join_url) { const a = el("a", null, "Teams link"); a.href = b.join_url; a.target = "_blank"; a.rel = "noopener"; mt.append(a); }
  } else mt.textContent = "not booked";
  $("#cv-next").textContent = b.next_action || "-";

  const dl = $("#cv-slots");
  dl.replaceChildren();
  const byName = Object.fromEntries(d.slots.map((s) => [s.name, s]));
  for (const [name, label] of Object.entries(state.labels)) {
    const s = byName[name];
    const div = el("div", "slot" + (s ? " filled" : "") + (s && s.confidence < 0.6 ? " verify" : ""));
    const dd = el("dd", null, s ? fmtSlot(name, s.value) : "-");
    if (s && s.evidence) dd.append(el("span", "ev", `"${s.evidence}"`));
    div.append(el("dt", null, label), dd);
    const sig = s ? JSON.stringify(s.value) : null;
    if (sig && state.lastSlots[name] !== undefined && state.lastSlots[name] !== sig) {
      div.classList.add("flash");
      setTimeout(() => div.classList.remove("flash"), 1500);
    }
    state.lastSlots[name] = sig;
    dl.append(div);
  }

  $("#cv-summary-wrap").hidden = !b.summary;
  $("#cv-summary").textContent = b.summary || "";

  const note = $("#cv-browser");
  const armed = d.attempt.channel === "browser" && ["queued", "in_progress"].includes(d.attempt.status);
  note.hidden = !armed;
  if (armed) {
    note.replaceChildren(
      el("b", null, "Browser preview armed. "),
      document.createTextNode("Open the voice agent's preview in Foundry and start talking as this lead. If the preview asks for inputs, use call_token = "),
      el("code", null, "browser"),
      document.createTextNode("."),
    );
  }
  if (state.historyFor !== state.selected) {  // seed the timeline with this call's history
    state.historyFor = state.selected;
    $("#events").replaceChildren();
    for (const ev of d.events) addEvent(ev);
  }
  const a = d.attempt;
  $("#cv-meta").textContent = [a.foundry_call_job_id && `Foundry call job ${a.foundry_call_job_id}`,
    a.foundry_status && `status ${a.foundry_status}`, a.terminal_reason && `reason ${a.terminal_reason}`,
    a.gate_reason && `gate ${a.gate_reason}`].filter(Boolean).join(" · ");
}

function addEvent(ev) {
  const ol = $("#events");
  const li = el("li");
  const head = el("div");
  head.append(el("span", "ek", KIND[ev.kind] || ev.kind), el("span", "et", time(ev.created_at)));
  li.append(head);
  const text = describe(ev);
  if (text) li.append(el("div", "ed", text));
  ol.prepend(li);
  while (ol.children.length > 80) ol.lastElementChild.remove();
}

function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => $("#live-dot").classList.add("on");
  es.onerror = () => $("#live-dot").classList.remove("on");
  es.addEventListener("audit", (m) => {
    const ev = JSON.parse(m.data);
    addEvent(ev);
    if (["gate_decision", "browser_session_armed"].includes(ev.kind) && ev.attempt_id) select(ev.attempt_id);
    else if (ev.attempt_id && ev.attempt_id === state.selected) scheduleAttempt();
    if (ev.kind === "demo_reset") { state.selected = null; $("#call-view").hidden = true; $("#call-empty").hidden = false; $("#events").replaceChildren(); }
    scheduleRefresh();
  });
}

$("#lead-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const msg = $("#form-msg");
  const data = Object.fromEntries(new FormData(f).entries());
  data.consent = f.consent.checked;
  if (!data.email) delete data.email;
  const btn = f.querySelector("button");
  btn.disabled = true;
  try {
    await api("/api/leads", { method: "POST", body: JSON.stringify(data) });
    msg.className = "form-msg ok"; msg.textContent = "Lead captured.";
    f.reset();
    scheduleRefresh();
  } catch (err) {
    msg.className = "form-msg err"; msg.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

$("#reset-btn").addEventListener("click", async () => {
  if (!confirm("Delete all demo leads, calls and events?")) return;
  try { await api("/api/demo/reset", { method: "POST" }); scheduleRefresh(); } catch (e) { alert(e.message); }
});

const m = location.hash.match(/#\/attempt\/([0-9a-f-]{36})/);
if (m) state.selected = m[1];
refresh().then(() => state.selected && loadAttempt()).catch((e) => console.error(e));
connect();
