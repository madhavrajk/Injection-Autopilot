# UX review: Injection Autopilot

> Written **before** one round of fixes. Problems 1, 2, 3, 5, 6 and 7 are fixed; see D10 in `docs/rationale.md`.

## 1. Scores

- **Owner's 10-second read: 7.** The lede states 55/34/21/$37,995, but the list omits both of today's uncleared visits.
- **Coordinator queue: 6.** "Next with" is clipped; the first row starts at y=384.
- **Trust: 8.** Findings show rule ID, record, field, value; Leak check passes on "0 audit lines scanned".
- **Visual craft: 7.** One accent, matching dark theme; counts repeat four times per screen.
- **Accessibility: 6.** Pills pair icon and word; input borders fail contrast; no dialog focus trap.

## 2. Functions to keep

| Area | Inventory |
|---|---|
| Shell | Rail (Today, 4 numbered stages with agent, open badge, Trust); synthetic-data footer; as-of chip; role select (6 roles, compliance hides names); skip link; toast; skeleton; error |
| Today | Lede; 4 stage cards (question, open/total, bar, legend, risk); Needs a person first; Waiting by role; Most common findings; checks-run line |
| Lists | 5 KPIs; filters Needs a person, Mine, ok-word, Decided, All, with counts; search (`/`); day groups; decided dimming; "+N more"; empty states; count line; arrows, j/k, Enter, Esc |
| Columns | Cadence: Patient, Eye and drug, timeline, Status, Finding, Next with. Clearance: Time, Patient, Injection, Payer, Status, Finding, Next with. Claim: Service date, Patient, Chart says, Payer, Charges, Status, Finding. Denials: Appeal by, Patient, Claim, Denied for, Amount, Status, Root cause |
| Timeline | Last, too-soon zone, due, on-time window, booked/bad, proposed, today, key |
| Drawer | Crumb with record ref; title; patient, token, date; status, at-risk, next-with chips; Schedule; What the agent found (evidence, changes table, Proposed/No fix proposed); Worth knowing; claim lines; draft, fact-check pill, provenance footer; checks passed; How the agent got here; Record; note; Approve, Edit draft, Reject; Mark resolved, Not an issue; decided banner |
| Trust | Blind cases, Answer key, Second-source, Leak check, Model use, What is real, Decision log |

## 3. Top 8 problems

1. **Today hides today** (today.png, `viewToday` `urgent`): Nov 22 holds outrank today's 10:30 visit. Fix: sort by `when`, then status.
2. **Clipped "Authorization coordina"** (clearance.png). Fix: drop `white-space: nowrap` from `.owner`.
3. **Contrast**: `--ink-3` is 4.39 on `--bg` (`.sec h3`, `.count`), 4.11 on `--sunken` (`.draft .df`), 4.10 on `--accent-soft`; `--line-strong` borders 1.63. Fix: `--ink-3: #586765` (5.04 minimum), `--line-strong: #84918E` (3.27).
4. **Biller-wrong copy** (denial.png): "CO-96 a reason code this prototype does not handle"; "Denied, recoverable" includes expired CO-29; "Waste modifier missing" when JZ and JW are both present. Fix: real CARC text; "Denied, open"; "Waste modifier conflict".
5. **24-hour times** ("13:30"). Fix: 12-hour format in `COLS.clearance.row` and the drawer header.
6. **Three names for one state**: "Needs a person", "Needs action", "need someone". Fix: `seg("open", "Open")`.
7. **Draft clipped mid-line, nested scroll** (clearance-drawer.png). Fix: `.draft textarea { field-sizing: content }`.
8. **Small clips**: placeholder "…payer, fir"; "Booked"/"Proposed" labels collide (cadence-drawer.png); "$0.0000". Fix: `.search input { width: 320px }`; offset `.lab`; hide zero cost.

## 4. Do not touch

1. Cadence timeline with the hatched "too soon to be paid" zone.
2. Evidence chips and "No fix proposed … does not pick a winner".
3. "Claim says / Chart supports" diff and the draft provenance footer.
