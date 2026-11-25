"""Rule tests: small inline datasets, one behaviour per test."""
from datetime import date, timedelta

from app import rules
from app.contracts import (
    ActionKind, Appointment, Claim, ClaimLine, ClinicNote, Coverage, Denial, Dataset, Drug, Eye, ExtractedPlan,
    Injection, Outcome, PayerPolicy, PlannedInjection, PriorAuth, Role, StepTherapy, TreatmentPlan,
)

OD, OS = Eye.OD, Eye.OS
H = Drug.AFLIBERCEPT_8MG        # Eylea HD
E = Drug.AFLIBERCEPT_2MG        # Eylea
V = Drug.FARICIMAB              # Vabysmo
B = Drug.BEVACIZUMAB
PID = "pt-1"
AS_OF = date(2026, 11, 18)
DOS = date(2026, 11, 20)


# ---------------------------------------------------------------- factories

def _make(cls, defaults, kw):
    return cls(**{**defaults, **kw})


def mk_policy(**kw):
    return _make(PayerPolicy, dict(payer_id="p1", name="Summit MA", kind="medicare_advantage", version="2026.2"), kw)


def mk_cov(**kw):
    return _make(Coverage, dict(id="cov-1", patient_id=PID, payer_id="p1", plan_name="Summit", member_id="M123",
                                period_start=date(2026, 1, 1)), kw)


def mk_auth(**kw):
    return _make(PriorAuth, dict(id="pa-1", patient_id=PID, payer_id="p1", auth_number="A-48213", drug=H, eyes=[OS],
                                 status="approved", valid_from=date(2026, 1, 1), valid_to=date(2027, 12, 31),
                                 injections_approved=10, injections_used=1), kw)


def mk_plan(**kw):
    return _make(TreatmentPlan, dict(id="plan-1", patient_id=PID, eye=OS, diagnosis_code="H35.3221", drug=H,
                                     interval_weeks=8, physician="Dr X", updated_on=date(2026, 6, 1)), kw)


def mk_inj(d, eye=OS, drug=H, **kw):
    tag = f"{d.isoformat()}-{eye.value}-{drug.value}"
    return _make(Injection, dict(id=f"proc-{tag}", patient_id=PID, date=d, eye=eye, drug=drug, dose_mg=2.0,
                                 admin_eye=eye, medadmin_id=f"ma-{tag}", ndc="12345-6789-01"), kw)


def mk_appt(**kw):
    return _make(Appointment, dict(id="appt-1", patient_id=PID, date=DOS, kind="injection", status="booked",
                                   planned=[PlannedInjection(eye=OS, drug=H)]), kw)


def mk_ds(pa=(), policy_kw=None, **kw):
    base = dict(as_of=AS_OF, coverages=[mk_cov()], policies=[mk_policy(pa_required=list(pa), **(policy_kw or {}))])
    return Dataset(**{**base, **kw})


def ids(checks):
    return [c.rule_id for c in checks]


def get(checks, rule_id, eye=None):
    found = [c for c in checks if c.rule_id == rule_id and (eye is None or c.eye == eye)]
    assert found, f"{rule_id} not in {ids(checks)}"
    return found[0]


# ---------------------------------------------------------------- 1. cadence

def cad(last, weeks=8, appt_date=None):
    appts = [mk_appt(date=appt_date)] if appt_date else []
    ds = mk_ds(plans=[mk_plan(interval_weeks=weeks)], injections=[mk_inj(last)] if last else [], appointments=appts)
    out = rules.check_cadence(ds.plans[0], ds)
    assert len(out) == 1 and out[0].eye == OS and out[0].label == "Injection cadence"
    return out[0]


def test_cad00_no_injection():
    c = cad(None)
    assert c.rule_id == "CAD-00" and c.outcome == Outcome.NOTE and c.action is None and c.evidence


def test_cad01_on_time_appointment():
    c = cad(date(2026, 10, 1), appt_date=date(2026, 11, 26))
    assert c.rule_id == "CAD-01" and c.outcome == Outcome.PASS
    assert "Nov 26, 2026" in c.detail


def test_cad01_not_due_yet_no_appointment():
    c = cad(date(2026, 10, 8))      # due Dec 3, one day beyond the booking horizon
    assert c.rule_id == "CAD-01" and c.outcome == Outcome.PASS


