"use strict";
/* Campaign orchestrator view. Reuses $, el, api and money from app.js. */

const camp = { selected: null, timer: null, active: false };
const FUNNEL = [
  ["pending", "Pending"], ["dialing", "Dialing"], ["in_call", "In call"], ["retry_wait", "Retry wait"],
  ["escalated", "Escalated"], ["nurture", "Nurture"], ["disqualified", "Disqualified"],
  ["suppressed", "Suppressed"], ["exhausted", "Exhausted"],
];
const num = (v) => (v === null || v === undefined ? "-" : Number(v).toLocaleString("en-US"));

function showView(view) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  $("#view-call").hidden = view !== "call";
  $("#view-campaign").hidden = view !== "campaign";
  camp.active = view === "campaign";
  try { localStorage.setItem("cockpit.view", view); } catch { /* storage unavailable */ }
  if (camp.active) { loadCampaigns().catch(console.error); poll(); } else clearTimeout(camp.timer);
}

async function loadCampaigns() {
  const list = await api("/api/campaigns");
  const ul = $("#camp-list");
  ul.replaceChildren();
  if (!list.length) ul.append(el("li", "li-co", "No campaigns yet."));
  for (const c of list) {
    const li = el("li");
    const left = el("div");
    const title = el("b", null, c.name);
    left.append(title, el("div", "li-co", `${c.mode === "simulation" ? "Simulation" : "Live"} · ${num(c.leads)} leads`));
    li.append(left, el("div", "cs", `${c.status} · ${num(c.escalated)} esc`));
    li.addEventListener("click", () => selectCampaign(c.id));
    ul.append(li);
  }
  if (!camp.selected && list.length) selectCampaign(list[0].id);
}

function selectCampaign(id) {
  camp.selected = id;
  refreshCampaign().catch(console.error);
}

function poll() {
  clearTimeout(camp.timer);
  if (!camp.active) return;
  camp.timer = setTimeout(async () => {
    try { await refreshCampaign(); } catch (e) { console.error(e); }
    poll();
  }, 1500);
}

