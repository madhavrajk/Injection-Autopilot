"""Deterministic rules from docs/spec.md sections 1 to 4. Pure functions: no I/O, no model, no clock.

The only "today" is `ds.as_of`. Every function returns Check objects, pass or not.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

from .contracts import (
    BOOKING_HORIZON_DAYS, CARC, DRUGS, EM_CODES, EYE_MODIFIER, INJECTION_CPT, LAPSE_DAYS, ON_TIME_TOLERANCE_DAYS,
    PA_EXPIRY_NOTE_DAYS, WASTE_MODIFIERS, ActionKind, Appointment, Check, Claim, ClaimLine, ClinicNote, Coverage,
    Dataset, Denial, Drug, Evidence, ExtractedPlan, Eye, Injection, Outcome, PayerPolicy, PlannedInjection,
    PriorAuth, ProposedAction, Role, TreatmentPlan, all_drug_codes, dx_eyes, expected_drug_line, fmt_date, next_due,
)

DEFAULT_MIN_INTERVAL_DAYS = 28
NDC_PATTERN = re.compile(r"\d{11}|\d{5}-\d{4}-\d{2}")


# ---------------------------------------------------------------- small helpers

def ev(ref: str, field: str, value) -> Evidence:
    return Evidence(ref=ref, field=field, value=str(value))


def make(rule_id, label, outcome, detail, eye=None, evidence=None, action=None) -> Check:
    return Check(rule_id=rule_id, label=label, outcome=outcome, detail=detail, eye=eye,
                 evidence=evidence or [], action=action)


def act(kind, owner, summary, **kw) -> ProposedAction:
    return ProposedAction(kind=kind, owner=owner, summary=summary, **kw)


def brand(drug: Drug) -> str:
    return DRUGS[drug].brand


def min_interval(policy: Optional[PayerPolicy]) -> int:
    return policy.min_interval_days if policy else DEFAULT_MIN_INTERVAL_DAYS


def policy_for(ds: Dataset, patient_id: str, d: date) -> tuple[Optional[Coverage], Optional[PayerPolicy]]:
    cov = ds.coverage_on(patient_id, d)
    return cov, (ds.policy(cov.payer_id) if cov else None)


def auth_ev(a: PriorAuth, *fields: str) -> list[Evidence]:
    return [ev(f"ClaimResponse/{a.id}", f, getattr(a, f)) for f in fields]


def window_contains(a: PriorAuth, d: date) -> bool:
    return a.valid_from is not None and a.valid_to is not None and a.valid_from <= d <= a.valid_to


def matching_auth(ds: Dataset, patient_id: str, payer_id: str, drug: Drug, eye: Eye, dos: date) -> list[PriorAuth]:
    """Approved auths for this payer, drug and eye whose window contains dos (injection count not considered)."""
    return [a for a in ds.auths_for(patient_id) if a.payer_id == payer_id and a.status == "approved"
            and a.drug == drug and eye in a.eyes and window_contains(a, dos)]


# ---------------------------------------------------------------- 1. cadence

def check_cadence(plan: TreatmentPlan, ds: Dataset) -> list[Check]:
    label, eye = "Injection cadence", plan.eye
    plan_ev = ev(f"MedicationRequest/{plan.id}", "interval_weeks", plan.interval_weeks)

    def done(rule, outcome, detail, evidence, action=None):
        return [make(rule, label, outcome, detail, eye, evidence, action)]

    injs = ds.injections_for(plan.patient_id, eye)
    if not injs:
        return done("CAD-00", Outcome.NOTE, f"No completed injection is on record for {eye.value}, so no due date can be "
                    f"worked out.", [ev(f"MedicationRequest/{plan.id}", "eye", eye.value)])
    last = injs[0]
    due = next_due(last.date, plan.interval_weeks)
    booked = ds.booked_injections(plan.patient_id, eye, ds.as_of)
    appt = booked[0] if booked else None
    _, policy = policy_for(ds, plan.patient_id, appt.date if appt else ds.as_of)
    gap = min_interval(policy)
    last_ev = ev(f"Procedure/{last.id}", "performedDateTime", last.date)
    base = f"{brand(plan.drug)} {eye.value}: last injection {fmt_date(last.date)}, every {plan.interval_weeks} weeks, due {fmt_date(due)}"
    evidence = [last_ev, plan_ev]

    if appt:
        evidence.append(ev(f"Appointment/{appt.id}", "start", appt.date))
        days = (appt.date - last.date).days
        if days < gap:
            proposed = last.date + timedelta(days=gap)
            return done("CAD-03", Outcome.ACTION, f"{base}; the visit on {fmt_date(appt.date)} is only {days} days after "
                        f"the last injection and the minimum is {gap}.", evidence,
                        act(ActionKind.RESCHEDULE, Role.SCHEDULER, f"Move {eye.value} visit to {fmt_date(proposed)} or later",
                            proposed_date=proposed))
        if appt.date > due + timedelta(days=ON_TIME_TOLERANCE_DAYS):
            proposed = max(due, ds.as_of)
            return done("CAD-04", Outcome.ACTION, f"{base}; the visit on {fmt_date(appt.date)} is more than "
                        f"{ON_TIME_TOLERANCE_DAYS} days late.", evidence,
                        act(ActionKind.RESCHEDULE, Role.SCHEDULER, f"Move {eye.value} visit up to {fmt_date(proposed)}",
                            proposed_date=proposed))
        return done("CAD-01", Outcome.PASS, f"{base}; booked on {fmt_date(appt.date)}.", evidence)

    if ds.as_of > due + timedelta(days=LAPSE_DAYS):
        return done("CAD-05", Outcome.ACTION, f"{base}; nothing is booked and it is more than {LAPSE_DAYS} days overdue "
                    f"as of {fmt_date(ds.as_of)}.", evidence,
                    act(ActionKind.OUTREACH, Role.SCHEDULER, f"Contact the patient: {eye.value} injection is overdue",
                        draft_kind="outreach_sms"))
    if due <= ds.as_of + timedelta(days=BOOKING_HORIZON_DAYS):
        proposed = max(due, ds.as_of)
        return done("CAD-02", Outcome.ACTION, f"{base}; nothing is booked.", evidence,
                    act(ActionKind.BOOK, Role.SCHEDULER, f"Book {eye.value} injection for {fmt_date(proposed)}",
                        proposed_date=proposed))
    return done("CAD-01", Outcome.PASS, f"{base}; not due within {BOOKING_HORIZON_DAYS} days of {fmt_date(ds.as_of)}.",
                evidence)


# ---------------------------------------------------------------- 2. clearance

def check_clearance(appt: Appointment, ds: Dataset) -> list[Check]:
    cov, policy = policy_for(ds, appt.patient_id, appt.date)
    out = [_eligibility(appt, cov, ds)]
    for line in appt.planned:
        out += _line_checks(appt, line, cov, policy, ds)
    return out


def _eligibility(appt: Appointment, cov: Optional[Coverage], ds: Dataset) -> Check:
    label = "Eligibility"
    if cov:
        evidence = [ev(f"Coverage/{cov.id}", "period.start", cov.period_start),
                    ev(f"Coverage/{cov.id}", "period.end", cov.period_end)]
        end = fmt_date(cov.period_end) if cov.period_end else "open ended"
        return make("CLR-01", label, Outcome.PASS, f"{cov.plan_name} coverage is active on {fmt_date(appt.date)} "
                    f"(from {fmt_date(cov.period_start)}, {end}).", evidence=evidence)
    latest = ds.latest_coverage(appt.patient_id)
    evidence = []
    if latest:
        evidence += [ev(f"Coverage/{latest.id}", "period.start", latest.period_start),
                     ev(f"Coverage/{latest.id}", "period.end", latest.period_end)]
        end = fmt_date(latest.period_end) if latest.period_end else "open ended"
        why = f"the latest coverage on file ({latest.plan_name}) runs {fmt_date(latest.period_start)} to {end}"
    else:
        why = "no coverage is on file"
    evidence.append(ev(f"Appointment/{appt.id}", "start", appt.date))
    return make("CLR-01", label, Outcome.ACTION, f"No active coverage on the visit date {fmt_date(appt.date)}: {why}.",
                evidence=evidence,
                action=act(ActionKind.VERIFY_ELIGIBILITY, Role.AUTH,
                           f"Verify eligibility before the {fmt_date(appt.date)} visit"))


def _line_checks(appt, line: PlannedInjection, cov, policy, ds) -> list[Check]:
    eye, drug, dos = line.eye, line.drug, appt.date
    plan = ds.plan_for(appt.patient_id, eye)
    out = [_order_match(appt, line, plan), _interval(appt, line, policy, ds)]
    if cov:
        out += _prior_auth(appt, line, cov, policy, ds)
        out += _step_therapy(appt, line, cov, policy, ds)
    if plan:
        out.append(_laterality(line, plan))
    return out


def _order_match(appt, line, plan) -> Check:
    label = "Matches the order"
    planned_ev = ev(f"Appointment/{appt.id}", "planned", f"{line.eye.value} {line.drug.value}")
    if plan is None:
        return make("CLR-02", label, Outcome.HOLD, f"The visit plans {brand(line.drug)} in {line.eye.value} but there is "
                    f"no treatment order for that eye.", line.eye, [planned_ev])
    plan_ev = ev(f"MedicationRequest/{plan.id}", "drug", plan.drug.value)
    if plan.drug != line.drug:
        return make("CLR-02", label, Outcome.HOLD, f"The visit plans {brand(line.drug)} in {line.eye.value} but the order "
                    f"says {brand(plan.drug)}.", line.eye, [planned_ev, plan_ev])
    return make("CLR-02", label, Outcome.PASS, f"The visit plans {brand(line.drug)} in {line.eye.value}, as ordered.",
                line.eye, [planned_ev, plan_ev])


def _interval(appt, line, policy, ds) -> Check:
    label, eye, dos = "Minimum interval", line.eye, appt.date
    gap = min_interval(policy)
    prior = ds.injections_for(appt.patient_id, eye, before=dos)
    if not prior:
        return make("CLR-03", label, Outcome.PASS, f"First injection on record in {eye.value}, so no interval applies.", eye)
    last = prior[0]
    days = (dos - last.date).days
    evidence = [ev(f"Procedure/{last.id}", "performedDateTime", last.date)]
    if days >= gap:
        return make("CLR-03", label, Outcome.PASS, f"Last {eye.value} injection was {fmt_date(last.date)}, {days} days "
                    f"before the visit on {fmt_date(dos)}; the minimum is {gap}.", eye, evidence)
    proposed = last.date + timedelta(days=gap)
    return make("CLR-03", label, Outcome.ACTION, f"Last {eye.value} injection was {fmt_date(last.date)}, only {days} days "
                f"before the visit on {fmt_date(dos)}; the minimum is {gap}.", eye, evidence,
                act(ActionKind.RESCHEDULE, Role.SCHEDULER, f"Move {eye.value} visit to {fmt_date(proposed)} or later",
                    proposed_date=proposed))


def _prior_auth(appt, line, cov, policy, ds) -> list[Check]:
    label, eye, drug, dos = "Prior authorization", line.eye, line.drug, appt.date
    visit = f"{brand(drug)} {eye.value}"
    if policy is None:
        return [make("CLR-04", label, Outcome.HOLD, f"No payer policy is on file for {cov.plan_name}, so the "
                     f"authorization rules for {visit} cannot be checked.", eye, [ev(f"Coverage/{cov.id}", "payer_id", cov.payer_id)])]
    if drug not in policy.pa_required:
        return [make("CLR-04", label, Outcome.PASS, f"{policy.name} (policy {policy.version}) requires no prior "
                     f"authorization for {brand(drug)}.", eye)]
    auths = [a for a in ds.auths_for(appt.patient_id) if a.payer_id == cov.payer_id]
    same = [a for a in auths if a.drug == drug and eye in a.eyes]
    approved = [a for a in same if a.status == "approved"]
    pol = f"{policy.name} (policy {policy.version})"
    request = lambda summary: act(ActionKind.REQUEST_PA, Role.AUTH, summary, draft_kind="pa_request")
    renew = lambda summary: act(ActionKind.RENEW_PA, Role.AUTH, summary, draft_kind="pa_request")

    covering = [a for a in same if a.covers(drug, eye, dos)]
    if covering:
        a = covering[0]
        out = [make("CLR-04", label, Outcome.PASS, f"Authorization {a.auth_number} for {visit} is valid "
                    f"{fmt_date(a.valid_from)} to {fmt_date(a.valid_to)}, covering the visit on {fmt_date(dos)}.",
                    eye, auth_ev(a, "auth_number", "valid_from", "valid_to"))]
        out += _runway(a, eye, drug, dos)
        return out
    pending = [a for a in same if a.status == "pending"]
    if pending:
        a = pending[0]
        if a.requested_on:
            decision = a.requested_on + timedelta(days=policy.pa_turnaround_days)
            when = "after" if decision > dos else "on or before"
            detail = (f"Authorization {a.auth_number} for {visit} is still pending: requested {fmt_date(a.requested_on)}, "
                      f"decision expected {fmt_date(decision)} ({pol}), which is {when} the visit on {fmt_date(dos)}.")
        else:
            detail = (f"Authorization {a.auth_number} for {visit} is still pending and no request date is recorded, "
                      f"so a decision date cannot be estimated for the visit on {fmt_date(dos)}.")
        return [make("CLR-08", label, Outcome.ACTION, detail, eye, auth_ev(a, "auth_number", "status", "requested_on"),
                     act(ActionKind.EXPEDITE_PA, Role.AUTH, f"Expedite authorization {a.auth_number} for {visit}"))]
    outside = [a for a in approved if not window_contains(a, dos)]
    if outside:
        a = outside[0]
        window = (f"{fmt_date(a.valid_from)} to {fmt_date(a.valid_to)}" if a.valid_from and a.valid_to
                  else "with no complete valid window")
        ended = a.valid_to is not None and a.valid_to < dos
        detail = (f"Authorization {a.auth_number} for {visit} ended {fmt_date(a.valid_to)}; the visit is {fmt_date(dos)}."
                  if ended else f"Authorization {a.auth_number} for {visit} is valid {window}; the visit is {fmt_date(dos)}.")
        return [make("CLR-06", label, Outcome.ACTION, detail, eye, auth_ev(a, "auth_number", "valid_from", "valid_to"),
                     renew(f"Renew authorization for {visit}"))]
    used_up = [a for a in approved if a.injections_approved is not None and a.injections_used >= a.injections_approved]
    if used_up:
        a = used_up[0]
        return [make("CLR-07", label, Outcome.ACTION, f"Authorization {a.auth_number} for {visit} has used "
                     f"{a.injections_used} of {a.injections_approved} approved injections.", eye,
                     auth_ev(a, "auth_number", "injections_approved", "injections_used"),
                     renew(f"Renew authorization for {visit}"))]
    other = [a for a in auths if a.status == "approved"]
    if other:
        a = other[0]
        eyes = "/".join(e.value for e in a.eyes)
        return [make("CLR-05", label, Outcome.ACTION, f"The approved authorization {a.auth_number} covers "
                     f"{brand(a.drug)} for {eyes}, but the visit is {visit}.", eye,
                     auth_ev(a, "auth_number", "drug", "eyes"), request(f"Request authorization for {visit}"))]
    return [make("CLR-04", label, Outcome.ACTION, f"{pol} requires prior authorization for {brand(drug)} and none is on "
                 f"file for {eye.value}.", eye, [ev(f"Appointment/{appt.id}", "planned", f"{eye.value} {drug.value}")],
                 request(f"Request authorization for {visit}"))]


def _runway(a: PriorAuth, eye, drug, dos) -> list[Check]:
    days = (a.valid_to - dos).days
    last_one = a.injections_approved is not None and a.injections_used + 1 >= a.injections_approved
    if days > PA_EXPIRY_NOTE_DAYS and not last_one:
        return []
    if last_one:
        detail = (f"Authorization {a.auth_number} has used {a.injections_used} of {a.injections_approved} injections; "
                  f"this visit uses the last one.")
    else:
        detail = f"Authorization {a.auth_number} ends {fmt_date(a.valid_to)}, {days} days after the visit on {fmt_date(dos)}."
    return [make("CLR-09", "Authorization runway", Outcome.NOTE, detail, eye,
                 auth_ev(a, "auth_number", "valid_to", "injections_used"),
                 act(ActionKind.RENEW_PA, Role.AUTH, f"Start renewal of authorization {a.auth_number} for {brand(drug)}"))]


def _step_therapy(appt, line, cov, policy, ds) -> list[Check]:
    if policy is None or policy.step_therapy is None or line.drug == policy.step_therapy.first_line:
        return []
    step, label, dos, eye = policy.step_therapy, "Step therapy", appt.date, line.eye
    first = brand(step.first_line)
    done = [i for i in ds.injections_for(appt.patient_id, None, before=dos) if i.drug == step.first_line]
    evidence = [ev(f"MedicationAdministration/{i.medadmin_id}", "effectiveDateTime", i.date) for i in done]
    exception = [a for a in ds.auths_for(appt.patient_id) if a.payer_id == cov.payer_id and a.status == "approved"
                 and a.drug == line.drug and a.step_therapy_exception]
    pol = f"{policy.name} (policy {policy.version})"
    if len(done) >= step.min_doses:
        return [make("CLR-10", label, Outcome.PASS, f"{len(done)} {first} injections are documented before "
                     f"{fmt_date(dos)}; {pol} needs {step.min_doses}.", eye, evidence)]
    if exception:
        a = exception[0]
        return [make("CLR-10", label, Outcome.PASS, f"Authorization {a.auth_number} carries a step therapy exception, "
                     f"so {pol} does not need {step.min_doses} {first} injections.", eye,
                     auth_ev(a, "auth_number", "step_therapy_exception"))]
    evidence = evidence or [ev(f"Coverage/{cov.id}", "payer_id", cov.payer_id)]
    return [make("CLR-10", label, Outcome.ACTION, f"Only {len(done)} of {step.min_doses} required {first} injections are "
                 f"documented before {fmt_date(dos)} ({pol}) and no exception is on file for {brand(line.drug)}.", eye,
                 evidence, act(ActionKind.STEP_EXCEPTION, Role.PHYSICIAN,
                               f"Request a step therapy exception for {brand(line.drug)} {eye.value}",
                               draft_kind="step_exception"))]


def _laterality(line, plan) -> Check:
    label, eye = "Diagnosis matches the eye", line.eye
    evidence = [ev(f"MedicationRequest/{plan.id}", "reasonCode", plan.diagnosis_code)]
    eyes = dx_eyes(plan.diagnosis_code)
    if eyes is None:
        return make("CLR-11", label, Outcome.HOLD, f"Diagnosis code {plan.diagnosis_code} on the {eye.value} order is not "
                    f"in the practice's code table.", eye, evidence)
    if eye not in eyes:
        return make("CLR-11", label, Outcome.HOLD, f"Diagnosis code {plan.diagnosis_code} on the {eye.value} order does "
                    f"not cover {eye.value}.", eye, evidence)
    return make("CLR-11", label, Outcome.PASS, f"Diagnosis code {plan.diagnosis_code} covers {eye.value}.", eye, evidence)


def reconcile_plan(plan: TreatmentPlan, extracted: list[ExtractedPlan], note: ClinicNote) -> Check:
    """CLR-12: the clinic note must not contradict the structured order."""
    label, eye = "Clinic note agrees with the order", plan.eye
    if note.date < plan.updated_on:
        return make("CLR-12", label, Outcome.PASS, f"The note of {fmt_date(note.date)} is older than the order updated "
                    f"{fmt_date(plan.updated_on)}, so the order stands.", eye,
                    [ev(f"DocumentReference/{note.id}", "date", note.date)])
    mine = [e for e in extracted if e.eye == eye]
    if not mine:
        return make("CLR-12", label, Outcome.PASS, f"The note of {fmt_date(note.date)} says nothing about {eye.value}.", eye,
                    [ev(f"DocumentReference/{note.id}", "date", note.date)])
    problems, evidence = [], []
    for e in mine:
        found = []
        if e.drug is not None and e.drug != plan.drug:
            found.append((f"the note says {brand(e.drug)} but the order is {brand(plan.drug)}", "drug", plan.drug.value))
        if e.interval_weeks is not None and e.interval_weeks != plan.interval_weeks:
            found.append((f"the note says every {e.interval_weeks} weeks but the order is every "
                          f"{plan.interval_weeks}", "interval_weeks", plan.interval_weeks))
        for text, field, value in found:
            problems.append(text)
            evidence += [ev(f"DocumentReference/{note.id}", "text", e.quote), ev(f"MedicationRequest/{plan.id}", field, value)]
    if problems:
        return make("CLR-12", label, Outcome.HOLD, f"For {eye.value}, " + " and ".join(problems) +
                    f" (note {fmt_date(note.date)}, order {fmt_date(plan.updated_on)}).", eye, evidence)
    return make("CLR-12", label, Outcome.PASS, f"The note of {fmt_date(note.date)} agrees with the {eye.value} order.", eye,
                [ev(f"DocumentReference/{note.id}", "text", mine[0].quote)])


# ---------------------------------------------------------------- 3. claim check

def check_claim(claim: Claim, ds: Dataset) -> list[Check]:
    injs = ds.injections_on(claim.patient_id, claim.dos)
    cov = ds.coverage_on(claim.patient_id, claim.dos)
    policy = ds.policy(cov.payer_id if cov else claim.payer_id)
    procs = [l for l in claim.lines if l.code == INJECTION_CPT]
    if procs and not injs:
        return [make("CLM-02", "Injection on record", Outcome.HOLD, f"The claim bills {INJECTION_CPT} on "
                     f"{fmt_date(claim.dos)} but no injection is recorded in the chart for that day.",
                     evidence=[ev(f"Claim/{claim.id}", "item.productOrService", INJECTION_CPT),
                               ev(f"Claim/{claim.id}", "billablePeriod.start", claim.dos)])]
    out: list[Check] = []
    used: set[int] = set()
    for inj in injs:
        out += _injection_checks(claim, inj, policy, ds, used)
    out += _modifier_25(claim, procs)
    return out


def _line_ev(claim: Claim, line: ClaimLine, field: str, value) -> Evidence:
    return ev(f"Claim/{claim.id}", f"item[{line.seq}].{field}", value)


def _pick(lines: list[ClaimLine], used: set[int], wanted=lambda l: True) -> Optional[ClaimLine]:
    return next((l for l in lines if l.seq not in used and wanted(l)), None)


def _fix(summary: str, changes: list[dict]) -> ProposedAction:
    return act(ActionKind.FIX_CLAIM, Role.BILLER, summary, changes=changes)


def _change(line: int, field: str, old, new) -> dict:
    return {"line": line, "field": field, "from": str(old), "to": str(new)}


def _injection_checks(claim, inj: Injection, policy, ds, used) -> list[Check]:
    eye, drug = inj.eye, inj.drug
    who = f"{brand(drug)} {eye.value}"
    proc_ev = ev(f"Procedure/{inj.id}", "bodySite", eye.value)
    ma_ev = ev(f"MedicationAdministration/{inj.medadmin_id}", "dosage.site", inj.admin_eye.value)
    if inj.eye != inj.admin_eye:
        return [make("CLM-01", "Chart sources agree", Outcome.HOLD, f"The procedure record says {eye.value} but the "
                     f"medication record says {inj.admin_eye.value} for the {brand(drug)} injection on "
                     f"{fmt_date(inj.date)}.", eye, [proc_ev, ma_ev])]
    out = [make("CLM-01", "Chart sources agree", Outcome.PASS, f"Procedure and medication records both say {eye.value} "
               f"for the {brand(drug)} injection.", eye, [proc_ev, ma_ev])]
    proc_lines = [l for l in claim.lines if l.code == INJECTION_CPT]
    proc = _match_procedure(claim, inj, proc_lines, used, out)
    drug_line = _match_drug(claim, inj, policy, ds, used, out)
    if proc:
        out.append(_dx_check(claim, inj, proc, ds))
    if policy and drug in policy.pa_required:
        out.append(_auth_check(claim, inj, policy, ds, drug_line or proc))
    return out


def _match_procedure(claim, inj, proc_lines, used, out) -> Optional[ClaimLine]:
    label, eye = "Eye modifier", inj.eye
    mod = EYE_MODIFIER[eye]
    line = _pick(proc_lines, used, lambda l: mod in l.modifiers)
    if line:
        used.add(line.seq)
        out.append(make("CLM-03", label, Outcome.PASS, f"The {INJECTION_CPT} line {line.seq} carries {mod} for {eye.value}.",
                        eye, [_line_ev(claim, line, "modifier", line.modifiers)]))
        return line
    line = _pick(proc_lines, used)
    if line is None:
        out.append(make("CLM-03", label, Outcome.ACTION, f"The claim has no injection line for {eye.value} "
                        f"({brand(inj.drug)} on {fmt_date(inj.date)}).", eye,
                        [ev(f"Claim/{claim.id}", "item.productOrService", INJECTION_CPT)],
                        _fix(f"Add the {INJECTION_CPT} line for {eye.value}", [])))
        return None
    used.add(line.seq)
    wrong = next((m for m in line.modifiers if m in EYE_MODIFIER.values()), "")
    out.append(make("CLM-03", label, Outcome.ACTION, f"Line {line.seq} ({INJECTION_CPT}) carries "
                    f"{wrong or 'no eye modifier'} but the injection was in {eye.value}, which needs {mod}.", eye,
                    [_line_ev(claim, line, "modifier", line.modifiers)],
                    _fix(f"Change line {line.seq} eye modifier to {mod}", [_change(line.seq, "modifier", wrong, mod)])))
    return line


def _match_drug(claim, inj, policy, ds, used, out) -> Optional[ClaimLine]:
    eye, drug = inj.eye, inj.drug
    code, units = expected_drug_line(drug, policy)
    label = "Drug code"
    if code is None:
        out.append(make("CLM-04", label, Outcome.HOLD, f"No payer policy is on file, so the billing code for "
                        f"{brand(drug)} cannot be checked.", eye, [ev(f"Claim/{claim.id}", "payer_id", claim.payer_id)]))
        return None
    lines = claim.lines
    mod = EYE_MODIFIER[eye]
    line = (_pick(lines, used, lambda l: l.code == code and mod in l.modifiers) or _pick(lines, used, lambda l: l.code == code))
    changes = None
    if line is None:
        line = _pick(lines, used, lambda l: l.code in all_drug_codes(ds.policies))
        if line is None:
            out.append(make("CLM-04", label, Outcome.ACTION, f"The claim has no drug line for {brand(drug)} {eye.value}; "
                            f"expected {code}.", eye, [_line_ev(claim, l, "code", l.code) for l in lines],
                            _fix(f"Add the {code} line for {brand(drug)} {eye.value}", [])))
            return None
        changes = [_change(line.seq, "code", line.code, code)]
        out.append(make("CLM-04", label, Outcome.ACTION, f"Line {line.seq} bills {line.code} but {brand(drug)} is billed "
                        f"as {code}.", eye, [_line_ev(claim, line, "code", line.code)],
                        _fix(f"Change line {line.seq} code to {code}", changes)))
    else:
        out.append(make("CLM-04", label, Outcome.PASS, f"Line {line.seq} bills {code} for {brand(drug)}.", eye,
                        [_line_ev(claim, line, "code", line.code)]))
    used.add(line.seq)
    out += _units_waste_ndc(claim, inj, line, units)
    return line


def _units_waste_ndc(claim, inj, line, units) -> list[Check]:
    eye, drug = inj.eye, inj.drug
    out = []
    if units is not None:
        ok = line.units == units
        out.append(make("CLM-05", "Drug units", Outcome.PASS if ok else Outcome.ACTION,
                        f"Line {line.seq} bills {line.units} units of {brand(drug)}; one dose is {units}.", eye,
                        [_line_ev(claim, line, "quantity", line.units)],
                        None if ok else _fix(f"Change line {line.seq} units to {units}",
                                             [_change(line.seq, "units", line.units, units)])))
    waste = [m for m in line.modifiers if m in WASTE_MODIFIERS]
    ok = len(waste) == 1
    out.append(make("CLM-06", "Waste modifier", Outcome.PASS if ok else Outcome.ACTION,
                    f"Line {line.seq} carries {', '.join(waste)} (exactly one of JZ or JW is required)." if ok else
                    f"Line {line.seq} carries {', '.join(waste) or 'neither JZ nor JW'}; exactly one is required.", eye,
                    [_line_ev(claim, line, "modifier", line.modifiers)],
                    None if ok else _fix(f"Keep exactly one waste modifier on line {line.seq}: JZ if nothing was discarded, JW if drug was wasted",
                                         [_change(line.seq, "modifier", ",".join(waste), "JZ")])))
    ok = bool(line.ndc and NDC_PATTERN.fullmatch(line.ndc))
    fix = None
    if not ok:
        changes = [_change(line.seq, "ndc", line.ndc or "", inj.ndc)] if inj.ndc else []
        fix = _fix(f"Correct the NDC on line {line.seq}", changes)
    out.append(make("CLM-07", "NDC", Outcome.PASS if ok else Outcome.ACTION,
                    f"Line {line.seq} NDC {line.ndc} is in a valid format." if ok else
                    f"Line {line.seq} NDC {line.ndc or 'is missing'} is not an 11-digit NDC.", eye,
                    [_line_ev(claim, line, "ndc", line.ndc)], fix))
    return out


def _dx_check(claim, inj, proc: ClaimLine, ds) -> Check:
    eye = inj.eye
    evidence = [_line_ev(claim, proc, "diagnosisSequence", proc.dx_codes)]
    if any(eye in (dx_eyes(c) or ()) for c in proc.dx_codes):
        return make("CLM-08", "Diagnosis matches the eye", Outcome.PASS,
                    f"Line {proc.seq} has a diagnosis code that covers {eye.value}.", eye, evidence)
    plan = ds.plan_for(claim.patient_id, eye)
    changes = []
    if plan and eye in (dx_eyes(plan.diagnosis_code) or ()):
        changes = [_change(proc.seq, "dx_codes", ",".join(proc.dx_codes), plan.diagnosis_code)]
    return make("CLM-08", "Diagnosis matches the eye", Outcome.ACTION,
                f"Line {proc.seq} lists {', '.join(proc.dx_codes) or 'no diagnosis'}, none of which covers {eye.value}.",
                eye, evidence, _fix(f"Correct the diagnosis on line {proc.seq} for {eye.value}", changes))


def _auth_check(claim, inj, policy, ds, line: Optional[ClaimLine]) -> Check:
    label, eye, drug = "Authorization number", inj.eye, inj.drug
    auths = matching_auth(ds, claim.patient_id, policy.payer_id, drug, eye, claim.dos)
    claim_ev = ev(f"Claim/{claim.id}", "insurance.preAuthRef", claim.auth_number)
    if not auths:
        return make("CLM-09", label, Outcome.HOLD, f"{policy.name} (policy {policy.version}) requires authorization for "
                    f"{brand(drug)}, but no approved authorization for {eye.value} covers {fmt_date(claim.dos)}.", eye,
                    [claim_ev])
    if any(a.auth_number == claim.auth_number for a in auths):
        return make("CLM-09", label, Outcome.PASS, f"The claim carries authorization {claim.auth_number} for "
                    f"{brand(drug)} {eye.value}.", eye, [claim_ev] + auth_ev(auths[0], "auth_number"))
    right = auths[0]
    return make("CLM-09", label, Outcome.ACTION, f"The claim carries authorization {claim.auth_number or 'none'} but the "
                f"approved one for {brand(drug)} {eye.value} on {fmt_date(claim.dos)} is {right.auth_number}.", eye,
                [claim_ev] + auth_ev(right, "auth_number"),
                _fix(f"Put authorization {right.auth_number} on the claim",
                     [_change(0, "auth_number", claim.auth_number or "", right.auth_number)]))


def _modifier_25(claim, procs) -> list[Check]:
    ems = [l for l in claim.lines if l.code in EM_CODES]
    if not ems or not procs:
        return []
    missing = [l for l in ems if "25" not in l.modifiers]
    evidence = [_line_ev(claim, l, "modifier", l.modifiers) for l in ems]
    if not missing:
        return [make("CLM-10", "Modifier 25", Outcome.PASS, f"Visit line(s) {', '.join(str(l.seq) for l in ems)} carry "
                     f"modifier 25 alongside {INJECTION_CPT}.", evidence=evidence)]
    changes = [_change(l.seq, "modifier", "", "25") for l in missing]
    return [make("CLM-10", "Modifier 25", Outcome.ACTION, f"Visit line(s) {', '.join(str(l.seq) for l in missing)} "
                 f"({', '.join(l.code for l in missing)}) lack modifier 25 but the claim also bills {INJECTION_CPT}.",
                 evidence=evidence, action=_fix("Add modifier 25 to the visit line", changes))]


# ---------------------------------------------------------------- 4. denial triage

def triage_denial(denial: Denial, ds: Dataset) -> list[Check]:
    label = "Denial root cause"
    head = f"{denial.group}-{denial.carc} ({CARC.get(denial.carc, 'unrecognized code')}): "
    carc_ev = ev(f"ClaimResponse/{denial.id}", "reason", denial.carc)

    def done(rule, outcome, text, evidence=None, action=None):
        return [make(rule, label, outcome, head + text, evidence=[carc_ev] + (evidence or []), action=action)]

    claim = ds.claim(denial.claim_id)
    if claim is None:
        return done("DEN-00", Outcome.HOLD, f"claim {denial.claim_id} is not in the records.",
                    [ev(f"ClaimResponse/{denial.id}", "request", f"Claim/{denial.claim_id}")])
    cov = ds.coverage_on(claim.patient_id, claim.dos)
    policy = ds.policy(cov.payer_id if cov else claim.payer_id)
    claim_ev = ev(f"Claim/{claim.id}", "billablePeriod.start", claim.dos)
    appeal = lambda s: act(ActionKind.APPEAL, Role.BILLER, s, draft_kind="appeal")
    code = denial.carc

    if code == "197":
        return _triage_197(denial, claim, ds, done, appeal)
    if code in ("4", "11", "16"):
        found = check_claim(claim, ds)
        actions = [c for c in found if c.outcome == Outcome.ACTION]
        holds = [c for c in found if c.outcome == Outcome.HOLD]
        evidence = [e for c in actions + holds for e in c.evidence]
        if actions and not holds:
            changes = []
            for c in actions:
                changes += [row for row in c.action.changes if row not in changes]
            names = ", ".join(f"{c.rule_id} {c.label}" for c in actions)
            return done("DEN-05", Outcome.ACTION, f"the claim check found {len(actions)} problem(s) a corrected claim "
                        f"fixes: {names}.", evidence, _fix("Send a corrected claim", changes))
        why = ", ".join(f"{c.rule_id} {c.label}" for c in holds) if holds else "the claim check found nothing wrong"
        return done("DEN-06", Outcome.HOLD, f"a person must review it ({why}).", evidence or [claim_ev])
    if code == "151":
        return _triage_151(denial, claim, policy, ds, done, appeal)
    if code == "50":
        return done("DEN-09", Outcome.ACTION, "the payer denied medical necessity; an appeal with the chart is the next step.",
                    [claim_ev], appeal("Appeal the medical necessity denial"))
    if code == "27":
        if cov and cov.payer_id == claim.payer_id:
            return done("DEN-10", Outcome.ACTION, f"{cov.plan_name} coverage was active on {fmt_date(claim.dos)}, so the "
                        f"termination looks wrong; appeal.", [ev(f"Coverage/{cov.id}", "period.start", cov.period_start),
                        ev(f"Coverage/{cov.id}", "period.end", cov.period_end)], appeal("Appeal: coverage was active"))
        latest = ds.latest_coverage(claim.patient_id)
        evidence = [claim_ev] + ([ev(f"Coverage/{latest.id}", "period.end", latest.period_end)] if latest else [])
        return done("DEN-11", Outcome.ACTION, f"no coverage for this payer was active on {fmt_date(claim.dos)}; verify "
                    f"eligibility before anything else.", evidence,
                    act(ActionKind.VERIFY_ELIGIBILITY, Role.AUTH, "Verify the patient's coverage on the date of service"))
    return done("DEN-12", Outcome.HOLD, "this denial code has no automatic rule; a person must review it.", [claim_ev])


def _triage_197(denial, claim, ds, done, appeal):
    found = []
    for inj in ds.injections_on(claim.patient_id, claim.dos):
        found += [(inj, a) for a in matching_auth(ds, claim.patient_id, claim.payer_id, inj.drug, inj.eye, claim.dos)]
    if not found:
        return done("DEN-04", Outcome.HOLD, f"no approved authorization matches an injection on {fmt_date(claim.dos)}.",
                    [ev(f"Claim/{claim.id}", "insurance.preAuthRef", claim.auth_number)])
    inj, auth = next(((i, a) for i, a in found if a.auth_number == claim.auth_number), found[0])
    what = f"{brand(inj.drug)} {inj.eye.value}"
    evidence = [ev(f"Claim/{claim.id}", "insurance.preAuthRef", claim.auth_number)] + auth_ev(auth, "auth_number", "valid_to")
    if auth.auth_number == claim.auth_number:
        return done("DEN-03", Outcome.ACTION, f"the claim already carries authorization {auth.auth_number} for {what}, "
                    f"which is valid on {fmt_date(claim.dos)}; appeal.", evidence, appeal("Appeal: authorization was on the claim"))
    return done("DEN-02", Outcome.ACTION, f"the claim carries {claim.auth_number or 'no authorization number'} but "
                f"authorization {auth.auth_number} covers {what} on {fmt_date(claim.dos)}.", evidence,
                _fix("Send a corrected claim with the authorization number",
                     [_change(0, "auth_number", claim.auth_number or "", auth.auth_number)]))


def _triage_151(denial, claim, policy, ds, done, appeal):
    gap = min_interval(policy)
    pairs = []
    for inj in ds.injections_on(claim.patient_id, claim.dos):
        prior = ds.injections_for(claim.patient_id, inj.eye, before=claim.dos)
        if prior:
            pairs.append((inj, prior[0], (claim.dos - prior[0].date).days))
    if not pairs:
        injs = ds.injections_on(claim.patient_id, claim.dos)
        claim_ev = ev(f"Claim/{claim.id}", "billablePeriod.start", claim.dos)
        if not injs:
            return done("DEN-12", Outcome.HOLD, f"no injection is on record for {fmt_date(claim.dos)}; a person must "
                        f"review it.", [claim_ev])
        return done("DEN-08", Outcome.ACTION, f"the {injs[0].eye.value} injection on {fmt_date(claim.dos)} is the first on "
                    f"record in that eye, so the interval is not a reason to deny.", [claim_ev],
                    appeal("Appeal: no earlier injection in that eye"))
    inj, prev, days = min(pairs, key=lambda p: p[2])
    evidence = [ev(f"Procedure/{inj.id}", "performedDateTime", inj.date), ev(f"Procedure/{prev.id}", "performedDateTime", prev.date)]
    text = (f"{inj.eye.value} injections on {fmt_date(prev.date)} and {fmt_date(claim.dos)} are {days} days apart; the "
            f"minimum is {gap}.")
    if days < gap:
        return done("DEN-07", Outcome.HOLD, f"the denial looks correct: {text}", evidence)
    return done("DEN-08", Outcome.ACTION, f"the interval is allowed: {text}", evidence,
                appeal("Appeal: the interval met the payer minimum"))

