# Spec: rules, interfaces, data

The single source for what every module does. `app/contracts.py` holds the shapes; this file holds the behaviour.
"`as_of`" is `Dataset.as_of`: the rules' only notion of today. Every rule returns `Check` objects (pass or not), so
the screen can show what was checked, not only what failed. All dates in `detail` text use `contracts.fmt_date`.

Outcomes: **PASS**, **NOTE** (no status change), **ACTION** (specific problem, specific next step, `action` set),
**HOLD** (sources disagree or data is missing; `action` is `None`; a person decides).
Item status = `contracts.status_of(checks)`: hold beats action beats clear.

Every non-pass check carries `evidence`: the records and fields it read, values exactly as stored.

---

## 1. Cadence: `rules.check_cadence(plan: TreatmentPlan, ds: Dataset) -> list[Check]`
Returns exactly one check, with `eye = plan.eye`, label `"Injection cadence"`.

```
last   = newest completed injection for (patient, plan.eye)            # ds.injections_for(...)[0]
due    = next_due(last.date, plan.interval_weeks)
appt   = soonest booked injection appointment planning this eye, date >= as_of   # ds.booked_injections(...)[0]
policy = policy of the coverage active on (appt.date if appt else as_of); min_interval = policy.min_interval_days, or 28 if no coverage/policy
```
| Rule | When | Outcome | Action |
|---|---|---|---|
| CAD-00 | no completed injection for this eye | NOTE | none |
| CAD-03 | appt exists and `(appt.date - last.date).days < min_interval` | ACTION | `reschedule`, owner scheduler, `proposed_date = last.date + min_interval` |
| CAD-04 | appt exists (and not CAD-03) and `appt.date > due + ON_TIME_TOLERANCE_DAYS` | ACTION | `reschedule`, owner scheduler, `proposed_date = max(due, as_of)` |
| CAD-01 | appt exists otherwise | PASS | |
| CAD-05 | no appt and `as_of > due + LAPSE_DAYS` | ACTION | `outreach`, owner scheduler, `draft_kind = "outreach_sms"`, `proposed_date = None` |
| CAD-02 | no appt (and not CAD-05) and `due <= as_of + BOOKING_HORIZON_DAYS` | ACTION | `book`, owner scheduler, `proposed_date = max(due, as_of)` |
| CAD-01 | no appt otherwise (not due yet) | PASS | |

## 2. Clearance: `rules.check_clearance(appt: Appointment, ds: Dataset) -> list[Check]`
`dos = appt.date`. One CLR-01 check for the appointment (`eye=None`), then the per-line checks for each
`appt.planned` entry (`eye` set), in the order below.

**CLR-01 Eligibility** (label "Eligibility"). `cov = ds.coverage_on(patient, dos)`.
- PASS when a coverage is active on `dos`.
- ACTION `verify_eligibility` (owner auth_coordinator) when none is; evidence = `ds.latest_coverage` period, if any.
  With no coverage there is no policy: skip CLR-04..08 and CLR-10 for every line; CLR-03 uses 28 days.

**CLR-02 Order match** (label "Matches the order"). `plan = ds.plan_for(patient, eye)`.
- HOLD when there is no plan for that eye, or `plan.drug != line.drug`. Otherwise PASS.

**CLR-03 Interval** (label "Minimum interval"). `last` = newest same-eye injection with `date < dos`.
- PASS when there is none ("first injection in this eye") or `(dos - last.date).days >= policy.min_interval_days`.
- ACTION `reschedule` (owner scheduler, `proposed_date = last.date + min_interval_days`) otherwise.

**CLR-04..08 Prior authorization** (label "Prior authorization"). Exactly one check per line.
- Drug not in `policy.pa_required`: PASS with rule_id CLR-04, detail says none is required and cites `policy.version`.
- Otherwise look at the patient's auths with `payer_id == cov.payer_id`, and take the first that applies:

| Order | Condition | Rule | Outcome | Action (owner auth_coordinator) |
|---|---|---|---|---|
| 1 | some auth `covers(drug, eye, dos)` | CLR-04 | PASS | |
| 2 | a `pending` auth for this drug and eye | CLR-08 | ACTION | `expedite_pa`; detail gives `requested_on`, the expected decision date (`requested_on + pa_turnaround_days`) and whether that is after `dos` |
| 3 | an `approved` auth for this drug and eye whose window does not contain `dos` | CLR-06 | ACTION | `renew_pa`, `draft_kind="pa_request"` |
| 4 | an `approved` auth for this drug and eye, window ok, `injections_used >= injections_approved` | CLR-07 | ACTION | `renew_pa`, `draft_kind="pa_request"` |
| 5 | an `approved` auth exists for this patient and payer but for another eye or another drug | CLR-05 | ACTION | `request_pa`, `draft_kind="pa_request"`; detail says what the auth covers and what the visit is |
| 6 | nothing | CLR-04 | ACTION | `request_pa`, `draft_kind="pa_request"` |