def test_cad03_too_soon_and_boundary():
    c = cad(date(2026, 10, 24), weeks=4, appt_date=DOS)       # 27 days
    assert c.rule_id == "CAD-03" and c.outcome == Outcome.ACTION
    assert c.action.kind == ActionKind.RESCHEDULE and c.action.owner == Role.SCHEDULER
    assert c.action.proposed_date == date(2026, 11, 21)
    assert c.evidence
    ok = cad(date(2026, 10, 23), weeks=4, appt_date=DOS)      # exactly 28 days
    assert ok.rule_id == "CAD-01" and ok.outcome == Outcome.PASS


def test_cad04_late_and_boundary():
    c = cad(date(2026, 9, 22), appt_date=date(2026, 11, 25))   # due Nov 17, due+8
    assert c.rule_id == "CAD-04" and c.outcome == Outcome.ACTION
    assert c.action.kind == ActionKind.RESCHEDULE and c.action.proposed_date == AS_OF
    assert cad(date(2026, 9, 22), appt_date=date(2026, 11, 24)).rule_id == "CAD-01"   # due+7


def test_cad05_lapsed_and_boundary():
    c = cad(date(2026, 9, 1))                                  # due Oct 27, 22 days overdue
    assert c.rule_id == "CAD-05" and c.outcome == Outcome.ACTION
    assert c.action.kind == ActionKind.OUTREACH and c.action.draft_kind == "outreach_sms"
    assert c.action.proposed_date is None and c.action.owner == Role.SCHEDULER
    b = cad(date(2026, 9, 9))                                  # due Nov 4: exactly 14 days overdue, not lapsed
    assert b.rule_id == "CAD-02"


def test_cad02_due_not_booked_and_boundary():
    c = cad(date(2026, 10, 7))                                 # due Dec 2 = as_of + 14
    assert c.rule_id == "CAD-02" and c.outcome == Outcome.ACTION
    assert c.action.kind == ActionKind.BOOK and c.action.proposed_date == date(2026, 12, 2)
    assert cad(date(2026, 10, 8)).rule_id == "CAD-01"


def test_cad02_overdue_proposes_today():
    c = cad(date(2026, 9, 15))                                 # due Nov 10, 8 days overdue
    assert c.rule_id == "CAD-02" and c.action.proposed_date == AS_OF


# ---------------------------------------------------------------- 2. clearance

def clr_ds(pa=(H,), **kw):
    base = dict(plans=[mk_plan()], injections=[mk_inj(date(2026, 9, 25))], prior_auths=[mk_auth()])
    return mk_ds(pa=pa, **{**base, **kw})


def clr(ds, **appt_kw):
    return rules.check_clearance(mk_appt(**appt_kw), ds)


def test_clean_visit_all_pass():
    out = clr(clr_ds())
    assert ids(out) == ["CLR-01", "CLR-02", "CLR-03", "CLR-04", "CLR-11"]
    assert all(c.outcome == Outcome.PASS for c in out)
    assert get(out, "CLR-01").eye is None and get(out, "CLR-02").eye == OS
    assert "A-48213" in get(out, "CLR-04").detail and "Eylea HD" in get(out, "CLR-04").detail


def test_clr01_no_coverage_skips_policy_rules():
    ds = clr_ds(coverages=[mk_cov(period_end=date(2026, 10, 1))],
                policies=[mk_policy(pa_required=[H], step_therapy=StepTherapy())],
                injections=[mk_inj(DOS - timedelta(days=27))])
    out = clr(ds)
    c = get(out, "CLR-01")
    assert c.outcome == Outcome.ACTION and c.action.kind == ActionKind.VERIFY_ELIGIBILITY
    assert c.action.owner == Role.AUTH and c.evidence[0].ref == "Coverage/cov-1"
    assert not {"CLR-04", "CLR-05", "CLR-06", "CLR-07", "CLR-08", "CLR-09", "CLR-10"} & set(ids(out))
    c3 = get(out, "CLR-03")                      # falls back to 28 days
    assert c3.outcome == Outcome.ACTION and c3.action.proposed_date == date(2026, 11, 21)


def test_clr02_no_plan_and_drug_mismatch_hold():
    out = clr(clr_ds(plans=[]))
    c = get(out, "CLR-02")
    assert c.outcome == Outcome.HOLD and c.action is None and c.evidence
    assert "CLR-11" not in ids(out)
    out = clr(clr_ds(plans=[mk_plan(drug=V)]))
    c = get(out, "CLR-02")
    assert c.outcome == Outcome.HOLD and "Vabysmo" in c.detail and "Eylea HD" in c.detail


