"""Ingest: round trip with zero flags, and bad data is flagged, never repaired. Offline."""
import copy
from datetime import date

import pytest

from app import ingest, synth
from app.contracts import Eye

AS_OF = date(2026, 11, 18)


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    d = tmp_path_factory.mktemp("syn")
    synth.main(["--seed", "7", "--out", str(d)])
    return d


@pytest.fixture(scope="module")
def parts():
    return synth.generate(7, AS_OF)


def test_round_trip_no_flags(out, parts):
    ds = ingest.load(str(out))
    assert ds.flags == []
    assert ds.as_of == AS_OF and ds.practice == "Garden State Retina (synthetic)"
    expected = synth.build_world(7, AS_OF).dataset()
    assert ds.model_dump() == expected.model_dump()
    m = parts[2]["counts"]
    assert len(ds.patients) == m["patients"] and len(ds.plans) == m["plans"] and len(ds.denials) == m["denials"]


def _entries(bundle, rt):
    return [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == rt]


def test_bad_codes_and_dangling_reference_are_flagged_not_repaired(parts):
    bundle, policies, _ = parts
    base = ingest.parse_bundle(bundle, policies, AS_OF, "p")
    assert base.flags == []
    b = copy.deepcopy(bundle)
    plan = next(r for r in _entries(b, "MedicationRequest") if r["intent"] == "plan")
    plan["dosageInstruction"][0]["site"]["coding"][0]["code"] = "99999"
    ma = _entries(b, "MedicationAdministration")[0]
    ma["medicationCodeableConcept"]["coding"][0]["code"] = "mystery_drug"
    appt = _entries(b, "Appointment")[0]
    appt["participant"][0]["actor"]["reference"] = "Patient/pt-9999"
    ds = ingest.parse_bundle(b, policies, AS_OF, "p")
    refs = {f.ref for f in ds.flags}
    assert f"MedicationRequest/{plan['id']}" in refs
    assert f"MedicationAdministration/{ma['id']}" in refs
    assert f"Appointment/{appt['id']}" in refs
    reasons = " ".join(f.reason for f in ds.flags)
    assert "eye" in reasons and "drug" in reasons and "dangling" in reasons
    # nothing repaired: the bad plan and injection are not kept with a guessed value; the appointment is kept as stored
    assert all(p.id != plan["id"] for p in ds.plans)
    assert all(i.medadmin_id != ma["id"] for i in ds.injections)
    assert next(a for a in ds.appointments if a.id == appt["id"]).patient_id == "pt-9999"
    assert len(ds.plans) == len(base.plans) - 1
    assert all(p.eye in (Eye.OD, Eye.OS) for p in ds.plans)


def test_garbage_does_not_crash(parts):
    bundle, policies, _ = parts
    b = copy.deepcopy(bundle)
    b["entry"].append({"resource": {"resourceType": "Observation", "id": "x"}})
    b["entry"].append({"resource": {"resourceType": "Patient", "id": "pt-bad"}})
    _entries(b, "Claim")[0]["billablePeriod"]["start"] = "not-a-date"
    ds = ingest.parse_bundle(b, policies, AS_OF, "p")
    assert {"Observation/x", "Patient/pt-bad"} <= {f.ref for f in ds.flags}
    assert len(ds.flags) >= 3
