"""The orchestrator. For each subject it runs the rules (code), reads the clinic note and drafts letters (model,
stub by default), records a plain-English trace, and stops at a person. It never applies a fix and never sends
anything: every proposed action waits for approve / edit / reject."""
from __future__ import annotations

import time
from datetime import timedelta
from typing import Optional

from app import llm, rules
from app.contracts import (CARC, CLEARANCE_WINDOW_DAYS, DRUGS, DX_CODES, INJECTION_FEE_USD, Appointment, Check, Claim,
                           Dataset, Denial, Draft, Eye, ModelCall, Outcome, Role, Stage, Status, TraceStep,
                           TreatmentPlan, WorkItem, fmt_date, next_due, status_of)

EYE_WORDS = {Eye.OD: "right eye", Eye.OS: "left eye"}
HOLD_OWNER = {Stage.CADENCE: Role.PHYSICIAN, Stage.CLEARANCE: Role.PHYSICIAN,
              Stage.CLAIM: Role.BILLER, Stage.DENIAL: Role.BILLER}
RANK = {Outcome.HOLD: 0, Outcome.ACTION: 1, Outcome.NOTE: 2, Outcome.PASS: 3}

# Short names for the Today view ("where the practice leaks"). The rule itself is in docs/spec.md.
RULE_NAMES = {
    "CAD-02": "Due, nothing booked", "CAD-03": "Booked too early to be paid", "CAD-04": "Booked later than due",
    "CAD-05": "Lapsed, nothing booked", "CLR-01": "No active coverage on the visit date",
    "CLR-02": "Visit does not match the order", "CLR-03": "Too soon after the last injection",
    "CLR-04": "No authorization on file", "CLR-05": "Authorization is for another eye or drug",
    "CLR-06": "Authorization expired", "CLR-07": "Authorization used up", "CLR-08": "Authorization still pending",
    "CLR-10": "Step therapy not met", "CLR-11": "Diagnosis does not fit the eye", "CLR-12": "Note and order disagree",
    "CLM-01": "Chart records disagree on the eye", "CLM-02": "Billed injection with no record",
    "CLM-03": "Wrong eye modifier", "CLM-04": "Wrong drug code", "CLM-05": "Wrong drug units",
    "CLM-06": "Waste modifier missing or doubled", "CLM-07": "NDC missing or malformed", "CLM-08": "Diagnosis does not fit the eye",
    "CLM-09": "Authorization number wrong or missing", "CLM-10": "Modifier 25 missing",
}

CALLS: list[ModelCall] = []          # every model call made while building items (cost and latency telemetry)


# ---------------------------------------------------------------- shared pieces

class _Trace:
    def __init__(self) -> None:
        self.steps: list[TraceStep] = []

    def add(self, kind: str, name: str, story: str, call: Optional[ModelCall] = None, ms: float = 0.0) -> None:
        self.steps.append(TraceStep(
            n=len(self.steps) + 1, kind=kind, name=name, story=story,
            ms=round(call.ms if call else ms, 2), model=call.model if call else None,
            tokens=(call.tokens_in + call.tokens_out) if call else 0, cost_usd=call.cost_usd if call else 0.0))

    def add_checks(self, checks: list[Check]) -> None:
        for c in checks:
            eye = f" ({c.eye.value})" if c.eye else ""
            self.add("code", f"{c.label}{eye}", f"{_word(c.outcome)}: {c.detail}")


def _word(o: Outcome) -> str:
    return {Outcome.PASS: "Passed", Outcome.NOTE: "Note", Outcome.ACTION: "Needs action", Outcome.HOLD: "On hold"}[o]


def _worst(checks: list[Check]) -> Optional[Check]:
    bad = [c for c in checks if c.outcome in (Outcome.HOLD, Outcome.ACTION)]
    return min(bad, key=lambda c: RANK[c.outcome]) if bad else None