def test_clr03_interval_boundary():
    ok = get(clr(clr_ds(injections=[mk_inj(DOS - timedelta(days=28))])), "CLR-03")
    assert ok.outcome == Outcome.PASS
    bad = get(clr(clr_ds(injections=[mk_inj(DOS - timedelta(days=27))])), "CLR-03")
    assert bad.outcome == Outcome.ACTION and bad.action.kind == ActionKind.RESCHEDULE
    assert bad.action.owner == Role.SCHEDULER and bad.action.proposed_date == date(2026, 11, 21)
    assert bad.evidence and "27" in bad.detail


def test_clr03_first_injection_and_same_day_ignored():
    first = get(clr(clr_ds(injections=[mk_inj(DOS, OD)])), "CLR-03")          # other eye only
    assert first.outcome == Outcome.PASS and "First injection" in first.detail
    on_day = get(clr(clr_ds(injections=[mk_inj(DOS)])), "CLR-03")             # not before the visit
    assert on_day.outcome == Outcome.PASS


def test_clr04_no_pa_required_cites_policy_version():
    c = get(clr(clr_ds(pa=())), "CLR-04")
    assert c.outcome == Outcome.PASS and "2026.2" in c.detail and "Summit MA" in c.detail
    assert "CLR-09" not in ids(clr(clr_ds(pa=())))


def test_clr04_covered_by_auth():
    c = get(clr(clr_ds()), "CLR-04")
    assert c.outcome == Outcome.PASS and c.evidence[0].ref == "ClaimResponse/pa-1"


def test_clr05_auth_for_other_eye_not_clr04():
    out = clr(clr_ds(prior_auths=[mk_auth(eyes=[OD])]))
    c = get(out, "CLR-05")
    assert "CLR-04" not in ids(out)
    assert c.outcome == Outcome.ACTION and c.action.kind == ActionKind.REQUEST_PA
    assert c.action.owner == Role.AUTH and c.action.draft_kind == "pa_request"
    assert "OD" in c.detail and "OS" in c.detail and "Eylea HD" in c.detail and c.evidence


def test_clr05_auth_for_other_drug():
    c = get(clr(clr_ds(prior_auths=[mk_auth(drug=V)])), "CLR-05")
    assert c.outcome == Outcome.ACTION and "Vabysmo" in c.detail


def test_clr04_nothing_requests_pa():
    c = get(clr(clr_ds(prior_auths=[])), "CLR-04")
    assert c.outcome == Outcome.ACTION and c.action.kind == ActionKind.REQUEST_PA
    assert c.action.draft_kind == "pa_request" and c.evidence


def test_clr04_other_payer_auth_ignored():
    c = get(clr(clr_ds(prior_auths=[mk_auth(payer_id="other")])), "CLR-04")
    assert c.outcome == Outcome.ACTION and c.action.kind == ActionKind.REQUEST_PA


def test_clr06_expired_auth():
    c = get(clr(clr_ds(prior_auths=[mk_auth(valid_to=date(2026, 11, 14))])), "CLR-06")
    assert c.outcome == Outcome.ACTION and c.action.kind == ActionKind.RENEW_PA
    assert c.action.draft_kind == "pa_request" and c.action.owner == Role.AUTH
    assert "A-48213" in c.detail and "Nov 14, 2026" in c.detail and "Nov 20, 2026" in c.detail
    assert "Eylea HD" in c.detail and "OS" in c.detail
    assert any(e.field == "valid_to" and e.value == "2026-11-14" for e in c.evidence)


def test_clr06_not_started_yet():
    c = get(clr(clr_ds(prior_auths=[mk_auth(valid_from=date(2026, 12, 1))])), "CLR-06")
    assert c.outcome == Outcome.ACTION


def test_clr07_injections_used_up():
    c = get(clr(clr_ds(prior_auths=[mk_auth(injections_used=10)])), "CLR-07")
    assert c.outcome == Outcome.ACTION and c.action.kind == ActionKind.RENEW_PA
    assert c.action.draft_kind == "pa_request" and "10" in c.detail


def test_clr08_pending_with_dates():
    pending = mk_auth(id="pa-2", auth_number="A-2", status="pending", requested_on=date(2026, 11, 15), valid_from=None,
                      valid_to=None, injections_approved=None, injections_used=0)
    c = get(clr(clr_ds(prior_auths=[pending])), "CLR-08")
    assert c.outcome == Outcome.ACTION and c.action.kind == ActionKind.EXPEDITE_PA and c.action.owner == Role.AUTH
    assert "Nov 15, 2026" in c.detail and "Nov 22, 2026" in c.detail and "after" in c.detail
    early = mk_auth(id="pa-2", status="pending", requested_on=date(2026, 11, 10), valid_from=None, valid_to=None)
    c = get(clr(clr_ds(prior_auths=[early])), "CLR-08")
    assert "Nov 17, 2026" in c.detail and "after the visit" not in c.detail


