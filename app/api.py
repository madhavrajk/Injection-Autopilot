"""HTTP API and static site. Items are computed once at startup from the synthetic bundle; decisions are overlaid on
every request. Names are masked on the server for roles that may not see them."""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import agent, ingest, llm, store
from app.contracts import Dataset, Decision, Role, Stage, Status, WorkItem

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
PHI_FACT_KEYS = {"patient_name", "birth_date", "member_id"}

app = FastAPI(title="Injection Autopilot", docs_url="/api/docs")
STATE: dict = {}


def state() -> dict:
    if not STATE:
        t = time.perf_counter()
        ds: Dataset = ingest.load()
        items = agent.build_items(ds)
        STATE.update(ds=ds, items={i.id: i for i in items}, build_ms=round((time.perf_counter() - t) * 1000),
                     calls=list(agent.CALLS))
    return STATE


def _view(item: WorkItem, role: Role, full: bool) -> dict:
    """What one role may see of one item. Compliance gets the token only, and a redacted draft."""
    s = state()
    ds: Dataset = s["ds"]
    p = ds.patient(item.patient_id)
    out = item.model_copy(deep=True)
    out.patient_name = None if role == Role.COMPLIANCE or not p else p.name
    out.decision = store.latest().get(item.id)
    if not full:
        out.checks, out.trace, out.draft = [], [], None
        d = out.model_dump(mode="json")
        d["facts"] = {k: v for k, v in item.facts.items() if k in ("last", "due", "earliest", "booked", "proposed",
                                                                  "more_findings", "Days left", "Amount", "Reason")}
        d["has_draft"] = item.draft is not None
        return d
    if out.draft and role == Role.COMPLIANCE and p:
        cov = ds.latest_coverage(item.patient_id)
        out.draft.body = llm.redact(out.draft.body, p, cov)
        out.draft.subject = llm.redact(out.draft.subject, p, cov)
        out.draft.facts = {k: v for k, v in out.draft.facts.items() if k not in PHI_FACT_KEYS}
    return out.model_dump(mode="json")


def _role(role: Optional[str]) -> Role:
    try:
        return Role(role) if role else Role.COMPLIANCE   # least privilege when no role is given
    except ValueError:
        raise HTTPException(400, f"unknown role {role}")


@app.get("/api/summary")
def summary(role: Optional[str] = None) -> dict:
    s = state()
    ds: Dataset = s["ds"]
    items: list[WorkItem] = list(s["items"].values())
    decided = store.latest()
    stages = {}
    for st in Stage:
        its = [i for i in items if i.stage == st]
        open_ = [i for i in its if i.status != Status.CLEAR]
        stages[st.value] = {
            "total": len(its),
            "clear": sum(i.status == Status.CLEAR for i in its),
            "action": sum(i.status == Status.ACTION for i in its),
            "hold": sum(i.status == Status.HOLD for i in its),
            "dollars": round(sum(i.dollars_at_risk for i in open_), 2),
            "decided": sum(i.id in decided for i in open_),
        }
    owners = Counter(i.owner.value for i in items if i.status != Status.CLEAR and i.owner and i.id not in decided)
    rules = Counter(c.rule_id for i in items if i.stage != Stage.DENIAL
                    for c in i.checks if c.outcome.value in ("action", "hold"))
    labels = {c.rule_id: agent.RULE_NAMES.get(c.rule_id, c.label) for i in items for c in i.checks}
    return {
        "as_of": ds.as_of.isoformat(), "practice": ds.practice, "model_mode": llm.mode(),
        "patients": len(ds.patients), "checks_run": sum(len(i.checks) for i in items),
        "build_ms": s["build_ms"], "flags": len(ds.flags), "stages": stages, "owners": owners,
        "rule_names": agent.RULE_NAMES,
        "top_rules": [{"rule_id": r, "label": labels[r], "count": n} for r, n in rules.most_common(6)],
    }


@app.get("/api/items")
def items(stage: Optional[str] = None, role: Optional[str] = None) -> list[dict]:
    r = _role(role)
    order = {Status.HOLD: 0, Status.ACTION: 1, Status.CLEAR: 2}
    its = [i for i in state()["items"].values() if stage is None or i.stage.value == stage]
    its.sort(key=lambda i: (order[i.status], i.when.isoformat() if i.when else "9999", i.time or "", i.id))
    return [_view(i, r, full=False) for i in its]


@app.get("/api/items/{item_id}")
def item(item_id: str, role: Optional[str] = None) -> dict:
    it = state()["items"].get(item_id)
    if not it:
        raise HTTPException(404, "no such item")
    return _view(it, _role(role), full=True)


class DecisionIn(BaseModel):
    action: str
    role: Role
    note: str = ""


@app.post("/api/items/{item_id}/decision")
def decide(item_id: str, body: DecisionIn) -> dict:
    it = state()["items"].get(item_id)
    if not it:
        raise HTTPException(404, "no such item")
    if body.action not in ("approve", "edit", "reject"):
        raise HTTPException(400, "action must be approve, edit or reject")
    if it.status == Status.CLEAR:
        raise HTTPException(400, "nothing to decide: this item is clear")
    if body.action == "approve" and it.draft and not it.draft.verified:
        raise HTTPException(400, "the draft failed its fact check and cannot be approved as written")
    d = store.record(Decision(item_id=item_id, action=body.action, role=body.role, note=body.note,
                              status_at_decision=it.status, headline_at_decision=it.headline))
    return d.model_dump(mode="json")


@app.post("/api/reset")
def reset() -> dict:
    """Demo helper: forget recorded decisions."""
    store.clear()
    return {"ok": True}


def _json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


@app.get("/api/trust")
def trust() -> dict:
    s = state()
    ds: Dataset = s["ds"]
    calls = s["calls"]
    by_task: dict[str, dict] = {}
    for c in calls:
        t = by_task.setdefault(c.task, {"task": c.task, "model": c.model, "calls": 0, "tokens": 0, "cost_usd": 0.0, "ms": 0.0})
        t["calls"] += 1
        t["tokens"] += c.tokens_in + c.tokens_out
        t["cost_usd"] = round(t["cost_usd"] + c.cost_usd, 6)
        t["ms"] = round(t["ms"] + c.ms, 2)
    decisions = [d.model_dump(mode="json") for d in reversed(store.history())]
    tokens = {i.id: i.patient_token for i in s["items"].values()}
    for d in decisions:
        d["patient_token"] = tokens.get(d["item_id"], "")
    return {
        "evals": _json(store.RUNTIME / "eval_report.json"),
        "verify": _json(store.RUNTIME / "verify_report.json"),
        "model": {"mode": llm.mode(), "tasks": list(by_task.values()),
                  "cost_usd": round(sum(c.cost_usd for c in calls), 6), "calls": len(calls)},
        "flags": [f.model_dump() for f in ds.flags],
        "decisions": decisions,
        "build_ms": s["build_ms"],
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


app.mount("/", StaticFiles(directory=WEB), name="web")