**CLR-09 Authorization running out** (label "Authorization runway"). Only when order 1 passed with auth `a`:
NOTE (action `renew_pa`) when `(a.valid_to - dos).days <= PA_EXPIRY_NOTE_DAYS`, or when
`a.injections_approved is not None and a.injections_used + 1 >= a.injections_approved` (this visit uses the last one).
Emit nothing otherwise.

**CLR-10 Step therapy** (label "Step therapy"). Only when `policy.step_therapy` is set and `line.drug != first_line`.
- PASS when the patient has `>= min_doses` completed injections of `first_line` with `date < dos` (any eye), or an
  approved auth for this payer and drug has `step_therapy_exception = True`.
- ACTION `step_exception` (owner physician, `draft_kind="step_exception"`) otherwise; detail gives the count.

**CLR-11 Diagnosis laterality** (label "Diagnosis matches the eye"). Only when a plan exists for the eye.
- HOLD when `dx_eyes(plan.diagnosis_code)` is `None` (code not in the table) or does not include the eye. Else PASS.

**CLR-12 Note agrees with the order** (label "Clinic note agrees with the order"), added by the agent, not by
`check_clearance`: `rules.reconcile_plan(plan: TreatmentPlan, extracted: list[ExtractedPlan], note: ClinicNote) -> Check`
- PASS when `note.date < plan.updated_on` (the order is newer than the note).
- PASS when no extracted entry is for `plan.eye`.
- HOLD when the entry's `drug` is set and differs from `plan.drug`, or its `interval_weeks` is set and differs from
  `plan.interval_weeks`. Evidence: `DocumentReference/<note.id>`, field `text`, value = the entry's `quote`; plus the
  plan field that disagrees. Otherwise PASS.
The agent runs it for each planned line whose eye has a plan, using `ds.latest_note(patient, on_or_before=as_of)`.

## 3. Claim check: `rules.check_claim(claim: Claim, ds: Dataset) -> list[Check]`
`injs = ds.injections_on(patient, claim.dos)`; `cov`/`policy` from the coverage active on `claim.dos`
(if none, use `ds.policy(claim.payer_id)`). All ACTION checks use `fix_claim`, owner biller, with `changes` rows
`{"line": seq, "field": ..., "from": ..., "to": ...}` (strings). The agent never applies a change; a biller approves it.