def test_clr08_pending_beats_expired():
    expired = mk_auth(valid_to=date(2026, 11, 14))
    pending = mk_auth(id="pa-2", auth_number="A-2", status="pending", requested_on=date(2026, 11, 15))
    out = clr(clr_ds(prior_auths=[expired, pending]))
    assert "CLR-08" in ids(out) and "CLR-06" not in ids(out)


def test_clr06_beats_clr07_and_clr05():
    expired = mk_auth(valid_to=date(2026, 11, 14), injections_used=10)
    other = mk_auth(id="pa-2", auth_number="A-2", eyes=[OD])
    out = clr(clr_ds(prior_auths=[other, expired]))
    assert "CLR-06" in ids(out) and "CLR-05" not in ids(out) and "CLR-07" not in ids(out)


def test_clr09_valid_to_equals_visit_passes_with_note():
    out = clr(clr_ds(prior_auths=[mk_auth(valid_to=DOS)]))
    assert get(out, "CLR-04").outcome == Outcome.PASS
    c = get(out, "CLR-09")
    assert c.outcome == Outcome.NOTE and c.action.kind == ActionKind.RENEW_PA and c.action.owner == Role.AUTH
    assert "Nov 20, 2026" in c.detail and "A-48213" in c.detail and c.evidence


def test_clr09_thirty_day_boundary():
    assert "CLR-09" in ids(clr(clr_ds(prior_auths=[mk_auth(valid_to=DOS + timedelta(days=30))])))
    assert "CLR-09" not in ids(clr(clr_ds(prior_auths=[mk_auth(valid_to=DOS + timedelta(days=31))])))


def test_clr09_last_injection():
    out = clr(clr_ds(prior_auths=[mk_auth(injections_used=9)]))
    c = get(out, "CLR-09")
    assert c.outcome == Outcome.NOTE and "last" in c.detail
    assert "CLR-09" not in ids(clr(clr_ds(prior_auths=[mk_auth(injections_used=8)])))


def step_ds(bev_dates, **kw):
    return clr_ds(pa=(), policy_kw=dict(step_therapy=StepTherapy(min_doses=3)),
                  injections=[mk_inj(date(2026, 9, 25))] + [mk_inj(d, OD, B) for d in bev_dates], **kw)


BEV3 = [date(2026, 7, 1), date(2026, 7, 29), date(2026, 8, 26)]


def test_clr10_step_therapy_counts_any_eye_before_visit():
    assert get(clr(step_ds(BEV3)), "CLR-10").outcome == Outcome.PASS           # OD injections count for the OS visit
    bad = get(clr(step_ds(BEV3[:2])), "CLR-10")
    assert bad.outcome == Outcome.ACTION and bad.action.kind == ActionKind.STEP_EXCEPTION
    assert bad.action.owner == Role.PHYSICIAN and bad.action.draft_kind == "step_exception"
    assert "2" in bad.detail and "3" in bad.detail and bad.evidence
    same_day = get(clr(step_ds(BEV3[:2] + [DOS])), "CLR-10")                   # on the visit date: not before it
    assert same_day.outcome == Outcome.ACTION


def test_clr10_exception_and_first_line_drug():
    c = get(clr(step_ds([], prior_auths=[mk_auth(step_therapy_exception=True)])), "CLR-10")
    assert c.outcome == Outcome.PASS
    ds = step_ds([], plans=[mk_plan(drug=B)], prior_auths=[])
    assert "CLR-10" not in ids(clr(ds, planned=[PlannedInjection(eye=OS, drug=B)]))


def test_clr11_diagnosis_laterality():
    assert get(clr(clr_ds()), "CLR-11").outcome == Outcome.PASS
    assert get(clr(clr_ds(plans=[mk_plan(diagnosis_code="H35.3231")])), "CLR-11").outcome == Outcome.PASS
    wrong = get(clr(clr_ds(plans=[mk_plan(diagnosis_code="H35.3211")])), "CLR-11")
    assert wrong.outcome == Outcome.HOLD and wrong.action is None and "H35.3211" in wrong.detail and wrong.evidence
    unknown = get(clr(clr_ds(plans=[mk_plan(diagnosis_code="Z99.99")])), "CLR-11")
    assert unknown.outcome == Outcome.HOLD


