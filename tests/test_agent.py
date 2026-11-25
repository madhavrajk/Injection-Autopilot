"""Orchestrator behaviour: failures end with a person, holds carry no fix, every item explains itself."""
from datetime import date

from app import agent, rules
from app.contracts import (Appointment, Coverage, Dataset, Drug, Eye, Injection, Outcome, Patient, PayerPolicy,
                           PlannedInjection, Status, TreatmentPlan)

AS_OF = date(2026, 11, 18)


def world(**kw) -> Dataset:
    base = dict(
        as_of=AS_OF, practice="Test Retina",
        patients=[Patient(id="pt-1", token="PT-1", name="Ada Quill", birth_date=date(1950, 1, 2))],
        coverages=[Coverage(id="cov-1", patient_id="pt-1", payer_id="ffs", plan_name="Part B", member_id="M1",
                            period_start=date(2020, 1, 1))],
        policies=[PayerPolicy(payer_id="ffs", name="Medicare (test)", kind="medicare_ffs", version="t1")],
        plans=[TreatmentPlan(id="plan-1", patient_id="pt-1", eye=Eye.OD, diagnosis_code="H35.3211",
                             drug=Drug.AFLIBERCEPT_2MG, interval_weeks=8, physician="Dr. T", updated_on=date(2026, 1, 1))],
        injections=[Injection(id="proc-1", patient_id="pt-1", date=date(2026, 9, 25), eye=Eye.OD,
                              drug=Drug.AFLIBERCEPT_2MG, dose_mg=2.0, admin_eye=Eye.OD, medadmin_id="ma-1")],
        appointments=[Appointment(id="appt-1", patient_id="pt-1", date=date(2026, 11, 20), kind="injection",
                                  status="booked", planned=[PlannedInjection(eye=Eye.OD, drug=Drug.AFLIBERCEPT_2MG)])],
    )
    base.update(kw)
    return Dataset(**base)


def test_clear_item_explains_itself():
    it = agent.evaluate("clearance", "appt-1", world())
    assert it.status == Status.CLEAR and it.headline == "All checks passed" and it.draft is None
    assert all(c.outcome in (Outcome.PASS, Outcome.NOTE) for c in it.checks) and len(it.checks) >= 4
    assert it.trace[0].kind == "code" and it.trace[-1].name == "Result" and it.dollars_at_risk == 0


def test_hold_has_no_fix_and_no_draft():
    ds = world(appointments=[Appointment(id="appt-1", patient_id="pt-1", date=date(2026, 11, 20), kind="injection",
                                         status="booked", planned=[PlannedInjection(eye=Eye.OD, drug=Drug.FARICIMAB)])])
    it = agent.evaluate("clearance", "appt-1", ds)
    assert it.status == Status.HOLD and it.draft is None and it.owner.value == "physician"
    assert all(c.action is None for c in it.checks if c.outcome == Outcome.HOLD)
    assert it.trace[-1].kind == "person" and it.dollars_at_risk > 0


def test_one_bad_record_does_not_stop_the_rest(monkeypatch):
    ds = world()
    real = rules.check_cadence

    def boom(plan, d):
        raise RuntimeError("corrupt record")

    monkeypatch.setattr(rules, "check_cadence", boom)
    items = agent.build_items(ds)
    monkeypatch.setattr(rules, "check_cadence", real)
    by = {i.id: i for i in items}
    assert by["cadence:plan-1"].status == Status.HOLD and "corrupt record" in by["cadence:plan-1"].headline
    assert by["clearance:appt-1"].status == Status.CLEAR      # the other item still ran


def test_action_item_gets_a_verified_draft_with_facts_from_the_record():
    ds = world(coverages=[Coverage(id="cov-1", patient_id="pt-1", payer_id="ma", plan_name="Advantage", member_id="M1",
                                   period_start=date(2020, 1, 1))],
               policies=[PayerPolicy(payer_id="ma", name="Advantage (test)", kind="medicare_advantage", version="t1",
                                     pa_required=[Drug.AFLIBERCEPT_2MG])])
    it = agent.evaluate("clearance", "appt-1", ds)
    assert it.status == Status.ACTION and agent.flagged_rules(it) == ["CLR-04"]
    assert it.draft and it.draft.verified and "Ada Quill" in it.draft.body and "Eylea" in it.draft.body
    assert "{" not in it.draft.body


def test_evidence_never_quotes_a_value_that_is_not_in_the_record():
    """Found by the full check: a missing drug line was cited as productOrService = "none". Evidence must quote the
    record, so the finding now cites the codes the claim does carry."""
    from app import ingest
    ds = ingest.load()
    for it in agent.build_items(ds):
        for c in it.checks:
            for e in c.evidence:
                assert e.value not in ("none", ""), (it.id, c.rule_id, e.ref, e.field)
