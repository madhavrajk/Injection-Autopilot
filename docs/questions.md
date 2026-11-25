# Questions before building

The interview brief is open-ended ("build something useful for Visentia given our roadmap"), so nobody answered these
before the build. Each one has the answer I assumed, why, and the design decision it caused. **The first thing I would
do in the room is put these seven in front of the Visentia team and correct the assumptions.**

| # | Question | Assumed answer (and where it comes from) | Design decision it caused |
|---|---|---|---|
| 1 | Who uses it, and what decision does it feed? | Three desks in a retina practice: the scheduler ("who do I call"), the authorization coordinator ("is tomorrow's injection clinic safe to run"), the biller ("can this claim go out"). The owner wants one number: what is at risk. Source: visentia.ai names scheduling, care management and RCM agents as separate products. | One work queue per role over one shared engine, plus a Today view for the owner. |
| 2 | Which is worse: a miss or a false alarm? | A miss. A false alarm costs a coordinator a minute. A miss is a denied drug claim of about $1,500 to $2,400 that the practice already paid for, or a patient who drops off treatment. | Overflag. Nothing is shown as cleared unless every check passed; unknown means "needs review". |
| 3 | What must never be wrong? | Eye, drug, dose, dates, authorization number. A wrong eye is a never event in the clinic and a denial on the claim. | Code copies these from the structured record. No model writes them. Two sources that disagree go to a person: the agent proposes no fix. |
| 4 | What is the source of truth? | The EHR's structured record (procedure, drug administration, order), not the note and not the claim. The payer's written policy governs coverage. | Every finding cites the record and field it read. The claim is checked against the chart, never the reverse. Policies are data with a version, cited in each finding. |
| 5 | Who may see what? | Clinical and billing staff see names. Compliance and engineering see a token. A model vendor sees neither. | Names are masked on the server by role. Model prompts are redacted and scanned by a leak check. The audit log stores tokens only. |
| 6 | Is AI welcome, and at what cost? | Yes, where language is fuzzy; not where a date subtraction will do. Cost has to be cents per visit to make sense against a $105 injection fee. | Rules in code ($0). A model reads the free-text plan in the note and writes letter wording. Stub by default; cost per call is logged. |
| 7 | What can be cut? | Real EHR and payer connections, drug inventory, imaging, the patient app, ambient scribing. | Synthetic FHIR bundle in the real resource shapes, so the ingest is the part that gets swapped. The cut list is in the README. |

## Questions I would still ask in the room
1. Which EHR and practice-management system do your partner practices run (ModMed, Nextech IntelleChartPRO, other)? That decides whether ingest is FHIR R4, FHIR STU3 or a vendor API.
2. Where does prior authorization hurt most today: the initial request, renewals, or step-therapy exceptions? Research says about 96% are approved but about 60% are delayed more than a day, so I optimised for lead time, not for approval odds.
3. How is the treat-and-extend interval recorded in your charts: a structured order, or only in the note? If only in the note, the model step moves from a cross-check to the primary source and needs a much bigger eval set.
4. For bilateral same-day injections, do your payers want RT and LT on two lines or modifier 50? This varies by payer and I did not guess.
5. Who signs off on an appeal today, and how many a week does one biller get through?