def test_clearance_two_lines_in_order():
    planned = [PlannedInjection(eye=OD, drug=H), PlannedInjection(eye=OS, drug=H)]
    ds = clr_ds(plans=[mk_plan(id="plan-od", eye=OD, diagnosis_code="H35.3211"), mk_plan()],
                prior_auths=[mk_auth(eyes=[OD, OS])], injections=[mk_inj(date(2026, 9, 25), OD), mk_inj(date(2026, 9, 25))])
    out = clr(ds, planned=planned)
    assert ids(out) == ["CLR-01"] + ["CLR-02", "CLR-03", "CLR-04", "CLR-11"] * 2
    assert [c.eye for c in out[1:5]] == [OD] * 4 and [c.eye for c in out[5:]] == [OS] * 4


# ---------------------------------------------------------------- CLR-12 reconcile

def mk_note(d, nid="note-5"):
    return ClinicNote(id=nid, patient_id=PID, date=d, author="Dr X", text="Plan: switch OS to Vabysmo.")


def test_reconcile_pass_when_note_older_than_order():
    ep = ExtractedPlan(eye=OS, drug=V, quote="switch OS to Vabysmo")
    c = rules.reconcile_plan(mk_plan(), [ep], mk_note(date(2026, 5, 31)))
    assert c.rule_id == "CLR-12" and c.outcome == Outcome.PASS and c.eye == OS
    assert c.label == "Clinic note agrees with the order"


def test_reconcile_pass_other_eye_or_agreement():
    od = ExtractedPlan(eye=OD, drug=V, quote="x")
    assert rules.reconcile_plan(mk_plan(), [od], mk_note(date(2026, 6, 2))).outcome == Outcome.PASS
    same = ExtractedPlan(eye=OS, drug=H, interval_weeks=8, quote="Eylea HD q8w")
    assert rules.reconcile_plan(mk_plan(), [same], mk_note(date(2026, 6, 2))).outcome == Outcome.PASS
    unset = ExtractedPlan(eye=OS, quote="OS")
    assert rules.reconcile_plan(mk_plan(), [unset], mk_note(date(2026, 6, 2))).outcome == Outcome.PASS


def test_reconcile_hold_on_drug_with_quote():
    ep = ExtractedPlan(eye=OS, drug=V, quote="switch OS to Vabysmo")
    c = rules.reconcile_plan(mk_plan(), [ep], mk_note(date(2026, 6, 1)))        # same day is not older
    assert c.outcome == Outcome.HOLD and c.action is None
    assert "Vabysmo" in c.detail and "Eylea HD" in c.detail
    assert any(e.ref == "DocumentReference/note-5" and e.field == "text" and e.value == "switch OS to Vabysmo"
               for e in c.evidence)
    assert any(e.ref == "MedicationRequest/plan-1" and e.field == "drug" for e in c.evidence)


def test_reconcile_hold_on_interval():
    ep = ExtractedPlan(eye=OS, drug=H, interval_weeks=10, quote="extend to 10 weeks")
    c = rules.reconcile_plan(mk_plan(), [ep], mk_note(date(2026, 7, 1)))
    assert c.outcome == Outcome.HOLD and "10" in c.detail and "8" in c.detail
    assert any(e.value == "extend to 10 weeks" for e in c.evidence)
    assert any(e.ref == "MedicationRequest/plan-1" and e.field == "interval_weeks" and e.value == "8"
               for e in c.evidence)


# ---------------------------------------------------------------- 3. claim check

DX = "H35.3221"


def pline(seq=1, mods=("LT",), dx=(DX,)):
    return ClaimLine(seq=seq, code="67028", modifiers=list(mods), dx_codes=list(dx), charge=105.0)


def dline(seq=2, code="J0177", mods=("LT", "JZ"), units=8, ndc="12345-6789-01", dx=(DX,)):
    return ClaimLine(seq=seq, code=code, modifiers=list(mods), units=units, ndc=ndc, dx_codes=list(dx), charge=100.0)


def mk_claim(lines=None, **kw):
    lines = [pline(), dline()] if lines is None else lines
    return _make(Claim, dict(id="clm-1", patient_id=PID, payer_id="p1", dos=DOS, status="draft", lines=lines), kw)


def cl(claim=None, injs=None, pa=(), plans=None, **kw):
    claim = claim or mk_claim()
    ds = mk_ds(pa=pa, claims=[claim], plans=[mk_plan()] if plans is None else plans,
               injections=[mk_inj(DOS)] if injs is None else injs, **kw)
    return rules.check_claim(claim, ds)


def not_pass(checks):
    return [c for c in checks if c.outcome != Outcome.PASS]


def test_claim_clean_passes():
    out = cl()
    assert ids(out) == ["CLM-01", "CLM-03", "CLM-04", "CLM-05", "CLM-06", "CLM-07", "CLM-08"]
    assert not not_pass(out) and all(c.eye == OS for c in out) and all(c.evidence for c in out)