async function refreshCampaign() {
  if (!camp.selected) return;
  const d = await api(`/api/campaigns/${camp.selected}`);
  const c = d.campaign;
  $("#camp-empty").hidden = true;
  $("#camp-view").hidden = false;
  const name = $("#cp-name");
  name.textContent = c.name;
  name.append(el("span", c.mode === "simulation" ? "mode-sim" : "mode-live",
    c.mode === "simulation" ? "SIMULATION · no real calls" : "LIVE · Teams Phone"));
  $("#cp-sub").textContent = `Max ${c.max_concurrent} concurrent · ${c.max_attempts} attempts · retries after ${c.retry_seconds.join("/")}s · ${c.respect_calling_window ? "calling hours enforced" : "calling hours not enforced"}`;
  const st = $("#cp-status");
  st.textContent = c.status;
  st.className = "status-chip " + (c.status === "running" ? "in_progress" : c.status === "completed" ? "completed" : "");
  $("#cp-start").textContent = c.status === "paused" ? "Resume" : "Start";
  $("#cp-start").disabled = !["draft", "paused"].includes(c.status);
  $("#cp-pause").disabled = c.status !== "running";
  $("#cp-stop").disabled = ["completed", "stopped"].includes(c.status);

  const total = Object.values(d.funnel).reduce((a, b) => a + b, 0);
  $("#cp-leads").textContent = num(total);
  $("#cp-pacing").textContent = `${num(d.in_flight)} / ${num(c.max_concurrent)}`;
  $("#cp-esc").textContent = num(d.funnel.escalated || 0);
  $("#cp-pipe").textContent = money(d.pipeline_usd);

  const f = $("#cp-funnel");
  f.replaceChildren();
  for (const [key, label] of FUNNEL) {
    const n = d.funnel[key] || 0;
    const row = el("div", "fbar");
    const track = el("div", "track");
    const fill = el("div", "fill " + key);
    fill.style.width = total ? `${(100 * n) / total}%` : "0";
    track.append(fill);
    row.append(el("span", null, label), track, el("span", "n", num(n)));
    f.append(row);
  }

  const ul = $("#cp-escalations");
  ul.replaceChildren();
  if (!d.escalations.length) ul.append(el("li", "li-co", "No escalations yet."));
  for (const e of d.escalations) {
    const p = e.plan || {};
    const li = el("li");
    const head = el("div", "eh");
    const left = el("div");
    left.append(el("span", "pri " + (p.priority || ""), p.priority || "-"), el("b", null, e.company));
    head.append(left, el("span", "src", `score ${e.score ?? "-"} · ${money(e.contract_value)} · ${p.recommended_channel || ""} · ${e.plan_source}`));
    li.append(head, el("div", null, p.headline || ""));
    const tp = el("ul");
    for (const t of p.talking_points || []) tp.append(el("li", null, t));
    li.append(tp);
    ul.append(li);
  }

  const llm = d.llm;
  const agent = d.escalation_agent;
  $("#ag-model").textContent = agent.configured
    ? `MAF Agent on Foundry model ${agent.model}`
    : "Model not configured: deterministic plans (rules) only";
  const budget = Number(c.llm_budget_usd);
  const spent = Number(llm.cost_usd);
  const pct = budget > 0 ? Math.min(100, (100 * spent) / budget) : 0;
  const bar = $("#ag-bar");
  bar.style.width = `${pct}%`;
  bar.className = pct >= 100 ? "over" : pct >= 80 ? "warn" : "";
  $("#ag-spend").textContent = `$${spent.toFixed(4)} of $${budget.toFixed(2)} campaign budget`;
  const stats = [
    ["Model calls", num(llm.model_calls)],
    ["Cache hits (no tokens)", num(llm.cache_hits)],
    ["Budget skips → rules", num(llm.budget_skips)],
    ["Guardrail fallbacks", num(llm.guardrail_fallbacks)],
    ["Model errors → rules", num(llm.errors)],
    ["Input tokens", num(llm.input_tokens)],
    ["  of which cached", num(llm.cached_tokens)],
    ["Output tokens", num(llm.output_tokens)],
    ["Cost per escalation", llm.cost_per_escalation == null ? "-" : `$${llm.cost_per_escalation.toFixed(5)}`],
    ["Avg model latency", llm.avg_latency_ms ? `${Math.round(llm.avg_latency_ms)} ms` : "-"],
  ];
  const dl = $("#ag-stats");
  dl.replaceChildren();
  for (const [k, v] of stats) dl.append(el("dt", null, k), el("dd", null, v));

  const g = $("#ag-guards");
  g.replaceChildren();
  if (!d.guardrails.length) g.append(el("li", "li-co", "No interventions yet."));
  for (const r of d.guardrails) {
    const li = el("li");
    const left = el("span");
    left.append(el("span", "stage", r.stage), document.createTextNode(r.rule.replace(/_/g, " ").toLowerCase()));
    li.append(left, el("b", null, num(r.n)));
    g.append(li);
  }
}

async function campaignAction(action) {
  try { await api(`/api/campaigns/${camp.selected}/${action}`, { method: "POST" }); await refreshCampaign(); loadCampaigns(); }
  catch (e) { alert(e.message); }
}

$("#cp-start").addEventListener("click", () => campaignAction($("#cp-start").textContent === "Resume" ? "resume" : "start"));
$("#cp-pause").addEventListener("click", () => campaignAction("pause"));
$("#cp-stop").addEventListener("click", () => { if (confirm("Stop this campaign? In-flight calls finish; nothing new is dialled.")) campaignAction("stop"); });

$("#camp-mode").addEventListener("change", (e) => {
  const sim = e.target.value === "simulation";
  $("#camp-count-wrap").hidden = !sim;
  $("#camp-window").checked = !sim;
});

$("#camp-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const msg = $("#camp-msg");
  const data = Object.fromEntries(new FormData(f).entries());
  for (const k of ["lead_count", "max_concurrent", "max_attempts", "escalation_daily_cap"]) data[k] = Number(data[k]);
  data.llm_budget_usd = Number(data.llm_budget_usd);
  data.respect_calling_window = f.respect_calling_window.checked;
  if (data.mode === "live") delete data.lead_count;
  const btn = f.querySelector("button");
  btn.disabled = true;
  try {
    const r = await api("/api/campaigns", { method: "POST", body: JSON.stringify(data) });
    msg.className = "form-msg ok";
    msg.textContent = `Created with ${num(r.leads)} leads. Press Start.`;
    camp.selected = r.id;
    await loadCampaigns();
    await refreshCampaign();
  } catch (err) {
    msg.className = "form-msg err";
    msg.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));
let initialView = "call";
try { initialView = localStorage.getItem("cockpit.view") || "call"; } catch { /* storage unavailable */ }
showView(initialView);
