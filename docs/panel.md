# Panel review (one time, run against the live repo)

> Written **before** one round of fixes. Several notes below are now fixed (per-payer appeal windows, the
> waste-modifier wording, no role means no names); see "Fixed after review" in the README and D10 in `docs/rationale.md`.

Every criterion below was checked by running the command it names. Nothing was posted to the running server, so
`data/runtime/decisions.jsonl` does not exist and the audit log was not touched.

## Criteria: 10 of 10 PASS

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | Zero wrong clears | PASS | `evals.report`: `cases 52/52 · wrong clears 0 · false alarms 0`; `app.verify`: `answer_key PASS 287/287 match, 0 wrong clears` |
| 2 | Held values equal the raw bundle | PASS | `app.verify`: `cross_source PASS 2729 records, 16092 fields, 0 mismatches` |
| 3 | Evidence resolves and quotes exactly | PASS | `app.verify`: `evidence PASS 1581 rows, 0 unresolved, 0 value mismatches` |
| 4 | No PHI in prompts or audit lines | PASS | `app.verify`: `leak PASS 73 prompts, 1673 trace steps, 0 log lines, 1127 identifiers, 0 hits`; `tests/test_llm.py` all pass |
| 5 | Model never writes a fact; unverified draft cannot be approved | PASS | `tests/test_llm.py` pass; `tests/test_api.py::test_unverified_draft_cannot_be_approved` pass; all 287 live items carry a verified draft or none. The POST was not exercised live, by instruction |
| 6 | Disagreeing sources hold with no fix | PASS | `tests/test_rules.py` all pass (CLM-01, CLR-02, CLR-12 assert `action is None`) |
| 7 | Human decision recorded, latest wins, all kept | PASS | `tests/test_api.py` 8 pass; `app/store.py` is append-only JSONL, `latest()` takes the last per item |
| 8 | Compliance never gets a name | PASS | `tests/test_api.py` pass; independent live scan of all 286 patient items with `role=compliance` for full name, surname, DOB, phone and member ID: 0 hits |
| 9 | One bad record never stops the rest | PASS | `tests/test_agent.py` 5 pass; live item `denial:den-0008` (claim not in records, "Unknown patient") ends as a HOLD |
| 10 | Every item explains itself | PASS | curl of all 287 items: every one has checks and a trace; every non-clear item has evidence on its non-pass check |

`scripts/gate.py` ends `VERDICT: PASS`.

## Scores

**Retina practice administrator / biller: 6/10.** The mechanics are right where it matters: the units are correct (J0178 x2, J0177 x8, J2777 x60, J2778 x5), laterality ICD-10 codes are real (H35.3211, E11.3211, H34.8310 and so on), CARC meanings are right, and the interval arithmetic is correct on every item I read (for example Eylea due Sep 9 from Jul 15 + 8 weeks). Some rule and wording choices would draw eye-rolls from a biller or give wrong advice, listed below.
Change that matters most: stop treating the appeal window as 180 days for every payer, and add the established-patient exemption to step therapy.

Specific errors and naive points:
- Appeal deadline is `180` days for every payer (`app/contracts.py:185`, used at `app/agent.py:262`). The screen shows "Appeal by Apr 14, 2027, 147 days left" for a Medicare Part B denial (`denial:den-0009`). Traditional Medicare redetermination is 120 days from the notice and MA reconsideration is about 60 days, so this overstates the time left and appears in the appeal letter text.
- Step therapy (CLR-10, `app/rules.py:278-301`) asks for 3 documented bevacizumab doses before any other drug, with no exemption for a patient already established on the drug. CMS step therapy applies to new starts, so every long-term Eylea patient on a Summit-type plan would get a step-exception ACTION routed to the physician. It also runs even when the PA for that drug is already approved, which is a false alarm.
- CLM-06 always proposes `JZ` as the fix, including when the line carries both or neither (`clm-0025`, `clm-0056`). JZ versus JW depends on whether any drug was discarded, which the record does not hold. It should ask the biller, not propose a value. It also applies JZ/JW to Avastin repackaged syringes without comment.
- Bilateral same-day is treated as RT and LT on two lines and kept clear. Many MACs want 67028-50 for same-session bilateral. `docs/questions.md` admits this was not guessed, but the app still clears those claims.
- The diagnosis table holds 10 codes. A real chart will have E11.3311, E11.3511 (PDR with DME), H35.3212 and others. Each becomes a CLR-11 HOLD ("not in the practice's code table"), so the real false-alarm rate on a live feed would be high.
- Cadence is wrong for treat-and-extend and loading doses in two ways. A lapsed nAMD eye (for example `plan-0117`, 70 days overdue) goes to the scheduler as an SMS with `dollars_at_risk 0.0`. No physician is told, and vision loss is not what the ranking measures. Also `docs/domain_recon.md` says loading doses and maximum intervals are not modelled.
- Biosimilars (Pavblu and others) and C9257 on a professional MA claim are not modelled. C9257 is a hospital outpatient code. Both are illustrative, but a retina biller will notice by item three.
- Wording: "Drafted a appeal", "Drafted a outreach sms", "carries authorization none".