def test_claim_bilateral_same_day_two_drug_lines_passes():
    lines = [pline(1, ("RT",), ("H35.3231",)), dline(2, "J0178", ("RT", "JZ"), 2, dx=("H35.3231",)),
             pline(3, ("LT",), ("H35.3231",)), dline(4, "J2777", ("LT", "JZ"), 60, dx=("H35.3231",))]
    injs = [mk_inj(DOS, OD, E), mk_inj(DOS, OS, V)]
    out = cl(mk_claim(lines), injs=injs)
    assert not not_pass(out), [(c.rule_id, c.detail) for c in not_pass(out)]
    assert {c.eye for c in out} == {OD, OS}


def test_clm02_no_injection_holds_and_stops():
    out = cl(injs=[])
    assert len(out) == 1 and out[0].rule_id == "CLM-02" and out[0].outcome == Outcome.HOLD
    assert out[0].action is None and out[0].evidence


def test_clm01_sources_disagree_holds_and_skips_that_injection():
    lines = [pline(1, ("RT",)), dline(2, mods=("RT", "JZ")), pline(3, ("LT",)), dline(4, mods=("LT", "JZ"))]
    injs = [mk_inj(DOS, OD, H), mk_inj(DOS, OS, H, admin_eye=OD)]
    out = cl(mk_claim(lines), injs=injs, plans=[mk_plan(eye=OD, diagnosis_code="H35.3211")])
    hold = get(out, "CLM-01", OS)
    assert hold.outcome == Outcome.HOLD and hold.action is None and len(hold.evidence) == 2
    assert [c.rule_id for c in out if c.eye == OS] == ["CLM-01"]
    assert get(out, "CLM-03", OD).outcome == Outcome.PASS


def test_clm03_wrong_eye_modifier():
    out = cl(mk_claim([pline(mods=("RT",)), dline()]))
    c = get(out, "CLM-03")
    assert c.outcome == Outcome.ACTION and c.action.kind == ActionKind.FIX_CLAIM and c.action.owner == Role.BILLER
    assert c.action.changes == [{"line": 1, "field": "modifier", "from": "RT", "to": "LT"}]


def test_clm03_no_line_left_has_empty_changes():
    lines = [pline(1, ("RT",)), dline(2, mods=("RT", "JZ"))]
    out = cl(mk_claim(lines), injs=[mk_inj(DOS, OD, H), mk_inj(DOS, OS, H)],
             plans=[mk_plan(eye=OD, diagnosis_code="H35.3211")])
    c = get(out, "CLM-03", OS)
    assert c.outcome == Outcome.ACTION and c.action.changes == [] and "no injection line" in c.detail


def test_clm04_eylea_code_for_eylea_hd():
    out = cl(mk_claim([pline(), dline(code="J0178")]))
    c = get(out, "CLM-04")
    assert c.outcome == Outcome.ACTION and c.action.changes == [{"line": 2, "field": "code", "from": "J0178", "to": "J0177"}]
    assert "Eylea HD" in c.detail and "J0177" in c.detail


def test_clm04_no_drug_line_skips_units_waste_ndc():
    out = cl(mk_claim([pline()]))
    c = get(out, "CLM-04")
    assert c.outcome == Outcome.ACTION and c.action.changes == []
    assert not {"CLM-05", "CLM-06", "CLM-07"} & set(ids(out))


def test_clm05_units_eylea_hd_two_units():
    out = cl(mk_claim([pline(), dline(units=2)]))
    c = get(out, "CLM-05")
    assert c.outcome == Outcome.ACTION and c.action.changes == [{"line": 2, "field": "units", "from": "2", "to": "8"}]
    assert get(out, "CLM-04").outcome == Outcome.PASS


def test_bevacizumab_uses_payer_code_and_units():
    pol = dict(policy_kw=dict(bevacizumab_hcpcs="C9257", bevacizumab_units=5))
    inj = [mk_inj(DOS, OS, B)]
    ok = cl(mk_claim([pline(), dline(code="C9257", units=5)]), injs=inj, **pol)
    assert not not_pass(ok)
    bad = cl(mk_claim([pline(), dline(code="C9257", units=1)]), injs=inj, **pol)
    assert get(bad, "CLM-05").action.changes == [{"line": 2, "field": "units", "from": "1", "to": "5"}]
    wrong_code = cl(mk_claim([pline(), dline(code="J0178", units=5)]), injs=inj, **pol)
    c = get(wrong_code, "CLM-04")
    assert c.action.changes == [{"line": 2, "field": "code", "from": "J0178", "to": "C9257"}]