def _base_facts(ds: Dataset, patient_id: str, on) -> dict[str, str]:
    p = ds.patient(patient_id)
    cov = ds.coverage_on(patient_id, on) or ds.latest_coverage(patient_id)
    pol = ds.policy(cov.payer_id) if cov else None
    f = {"practice": ds.practice}
    if p:
        f.update(patient_name=p.name, birth_date=fmt_date(p.birth_date), patient_token=p.token)
    if cov:
        f.update(member_id=cov.member_id, plan_name=cov.plan_name)
    if pol:
        f.update(payer_name=pol.name)
    return f


def _drug_facts(drug, eye: Optional[Eye]) -> dict[str, str]:
    info = DRUGS[drug]
    f = {"drug_brand": info.brand, "drug_generic": info.generic}
    if info.hcpcs:
        f["hcpcs"] = info.hcpcs
    if eye:
        f.update(eye=eye.value, eye_words=EYE_WORDS[eye])
    return f


def _plan_facts(plan: Optional[TreatmentPlan]) -> dict[str, str]:
    if not plan:
        return {}
    f = {"physician": plan.physician, "diagnosis_code": plan.diagnosis_code,
         "interval_weeks": str(plan.interval_weeks)}
    if plan.diagnosis_code in DX_CODES:
        f["diagnosis_label"] = DX_CODES[plan.diagnosis_code][0]
    return f


def _finish(item: WorkItem, checks: list[Check], trace: _Trace, draft_facts: dict[str, str]) -> WorkItem:
    """Status, headline, owner, draft and the closing trace step: the same for every stage."""
    item.checks = sorted(checks, key=lambda c: RANK[c.outcome])
    item.status = status_of(checks)
    worst = _worst(checks)
    if worst is None:
        item.headline = "All checks passed"
        item.dollars_at_risk = 0.0
        trace.add("code", "Result", f"Clear: {len(checks)} checks passed. Nothing for a person to do.")
    else:
        more = sum(1 for c in checks if c.outcome in (Outcome.HOLD, Outcome.ACTION)) - 1
        item.headline = worst.detail
        item.facts["more_findings"] = str(more)
        item.owner = worst.action.owner if worst.action else HOLD_OWNER[item.stage]
        drafting = next((c for c in item.checks if c.action and c.action.draft_kind), None)
        if drafting and item.status == Status.ACTION:
            facts = {**draft_facts, "finding": drafting.detail}
            if drafting.action.proposed_date:
                facts["proposed_date"] = fmt_date(drafting.action.proposed_date)
            item.draft = llm.draft(drafting.action.draft_kind, facts)
            if item.draft.call:
                CALLS.append(item.draft.call)
            ok = "every fact was filled in by code from the record" if item.draft.verified else \
                "the draft failed its fact check and is not ready to send: " + "; ".join(item.draft.problems)
            trace.add("model", "Draft", f"Drafted a {drafting.action.draft_kind.replace('_', ' ')}; {ok}.",
                      call=item.draft.call)
        if item.status == Status.HOLD:
            trace.add("person", "Stopped", f"Sources disagree or data is missing, so the agent proposes no fix. "
                                           f"Waiting for the {item.owner.value.replace('_', ' ')}.")
        else:
            trace.add("person", "Waiting", f"Proposed: {worst.action.summary} "
                                           f"Waiting for the {item.owner.value.replace('_', ' ')} to approve, edit or reject.")
    item.trace = trace.steps
    return item


def _timed(fn, *args):
    t = time.perf_counter()
    out = fn(*args)
    return out, (time.perf_counter() - t) * 1000


# ---------------------------------------------------------------- one item per stage

