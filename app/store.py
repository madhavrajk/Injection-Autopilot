"""Human decisions. Append-only JSON lines; the latest decision for an item wins. Nothing is ever rewritten, so the
file is also the audit trail. It stores the patient token, never a name."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.contracts import Decision

RUNTIME = Path(__file__).resolve().parent.parent / "data" / "runtime"
DECISIONS = RUNTIME / "decisions.jsonl"


def record(decision: Decision, path: Path | None = None) -> Decision:
    path = path or DECISIONS
    decision.at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(decision.model_dump_json() + "\n")
    return decision


def history(path: Path | None = None) -> list[Decision]:
    """Oldest first. A line that does not parse is skipped, not fatal."""
    path = path or DECISIONS
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(Decision.model_validate(json.loads(line)))
        except Exception:
            continue
    return out


def latest(path: Path | None = None) -> dict[str, Decision]:
    return {d.item_id: d for d in history(path)}


def clear(path: Path | None = None) -> None:
    path = path or DECISIONS
    if path.exists():
        path.unlink()