def test_clm06_waste_modifier():
    both = get(cl(mk_claim([pline(), dline(mods=("LT", "JZ", "JW"))])), "CLM-06")
    assert both.outcome == Outcome.ACTION and both.action.changes[0]["to"] == "JZ" and both.action.changes[0]["line"] == 2
    none = get(cl(mk_claim([pline(), dline(mods=("LT",))])), "CLM-06")
    assert none.outcome == Outcome.ACTION and none.action.changes == [{"line": 2, "field": "modifier", "from": "", "to": "JZ"}]
    assert get(cl(mk_claim([pline(), dline(mods=("LT", "JW"))])), "CLM-06").outcome == Outcome.PASS


def test_clm07_ndc_formats():
    for ndc in ("12345-6789-01", "12345678901"):
        assert get(cl(mk_claim([pline(), dline(ndc=ndc)])), "CLM-07").outcome == Outcome.PASS
    bad = get(cl(mk_claim([pline(), dline(ndc="1234567890")])), "CLM-07")
    assert bad.outcome == Outcome.ACTION and "1234567890" in bad.detail
    assert bad.action.changes == [{"line": 2, "field": "ndc", "from": "1234567890", "to": "12345-6789-01"}]
    missing = get(cl(mk_claim([pline(), dline(ndc=None)])), "CLM-07")
    assert missing.outcome == Outcome.ACTION and missing.action.changes[0]["from"] == ""


def test_clm08_dx_laterality():
    wrong = get(cl(mk_claim([pline(dx=("H35.3211",)), dline()])), "CLM-08")
    assert wrong.outcome == Outcome.ACTION
    assert wrong.action.changes == [{"line": 1, "field": "dx_codes", "from": "H35.3211", "to": DX}]
    both = get(cl(mk_claim([pline(dx=("H35.3231",)), dline()])), "CLM-08")
    assert both.outcome == Outcome.PASS
    unknown = get(cl(mk_claim([pline(dx=("Z99.99",)), dline()])), "CLM-08")
    assert unknown.outcome == Outcome.ACTION


def test_clm09_authorization_number():
    auth = mk_auth()
    good = cl(mk_claim(auth_number="A-48213"), pa=(H,), prior_auths=[auth])
    assert get(good, "CLM-09").outcome == Outcome.PASS
    bad = get(cl(mk_claim(auth_number="A-48214"), pa=(H,), prior_auths=[auth]), "CLM-09")
    assert bad.outcome == Outcome.ACTION and bad.action.changes[0]["from"] == "A-48214"
    assert bad.action.changes[0]["to"] == "A-48213" and bad.action.changes[0]["field"] == "auth_number"
    missing = get(cl(mk_claim(), pa=(H,), prior_auths=[auth]), "CLM-09")
    assert missing.outcome == Outcome.ACTION and missing.action.changes[0]["from"] == ""
    none = get(cl(mk_claim(auth_number="A-1"), pa=(H,), prior_auths=[]), "CLM-09")
    assert none.outcome == Outcome.HOLD and none.action is None
    expired = get(cl(mk_claim(), pa=(H,), prior_auths=[mk_auth(valid_to=date(2026, 11, 19))]), "CLM-09")
    assert expired.outcome == Outcome.HOLD
    assert "CLM-09" not in ids(cl())          # PA not required: not evaluated


def test_clm10_modifier_25():
    em = ClaimLine(seq=3, code="99213", modifiers=[], charge=90.0)
    bad = get(cl(mk_claim([pline(), dline(), em])), "CLM-10")
    assert bad.outcome == Outcome.ACTION and bad.eye is None
    assert bad.action.changes == [{"line": 3, "field": "modifier", "from": "", "to": "25"}]
    ok = ClaimLine(seq=3, code="99213", modifiers=["25"], charge=90.0)
    assert get(cl(mk_claim([pline(), dline(), ok])), "CLM-10").outcome == Outcome.PASS
    assert "CLM-10" not in ids(cl())


# ---------------------------------------------------------------- 4. denials

def mk_den(carc, **kw):
    return _make(Denial, dict(id="den-1", claim_id="clm-1", date=date(2026, 12, 1), carc=carc, amount=2410.0), kw)


def tri(carc, claim=None, den_kw=None, pa=(), **kw):
    claim = claim or mk_claim()
    ds = mk_ds(pa=pa, claims=[claim], plans=[mk_plan()], injections=kw.pop("injs", [mk_inj(DOS)]), **kw)
    out = rules.triage_denial(mk_den(carc, **(den_kw or {})), ds)
    assert len(out) == 1 and out[0].label == "Denial root cause"
    return out[0]


