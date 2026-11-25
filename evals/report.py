"""Blind labelled cases. Usage: python -m evals.report

`tests/cases.json` was written by an agent that read only the spec and the contracts. Each case is a small world, a
subject and the answer derived by hand. A case passes only when the status AND the exact set of flagged rules match.
The worst failure is a "wrong clear": the answer key says a person is needed and the agent said clear. Exits 1 on any
mismatch. Writes data/runtime/eval_report.json for the Trust page.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ["AUTOPILOT_MODEL"] = "stub"   # evals never spend money

from app import agent  # noqa: E402
from app.contracts import Dataset  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def run(cases_path: Path = ROOT / "tests" / "cases.json") -> dict:
    cases = json.loads(cases_path.read_text())["cases"]
    rows = []
    for c in cases:
        try:
            item = agent.evaluate(c["stage"], c["subject_id"], Dataset.model_validate(c["dataset"]))
            got_status, got_rules = item.status.value, agent.flagged_rules(item)
        except Exception as exc:
            got_status, got_rules = f"error: {type(exc).__name__}: {exc}", []
        ok = got_status == c["expect_status"] and got_rules == sorted(c["expect_rules"])
        rows.append({"id": c["id"], "stage": c["stage"], "ok": ok, "expect_status": c["expect_status"],
                     "got_status": got_status, "expect_rules": sorted(c["expect_rules"]), "got_rules": got_rules,
                     "why": c.get("why", "")})
    by_stage = {}
    for r in rows:
        s = by_stage.setdefault(r["stage"], {"total": 0, "passed": 0})
        s["total"] += 1
        s["passed"] += r["ok"]
    return {
        "total": len(rows), "passed": sum(r["ok"] for r in rows),
        "wrong_clear": sum(1 for r in rows if r["expect_status"] != "clear" and r["got_status"] == "clear"),
        "false_alarm": sum(1 for r in rows if r["expect_status"] == "clear" and r["got_status"] != "clear"),
        "expected_clear": sum(1 for r in rows if r["expect_status"] == "clear"),
        "by_stage": by_stage, "failures": [r for r in rows if not r["ok"]], "rows": rows,
    }


def main() -> int:
    rep = run()
    out = ROOT / "data" / "runtime"
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval_report.json").write_text(json.dumps(rep, indent=1))
    for stage, s in rep["by_stage"].items():
        print(f"  {stage:<10} {s['passed']}/{s['total']}")
    for r in rep["failures"]:
        print(f"  FAIL {r['id']}: expected {r['expect_status']} {r['expect_rules']}, got {r['got_status']} {r['got_rules']}")
        print(f"       {r['why']}")
    print(f"cases {rep['passed']}/{rep['total']} · wrong clears {rep['wrong_clear']} · false alarms {rep['false_alarm']}")
    return 0 if rep["passed"] == rep["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
