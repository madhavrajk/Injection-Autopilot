# Pass/fail criteria

Written before the build. The final review checks these and nothing else. Each one can be checked by running something.

| # | Criterion | How to check |
|---|---|---|
| 1 | No item is shown as clear when the answer key says a person is needed (zero wrong clears) | `python -m evals.report` and `python -m app.verify`: wrong clears = 0 |
| 2 | Every eye, drug, dose, date and code the app holds equals the raw FHIR bundle, for every record | `python -m app.verify`: second-source mismatches = 0 |
| 3 | Every finding cites a record that exists and quotes its value exactly | `python -m app.verify`: unresolved evidence = 0, value mismatches = 0 |
| 4 | No patient name, birth date, phone or member ID in any model prompt or audit line | `python -m app.verify`: leak hits = 0; `pytest -q tests/test_llm.py` |
| 5 | The model never writes a fact: drafts with a digit, drug or eye outside a placeholder are marked unverified and cannot be approved | `pytest -q tests/test_llm.py`; `POST /api/items/{id}/decision` refuses |
| 6 | When two sources disagree the agent holds and proposes no fix | `pytest -q tests/test_rules.py` (CLM-01, CLR-02, CLR-12 have `action is None`) |
| 7 | Nothing leaves the practice without a recorded human decision; the latest decision wins and all are kept | `pytest -q tests/test_api.py`; the Trust page decision log |
| 8 | Compliance role never receives a patient name from the server | `pytest -q tests/test_api.py` |
| 9 | One bad record never stops the rest; it ends as a hold for a person | `pytest -q tests/test_agent.py` |
| 10 | Every item explains itself: the checks run, the records read, and a plain-English trace | open any item in the app |