def cadence_item(plan: TreatmentPlan, ds: Dataset) -> WorkItem:
    p = ds.patient(plan.patient_id)
    trace = _Trace()
    checks, ms = _timed(rules.check_cadence, plan, ds)
    last = next(iter(ds.injections_for(plan.patient_id, plan.eye)), None)
    booked = next(iter(ds.booked_injections(plan.patient_id, plan.eye, ds.as_of)), None)
    due = next_due(last.date, plan.interval_weeks) if last else None
    cov = ds.coverage_on(plan.patient_id, booked.date if booked else ds.as_of)
    pol = ds.policy(cov.payer_id) if cov else None
    min_iv = pol.min_interval_days if pol else 28
    trace.add("code", "Read the record",
              f"Order {plan.id}: {DRUGS[plan.drug].brand} {plan.eye.value} every {plan.interval_weeks} weeks. "
              + (f"Last injection {fmt_date(last.date)}, so due {fmt_date(due)}. " if last else "No injection on record. ")
              + (f"Booked {fmt_date(booked.date)}." if booked else "Nothing booked."), ms=ms)
    trace.add_checks(checks)
    facts = {"Drug": DRUGS[plan.drug].brand, "Eye": plan.eye.value, "Interval": f"every {plan.interval_weeks} weeks",
             "Physician": plan.physician, "Diagnosis": plan.diagnosis_code}
    if last:
        facts.update(last=last.date.isoformat(), due=due.isoformat(),
                     earliest=(last.date + timedelta(days=min_iv)).isoformat())
    if booked:
        facts["booked"] = booked.date.isoformat()
    if pol:
        facts["Payer"] = pol.name
    worst = _worst(checks)
    if worst and worst.action and worst.action.proposed_date:
        facts["proposed"] = worst.action.proposed_date.isoformat()
    item = WorkItem(id=f"cadence:{plan.id}", stage=Stage.CADENCE, subject_ref=f"MedicationRequest/{plan.id}",
                    patient_id=plan.patient_id, patient_token=p.token if p else plan.patient_id,
                    title=f"{DRUGS[plan.drug].brand} · {plan.eye.value}", subtitle=f"every {plan.interval_weeks} weeks",
                    when=booked.date if booked else due, status=Status.CLEAR, headline="", facts=facts)
    df = {**_base_facts(ds, plan.patient_id, ds.as_of), **_drug_facts(plan.drug, plan.eye), **_plan_facts(plan)}
    if last:
        df["last_injection_date"] = fmt_date(last.date)
    return _finish(item, checks, trace, df)


def clearance_item(appt: Appointment, ds: Dataset) -> WorkItem:
    p = ds.patient(appt.patient_id)
    cov = ds.coverage_on(appt.patient_id, appt.date)
    pol = ds.policy(cov.payer_id) if cov else None
    trace = _Trace()
    checks, ms = _timed(rules.check_clearance, appt, ds)
    lines = ", ".join(f"{DRUGS[l.drug].brand} {l.eye.value}" for l in appt.planned)
    trace.add("code", "Read the record",
              f"Visit {fmt_date(appt.date)} {appt.time}: {lines}. "
              + (f"Coverage {cov.plan_name} under {pol.name} policy {pol.version}." if cov and pol else
                 "No active coverage found for that date."), ms=ms)
    trace.add_checks(checks)

    # The model reads the latest free-text note; code compares what it read with the structured order.
    note = ds.latest_note(appt.patient_id, on_or_before=ds.as_of)
    if note and p:
        extracted, call = llm.extract_plan(note, p, cov or ds.latest_coverage(appt.patient_id))
        CALLS.append(call)
        found = "; ".join(f"{e.eye.value}: {DRUGS[e.drug].brand if e.drug else 'no drug named'}"
                          f"{f', every {e.interval_weeks} weeks' if e.interval_weeks else ''}" for e in extracted)
        trace.add("model", "Read the clinic note",
                  f"Note {note.id} of {fmt_date(note.date)}, patient identifiers removed before the model saw it. "
                  + (f"Plan stated: {found}." if extracted else "No plan sentence found."), call=call)
        note_checks = [rules.reconcile_plan(plan, extracted, note) for l in appt.planned
                       if (plan := ds.plan_for(appt.patient_id, l.eye))]
        trace.add_checks(note_checks)
        checks = checks + note_checks

    first = appt.planned[0] if appt.planned else None
    facts = {"Visit": f"{fmt_date(appt.date)} · {appt.time}", "Provider": appt.provider, "Site": appt.location,
             "Payer": pol.name if pol else "No active coverage", "Plan": cov.plan_name if cov else "",
             "Policy version": pol.version if pol else ""}
    item = WorkItem(id=f"clearance:{appt.id}", stage=Stage.CLEARANCE, subject_ref=f"Appointment/{appt.id}",
                    patient_id=appt.patient_id, patient_token=p.token if p else appt.patient_id,
                    title=" + ".join(f"{DRUGS[l.drug].brand} · {l.eye.value}" for l in appt.planned) or "Injection",
                    subtitle=pol.name if pol else "No active coverage", when=appt.date, time=appt.time,
                    status=Status.CLEAR, headline="", facts=facts)
    item.dollars_at_risk = round(sum(DRUGS[l.drug].est_allowed_usd + INJECTION_FEE_USD for l in appt.planned), 2)
    df = {**_base_facts(ds, appt.patient_id, appt.date), "visit_date": fmt_date(appt.date)}
    worst = _worst(checks)
    line = next((l for l in appt.planned if worst and l.eye == worst.eye), first)
    if line:
        df.update(_drug_facts(line.drug, line.eye))
        df.update(_plan_facts(ds.plan_for(appt.patient_id, line.eye)))
        last = next(iter(ds.injections_for(appt.patient_id, line.eye, before=appt.date)), None)
        if last:
            df["last_injection_date"] = fmt_date(last.date)
    return _finish(item, checks, trace, df)