| Rule | Label | Not-pass condition | Outcome |
|---|---|---|---|
| CLM-01 | Chart sources agree | per injection: `inj.eye != inj.admin_eye` (Procedure site vs MedicationAdministration site) | HOLD |
| CLM-02 | Injection on record | claim has a 67028 line but `injs` is empty | HOLD (and stop: emit nothing else) |
| CLM-03 | Eye modifier | per injection: no 67028 line carries `EYE_MODIFIER[inj.eye]` | ACTION |
| CLM-04 | Drug code | per injection: no drug line has the expected HCPCS (`expected_drug_line(inj.drug, policy)`) | ACTION |
| CLM-05 | Drug units | the matched drug line's `units` differs from the expected units | ACTION |
| CLM-06 | Waste modifier | the matched drug line does not carry exactly one of JZ / JW | ACTION (`to: "JZ"`) |
| CLM-07 | NDC | the matched drug line's `ndc` is missing or not `\d{11}` or `\d{5}-\d{4}-\d{2}` | ACTION (`to: inj.ndc` when present) |
| CLM-08 | Diagnosis matches the eye | the matched 67028 line has no dx code whose `dx_eyes` include `inj.eye` | ACTION (`to:` the plan's dx when it fits the eye) |
| CLM-09 | Authorization number | policy requires PA for `inj.drug`: an approved auth for payer, drug and eye whose window contains `dos` exists, and `claim.auth_number` differs from its `auth_number` | ACTION (`to:` that number) |
| CLM-09 | Authorization number | PA required and no such auth exists | HOLD |
| CLM-10 | Modifier 25 | a line with a code in `EM_CODES` lacks modifier `25` while the claim has a 67028 line | ACTION |

Matching lines to injections: for each injection in `injs` whose sources agree (skip per-injection rules for one that
failed CLM-01), first pick the unused 67028 line carrying its eye modifier; if none, pair it with the first unused
67028 line (that is the CLM-03 case, `from` = that line's eye modifier or "", `to` = the right one); if no 67028 line is
left, CLM-03 with an empty `changes` list ("no injection line for this eye"). For the drug line: first the unused
line whose code equals the expected HCPCS (prefer one carrying the same eye modifier); if none, the first unused line
whose code is in `all_drug_codes(ds.policies)` (CLM-04, `from` that code, `to` expected; still run CLM-05..07 on it
against the expected units); if none at all, CLM-04 with empty `changes` and skip CLM-05..07.
Each rule that was evaluated and held emits a PASS check (per injection for CLM-01, 03..09; once for CLM-10 when
an E/M line exists).

## 4. Denial triage: `rules.triage_denial(denial: Denial, ds: Dataset) -> list[Check]`
Returns exactly one check, label `"Denial root cause"`. `claim = ds.claim(denial.claim_id)`, policy as in section 3.
Every detail starts with the code and its meaning, e.g. `"CO-197 (Precertification or authorization absent): ..."`.

| CARC | Finding | Rule | Outcome | Action (owner biller) |
|---|---|---|---|---|
| any | claim not found | DEN-00 | HOLD | |
| 197 | an approved auth for the payer matches an injection that day (drug, eye, window contains dos) and the claim's `auth_number` differs or is missing | DEN-02 | ACTION | `fix_claim`: corrected claim with the number (`changes` row for `auth_number`) |
| 197 | such an auth exists and the claim already carries its number | DEN-03 | ACTION | `appeal`, `draft_kind="appeal"` |
| 197 | no such auth | DEN-04 | HOLD | |
| 4, 11, 16 | `check_claim` returns at least one ACTION and no HOLD | DEN-05 | ACTION | `fix_claim` with all `changes` from those checks |
| 4, 11, 16 | `check_claim` returns a HOLD, or nothing wrong | DEN-06 | HOLD | |
| 151 | for some injection that day, the previous same-eye injection is `< min_interval_days` earlier | DEN-07 | HOLD (the denial looks correct) | |
| 151 | otherwise | DEN-08 | ACTION | `appeal`, `draft_kind="appeal"`; detail gives both dates and the gap |
| 50 | always | DEN-09 | ACTION | `appeal`, `draft_kind="appeal"` |
| 27 | a coverage for the claim's payer was active on `dos` | DEN-10 | ACTION | `appeal`, `draft_kind="appeal"` |
| 27 | not active | DEN-11 | ACTION | `verify_eligibility` (owner auth_coordinator) |
| 29 or any other | | DEN-12 | HOLD | |

---

## 5. Model layer: `app/llm.py`
Stub-first. With no `ANTHROPIC_API_KEY`, or with `AUTOPILOT_MODEL=stub`, the deterministic stub runs; tests and evals
always run the stub. Every call returns a `ModelCall` (task, model, tokens, cost, ms). The prompt of every call (stub
or real) is appended to `llm.PROMPT_LOG` (a list of `{"task", "prompt"}` dicts) so the leak check can read them.

```python
def redact(text: str, patient: Patient, coverage: Coverage | None = None) -> str
    # replaces the patient's name (full name and each name part as a whole word), birth date (ISO, MM/DD/YYYY and
    # "Mon D, YYYY" forms), phone and member ID with the patient token / "[DOB]" / "[PHONE]" / "[MEMBER]"

def extract_plan(note: ClinicNote, patient: Patient, coverage: Coverage | None = None) -> tuple[list[ExtractedPlan], ModelCall]
    # redact first; the model (or stub) sees only the redacted text. Each returned entry must have a `quote` that is
    # a substring of the redacted note text and an eye; entries that fail either test are dropped (grounding check).

def draft(kind: str, facts: dict[str, str]) -> Draft
    # kind: "pa_request" | "appeal" | "outreach_sms" | "step_exception"
    # The model writes a TEMPLATE: prose with {placeholders} named after keys of `facts`. The prompt contains the
    # kind, the list of fact KEYS and nothing else from the record. Code renders the template with `facts`.
    # Returns a Draft that has been through verify_draft.

def verify_template(template: str, facts: dict[str, str]) -> list[str]
    # Problems found in the model's template BEFORE rendering. Empty list = clean. Rules:
    #  - every {placeholder} is a key of facts
    #  - outside placeholders the template contains no digit, no drug brand or generic name, and no eye word
    #    (OD, OS, OU, "right eye", "left eye", "both eyes")  -> the model wrote a fact it must not write
    # A Draft with problems has verified=False and is never shown as ready to send.
```
Fact keys the agent passes (all strings, copied from the record): `patient_name`, `birth_date`, `member_id`,
`patient_token`, `payer_name`, `plan_name`, `practice`, `physician`, `drug_brand`, `drug_generic`, `hcpcs`, `eye`,
`eye_words` ("right eye"), `diagnosis_code`, `diagnosis_label`, `visit_date`, `last_injection_date`,
`interval_weeks`, `auth_number`, `claim_id`, `denial_code`, `denial_reason`, `denial_date`, `appeal_deadline`,
`amount`, `finding`, `proposed_date`. A kind uses the subset it needs; missing keys are simply not available.

**Clinic note grammar** (the synthetic notes use it, and the stub must read every form). A plan sentence names a
drug and an eye and usually an interval:
- drug: brand (`Eylea`, `Eylea HD`, `Vabysmo`, `Lucentis`, `Avastin`) or generic (`aflibercept 2 mg`,
  `aflibercept 8 mg`, `faricimab`, `ranibizumab`, `bevacizumab`), any letter case. `Eylea HD` must not be read as `Eylea`.
- eye: `OD`, `OS`, `right eye`, `left eye`. `OU` / `both eyes` yields one entry per eye.
- interval: `q8w`, `q 8 wk`, `every 8 weeks`, `8-week interval`, `extend to 10 weeks`, `monthly` (= 4).
- verbs: `continue`, `switch to`, `start`, `extend`, `maintain`. Example lines:
  `Plan: continue Eylea OD, extend to 10 weeks.` · `Switch OS to Vabysmo, q4w x3.` ·
  `Right eye: maintain aflibercept 8 mg every 12 weeks.`
- A sentence about history (`previously`, `prior`, `was on`, `s/p`, `history of`) is not a plan and yields nothing.
Real mode sends the redacted note to the model and asks for JSON entries with a verbatim quote; the same grounding
check applies to whatever comes back.

Pricing for the cost column (per million tokens, input/output) lives in one table in `app/llm.py`.

## 6. Synthetic data: `app/synth.py` and `app/ingest.py`
`python -m app.synth [--seed 7] [--as-of 2026-11-18]` writes, deterministically:
- `data/synthetic/bundle.json`: a FHIR R4 `Bundle` (type `collection`, `timestamp` = as_of) of the resources below
- `data/synthetic/policies.json`: the four illustrative `PayerPolicy` objects
- `data/synthetic/manifest.json`: `{"as_of", "seed", "practice", "counts", "planted": [...]}`; each planted entry is
  `{"item_id": "clearance:appt-0031", "stage", "expect_status", "expect_rules": ["CLR-06"], "why": "..."}`.
  `expect_rules` = the rule ids whose outcome is ACTION or HOLD. **Every item not listed is expected to be clear.**

`ingest.load(dir="data/synthetic") -> Dataset` reads the three files through the mapping below. A value that fails
validation (unknown drug or eye code, bad date, dangling reference) is recorded in `Dataset.flags` and the record is
kept when its shape allows; nothing is repaired.

FHIR mapping (system URIs: drug `urn:visentia:drug`, eye `http://snomed.info/sct` with `EYE_SNOMED`, extensions
`urn:visentia:<name>`):

| Contract | Resource | Fields |
|---|---|---|
| Patient | `Patient` | `id`; `name[0].given[0] + " " + name[0].family`; `birthDate`; `telecom[0].value`; `identifier[system=urn:visentia:token].value` |
| Coverage | `Coverage` | `id`; `beneficiary.reference`; `payor[0].reference = Organization/<payer_id>`; `class[0].name` = plan_name; `subscriberId`; `period.start`/`end` |
| PriorAuth | `ClaimResponse` with `use = "preauthorization"` | `id`; `patient.reference`; `insurer.reference`; `preAuthRef`; `preAuthPeriod.start`/`end`; `outcome`: `complete` approved, `queued` pending, `error` denied; extensions `pa-drug` (valueCode), `pa-eye` (valueCode OD/OS, repeated), `pa-injections-approved`, `pa-injections-used` (valueInteger), `pa-step-exception` (valueBoolean), `pa-requested-on` (valueDate) |
| TreatmentPlan | `MedicationRequest` with `intent = "plan"` | `id`; `subject.reference`; `medicationCodeableConcept.coding[0].code` = Drug value; `dosageInstruction[0].site.coding[0].code` = eye SNOMED; `dosageInstruction[0].timing.repeat.period` + `periodUnit = "wk"`; `reasonCode[0].coding[0].code` = ICD-10; `requester.display`; `authoredOn` |
| Injection | `Procedure` + `MedicationAdministration` | Procedure: `id`, `subject`, `performedDateTime`, `code.coding[0].code = 67028`, `bodySite[0].coding[0].code` = eye SNOMED, extension `appointment` (valueReference). MedicationAdministration: `id`, `subject`, `effectiveDateTime`, `medicationCodeableConcept.coding[0].code` = Drug value and `coding[1]` = NDC (system `http://hl7.org/fhir/sid/ndc`), `dosage.dose.value` (mg), `dosage.site.coding[0].code` = eye SNOMED, `partOf[0].reference = Procedure/<id>` |
| Appointment | `Appointment` | `id`; `participant[0].actor.reference` = patient; `participant[1].actor.display` = provider; `start` (local ISO datetime); `status` booked/fulfilled/cancelled/noshow; `appointmentType.coding[0].code` injection/exam; extension `planned-injection` repeated, each with nested extensions `eye` and `drug` (valueCode); extension `location` (valueString) |
| ClinicNote | `DocumentReference` | `id`; `subject`; `date`; `author[0].display`; `content[0].attachment.data` (base64 of UTF-8 text) |
| Claim | `Claim` with `use = "claim"` | `id`; `patient`; `insurer`; `billablePeriod.start` = dos; extension `claim-status` (valueCode); `insurance[0].preAuthRef[0]`; `diagnosis[]` (`sequence`, `diagnosisCodeableConcept.coding[0].code`); `item[]`: `sequence`, `productOrService.coding[0].code`, `modifier[].coding[0].code`, `quantity.value`, `diagnosisSequence[]`, `net.value`, extension `ndc` (valueString); extension `appointment` |
| Denial | `ClaimResponse` with `use = "claim"`, `outcome = "error"` | `id`; `request.reference = Claim/<id>`; `created`; `item[0].itemSequence`; `item[0].adjudication[0]`: `reason.coding[0].code` = CARC, `amount.value`; extensions `carc-group`, `rarc` |

Payers (fictional names; the policies are illustrative):
| payer_id | name | PA required | Step therapy | Bevacizumab line |
|---|---|---|---|---|
| `medicare-ffs` | Medicare Part B (illustrative) | none | none | J7999 x1 |
| `ma-summit` | Summit Medicare Advantage | Eylea, Eylea HD, Vabysmo, Lucentis | bevacizumab x3 | J7999 x1 |
| `ma-harbor` | Harbor Advantage HMO | Eylea, Eylea HD, Vabysmo, Lucentis | none | C9257 x5 |
| `com-northfield` | Northfield Commercial PPO | Eylea HD, Vabysmo | none | J3590 x1, timely filing 180 days |
All: `min_interval_days = 28`, `pa_turnaround_days = 7`, `appeal_days = 180`, version like `"2026.2"`.

World to generate (about 140 patients, practice "Garden State Retina (synthetic)", two sites, three physicians):
a clean baseline in which every treated eye has a plan, an injection history at its interval, a next appointment on
time, valid coverage and authorization, and a claim that agrees with the chart; then planted defects, at least one for
**every** ACTION and HOLD rule in sections 1 to 4 and CLR-12, plus near misses that must stay clear (interval exactly
28 days; authorization ending on the visit date; `Eylea HD` in a note for an Eylea HD order; an old note that
predates a newer order; a bilateral same-day visit with RT and LT lines). Target volumes: about 55 booked injection
appointments within `as_of .. as_of + 7`, about 60 draft claims from the last 10 days, 14 to 18 denials, and roughly
one item in five not clear. Baseline cadence must be clean too: a plan with no planted defect has either an on-time
appointment or is not due within 14 days.

## 7. Agent, API, store (main session)
`app/agent.py`: `build_items(ds) -> list[WorkItem]`: one per plan (cadence), per booked injection appointment in the
clearance window, per draft claim, per denial. It calls the rules, the model layer, builds the trace and the draft,
and computes dollars at risk. `evaluate(stage, subject_id, ds) -> WorkItem` runs one subject (used by the evals).
`app/store.py`: decisions appended to `data/runtime/decisions.jsonl`, latest wins. `app/api.py`: FastAPI.
`app/verify.py`: independent cross-source check (reads the raw bundle, not `ingest`), manifest comparison, leak check.
