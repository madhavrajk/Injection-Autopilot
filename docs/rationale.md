# Rationale

Appended as the build went, one entry per decision: decision, assumption, why, tradeoff.

## Design restated
> Users are the scheduler, the authorization coordinator and the biller of a retina practice. They need to know,
> before the patient is in the chair and before the claim goes out, which injection visits will go wrong and what to
> do about each. The worst failure is a wrong eye, drug, dose, date or authorization number, or a visit shown as
> cleared that was not. So code checks every visit against the structured record and the payer's policy, a model
> only reads note language and writes letter wording, and a person approves anything that leaves the practice.

**Metric:** wrong clears (said fine when a person was needed). **Zero-tolerance line:** 0 wrong clears, 0 identifiers
that differ from the source, 0 PHI in prompts or logs. **Cut line:** live EHR and payer connections, inventory, imaging,
patient app, scribe.

Pipeline (C = code, M = model, P = person):
```
FHIR bundle ─C─► ingest: validate, flag, never repair ─► Dataset
   ─C─► rules: cadence · clearance · claim check · denial triage ─► checks with evidence
   ─M─► read the clinic note's plan sentence (redacted) ─C─► compare with the structured order
   ─M─► letter wording as a template ─C─► fill facts from the record, fact-check the template
   ─P─► approve / edit / reject ─C─► append-only decision log ─► candidate eval cases
raw bundle ─C─► verify: independent re-read, answer key for every item, leak scan
```

## Decisions

### D1. The unit of work is the injection visit, not the agent
- **Decision:** one engine that follows eye + drug + payer + date through four checkpoints; the "scheduling", "care
  management" and "RCM" agents on Visentia's site are three queues over it.
- **Why:** the same four facts decide all four checkpoints. Research found vendors for each checkpoint and nobody
  joining them (`docs/research.md`). The errors live in the seams.
- **Tradeoff:** broader than one deep feature. Mitigated by one shared contract (`Check`) so each stage is about 150 lines of rules.

### D2. Rules in code; a model only where language is fuzzy
- **Decision:** intervals, windows, units and modifiers are plain functions. The model reads the note's plan sentence
  and writes letter wording. It never writes an eye, drug, dose, date or number.
- **Assumption:** the treat-and-extend interval exists as a structured order. If a practice keeps it only in the
  note, the model step becomes the primary source and needs a far larger eval set (question 3 in `docs/questions.md`).
- **Why:** a date subtraction that is right every time costs nothing. A wrong eye is a never event.
- **Tradeoff:** less "AI" on screen. The Trust page shows where the model runs and what it costs.

### D3. Disagreement is a hold, never a guess
- **Decision:** when the procedure record and the drug administration record name different eyes, or the note and the
  order name different drugs, the item goes on hold with `action = None`.
- **Why:** an agent that picks a winner between two clinical records is making a clinical decision.
- **Tradeoff:** more holds for a physician. They are the most valuable items in the product.

### D4. Letters are templates; facts are filled by code
- **Decision:** the model returns prose with `{placeholders}`. Its prompt holds the letter kind and the fact keys,
  never a value. Code renders it, and `verify_template` rejects any digit, drug name or eye word outside a placeholder.
  An unverified draft cannot be approved (the API refuses).
- **Why:** it makes "the model cannot state a wrong fact" a property that is tested, and it means no PHI is sent to a
  model for drafting at all.
- **Tradeoff:** letters are less tailored than free generation. The `finding` sentence carries the specifics.

### D5. Overflag, and show what passed
- **Decision:** every rule returns a check whether it passed or not. Status is clear only when all pass.
- **Why:** a miss costs a drug claim or a treatment gap; a false alarm costs a minute. Showing the passed checks is
  what lets a coordinator trust "Cleared".

### D6. Payer policy is versioned data
- **Decision:** `PayerPolicy` carries the authorization list, step therapy, minimum interval and the bevacizumab code;
  every finding that used a policy cites its version.
- **Why:** recon showed bevacizumab coding differs by payer (J7999, C9257, J3590) and that MACs changed interval edits
  without publishing them. Hard-coding either would be wrong on day one.
- **Tradeoff:** someone has to maintain the policies. That is a real operating cost and a moat.

### D7. FastAPI and a no-build front end instead of Streamlit
- **Decision:** a JSON API (the same one a ModMed or Nextech integration would call) and one HTML, CSS and JS file each.
- **Why:** the brief is a product for clinic staff: a drawer, keyboard navigation, role masking on the server and a
  schedule timeline are the product, and Streamlit fights all four. No bundler means `uvicorn` is the whole setup.
- **Tradeoff:** about 600 lines of hand-written front end; no component library.

### D8. Synthetic data in real FHIR shapes, with an answer key
- **Decision:** the generator writes a FHIR R4 bundle and records every defect it planted. A separate verifier
  re-reads the raw bundle and compares every item with that key.
- **Why:** no real PHI may be used, and the second source has to be something the rules were not built from.
- **Tradeoff:** synthetic notes follow a known grammar, so the note reader looks better here than it would on real
  dictation. Said plainly in the README.

### D9. Concerns raised while writing the blind cases (kept as written, recorded)
- CLR-09 stays a note, not an action: an authorization that ends on the visit day still covers the visit. The note
  proposes the renewal.
- CLM-09 ignores `injections_used` on purpose: by claim time the visit itself has used an injection.
- CARC 27 with active coverage is drafted as an appeal; a wrong payer on the claim is the other possibility and the
  biller sees both records.
- Interval rules are per eye, so a same-day bilateral visit is two independent checks.

### D10. What the full check and the reviews changed
- **Evidence defect (found by `app.verify`, not by the tests):** a missing drug line was cited as
  `productOrService = "none"`, a value in no record. Test first (`tests/test_agent.py`), then the finding was changed
  to cite the codes the claim does carry.
- **Design review:** the Today list now puts the soonest injection visit first (it had put next week's holds above
  today's uncleared visits); muted text and field borders raised to 5.0 and 3.3 contrast; 12-hour times; one name
  ("Open") for the filter; the draft letter grows to its content.
- **Panel:** appeal windows are per payer (120 days Medicare, 60 Medicare Advantage, 180 commercial, all
  illustrative) instead of 180 for everyone; the waste-modifier fix no longer assumes JZ; a request with no role gets
  no names.
- **Not changed, on purpose:** the step-therapy exemption for established patients. It changes a rule that the blind
  cases label, so it needs the spec, the cases and the answer key revised together, by someone who knows how the
  payers apply it. Recorded as a limit.

## Check log
| Milestone | Tests | Blind cases | Full check | Note |
|---|---|---|---|---|
| Contracts | collects | n/a | n/a | contracts, spec, criteria |
| First end-to-end run | 194 | 52/52 | 287/287 vs key (ad hoc) | first end-to-end run matched the key |
| Hard cases | 210 | 52/52 | FAIL: 1 bad evidence row of 1,580 | found by the verifier; test added, then fixed |
| Hard cases | 210 | 52/52 | PASS: 0 / 16,092 fields, 1,581 evidence rows, 287/287, 0 leaks | five fault injections pass |
| Interface | 210 | 52/52 | PASS | design pass: hidden drawer covered the page; KPI icons unsized; denial columns reworked |
| Review | 210 | 52/52 | PASS | 10/10 criteria, no blockers; one round of review fixes |