def claim_item(claim: Claim, ds: Dataset) -> WorkItem:
    p = ds.patient(claim.patient_id)
    pol = ds.policy(claim.payer_id)
    trace = _Trace()
    checks, ms = _timed(rules.check_claim, claim, ds)
    injs = ds.injections_on(claim.patient_id, claim.dos)
    chart = ", ".join(f"{DRUGS[i.drug].brand} {i.eye.value}" for i in injs) or "no injection record"
    trace.add("code", "Read the record",
              f"Draft claim {claim.id} for {fmt_date(claim.dos)}: {len(claim.lines)} lines, ${claim.total:,.2f}. "
              f"Chart for that date: {chart}.", ms=ms)
    trace.add_checks(checks)
    facts = {"Date of service": fmt_date(claim.dos), "Payer": pol.name if pol else claim.payer_id,
             "Charges": f"${claim.total:,.2f}", "Authorization on claim": claim.auth_number or "none",
             "Chart": chart}
    item = WorkItem(id=f"claim:{claim.id}", stage=Stage.CLAIM, subject_ref=f"Claim/{claim.id}",
                    patient_id=claim.patient_id, patient_token=p.token if p else claim.patient_id,
                    title=chart if injs else "Injection claim", subtitle=pol.name if pol else claim.payer_id,
                    when=claim.dos, status=Status.CLEAR, headline="", facts=facts)
    item.dollars_at_risk = claim.total
    item.facts["lines"] = "\n".join(
        f"{l.seq}|{l.code}|{' '.join(l.modifiers)}|{l.units}|{' '.join(l.dx_codes)}|{l.charge:.2f}|{l.ndc or ''}"
        for l in claim.lines)
    return _finish(item, checks, trace, {})


