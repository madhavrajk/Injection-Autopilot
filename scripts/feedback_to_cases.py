"""Turn reviewer disagreements into CANDIDATE eval cases. Usage: python scripts/feedback_to_cases.py

A rejection ("not an issue") or an edit means a person disagreed with the agent. Each one is written to
data/runtime/candidate_cases.json with the agent's answer and the person's note. Nothing is added to
tests/cases.json automatically: a person reads each candidate, writes the expected answer and promotes it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import agent, ingest, store  # noqa: E402


def main() -> int:
    ds = ingest.load()
    items = {i.id: i for i in agent.build_items(ds)}
    out = []
    for item_id, d in store.latest().items():
        it = items.get(item_id)
        if d.action == "approve" or it is None:
            continue
        out.append({
            "candidate_id": f"feedback-{item_id.replace(':', '-')}",
            "stage": it.stage.value, "subject_ref": it.subject_ref, "patient_token": it.patient_token,
            "agent_status": it.status.value, "agent_rules": agent.flagged_rules(it), "agent_headline": it.headline,
            "reviewer_role": d.role.value, "reviewer_action": d.action, "reviewer_note": d.note, "decided_at": d.at,
            "to_promote": "Write expect_status and expect_rules by hand, cut the dataset down to this subject, "
                          "then add it to tests/cases.json.",
        })
    path = store.RUNTIME / "candidate_cases.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"candidates": out}, indent=1))
    print(f"{len(out)} candidate case(s) from {len(store.latest())} decision(s) -> {path.relative_to(path.parents[2])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
