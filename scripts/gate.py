"""The gate: run after every change. Usage: python scripts/gate.py

Steps, cheapest first: unit tests, blind labelled cases, then the full check (second source, answer key, leak).
Nothing here spends money: the model layer is forced to its stub.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_step(name: str, args: list[str]) -> bool:
    start = time.monotonic()
    env = {**os.environ, "AUTOPILOT_MODEL": "stub"}
    proc = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, env=env)
    secs = time.monotonic() - start
    out = (proc.stdout + proc.stderr).rstrip().splitlines()
    ok = proc.returncode == 0
    print(f"[{'PASS' if ok else 'FAIL'}] {name:<22} {secs:5.1f}s  {out[-1] if out else '(no output)'}")
    if not ok:
        for line in out[-30:]:
            print(f"    {line}")
    return ok


def main() -> int:
    steps = [
        ("unit tests", ["-m", "pytest", "-q"]),
        ("blind cases", ["-m", "evals.report"]),
        ("full check", ["-m", "app.verify"]),
    ]
    results = [run_step(name, args) for name, args in steps]
    if all(results):
        print("VERDICT: PASS")
        return 0
    print("VERDICT: FAIL. Fix the cause, not the test. Three attempts, then stop and report.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
