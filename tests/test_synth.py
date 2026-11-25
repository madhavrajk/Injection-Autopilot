"""Synthetic world: determinism, shape, counts, and the manifest answer key. Offline."""
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import synth

AS_OF = date(2026, 11, 18)
SPEC = (Path(__file__).resolve().parent.parent / "docs" / "spec.md").read_text()

MAPPED = {"Patient", "Coverage", "ClaimResponse", "MedicationRequest", "Procedure", "MedicationAdministration",
          "Appointment", "DocumentReference", "Claim"}

# every ACTION or HOLD rule of spec sections 1 to 4, plus CLR-12 (CAD-00 and CAD-01 are not problems)
REQUIRED = ({"CAD-02", "CAD-03", "CAD-04", "CAD-05"}
            | {f"CLR-{n:02d}" for n in (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12)}
            | {f"CLM-{n:02d}" for n in range(1, 11)}
            | {f"DEN-{n:02d}" for n in (0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)})


@pytest.fixture(scope="module")
def world():
    return synth.generate(7, AS_OF)


def test_deterministic(world, tmp_path):
    assert synth.generate(7, AS_OF) == world
    assert synth.generate(8, AS_OF) != world
    a, b = tmp_path / "a", tmp_path / "b"
    synth.main(["--seed", "7", "--out", str(a)])
    synth.main(["--seed", "7", "--out", str(b)])
    for f in ("bundle.json", "policies.json", "manifest.json"):
        assert (a / f).read_bytes() == (b / f).read_bytes()
        json.loads((a / f).read_text())


def test_bundle_shape(world):
    bundle, policies, manifest = world
    assert bundle["resourceType"] == "Bundle" and bundle["type"] == "collection"
    assert bundle["timestamp"].startswith("2026-11-18")
    assert {e["resource"]["resourceType"] for e in bundle["entry"]} <= MAPPED
    assert [p["payer_id"] for p in policies] == ["medicare-ffs", "ma-summit", "ma-harbor", "com-northfield"]
    assert manifest["practice"] == "Garden State Retina (synthetic)"


def _res(bundle, rt, **eq):
    return [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == rt
            and all(e["resource"].get(k) == v for k, v in eq.items())]


def _ids(bundle):
    return {
        "cadence": {r["id"] for r in _res(bundle, "MedicationRequest", intent="plan")},
        "clearance": {r["id"] for r in _res(bundle, "Appointment")},
        "claim": {r["id"] for r in _res(bundle, "Claim")},
        "denial": {r["id"] for r in _res(bundle, "ClaimResponse", use="claim")},
    }


def test_counts(world):
    bundle, _, m = world
    c = m["counts"]
    assert 125 <= c["patients"] <= 155
    assert c["plans"] >= c["patients"]
    assert 50 <= c["clearance_appointments"] <= 60
    assert 55 <= c["draft_claims"] <= 65
    assert 14 <= c["denials"] <= 18
    assert len(_res(bundle, "Patient")) == c["patients"]
    items = c["plans"] + c["clearance_appointments"] + c["draft_claims"] + c["denials"]
    not_clear = sum(1 for p in m["planted"])
    assert 0.15 <= not_clear / items <= 0.30


def test_manifest_items_exist(world):
    bundle, _, m = world
    ids = _ids(bundle)
    appts = {r["id"]: r for r in _res(bundle, "Appointment")}
    claims = {r["id"]: r for r in _res(bundle, "Claim")}
    for entry in m["planted"] + m["near_misses"]:
        stage, _, rid = entry["item_id"].partition(":")
        assert rid in ids[stage], entry["item_id"]
        assert entry["why"]
        if stage == "clearance":
            day = date.fromisoformat(appts[rid]["start"][:10])
            assert appts[rid]["status"] == "booked" and AS_OF <= day <= AS_OF + timedelta(days=7)
        if stage == "claim":
            c = claims[rid]
            day = date.fromisoformat(c["billablePeriod"]["start"])
            ext = {e["url"]: e.get("valueCode") for e in c["extension"]}
            assert ext["urn:visentia:claim-status"] == "draft" and AS_OF - timedelta(days=10) <= day < AS_OF
    keys = [p["item_id"] for p in m["planted"]]
    assert len(keys) == len(set(keys))
    assert not {p["item_id"] for p in m["planted"]} & {n["item_id"] for n in m["near_misses"]}
    for p in m["planted"]:
        assert p["stage"] == p["item_id"].split(":")[0]
        assert p["expect_status"] in ("action", "hold")
        assert p["expect_rules"] == sorted(p["expect_rules"]) and p["expect_rules"]
    assert {p["item_id"].split(":")[1] for p in m["planted"] if p["stage"] == "denial"} == ids["denial"]


def test_manifest_rules_exist_and_cover_everything(world):
    _, _, m = world
    in_spec = set(re.findall(r"(?:CAD|CLR|CLM|DEN)-\d\d", SPEC))
    planted = {r for p in m["planted"] for r in p["expect_rules"]}
    assert planted <= in_spec
    assert REQUIRED <= planted, sorted(REQUIRED - planted)


def test_named_near_misses_present(world):
    _, _, m = world
    text = " ".join(n["why"].lower() for n in m["near_misses"])
    for needle in ("exactly 28", "ending on the visit", "eylea hd", "predates", "bilateral", "no completed injection"):
        assert needle in text, needle


def test_baseline_injection_spacing(world):
    bundle, _, _ = world
    by_eye = {}
    for r in _res(bundle, "Procedure"):
        key = (r["subject"]["reference"], r["bodySite"][0]["coding"][0]["code"])
        by_eye.setdefault(key, []).append(date.fromisoformat(r["performedDateTime"][:10]))
    short = 0
    for ds in by_eye.values():
        ds.sort()
        short += sum(1 for a, b in zip(ds, ds[1:]) if (b - a).days < 28)
    assert short == 1  # only the planted DEN-07 patient
