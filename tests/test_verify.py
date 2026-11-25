"""app.verify on the real data, then fault injection: each check must notice a planted fault."""
import json
import shutil
from pathlib import Path

import pytest

from app import agent, ingest, llm, verify

SRC = Path(__file__).resolve().parent.parent / "data" / "synthetic"


@pytest.fixture(scope="module")
def real():
    return verify.run(str(SRC))


@pytest.fixture
def copy(tmp_path):
    d = tmp_path / "synthetic"
    shutil.copytree(SRC, d)
    return d


def test_real_data_passes_every_check(real):
    for name in ("cross_source", "evidence", "answer_key", "leak"):
        assert real[name]["ok"], (name, {k: v for k, v in real[name].items() if k != "ok"})
    assert real["ok"] is True
    assert real["cross_source"]["fields_compared"] > 10000
    assert real["answer_key"]["matched"] == real["answer_key"]["items"] > 250


def test_report_shape(real):
    assert set(real) == {"ok", "seconds", "as_of", "cross_source", "evidence", "answer_key", "leak"}
    assert real["leak"]["prompts"] > 0 and real["leak"]["identifiers"] > 0


def test_a_flipped_procedure_eye_breaks_the_answer_key(copy):
    ds = ingest.load(str(copy))
    item = next(i for i in agent.build_items(ds) if i.stage.value == "claim" and i.status.value == "clear")
    appt = ds.claim(item.subject_ref.split("/")[-1]).appointment_id
    bundle = json.loads((copy / "bundle.json").read_text())
    proc = next(r for e in bundle["entry"] if (r := e["resource"])["resourceType"] == "Procedure"
                and any(x.get("valueReference", {}).get("reference") == f"Appointment/{appt}"
                        for x in r.get("extension", [])))
    code = proc["bodySite"][0]["coding"][0]
    code["code"] = "18944008" if code["code"] == "8966001" else "8966001"
    (copy / "bundle.json").write_text(json.dumps(bundle))
    r = verify.run(str(copy))
    assert not r["answer_key"]["ok"]
    assert r["answer_key"]["false_alarm"] or r["answer_key"]["rule_mismatch"]


def test_b_flipped_dataset_eye_is_a_mismatch(monkeypatch):
    ds = ingest.load(str(SRC))
    inj = ds.injections[0]
    inj.eye = type(inj.eye).OS if inj.eye.value == "OD" else type(inj.eye).OD
    monkeypatch.setattr(ingest, "load", lambda d="data/synthetic": ds)
    r = verify.run(str(SRC))["cross_source"]
    assert not r["ok"]
    assert any(m["ref"] == f"Procedure/{inj.id}" and m["field"] == "eye" for m in r["mismatches"])


def test_c_bad_evidence_rows_are_reported(monkeypatch):
    real_build = agent.build_items

    def broken(ds):
        items = real_build(ds)
        rows = [ev for it in items for ch in it.checks for ev in ch.evidence]
        rows[0].value = "definitely not in the record"
        rows[1].ref = "Coverage/cov-9999"
        broken.refs = (rows[0].ref, rows[1].ref)
        return items

    monkeypatch.setattr(agent, "build_items", broken)
    r = verify.run(str(SRC))["evidence"]
    assert not r["ok"]
    assert any(m["ref"] == broken.refs[0] for m in r["value_mismatches"])
    assert any(u["ref"] == "Coverage/cov-9999" for u in r["unresolved"])
    assert r["unresolved_count"] >= 1 and r["value_mismatch_count"] >= 1


def test_d_a_name_in_a_prompt_is_a_hit_without_echoing_it(monkeypatch):
    raw = verify.load_raw(SRC)
    pat = next(iter(raw["Patient"].values()))
    name = f"{pat['name'][0]['given'][0]} {pat['name'][0]['family']}"
    real_build = agent.build_items

    def build_then_leak(ds):
        items = real_build(ds)
        llm.PROMPT_LOG.append({"task": "draft", "prompt": f"Write to {name.upper()} about the visit."})
        return items

    monkeypatch.setattr(agent, "build_items", build_then_leak)
    r = verify.run(str(SRC))["leak"]
    assert not r["ok"] and r["hit_count"] >= 1
    assert {h["identifier_kind"] for h in r["hits"]} >= {"full name"}
    assert pat["identifier"][0]["value"] in {h["patient_token"] for h in r["hits"]}
    blob = json.dumps(r)
    assert name not in blob and pat["name"][0]["family"] not in blob


def test_e_manifest_faults_become_false_alarm_and_wrong_clear(copy):
    mf_path = copy / "manifest.json"
    mf = json.loads(mf_path.read_text())
    dropped = mf["planted"].pop(0)
    clean = next(i for i in agent.build_items(ingest.load(str(copy)))
                 if i.status.value == "clear" and i.id not in {p["item_id"] for p in mf["planted"]})
    mf["planted"].append({"item_id": clean.id, "stage": clean.stage.value, "expect_status": "hold",
                          "expect_rules": ["CLR-02"], "why": "injected"})
    mf_path.write_text(json.dumps(mf))
    r = verify.run(str(copy))["answer_key"]
    assert not r["ok"]
    assert any(x["item_id"] == dropped["item_id"] for x in r["false_alarm"])
    assert any(x["item_id"] == clean.id for x in r["wrong_clear"])


def test_missing_planted_item_is_reported(copy):
    mf_path = copy / "manifest.json"
    mf = json.loads(mf_path.read_text())
    mf["planted"].append({"item_id": "claim:clm-nope", "stage": "claim", "expect_status": "action",
                          "expect_rules": ["CLM-01"], "why": "injected"})
    mf_path.write_text(json.dumps(mf))
    assert verify.run(str(copy))["answer_key"]["missing_items"] == ["claim:clm-nope"]
