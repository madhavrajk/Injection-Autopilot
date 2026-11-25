"""API behaviour on the synthetic bundle: role masking, decisions, and the rule that nothing unverified is approved."""
import pytest
from fastapi.testclient import TestClient

from app import api, store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_MODEL", "stub")
    monkeypatch.setattr(store, "DECISIONS", tmp_path / "decisions.jsonl")
    return TestClient(api.app)


def _first(client, status, stage=None):
    items = client.get("/api/items", params={"stage": stage} if stage else None).json()
    return next(i for i in items if i["status"] == status)


def test_summary_counts_add_up(client):
    s = client.get("/api/summary").json()
    for st in s["stages"].values():
        assert st["clear"] + st["action"] + st["hold"] == st["total"]
    assert s["stages"]["clearance"]["total"] > 0 and s["checks_run"] > 0


def test_holds_first_then_actions(client):
    order = [i["status"] for i in client.get("/api/items", params={"stage": "clearance"}).json()]
    rank = {"hold": 0, "action": 1, "clear": 2}
    assert order == sorted(order, key=rank.get)


def test_compliance_never_gets_a_name(client):
    ds = api.state()["ds"]
    names = {p.name for p in ds.patients}
    for i in client.get("/api/items", params={"role": "compliance"}).json():
        assert i["patient_name"] is None
    drafted = next(i for i in client.get("/api/items").json() if i["has_draft"])
    body = client.get(f"/api/items/{drafted['id']}", params={"role": "compliance"}).text
    assert not any(n in body for n in names)
    assert client.get(f"/api/items/{drafted['id']}", params={"role": "biller"}).json()["patient_name"] in names


def test_decision_is_recorded_and_latest_wins(client):
    it = _first(client, "action")
    url = f"/api/items/{it['id']}/decision"
    assert client.post(url, json={"action": "reject", "role": "biller", "note": "first"}).status_code == 200
    assert client.post(url, json={"action": "approve", "role": "biller", "note": "second"}).status_code == 200
    got = client.get(f"/api/items/{it['id']}").json()["decision"]
    assert got["action"] == "approve" and got["note"] == "second"
    assert len(store.history()) == 2                      # both kept
    log = client.get("/api/trust").json()["decisions"]
    assert log[0]["note"] == "second" and "patient_token" in log[0]


def test_audit_log_holds_no_names(client):
    it = _first(client, "action")
    client.post(f"/api/items/{it['id']}/decision", json={"action": "approve", "role": "owner"})
    text = store.DECISIONS.read_text()
    assert not any(p.name in text for p in api.state()["ds"].patients)


def test_clear_items_cannot_be_decided(client):
    it = _first(client, "clear")
    assert client.post(f"/api/items/{it['id']}/decision", json={"action": "approve", "role": "owner"}).status_code == 400


def test_unverified_draft_cannot_be_approved(client):
    drafted = next(i for i in client.get("/api/items").json() if i["has_draft"])
    item = api.state()["items"][drafted["id"]]
    item.draft.verified = False
    try:
        r = client.post(f"/api/items/{drafted['id']}/decision", json={"action": "approve", "role": "owner"})
        assert r.status_code == 400
    finally:
        item.draft.verified = True


def test_unknown_item_and_role(client):
    assert client.get("/api/items/nope:1").status_code == 404
    assert client.get("/api/items", params={"role": "janitor"}).status_code == 400
