/* Injection Autopilot front end. No build step, no dependencies. Every string from the API is escaped. */
(() => {
  "use strict";

  const STAGES = {
    cadence:   { n: 1, name: "Cadence",   agent: "Scheduling agent",      q: "Is every treated eye booked on a safe, payable date?", unit: "eyes under treatment", okWord: "On track", okShort: "on track",
                 sub: "Every eye on a treatment plan: when it is due, when it is booked, and the earliest date the payer will pay." },
    clearance: { n: 2, name: "Clearance", agent: "Care management agent", q: "Is each injection in the next 7 days cleared before the patient is in the chair?", unit: "injection visits, next 7 days", okWord: "Cleared", okShort: "cleared",
                 sub: "Eligibility, authorization, step therapy, interval and the order, checked for every injection visit in the next 7 days." },
    claim:     { n: 3, name: "Claim check", agent: "Revenue cycle agent", q: "Does every draft claim agree with the chart before it is sent?", unit: "draft claims", okWord: "Ready to send", okShort: "ready",
                 sub: "Each draft claim compared with the procedure record, the drug administration record and the authorization." },
    denial:    { n: 4, name: "Denials",   agent: "Revenue cycle agent",   q: "For each denial: what went wrong, and what is the next step?", unit: "open denials", okWord: "Resolved", okShort: "resolved",
                 sub: "Root cause for each denied claim, with a corrected claim or an appeal drafted and the deadline in view." },
  };
  const ROLE_NAMES = { owner: "Practice owner", auth_coordinator: "Authorization coordinator", scheduler: "Scheduler",
                       biller: "Biller", physician: "Physician", compliance: "Compliance" };
  const ROLE_HOME = { owner: "today", auth_coordinator: "clearance", scheduler: "cadence", biller: "claim", physician: "today", compliance: "trust" };
  const STATUS_WORD = { clear: "Cleared", action: "Needs action", hold: "On hold", note: "Note" };
  const ACTION_WORD = { verify_eligibility: "Verify eligibility", request_pa: "Request authorization", renew_pa: "Renew authorization",
    expedite_pa: "Expedite authorization", step_exception: "Step therapy exception", reschedule: "Reschedule", book: "Book",
    outreach: "Contact patient", review_chart: "Review chart", fix_claim: "Correct claim", appeal: "Appeal", review_denial: "Review denial" };
  const DRAFT_WORD = { pa_request: "Authorization request", appeal: "Appeal letter", outreach_sms: "Text message to patient", step_exception: "Step therapy exception letter" };
  const ICON = {
    clear: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.2"/><path d="M5.2 8.2l1.9 1.9 3.7-4"/></svg>',
    action: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2.2l6.2 11H1.8z"/><path d="M8 6.6v3M8 11.6v.1"/></svg>',
    hold: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5.4 1.8h5.2l3.6 3.6v5.2l-3.6 3.6H5.4l-3.6-3.6V5.4z"/><path d="M5.6 8h4.8"/></svg>',
    note: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.2"/><path d="M8 7.4v3.4M8 5.2v.1"/></svg>',
    search: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="7" cy="7" r="4.6"/><path d="M10.6 10.6l3 3"/></svg>',
  };

  const $ = (s, el = document) => el.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n) => "$" + Math.round(n).toLocaleString("en-US");
  const day = (iso) => new Date(iso + "T12:00:00");
  const fmtDay = (iso, long) => day(iso).toLocaleDateString("en-US", long ? { weekday: "long", month: "long", day: "numeric" } : { month: "short", day: "numeric" });
  const fmtTime = (t) => { if (!t) return ""; const [h, m] = t.split(":").map(Number); return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`; };
  const daysBetween = (a, b) => Math.round((day(b) - day(a)) / 864e5);
  const pill = (status, word) => `<span class="pill ${esc(status)}">${ICON[status] || ""}${esc(word || STATUS_WORD[status])}</span>`;

  const store = { get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } }, set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } } };
  const S = { role: store.get("role", "owner"), summary: null, items: {}, filter: "open", q: "", view: "today", drawer: null, lastFocus: null };

  async function api(path, opts) {
    const sep = path.includes("?") ? "&" : "?";
    const res = await fetch(`/api/${path}${sep}role=${encodeURIComponent(S.role)}`, opts);
    if (!res.ok) { let m = res.statusText; try { m = (await res.json()).detail || m; } catch { /* not json */ } throw new Error(m); }
    return res.json();
  }
  function toast(msg) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), 2600); }

  // ---------------------------------------------------------------- shell
  function renderNav() {
    const s = S.summary;
    const open = (st) => s ? s.stages[st].action + s.stages[st].hold - s.stages[st].decided : 0;
    const link = (id, inner) => `<a href="#/${id}" ${S.view === id ? 'aria-current="page"' : ""}>${inner}</a>`;
    $("#nav").innerHTML =
      link("today", `<span class="step" style="border-style:dashed">◦</span><span>Today</span>`) +
      `<div class="group">The injection visit</div>` +
      Object.entries(STAGES).map(([id, st]) => link(id,
        `<span class="step">${st.n}</span><span>${esc(st.name)}<small>${esc(st.agent)}</small></span>` +
        (open(id) ? `<span class="badge hot" title="Waiting for a person">${open(id)}</span>` : s ? `<span class="badge">0</span>` : ""))).join("") +
      `<div class="group">Proof</div>` +
      link("trust", `<span class="step" style="border-radius:5px">✓</span><span>Trust<small>Evals, checks, audit</small></span>`);
  }
  function setHeader(title, sub) { $("#title").textContent = title; $("#sub").textContent = sub; document.title = `${title} · Injection Autopilot`; }

  // ---------------------------------------------------------------- today
  function viewToday() {
    const s = S.summary, c = s.stages.clearance, k = s.stages.claim, d = s.stages.denial, cad = s.stages.cadence;
    setHeader("Today", `${s.practice} · ${s.patients} patients on anti-VEGF treatment plans`);
    const need = c.action + c.hold;
    const stageCard = ([id, st]) => {
      const x = s.stages[id], open = x.action + x.hold, w = (n) => (x.total ? (100 * n / x.total).toFixed(1) : 0);
      const riskLabel = { cadence: "Eyes at risk of a gap", clearance: "Drug and fee at risk", claim: "Held back from denial", denial: "Denied, open" }[id];
      return `<a class="card stage" href="#/${id}">
        <div><div class="who">${st.n} · ${esc(st.agent)}</div><h3>${esc(st.name)}</h3></div>
        <p class="q">${esc(st.q)}</p>
        <div class="big"><b>${open}</b><span>of ${x.total} need a person</span></div>
        <div class="bar" role="img" aria-label="${x.clear} clear, ${x.action} need action, ${x.hold} on hold">
          <i class="c" style="width:${w(x.clear)}%"></i><i class="a" style="width:${w(x.action)}%"></i><i class="h" style="width:${w(x.hold)}%"></i></div>
        <div class="legend"><span class="c">${x.clear} ${esc(st.okShort)}</span><span class="a">${x.action} action</span><span class="h">${x.hold} hold</span></div>
        <div class="risk"><span>${riskLabel}</span><b>${id === "cadence" ? open : money(x.dollars)}</b></div></a>`;
    };
    const all = Object.values(S.items).flat();
    const urgent = all.filter((i) => i.status !== "clear" && !i.decision)
      .sort((a, b) => (a.stage === "clearance" ? 0 : 1) - (b.stage === "clearance" ? 0 : 1) || String(a.when).localeCompare(String(b.when)) || (a.status === b.status ? 0 : a.status === "hold" ? -1 : 1) || String(a.time).localeCompare(String(b.time)))
      .slice(0, 8);
    const owners = Object.entries(s.owners).sort((a, b) => b[1] - a[1]);
    const maxO = Math.max(1, ...owners.map((o) => o[1]));
    const maxR = Math.max(1, ...s.top_rules.map((r) => r.count));
    $("#view").innerHTML = `
      <p class="lede"><b>${c.total} injections</b> are booked in the next 7 days. <b>${c.clear} are cleared.</b>
        ${need ? `<b>${need} need someone</b> before the patient is in the chair, with ${money(c.dollars)} of drug and fees riding on them.` : "Nothing is waiting on a person."}
        ${k.action + k.hold} of ${k.total} draft claims would not have matched the chart, and ${cad.action + cad.hold} treated eyes are off their schedule.</p>
      <div class="flow">${Object.entries(STAGES).map(stageCard).join("")}</div>
      <div class="two">
        <div class="card"><header><h2>Needs a person first</h2><span class="hint">Soonest injection visit first</span></header>
          <ul class="rows">${urgent.map((i) => `<li data-open="${esc(i.id)}" tabindex="0">
            <span>${pill(i.status)}</span>
            <span class="what"><b>${esc(i.patient_name || i.patient_token)} · ${esc(i.title)}</b><span>${esc(i.headline)}</span></span>
            <span class="meta">${esc(STAGES[i.stage].name)}<br>${i.when ? esc(fmtDay(i.when)) : ""}${i.time ? " · " + esc(fmtTime(i.time)) : ""}</span></li>`).join("") || `<li class="empty">Nothing is waiting.</li>`}</ul></div>
        <div class="stack">
          <div class="card"><header><h2>Waiting, by role</h2><span class="hint">Undecided items</span></header>
            <div class="hbars">${owners.map(([r, n]) => `<div class="hbar"><div class="t"><span>${esc(ROLE_NAMES[r] || r)}</span><b>${n}</b></div><div class="track"><i style="width:${100 * n / maxO}%"></i></div></div>`).join("") || `<p class="hint">Nothing waiting.</p>`}</div></div>
          <div class="card"><header><h2>Most common findings</h2><span class="hint">Where the practice leaks</span></header>
            <div class="hbars">${s.top_rules.map((r) => `<div class="hbar"><div class="t"><span>${esc(r.label)} <span class="mono rid">${esc(r.rule_id)}</span></span><b>${r.count}</b></div><div class="track"><i style="width:${100 * r.count / maxR}%"></i></div></div>`).join("")}</div></div>
        </div>
      </div>
      <p class="count">${s.checks_run.toLocaleString()} checks run across ${all.length} items in ${s.build_ms} ms. Every finding names the record it came from.</p>`;
  }

  // ---------------------------------------------------------------- stage lists
  function timeline(f, asOf, big) {
    if (!f.last) return `<span class="hint">No injection yet</span>`;
    const pts = [f.last, f.due, f.earliest, f.booked, f.proposed, asOf].filter(Boolean).map((x) => day(x).getTime());
    const min = Math.min(...pts) - 4 * 864e5, max = Math.max(...pts) + 9 * 864e5;
    const pos = (iso) => (100 * (day(iso).getTime() - min) / (max - min)).toFixed(2);
    const dueEnd = new Date(day(f.due).getTime() + 7 * 864e5).toISOString().slice(0, 10);
    const bad = f.booked && (f.booked < f.earliest || f.booked > dueEnd);
    const near = f.booked && f.proposed && f.proposed !== f.booked && Math.abs(pos(f.booked) - pos(f.proposed)) < 12;
    const side = (iso) => !near ? "" : iso === (f.booked < f.proposed ? f.booked : f.proposed) ? "left" : "right";
    const lab = (iso, name, cls = "") => big ? `<span class="lab ${cls}" style="left:${pos(iso)}%"><b>${esc(name)}</b>${esc(fmtDay(iso))}</span>` : "";
    const sameDay = f.booked && f.booked === f.due;
    return `<div class="tl ${big ? "big" : ""}" role="img" aria-label="Last injection ${esc(fmtDay(f.last))}, due ${esc(fmtDay(f.due))}, ${f.booked ? "booked " + esc(fmtDay(f.booked)) : "not booked"}">
      <div class="track"></div>
      <div class="zone block" style="left:${pos(f.last)}%;width:${pos(f.earliest) - pos(f.last)}%" title="Before ${esc(fmtDay(f.earliest))} the payer will not pay"></div>
      <div class="zone ok" style="left:${pos(f.due)}%;width:${pos(dueEnd) - pos(f.due)}%" title="On time: ${esc(fmtDay(f.due))} to ${esc(fmtDay(dueEnd))}"></div>
      <div class="today" style="left:${pos(asOf)}%" title="Today"></div>
      <i class="m last" style="left:${pos(f.last)}%" title="Last injection ${esc(fmtDay(f.last))}"></i>
      <i class="m due" style="left:${pos(f.due)}%" title="Due ${esc(fmtDay(f.due))}"></i>
      ${f.proposed && f.proposed !== f.booked ? `<i class="m prop" style="left:${pos(f.proposed)}%" title="Proposed ${esc(fmtDay(f.proposed))}"></i>` : ""}
      ${f.booked ? `<i class="m booked ${bad ? "bad" : ""}" style="left:${pos(f.booked)}%" title="Booked ${esc(fmtDay(f.booked))}"></i>` : ""}
      ${lab(f.last, "Last")}${sameDay ? "" : lab(f.due, "Due")}${f.booked ? lab(f.booked, sameDay ? "Due · booked" : "Booked", side(f.booked)) : ""}${f.proposed && f.proposed !== f.booked && f.proposed !== f.due ? lab(f.proposed, "Proposed", side(f.proposed)) : ""}
    </div>`;
  }
  const TL_KEY = `<div class="tl-key"><span><i class="k-last"></i>Last injection</span><span><i class="k-block"></i>Too soon to be paid</span><span><i class="k-due"></i>Due</span><span><i class="k-ok"></i>On-time window</span><span><i class="k-booked"></i>Booked</span><span><i class="k-prop"></i>Proposed</span><span><i class="k-today"></i>Today</span></div>`;

  function findingCell(i) {
    if (i.status === "clear") return `<td class="finding ok">All checks passed</td>`;
    const more = Number(i.facts.more_findings || 0);
    return `<td class="finding"><span class="txt" title="${esc(i.headline)}">${esc(i.headline)}</span>${more ? `<span class="more">+${more} more finding${more > 1 ? "s" : ""}</span>` : ""}</td>`;
  }
  const who = (i) => `<b>${esc(i.patient_name || i.patient_token)}</b>${i.patient_name ? `<span class="sub mono">${esc(i.patient_token)}</span>` : ""}`;
  const statusCell = (i, okWord) => `<td class="st">${i.decision ? pill("done", { approve: "Approved", edit: "Edited, approved", reject: "Rejected" }[i.decision.action]) : pill(i.status, i.status === "clear" ? okWord : null)}</td>`;
  const ownerCell = (i) => `<td class="owner">${i.status === "clear" ? "" : esc(ROLE_NAMES[i.owner] || "")}</td>`;

  const COLS = {
    cadence: { head: ["Patient", "Eye and drug", "Last injection to next", "Status", "Finding", "Next with"],
      row: (i, s) => `<td>${who(i)}</td><td><b>${esc(i.title)}</b><span class="sub">${esc(i.subtitle)}</span></td><td>${timeline(i.facts, s.as_of)}</td>${statusCell(i, "On track")}${findingCell(i)}${ownerCell(i)}` },
    clearance: { head: ["Time", "Patient", "Injection", "Payer", "Status", "Finding", "Next with"],
      row: (i) => `<td class="n nw">${esc(fmtTime(i.time))}</td><td>${who(i)}</td><td><b>${esc(i.title)}</b></td><td class="nw">${esc(i.subtitle)}</td>${statusCell(i, "Cleared")}${findingCell(i)}${ownerCell(i)}` },
    claim: { head: ["Service date", "Patient", "Chart says", "Payer", "Charges", "Status", "Finding"],
      row: (i) => `<td>${esc(fmtDay(i.when))}</td><td>${who(i)}</td><td><b>${esc(i.title)}</b></td><td class="nw">${esc(i.subtitle)}</td><td class="n">${money(i.dollars_at_risk || 0) === "$0" ? "" : money(i.dollars_at_risk)}</td>${statusCell(i, "Ready to send")}${findingCell(i)}` },
    denial: { head: ["Appeal by", "Patient", "Claim", "Denied for", "Amount", "Status", "Root cause and next step"],
      row: (i) => `<td class="nw"><b>${esc(fmtDay(i.when))}</b><span class="sub">${esc(i.facts["Days left"])} days left</span></td><td>${who(i)}</td><td><b>${esc(i.title)}</b><span class="sub">${esc(i.subtitle)}</span></td><td><b class="mono">${esc((i.facts.Reason || "").split(" · ")[0])}</b><span class="sub">${esc((i.facts.Reason || "").split(" · ").slice(1).join(" · "))}</span></td><td class="n">${esc(i.facts.Amount || "")}</td>${statusCell(i)}${findingCell(i)}` },
  };

  function viewStage(id) {
    const st = STAGES[id], s = S.summary, x = s.stages[id], items = S.items[id] || [];
    setHeader(st.name, st.sub);
    const mine = (i) => S.role === "owner" || S.role === "compliance" || i.owner === S.role;
    const match = (i) => !S.q || `${i.patient_name || ""} ${i.patient_token} ${i.title} ${i.subtitle} ${i.headline}`.toLowerCase().includes(S.q.toLowerCase());
    const sets = { open: items.filter((i) => i.status !== "clear" && !i.decision), mine: items.filter((i) => i.status !== "clear" && !i.decision && mine(i)), clear: items.filter((i) => i.status === "clear"), done: items.filter((i) => i.decision), all: items };
    const shown = (sets[S.filter] || sets.open).filter(match);
    const rank = { hold: 0, action: 1, clear: 2 };   // the clearance desk works day by day: soonest first, worst first within a day
    if (id === "clearance") shown.sort((a, b) => a.when.localeCompare(b.when) || rank[a.status] - rank[b.status] || (a.time || "").localeCompare(b.time || ""));
    const kpi = (label, value, ctx, cls = "") => `<div class="card kpi ${cls}"><div class="label">${label}</div><div class="value">${value}</div><div class="ctx">${ctx}</div></div>`;
    const riskCtx = { cadence: "no dollars: this one is about vision", clearance: "drug and fee on visits not yet cleared", claim: "charges on claims that would have gone out wrong", denial: "denied and still inside the appeal window" }[id];
    const seg = (key, label) => `<button type="button" aria-pressed="${S.filter === key}" data-filter="${key}">${label}<span>${sets[key].length}</span></button>`;
    let body = "", lastDay = null;
    for (const i of shown) {
      if (id === "clearance" && i.when !== lastDay) {
        lastDay = i.when;
        const n = shown.filter((y) => y.when === i.when).length, d = daysBetween(s.as_of, i.when);
        body += `<tr class="day"><td colspan="7">${esc(fmtDay(i.when, true))}<span>${d === 0 ? "today" : d === 1 ? "tomorrow" : `in ${d} days`} · ${n} shown</span></td></tr>`;
      }
      body += `<tr class="row ${i.decision ? "decided" : ""}" tabindex="0" data-open="${esc(i.id)}">${COLS[id].row(i, s)}</tr>`;
    }
    const numeric = { Charges: 1, Amount: 1, Time: 1 };
    $("#view").innerHTML = `
      <div class="kpis">
        ${kpi(st.unit[0].toUpperCase() + st.unit.slice(1), x.total, esc(st.agent))}
        ${id === "denial" ? kpi("Decided", x.decided, "approved, edited or rejected") : kpi(ICON.clear + esc(st.okWord), x.clear, x.total ? Math.round(100 * x.clear / x.total) + "% with nothing to do" : "", "is-clear")}
        ${kpi(ICON.action + "Needs action", x.action, "a specific fix is proposed", x.action ? "is-action" : "")}
        ${kpi(ICON.hold + "On hold", x.hold, "sources disagree: a person decides", x.hold ? "is-hold" : "")}
        ${kpi(id === "cadence" ? "Decided" : "At risk", id === "cadence" ? x.decided : money(x.dollars), id === "cadence" ? "approved, edited or rejected" : riskCtx)}
      </div>
      <div class="toolbar">
        <div class="seg" role="group" aria-label="Filter">${seg("open", "Open")}${S.role !== "owner" && S.role !== "compliance" ? seg("mine", "Mine") : ""}${id === "denial" ? "" : seg("clear", esc(st.okWord))}${seg("done", "Decided")}${seg("all", "All")}</div>
        <label class="search">${ICON.search}<input id="q" type="search" placeholder="Search patient, drug, payer, finding" value="${esc(S.q)}" aria-label="Search"></label>
      </div>
      ${id === "cadence" ? TL_KEY + '<div style="height:10px"></div>' : ""}
      <div class="card table"><table>
        <thead><tr>${COLS[id].head.map((h) => `<th class="${numeric[h] ? "n" : ""}">${h}</th>`).join("")}</tr></thead>
        <tbody>${body || `<tr><td colspan="7"><div class="empty"><b>${S.q ? "Nothing matches that search." : S.filter === "open" || S.filter === "mine" ? "Nothing is waiting on a person here." : "Nothing in this list."}</b>${S.q ? "Try a patient token, a drug or a payer." : "Switch to All to see everything the agent checked."}</div></td></tr>`}</tbody>
      </table></div>
      <p class="count">${shown.length} of ${items.length} shown · Up and Down to move, Enter to open, Esc to close</p>`;
  }

  // ---------------------------------------------------------------- drawer
  const evidence = (ev) => ev && ev.length ? `<div class="ev">${ev.map((e) => `<span title="Record and field the agent read">${esc(e.ref)} · ${esc(e.field)} = <b>${esc(String(e.value).replace(/^\[|\]$|'/g, "") || "(empty)")}</b></span>`).join("")}</div>` : "";
  function changesTable(ch) {
    if (!ch || !ch.length) return "";
    return `<table class="changes"><thead><tr><th>Line</th><th>Field</th><th>Claim says</th><th>Chart supports</th></tr></thead><tbody>${ch.map((c) =>
      `<tr><td class="mono">${esc(c.line ?? "claim")}</td><td>${esc(c.field)}</td><td class="from">${esc(c.from || "(empty)")}</td><td class="to">${esc(c.to || "(needs a person)")}</td></tr>`).join("")}</tbody></table>`;
  }
  function findingCard(c) {
    const a = c.action;
    return `<div class="card finding-card ${esc(c.outcome)}">
      <div class="fh">${pill(c.outcome)}<b>${esc((c.outcome !== "note" && S.summary.rule_names[c.rule_id]) || c.label)}${c.eye ? " · " + esc(c.eye) : ""}</b><span class="rule mono">${esc(c.rule_id)}</span></div>
      <p>${esc(c.detail)}</p>${evidence(c.evidence)}
      ${a ? `${changesTable(a.changes)}<div class="next"><span class="tag">Proposed</span><span>${esc(a.summary)}</span><span class="by">${esc(ROLE_NAMES[a.owner] || a.owner)}</span></div>`
          : c.outcome === "hold" ? `<div class="next"><span class="tag" style="color:var(--hold)">No fix proposed</span><span>Two records disagree or one is missing. The agent does not pick a winner.</span></div>` : ""}
    </div>`;
  }
  function claimLines(it) {
    if (!it.facts.lines) return "";
    const touched = new Set(it.checks.flatMap((c) => (c.action?.changes || []).map((x) => String(x.line))));
    const rows = it.facts.lines.split("\n").map((l) => l.split("|"));
    return `<div class="sec"><h3>Draft claim as written</h3><div class="card table"><table class="lines"><thead><tr><th>#</th><th>Code</th><th>Modifiers</th><th>Units</th><th>Diagnosis</th><th class="n">Charge</th><th>NDC</th></tr></thead><tbody>${rows.map((r) =>
      `<tr class="${touched.has(r[0]) ? "touched" : ""}"><td>${esc(r[0])}</td><td>${esc(r[1])}</td><td>${esc(r[2])}</td><td>${esc(r[3])}</td><td>${esc(r[4])}</td><td class="n">${esc(r[5])}</td><td>${esc(r[6])}</td></tr>`).join("")}</tbody></table></div></div>`;
  }

  async function openDrawer(id) {
    const el = $("#drawer");
    S.lastFocus = document.activeElement;
    el.hidden = false; $("#scrim").hidden = false;
    el.innerHTML = `<div class="d-head"><div class="skeleton" style="height:60px"></div></div><div class="d-body"><div class="skeleton"></div><div class="skeleton"></div></div>`;
    let it;
    try { it = await api(`items/${encodeURIComponent(id)}`); }
    catch (e) { el.innerHTML = `<div class="d-head"><div class="crumb"><span>Error</span><button class="x" data-close aria-label="Close">×</button></div></div><div class="d-body"><div class="card error">Could not load this item: ${esc(e.message)}</div></div>`; return; }
    S.drawer = it;
    const st = STAGES[it.stage];
    const bad = it.checks.filter((c) => c.outcome === "hold" || c.outcome === "action");
    const notes = it.checks.filter((c) => c.outcome === "note");
    const passes = it.checks.filter((c) => c.outcome === "pass");
    const hidden = new Set(["lines", "more_findings", "last", "due", "earliest", "booked", "proposed"]);
    const facts = Object.entries(it.facts).filter(([k, v]) => !hidden.has(k) && v);
    const d = it.decision;
    const cost = it.trace.reduce((a, t) => a + t.cost_usd, 0), toks = it.trace.reduce((a, t) => a + t.tokens, 0);
    el.innerHTML = `
      <div class="d-head">
        <div class="crumb"><span>${st.n} · ${esc(st.name)} · <span class="mono">${esc(it.subject_ref)}</span></span><button class="x" data-close aria-label="Close">×</button></div>
        <h2 id="d-title">${esc(it.title)}</h2>
        <p class="who">${esc(it.patient_name || "Name hidden for this role")} <span class="mono">${esc(it.patient_token)}</span>${it.when ? ` · ${{ cadence: it.facts.booked ? "Booked " : "Due ", clearance: "", claim: "Service date ", denial: "Appeal by " }[it.stage]}${esc(fmtDay(it.when, true))}${it.time ? " · " + esc(fmtTime(it.time)) : ""}` : ""}</p>
        <p style="margin-top:10px">${pill(it.status, it.status === "clear" ? st.okWord : null)} ${it.status !== "clear" && it.dollars_at_risk ? `<span class="chip">${money(it.dollars_at_risk)} at risk</span>` : ""} ${it.owner && it.status !== "clear" ? `<span class="chip">Next with: ${esc(ROLE_NAMES[it.owner])}</span>` : ""}</p>
      </div>
      <div class="d-body">
        ${it.stage === "cadence" && it.facts.last ? `<div class="sec"><h3>Schedule</h3><div class="card" style="padding:12px 10px">${timeline(it.facts, S.summary.as_of, true)}${TL_KEY}</div></div>` : ""}
        ${bad.length ? `<div class="sec"><h3>What the agent found</h3>${bad.map(findingCard).join("")}</div>` : `<div class="sec"><div class="card finding-card"><div class="fh">${pill("clear", st.okWord)}<b>${passes.length} checks passed</b></div><p>Nothing here needs a person.</p></div></div>`}
        ${notes.length ? `<div class="sec"><h3>Worth knowing</h3>${notes.map(findingCard).join("")}</div>` : ""}
        ${it.stage === "claim" ? claimLines(it) : ""}
        ${it.draft ? `<div class="sec"><h3>Draft, ready for review</h3><div class="card draft">
            <div class="dh"><b>${esc(DRAFT_WORD[it.draft.kind] || it.draft.kind)}</b>${it.draft.verified ? pill("clear", "Fact check passed") : pill("hold", "Fact check failed")}</div>
            <textarea id="draft" readonly aria-label="Draft text">${esc((it.draft.subject ? it.draft.subject + "\n\n" : "") + it.draft.body)}</textarea>
            <div class="df">${it.draft.verified ? "The model wrote the wording only. Every name, date, drug, eye, code and number was inserted by code from the record." : esc(it.draft.problems.join("; "))}</div></div></div>` : ""}
        ${passes.length && bad.length ? `<details class="card"><summary>${passes.length} checks passed</summary><ul class="passes">${passes.map((c) => `<li>${ICON.clear}<div><b>${esc(c.label)}${c.eye ? " · " + esc(c.eye) : ""}</b><span>${esc(c.detail)}</span></div></li>`).join("")}</ul></details>` : ""}
        ${passes.length && !bad.length ? `<div class="sec"><h3>What was checked</h3><div class="card"><ul class="passes">${passes.map((c) => `<li>${ICON.clear}<div><b>${esc(c.label)}${c.eye ? " · " + esc(c.eye) : ""}</b><span>${esc(c.detail)}</span></div></li>`).join("")}</ul></div></div>` : ""}
        <details class="card"><summary>How the agent got here <span class="hint" style="font-weight:400;margin-left:auto;margin-right:10px">${it.trace.length} steps${toks ? ` · ${toks.toLocaleString()} tokens` : ""}${cost ? ` · $${cost.toFixed(4)}` : ""}</span></summary>
          <ol class="trace">${it.trace.map((t) => `<li><div><div class="th"><span class="kind ${esc(t.kind)}">${esc(t.kind)}</span><b>${esc(t.name)}</b>${t.model ? `<span class="cost">${esc(t.model)} · ${t.tokens} tok${t.cost_usd ? ` · $${t.cost_usd.toFixed(4)}` : ""} · ${t.ms} ms</span>` : t.ms ? `<span class="cost">${t.ms} ms</span>` : ""}</div><p>${esc(t.story)}</p></div></li>`).join("")}</ol></details>
        ${facts.length ? `<details class="card"><summary>Record</summary><dl class="dl">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl></details>` : ""}
      </div>
      ${it.status === "clear" ? "" : `<div class="d-foot">
        ${d ? `<div class="decided-banner">${pill("done", { approve: "Approved", edit: "Edited, approved", reject: "Rejected" }[d.action])}<span>by ${esc(ROLE_NAMES[d.role])} · ${esc(new Date(d.at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }))}${d.note ? ` · “${esc(d.note)}”` : ""}</span></div>` : ""}
        <input id="note" type="text" placeholder="${it.status === "hold" ? "What did you find in the chart? (recorded)" : "Optional note (recorded with the decision)"}" aria-label="Decision note">
        <div class="btns">
          ${it.status === "hold"
            ? `<button class="btn primary" data-decide="approve">Mark resolved</button><button class="btn danger" data-decide="reject">Not an issue</button>`
            : `<button class="btn primary" data-decide="approve" ${it.draft && !it.draft.verified ? "disabled" : ""}>Approve</button>${it.draft ? `<button class="btn" data-edit>Edit draft</button>` : ""}<button class="btn danger" data-decide="reject">Reject</button>`}
          <span class="sp">${d ? "Deciding again replaces this. Both stay in the audit log." : "Nothing leaves the practice until a person approves."}</span>
        </div></div>`}`;
    $(".x", el).focus();
  }
  function closeDrawer() {
    if ($("#drawer").hidden) return;
    $("#drawer").hidden = true; $("#scrim").hidden = true; S.drawer = null;
    if (S.lastFocus && document.contains(S.lastFocus)) S.lastFocus.focus();
  }
  async function decide(action) {
    const it = S.drawer; if (!it) return;
    const ta = $("#draft"), edited = ta && !ta.readOnly;
    const note = ($("#note").value || "").trim();
    try {
      await api(`items/${encodeURIComponent(it.id)}/decision`, { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ action: edited && action === "approve" ? "edit" : action, role: S.role, note }) });
      toast({ approve: edited ? "Edited and approved. Recorded." : "Approved. Recorded.", reject: "Rejected. Recorded as a candidate eval case." }[action]);
      closeDrawer(); await load(); render();
    } catch (e) { toast("Could not record: " + e.message); }
  }

  // ---------------------------------------------------------------- trust
  async function viewTrust() {
    setHeader("Trust", "How we know the agent is right: what was checked, against what, and what every decision was.");
    $("#view").innerHTML = `<div class="grid3"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>`;
    const t = await api("trust");
    if (S.view !== "trust") return;
    const ev = t.evals, v = t.verify, ok = (b) => (b ? pill("clear", "Passing") : pill("hold", "Failing"));
    const missing = (cmd) => `<div class="card proof"><p>Not run yet. Run <code>${cmd}</code>.</p></div>`;
    const cs = v?.cross_source, ak = v?.answer_key, lk = v?.leak, evd = v?.evidence;
    $("#view").innerHTML = `
      <div class="grid3">
        ${ev ? `<div class="card proof"><div class="ph"><h2>Blind labelled cases</h2>${ok(ev.passed === ev.total)}</div>
          <div class="big">${ev.passed}<small> / ${ev.total} correct</small></div>
          <p>Written by an agent that read the spec and never saw the code. A case passes only when the status and the exact set of rules match.</p>
          <ul><li><b>${ev.wrong_clear}</b> wrong clears (said fine when a person was needed)</li><li><b>${ev.false_alarm}</b> false alarms across ${ev.expected_clear} near misses</li>${Object.entries(ev.by_stage).map(([k, x]) => `<li>${esc(STAGES[k]?.name || k)}: <b>${x.passed}/${x.total}</b></li>`).join("")}</ul></div>` : missing("python -m evals.report")}
        ${ak ? `<div class="card proof"><div class="ph"><h2>Answer key, every item</h2>${ok(!ak.wrong_clear.length && !ak.false_alarm.length && !ak.rule_mismatch.length)}</div>
          <div class="big">${ak.matched}<small> / ${ak.items} items match</small></div>
          <p>The data generator recorded which defects it planted. All ${ak.items} items are compared with that key, not a sample.</p>
          <ul><li><b>${ak.wrong_clear.length}</b> planted defects missed</li><li><b>${ak.false_alarm.length}</b> clean items flagged</li><li><b>${ak.rule_mismatch.length}</b> flagged for the wrong reason</li><li><b>${ak.expected_not_clear}</b> defects planted</li></ul></div>` : missing("python -m app.verify")}
        ${cs ? `<div class="card proof"><div class="ph"><h2>Second-source check</h2>${ok(!cs.mismatches.length && !(evd?.unresolved.length) && !(evd?.value_mismatches.length))}</div>
          <div class="big">${cs.mismatches.length}<small> mismatches in ${cs.fields_compared.toLocaleString()} fields</small></div>
          <p>A separate reader parses the raw FHIR bundle and compares every eye, drug, dose, date and code with what the app loaded.</p>
          <ul><li><b>${cs.records.toLocaleString()}</b> records re-read from the bundle</li>${evd ? `<li><b>${evd.rows.toLocaleString()}</b> evidence rows traced back to a record</li><li><b>${evd.unresolved.length}</b> pointing at nothing · <b>${evd.value_mismatches.length}</b> quoting a wrong value</li>` : ""}</ul></div>` : ""}
        ${lk ? `<div class="card proof"><div class="ph"><h2>Leak check</h2>${ok(!lk.hits.length)}</div>
          <div class="big">${lk.hits.length}<small> identifiers found</small></div>
          <p>Every prompt the model layer built and every audit line is scanned for each patient's name, birth date, phone and member ID.</p>
          <ul><li><b>${lk.prompts.toLocaleString()}</b> prompts scanned</li><li><b>${lk.identifiers.toLocaleString()}</b> identifiers searched for</li><li><b>${lk.log_lines}</b> audit lines scanned</li></ul></div>` : ""}
        <div class="card proof"><div class="ph"><h2>Model use and cost</h2>${pill(t.model.mode === "stub" ? "note" : "clear", t.model.mode === "stub" ? "Stub" : "Live model")}</div>
          <div class="big">$${t.model.cost_usd.toFixed(2)}<small> for ${t.model.calls} calls</small></div>
          <p>${t.model.mode === "stub" ? "No API key is set, so a deterministic stand-in runs the two model steps. The pipeline, redaction and fact checks are the real ones." : "Live model calls, with a budget guard."}</p>
          <ul>${t.model.tasks.map((x) => `<li>${esc(x.task)}: <b>${x.calls}</b> calls · ${x.tokens.toLocaleString()} tokens · ${x.ms.toFixed(0)} ms</li>`).join("")}</ul></div>
        <div class="card proof"><div class="ph"><h2>What is real here</h2>${pill("note", "Read this")}</div>
          <p>All patients, payers and claims are synthetic. The four payer policies are written for the prototype and are not any real payer's rules.</p>
          <ul><li>Billing codes and units follow public sources, listed with their confidence in <code>docs/domain_recon.md</code></li><li>Dollar figures use illustrative allowed amounts</li><li>${t.flags.length} records flagged at ingest (kept and shown, never repaired)</li></ul></div>
      </div>
      <div class="card"><header><h2>Decision log</h2><span class="hint">Append-only · patient token only · the latest decision per item wins</span></header>
        <div class="pad">${t.decisions.length ? `<table class="plain"><thead><tr><th>When</th><th>Role</th><th>Decision</th><th>Item</th><th>Patient</th><th>What the agent had said</th><th>Note</th></tr></thead><tbody>${t.decisions.slice(0, 40).map((d) =>
          `<tr><td class="mono">${esc(new Date(d.at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }))}</td><td>${esc(ROLE_NAMES[d.role])}</td><td>${pill("done", { approve: "Approved", edit: "Edited", reject: "Rejected" }[d.action])}</td><td class="mono">${esc(d.item_id)}</td><td class="mono">${esc(d.patient_token)}</td><td class="finding">${esc(d.headline_at_decision)}</td><td>${esc(d.note)}</td></tr>`).join("")}</tbody></table>
          <p class="count">Rejections and edits become candidate eval cases: <code>python scripts/feedback_to_cases.py</code>. A person promotes each one.</p>`
          : `<div class="empty"><b>No decisions yet.</b>Open any item that needs a person and approve, edit or reject it. It will appear here.</div>`}</div></div>`;
  }

  // ---------------------------------------------------------------- routing and events
  async function load() {
    const [summary, items] = await Promise.all([api("summary"), api("items")]);
    S.summary = summary; S.items = {};
    for (const i of items) (S.items[i.stage] ||= []).push(i);
    $("#asof").textContent = `As of ${day(summary.as_of).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", year: "numeric" })}`;
    $("#mode").textContent = summary.model_mode === "stub" ? "Model steps: deterministic stub ($0)." : `Model: ${summary.model_mode}`;
  }
  function render() {
    renderNav();
    if (S.view === "today") viewToday();
    else if (S.view === "trust") viewTrust().catch(fail);
    else viewStage(S.view);
  }
  function fail(e) { $("#view").innerHTML = `<div class="card error"><b>Something went wrong.</b> ${esc(e.message)}. Is the server running, and has <code>python -m app.synth</code> been run?</div>`; }
  function route() {
    const [, view, item] = location.hash.split("/");
    const v = STAGES[view] || view === "trust" || view === "today" ? view : ROLE_HOME[S.role] || "today";
    if (v !== S.view) { S.filter = "open"; S.q = ""; }
    S.view = v; render(); window.scrollTo(0, 0);
    if (item) openDrawer(decodeURIComponent(item)); else closeDrawer();
  }
  const openItem = (id) => { location.hash = `#/${id.split(":")[0]}/${encodeURIComponent(id)}`; };
  const closeItem = () => { location.hash = `#/${S.view}`; };

  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-open],[data-filter],[data-close],[data-decide],[data-edit]");
    if (e.target.id === "scrim") return closeItem();
    if (!t) return;
    if (t.dataset.open) openItem(t.dataset.open);
    else if (t.dataset.filter) { S.filter = t.dataset.filter; viewStage(S.view); }
    else if ("close" in t.dataset) closeItem();
    else if (t.dataset.decide) decide(t.dataset.decide);
    else if ("edit" in t.dataset) { const ta = $("#draft"); ta.readOnly = false; ta.focus(); t.textContent = "Editing"; t.disabled = true; $('[data-decide="approve"]').textContent = "Approve edited draft"; }
  });
  document.addEventListener("input", (e) => {
    if (e.target.id !== "q") return;
    S.q = e.target.value; const pos = e.target.selectionStart;
    viewStage(S.view); const q = $("#q"); q.focus(); q.setSelectionRange(pos, pos);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#drawer").hidden) return closeItem();
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
    if (e.key === "Enter" && document.activeElement.dataset?.open) return openItem(document.activeElement.dataset.open);
    if (typing || !$("#drawer").hidden) return;
    if (e.key === "/") { const q = $("#q"); if (q) { e.preventDefault(); q.focus(); } return; }
    if (!["ArrowDown", "ArrowUp", "j", "k"].includes(e.key)) return;
    const rows = [...document.querySelectorAll("[data-open]")]; if (!rows.length) return;
    e.preventDefault();
    const i = rows.indexOf(document.activeElement), down = e.key === "ArrowDown" || e.key === "j";
    rows[Math.max(0, Math.min(rows.length - 1, i < 0 ? 0 : i + (down ? 1 : -1)))].focus();
  });
  $("#role").addEventListener("change", async (e) => {
    S.role = e.target.value; store.set("role", S.role);
    await load(); location.hash = `#/${ROLE_HOME[S.role]}`; route();
  });
  window.addEventListener("hashchange", route);

  $("#role").value = S.role;
  $("#view").innerHTML = `<div class="skeleton"></div>`;
  load().then(route).catch(fail);
})();