def denial_item(denial: Denial, ds: Dataset) -> WorkItem:
    claim = ds.claim(denial.claim_id)
    pid = claim.patient_id if claim else ""
    p = ds.patient(pid)
    pol = ds.policy(claim.payer_id) if claim else None
    trace = _Trace()
    checks, ms = _timed(rules.triage_denial, denial, ds)
    meaning = CARC.get(denial.carc, "Reason code outside the eight this version triages")
    deadline = denial.date + timedelta(days=pol.appeal_days if pol else 180)
    injs = ds.injections_on(pid, claim.dos) if claim else []
    chart = ", ".join(f"{DRUGS[i.drug].brand} {i.eye.value}" for i in injs) or "Injection claim"
    trace.add("code", "Read the remittance",
              f"Claim {denial.claim_id} denied {fmt_date(denial.date)} with {denial.group}-{denial.carc} ({meaning}), "
              f"${denial.amount:,.2f}. Appeal deadline {fmt_date(deadline)}.", ms=ms)
    trace.add_checks(checks)
    facts = {"Denied": fmt_date(denial.date), "Reason": f"{denial.group}-{denial.carc} · {meaning}",
             "Amount": f"${denial.amount:,.2f}", "Appeal by": fmt_date(deadline),
             "Days left": str((deadline - ds.as_of).days), "Claim": denial.claim_id,
             "Date of service": fmt_date(claim.dos) if claim else "", "Payer": pol.name if pol else ""}
    item = WorkItem(id=f"denial:{denial.id}", stage=Stage.DENIAL, subject_ref=f"ClaimResponse/{denial.id}",
                    patient_id=pid, patient_token=p.token if p else "Unknown patient",
                    title=chart if claim else "Claim not found", subtitle=pol.name if pol else "", when=deadline,
                    status=Status.CLEAR, headline="", facts=facts)
    item.dollars_at_risk = denial.amount
    df: dict[str, str] = {}
    if claim:
        df = {**_base_facts(ds, pid, claim.dos), "claim_id": claim.id, "visit_date": fmt_date(claim.dos),
              "denial_code": f"{denial.group}-{denial.carc}", "denial_reason": meaning,
              "denial_date": fmt_date(denial.date), "appeal_deadline": fmt_date(deadline),
              "amount": f"${denial.amount:,.2f}"}
        if claim.auth_number:
            df["auth_number"] = claim.auth_number
        if injs:
            df.update(_drug_facts(injs[0].drug, injs[0].eye))
            df.update(_plan_facts(ds.plan_for(pid, injs[0].eye)))
    item = _finish(item, checks, trace, df)
    # The list already shows the code and its meaning, so the headline keeps only what the agent concluded.
    _, sep, rest = item.headline.partition("): ")
    if sep and rest:
        item.headline = rest[0].upper() + rest[1:]
    return item


# ---------------------------------------------------------------- entry points

def clearance_appointments(ds: Dataset) -> list[Appointment]:
    end = ds.as_of + timedelta(days=CLEARANCE_WINDOW_DAYS)
    return [a for a in ds.appointments if a.kind == "injection" and a.status == "booked" and ds.as_of <= a.date <= end]


def build_items(ds: Dataset) -> list[WorkItem]:
    """Every work item, in stage order. Each item is isolated: one bad record never stops the rest."""
    CALLS.clear()
    jobs = ([(cadence_item, x) for x in ds.plans] + [(clearance_item, x) for x in clearance_appointments(ds)]
            + [(claim_item, x) for x in ds.claims if x.status == "draft"] + [(denial_item, x) for x in ds.denials])
    out: list[WorkItem] = []
    for fn, subject in jobs:
        try:
            out.append(fn(subject, ds))
        except Exception as exc:  # the failure ends with a person, not with a crash or a silent skip
            stage = {cadence_item: Stage.CADENCE, clearance_item: Stage.CLEARANCE,
                     claim_item: Stage.CLAIM, denial_item: Stage.DENIAL}[fn]
            pid = getattr(subject, "patient_id", "")
            p = ds.patient(pid)
            out.append(WorkItem(
                id=f"{stage.value}:{subject.id}", stage=stage, subject_ref=subject.id, patient_id=pid,
                patient_token=p.token if p else pid, title="Could not be checked", status=Status.HOLD,
                headline=f"The agent failed on this record ({type(exc).__name__}: {exc}). A person must check it.",
                owner=HOLD_OWNER[stage],
                trace=[TraceStep(n=1, kind="person", name="Stopped", story=f"{type(exc).__name__}: {exc}")]))
    return out


def evaluate(stage: str, subject_id: str, ds: Dataset) -> WorkItem:
    """Run one subject. Used by the evals, which hand in a small world and an expected answer."""
    if stage == "cadence":
        return cadence_item(next(x for x in ds.plans if x.id == subject_id), ds)
    if stage == "clearance":
        return clearance_item(next(x for x in ds.appointments if x.id == subject_id), ds)
    if stage == "claim":
        return claim_item(next(x for x in ds.claims if x.id == subject_id), ds)
    if stage == "denial":
        return denial_item(next(x for x in ds.denials if x.id == subject_id), ds)
    raise ValueError(f"unknown stage {stage}")


def flagged_rules(item: WorkItem) -> list[str]:
    return sorted({c.rule_id for c in item.checks if c.outcome in (Outcome.ACTION, Outcome.HOLD)})