**Healthcare data engineer / HIPAA security reviewer: 6/10.** The design is the right shape: code owns the facts, drafts are templates with placeholders, prompts carry keys only, and the leak check reads raw prompts, trace steps and the log against 1127 identifiers from the raw bundle (0 hits, independently re-confirmed against the compliance API). Weak points: role is a query-string value the client chooses, and the FHIR is far cleaner than a ModMed or Nextech feed will be.
Change that matters most: put real authentication in front of `/api` and derive role from the session, with the default role no longer `owner`.

- `app/api.py:_role` falls back to `Role.OWNER` when `role` is absent, so a bare `GET /api/items` returns names (confirmed live: "Michelle Okonkwo"). `POST /api/items/{id}/decision` takes `role` from the body, so any caller can approve as any role. Masking is a UI convention, not access control.
- The patient token is `PT-` plus the internal patient id (`PT-0125` / `pt-0125`), and `patient_id` is also returned to the compliance role. That is a trivially reversible identifier, not a pseudonym, and any non-synthetic data would need a keyed token.
- Redaction (`app/llm.py:133-148`) is exact-match on the patient's own name, DOB, phone and member ID. Real notes also carry relatives' names, addresses, SSN or MRN fragments, and dates of service, and none of those are touched. The leak check can only find the identifiers it was told about, so "0 leaks" is a claim about known identifiers. Real mode (`_complete_real`) is never exercised, so what is sent to the vendor is untested, and a BAA is not mentioned.
- `Decision.note` is free text written straight to the audit log (`app/store.py`). Staff typing a name there defeats "the log stores tokens only", and the leak check runs only at verify time.
- FHIR realism: the mapping uses a custom `urn:visentia:*` drug system and extensions (`pa-drug`, `planned-injection`, `claim-status`). Real EHRs send RxNorm or NDC, may split one injection across Procedure and MedicationAdministration without a `partOf`, and use Nextech STU3. Real eligibility is X12 271 and real prior auth is X12 278, not a ClaimResponse. The ingest flags and keeps bad records, which is the right behaviour, but the mapping is the part that will break first. The unresolved ModMed or Nextech question is honestly left open in `docs/questions.md`.

**Product lead at an AI healthcare startup: 7/10.** Claim check and clearance for intravitreal injections is a sharp, retina-specific wedge, since the drugs cost $1,500 to $2,400 and a denial hurts. The "code decides, model drafts" posture is a credible trust story for a clinical-adjacent buyer. A practice would pay if it saw even a few caught denials a month, but the proof today is on data the team planted.
Change that matters most: replace the planted-defect answer key with a one-week replay on a design partner's de-identified claims and remits, and report catches against real denials.

- The weakest claim is "0 wrong clears, 287/287 match". The answer key was written by the same system that generated the data and rules, so it proves internal consistency, not catch rate on real charts. The model is a stub, so there is no measured model behaviour at all.
- Fit with the roadmap is good (cadence maps to scheduling, outreach to care management, claim and denial to RCM), and one engine feeding three desks is a real architecture point. The dollar figure ($86,846 open at risk) is built from placeholder allowed amounts, so it should not be quoted as savings.
- Payer policies are illustrative, and real policies are the moat and the maintenance burden. That cost is not sized.

## Blockers

None. No criterion fails; I found no finding on screen that is factually wrong for the record it cites (all dates, intervals, units, auth numbers, eyes and codes I read matched the synthetic bundle); no patient identifier reached a prompt or the log; the demo runs. The appeal deadline and step therapy points above are wrong about the real world but are labelled illustrative, so they are notes, not blockers. Fix the 180-day default before anyone calls it a Medicare deadline.

## Notes (most important first)

1. Appeal window is 180 days for every payer (`app/contracts.py:185`, `app/agent.py:262`); Medicare Part B is 120 and MA about 60. Make it per-payer policy data.
2. Step therapy has no established-patient exemption and runs even after the PA is approved (`app/rules.py:278`). Likely the biggest source of false alarms on a real MA book.
3. Role comes from the query string and defaults to owner (`app/api.py:_role`, `DecisionIn.role`). Masking is not access control; the token is the internal id.
4. CLM-06 proposes JZ blindly when both or neither modifier is present (`clm-0025`, `clm-0056`); it should hold for the biller.
5. The 10-code diagnosis table would turn most real charts into CLR-11 HOLDs; bilateral modifier 50 and biosimilars are unmodelled.
6. The "0 leaks" and "0 wrong clears" figures are self-referential (same author as the data); lapsed-eye outreach shows $0 at risk and goes to the scheduler only.