def test_den00_claim_not_found():
    ds = mk_ds()
    c = rules.triage_denial(mk_den("197"), ds)[0]
    assert c.rule_id == "DEN-00" and c.outcome == Outcome.HOLD and c.action is None
    assert c.detail.startswith("CO-197 (Precertification or authorization absent): ")


def test_den02_missing_or_wrong_auth_number_fix():
    c = tri("197", mk_claim(auth_number="A-0"), pa=(H,), prior_auths=[mk_auth()])
    assert c.rule_id == "DEN-02" and c.outcome == Outcome.ACTION and c.detail.startswith("CO-197 (")
    assert c.action.kind == ActionKind.FIX_CLAIM and c.action.owner == Role.BILLER
    assert c.action.changes == [{"line": 0, "field": "auth_number", "from": "A-0", "to": "A-48213"}]
    assert tri("197", pa=(H,), prior_auths=[mk_auth()]).rule_id == "DEN-02"


def test_den03_claim_already_has_number_appeals():
    c = tri("197", mk_claim(auth_number="A-48213"), pa=(H,), prior_auths=[mk_auth()])
    assert c.rule_id == "DEN-03" and c.action.kind == ActionKind.APPEAL and c.action.draft_kind == "appeal"
    assert c.action.owner == Role.BILLER and c.evidence


def test_den04_no_auth_holds():
    c = tri("197", pa=(H,), prior_auths=[mk_auth(eyes=[OD])])
    assert c.rule_id == "DEN-04" and c.outcome == Outcome.HOLD and c.action is None and c.evidence


def test_den05_aggregates_changes():
    claim = mk_claim([pline(mods=("RT",)), dline(units=2)])
    c = tri("4", claim)
    assert c.rule_id == "DEN-05" and c.outcome == Outcome.ACTION and c.action.kind == ActionKind.FIX_CLAIM
    assert c.action.changes == [{"line": 1, "field": "modifier", "from": "RT", "to": "LT"},
                                {"line": 2, "field": "units", "from": "2", "to": "8"}]
    assert c.detail.startswith("CO-4 (")
    assert tri("16", claim).rule_id == "DEN-05" and tri("11", claim).rule_id == "DEN-05"


def test_den06_hold_when_claim_check_holds_or_finds_nothing():
    assert tri("4").rule_id == "DEN-06"                                    # claim is fine
    held = tri("4", mk_claim(), injs=[mk_inj(DOS, admin_eye=OD)])          # sources disagree
    assert held.rule_id == "DEN-06" and held.outcome == Outcome.HOLD and held.action is None
    mixed = mk_claim([pline(mods=("RT",)), dline(units=2)])
    assert tri("4", mixed, injs=[mk_inj(DOS, admin_eye=OD)]).rule_id == "DEN-06"


def test_den07_vs_den08_at_27_and_28_days():
    short = tri("151", injs=[mk_inj(DOS), mk_inj(DOS - timedelta(days=27))])
    assert short.rule_id == "DEN-07" and short.outcome == Outcome.HOLD and short.action is None
    ok = tri("151", injs=[mk_inj(DOS), mk_inj(DOS - timedelta(days=28))])
    assert ok.rule_id == "DEN-08" and ok.action.kind == ActionKind.APPEAL and ok.action.draft_kind == "appeal"
    assert "Oct 23, 2026" in ok.detail and "Nov 20, 2026" in ok.detail and "28" in ok.detail


def test_den08_first_injection_in_that_eye():
    assert tri("151").rule_id == "DEN-08"


def test_den09_medical_necessity():
    c = tri("50")
    assert c.rule_id == "DEN-09" and c.action.kind == ActionKind.APPEAL and c.action.draft_kind == "appeal"


def test_den10_den11_coverage_terminated():
    c = tri("27")
    assert c.rule_id == "DEN-10" and c.action.kind == ActionKind.APPEAL
    gone = tri("27", coverages=[mk_cov(period_end=date(2026, 10, 31))])
    assert gone.rule_id == "DEN-11" and gone.action.kind == ActionKind.VERIFY_ELIGIBILITY
    assert gone.action.owner == Role.AUTH and gone.evidence
    other = tri("27", coverages=[mk_cov(payer_id="elsewhere")])
    assert other.rule_id == "DEN-11"


def test_den12_other_codes_hold():
    for carc in ("29", "999"):
        c = tri(carc)
        assert c.rule_id == "DEN-12" and c.outcome == Outcome.HOLD and c.action is None
    assert tri("29").detail.startswith("CO-29 (Time limit for filing has expired): ")
