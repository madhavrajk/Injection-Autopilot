"""Shared contracts. Written first; every module builds against this file and nothing else.

Rules for everyone:
- Eye, drug, dose, dates, authorization numbers and member IDs are copied from the record byte for byte. Never
  normalise, re-case or "fix" them. A record that fails validation is kept and flagged.
- A model never writes one of those fields. Model output is a proposal that code checks against the record.
- Every Check names the records it read (Evidence.ref is "<ResourceType>/<id>", the id used in the FHIR bundle).
"""
from __future__ import annotations

from datetime import date, timedelta
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- enums

class Eye(str, Enum):
    OD = "OD"  # right eye
    OS = "OS"  # left eye


class Drug(str, Enum):
    AFLIBERCEPT_2MG = "aflibercept_2mg"
    AFLIBERCEPT_8MG = "aflibercept_8mg"
    FARICIMAB = "faricimab"
    RANIBIZUMAB = "ranibizumab"
    BEVACIZUMAB = "bevacizumab"


class Stage(str, Enum):
    CADENCE = "cadence"      # scheduling agent: is each treated eye booked on a safe, payable date
    CLEARANCE = "clearance"  # care management agent: is the booked injection cleared to happen
    CLAIM = "claim"          # RCM agent: does the draft claim agree with the chart
    DENIAL = "denial"        # RCM agent: root cause and next step for a denied claim


class Outcome(str, Enum):
    PASS = "pass"      # checked and fine
    NOTE = "note"      # worth knowing; does not change the status
    ACTION = "action"  # a specific problem with a specific next step
    HOLD = "hold"      # sources disagree or data is missing: a person decides, the agent proposes no fix


class Status(str, Enum):
    CLEAR = "clear"
    ACTION = "action"
    HOLD = "hold"


class Role(str, Enum):
    OWNER = "owner"                    # physician owner / practice administrator
    AUTH = "auth_coordinator"
    SCHEDULER = "scheduler"
    BILLER = "biller"
    PHYSICIAN = "physician"
    COMPLIANCE = "compliance"          # sees patient tokens only, never names


class ActionKind(str, Enum):
    VERIFY_ELIGIBILITY = "verify_eligibility"
    REQUEST_PA = "request_pa"
    RENEW_PA = "renew_pa"
    EXPEDITE_PA = "expedite_pa"
    STEP_EXCEPTION = "step_exception"
    RESCHEDULE = "reschedule"
    BOOK = "book"
    OUTREACH = "outreach"
    REVIEW_CHART = "review_chart"
    FIX_CLAIM = "fix_claim"
    APPEAL = "appeal"
    REVIEW_DENIAL = "review_denial"


# ---------------------------------------------------------------- reference tables

class DrugInfo(BaseModel):
    brand: str
    generic: str
    hcpcs: Optional[str]          # None for bevacizumab: the code depends on the payer (see PayerPolicy)
    unit_mg: Optional[float]      # mg per billing unit
    dose_mg: float                # standard intravitreal dose
    units_per_dose: Optional[int]
    est_allowed_usd: float        # illustrative allowed amount per dose, for "dollars at risk" only
    verified: bool                # True if code and unit size were read in a primary source (docs/domain_recon.md)


DRUGS: dict[Drug, DrugInfo] = {
    Drug.AFLIBERCEPT_2MG: DrugInfo(brand="Eylea", generic="aflibercept 2 mg", hcpcs="J0178", unit_mg=1.0, dose_mg=2.0,
                                   units_per_dose=2, est_allowed_usd=1464.0, verified=True),
    Drug.AFLIBERCEPT_8MG: DrugInfo(brand="Eylea HD", generic="aflibercept 8 mg", hcpcs="J0177", unit_mg=1.0, dose_mg=8.0,
                                   units_per_dose=8, est_allowed_usd=2410.0, verified=False),
    Drug.FARICIMAB: DrugInfo(brand="Vabysmo", generic="faricimab 6 mg", hcpcs="J2777", unit_mg=0.1, dose_mg=6.0,
                             units_per_dose=60, est_allowed_usd=2100.0, verified=True),
    Drug.RANIBIZUMAB: DrugInfo(brand="Lucentis", generic="ranibizumab 0.5 mg", hcpcs="J2778", unit_mg=0.1, dose_mg=0.5,
                               units_per_dose=5, est_allowed_usd=1100.0, verified=False),
    Drug.BEVACIZUMAB: DrugInfo(brand="Avastin", generic="bevacizumab 1.25 mg", hcpcs=None, unit_mg=None, dose_mg=1.25,
                               units_per_dose=None, est_allowed_usd=70.0, verified=False),
}

INJECTION_CPT = "67028"
INJECTION_FEE_USD = 105.0                      # illustrative
EM_CODES = {"99212", "99213", "99214", "99215", "92012", "92014"}   # same-day visit codes that need modifier 25
EYE_MODIFIER = {Eye.OD: "RT", Eye.OS: "LT"}
WASTE_MODIFIERS = {"JZ", "JW"}                 # single-dose vial: exactly one of these on the drug line
EYE_SNOMED = {Eye.OD: "8966001", Eye.OS: "18944008"}   # right eye structure, left eye structure

# ICD-10-CM codes used by the prototype: code -> (label, eyes the code covers)
DX_CODES: dict[str, tuple[str, frozenset[Eye]]] = {
    "H35.3211": ("Exudative AMD, right eye, active CNV", frozenset({Eye.OD})),
    "H35.3221": ("Exudative AMD, left eye, active CNV", frozenset({Eye.OS})),
    "H35.3231": ("Exudative AMD, bilateral, active CNV", frozenset({Eye.OD, Eye.OS})),
    "E11.3211": ("Type 2 diabetes, mild NPDR with macular edema, right eye", frozenset({Eye.OD})),
    "E11.3212": ("Type 2 diabetes, mild NPDR with macular edema, left eye", frozenset({Eye.OS})),
    "E11.3213": ("Type 2 diabetes, mild NPDR with macular edema, bilateral", frozenset({Eye.OD, Eye.OS})),
    "H34.8110": ("Central retinal vein occlusion with macular edema, right eye", frozenset({Eye.OD})),
    "H34.8120": ("Central retinal vein occlusion with macular edema, left eye", frozenset({Eye.OS})),
    "H34.8310": ("Branch retinal vein occlusion with macular edema, right eye", frozenset({Eye.OD})),
    "H34.8320": ("Branch retinal vein occlusion with macular edema, left eye", frozenset({Eye.OS})),
}

# Claim adjustment reason codes handled by denial triage: code -> plain meaning
CARC: dict[str, str] = {
    "197": "Precertification or authorization absent",
    "4": "Procedure code inconsistent with the modifier, or a required modifier is missing",
    "11": "Diagnosis inconsistent with the procedure",
    "16": "Claim lacks information needed for adjudication",
    "151": "Payer says the information does not support this frequency of services",
    "50": "Not deemed a medical necessity by the payer",
    "27": "Expenses incurred after coverage terminated",
    "29": "Time limit for filing has expired",
}

# Cadence tolerances (days)
ON_TIME_TOLERANCE_DAYS = 7     # booked later than due + this -> "late"
LAPSE_DAYS = 14                # overdue by more than this with nothing booked -> "lapsed"
BOOKING_HORIZON_DAYS = 14      # due within this many days with nothing booked -> "due, not booked"
CLEARANCE_WINDOW_DAYS = 7      # clearance looks at injection appointments from as_of through as_of + this
PA_EXPIRY_NOTE_DAYS = 30       # authorization ends within this many days after the visit -> note


# ---------------------------------------------------------------- records (inputs)

class Patient(BaseModel):
    id: str                 # FHIR Patient id, e.g. "pt-0042"
    token: str              # non-identifying label shown to roles without PHI access, e.g. "PT-0042"
    name: str               # PHI
    birth_date: date        # PHI
    phone: Optional[str] = None  # PHI


class Coverage(BaseModel):
    id: str
    patient_id: str
    payer_id: str
    plan_name: str
    member_id: str          # PHI
    period_start: date
    period_end: Optional[date] = None   # None = open ended

    def active_on(self, d: date) -> bool:
        return self.period_start <= d and (self.period_end is None or d <= self.period_end)


class StepTherapy(BaseModel):
    first_line: Drug = Drug.BEVACIZUMAB
    min_doses: int = 3      # documented first-line injections (any eye) before another drug is covered


class PayerPolicy(BaseModel):
    """Illustrative policy written for the prototype. Not a real payer's policy."""
    payer_id: str
    name: str
    kind: str                               # "medicare_ffs" | "medicare_advantage" | "commercial"
    version: str                            # cited in every check that uses the policy
    pa_required: list[Drug] = []
    step_therapy: Optional[StepTherapy] = None
    min_interval_days: int = 28             # same eye, any anti-VEGF drug
    pa_turnaround_days: int = 7             # standard decision time
    bevacizumab_hcpcs: str = "J7999"
    bevacizumab_units: int = 1
    timely_filing_days: int = 365
    appeal_days: int = 180


class PriorAuth(BaseModel):
    id: str
    patient_id: str
    payer_id: str
    auth_number: str
    drug: Drug
    eyes: list[Eye]
    status: str                             # "approved" | "pending" | "denied"
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    injections_approved: Optional[int] = None
    injections_used: int = 0
    step_therapy_exception: bool = False
    requested_on: Optional[date] = None

    def covers(self, drug: Drug, eye: Eye, d: date) -> bool:
        """Approved, right drug, right eye, date inside the window, at least one injection left."""
        return (self.status == "approved" and self.drug == drug and eye in self.eyes
                and self.valid_from is not None and self.valid_to is not None
                and self.valid_from <= d <= self.valid_to
                and (self.injections_approved is None or self.injections_used < self.injections_approved))


class TreatmentPlan(BaseModel):
    """The structured order for one eye (FHIR CarePlan / MedicationRequest)."""
    id: str
    patient_id: str
    eye: Eye
    diagnosis_code: str
    drug: Drug
    interval_weeks: int
    physician: str
    updated_on: date


class Injection(BaseModel):
    """One completed injection in one eye. Two structured sources describe it: the Procedure and the
    MedicationAdministration. They should agree; when they do not, nobody guesses."""
    id: str                                 # Procedure id
    patient_id: str
    date: date
    eye: Eye                                # Procedure.bodySite
    drug: Drug                              # MedicationAdministration.medication
    dose_mg: float
    admin_eye: Eye                          # MedicationAdministration.dosage.site
    medadmin_id: str
    appointment_id: Optional[str] = None
    ndc: Optional[str] = None


class PlannedInjection(BaseModel):
    eye: Eye
    drug: Drug


class Appointment(BaseModel):
    id: str
    patient_id: str
    date: date
    time: str = "09:00"
    kind: str                               # "injection" | "exam"
    status: str                             # "booked" | "fulfilled" | "cancelled" | "noshow"
    planned: list[PlannedInjection] = []
    provider: str = ""
    location: str = ""


class ClinicNote(BaseModel):
    id: str                                 # DocumentReference id
    patient_id: str
    date: date
    author: str
    text: str                               # free text; contains PHI


class ClaimLine(BaseModel):
    seq: int
    code: str                               # CPT or HCPCS
    modifiers: list[str] = []
    units: int = 1
    dx_codes: list[str] = []
    charge: float = 0.0
    ndc: Optional[str] = None


class Claim(BaseModel):
    id: str
    patient_id: str
    payer_id: str
    dos: date                               # date of service
    status: str                             # "draft" | "submitted" | "paid" | "denied"
    auth_number: Optional[str] = None
    lines: list[ClaimLine] = []
    appointment_id: Optional[str] = None

    @property
    def total(self) -> float:
        return round(sum(l.charge for l in self.lines), 2)


class Denial(BaseModel):
    id: str
    claim_id: str
    date: date
    group: str = "CO"
    carc: str
    rarc: Optional[str] = None
    amount: float
    line_seq: Optional[int] = None


class Flag(BaseModel):
    """A record that failed validation at ingest. Kept and shown, never fixed or dropped."""
    ref: str
    field: str
    value: str
    reason: str


class Dataset(BaseModel):
    as_of: date                             # "today" for every rule
    practice: str = ""
    patients: list[Patient] = []
    coverages: list[Coverage] = []
    policies: list[PayerPolicy] = []
    prior_auths: list[PriorAuth] = []
    plans: list[TreatmentPlan] = []
    injections: list[Injection] = []
    appointments: list[Appointment] = []
    notes: list[ClinicNote] = []
    claims: list[Claim] = []
    denials: list[Denial] = []
    flags: list[Flag] = []

    # -- lookups shared by every module (keep rules free of plumbing)
    def patient(self, patient_id: str) -> Optional[Patient]:
        return next((p for p in self.patients if p.id == patient_id), None)

    def coverage_on(self, patient_id: str, d: date) -> Optional[Coverage]:
        return next((c for c in self.coverages if c.patient_id == patient_id and c.active_on(d)), None)

    def latest_coverage(self, patient_id: str) -> Optional[Coverage]:
        cs = [c for c in self.coverages if c.patient_id == patient_id]
        return max(cs, key=lambda c: c.period_start) if cs else None

    def policy(self, payer_id: str) -> Optional[PayerPolicy]:
        return next((p for p in self.policies if p.payer_id == payer_id), None)

    def auths_for(self, patient_id: str) -> list[PriorAuth]:
        return [a for a in self.prior_auths if a.patient_id == patient_id]

    def plan_for(self, patient_id: str, eye: Eye) -> Optional[TreatmentPlan]:
        return next((p for p in self.plans if p.patient_id == patient_id and p.eye == eye), None)

    def injections_for(self, patient_id: str, eye: Optional[Eye] = None, before: Optional[date] = None) -> list[Injection]:
        """Completed injections, newest first. `before` is exclusive."""
        out = [i for i in self.injections if i.patient_id == patient_id
               and (eye is None or i.eye == eye) and (before is None or i.date < before)]
        return sorted(out, key=lambda i: i.date, reverse=True)

    def booked_injections(self, patient_id: str, eye: Eye, on_or_after: date) -> list[Appointment]:
        """Booked injection appointments that plan this eye, soonest first."""
        out = [a for a in self.appointments if a.patient_id == patient_id and a.kind == "injection"
               and a.status == "booked" and a.date >= on_or_after and any(p.eye == eye for p in a.planned)]
        return sorted(out, key=lambda a: a.date)

    def latest_note(self, patient_id: str, on_or_before: Optional[date] = None) -> Optional[ClinicNote]:
        ns = [n for n in self.notes if n.patient_id == patient_id and (on_or_before is None or n.date <= on_or_before)]
        return max(ns, key=lambda n: n.date) if ns else None

    def claim(self, claim_id: str) -> Optional[Claim]:
        return next((c for c in self.claims if c.id == claim_id), None)

    def injections_on(self, patient_id: str, d: date) -> list[Injection]:
        return [i for i in self.injections if i.patient_id == patient_id and i.date == d]


# ---------------------------------------------------------------- outputs

class Evidence(BaseModel):
    ref: str                # "Coverage/cov-0042": must resolve to a record in the bundle
    field: str              # the field that was read, e.g. "period.end"
    value: str              # the value exactly as stored


class ProposedAction(BaseModel):
    kind: ActionKind
    owner: Role
    summary: str                            # one line a coordinator can act on
    proposed_date: Optional[date] = None    # for reschedule / book
    changes: list[dict] = []                # for fix_claim: {"line": 2, "field": "units", "from": "2", "to": "8"}
    draft_kind: Optional[str] = None        # "pa_request" | "appeal" | "outreach_sms" | "step_exception"


class Check(BaseModel):
    rule_id: str                            # e.g. "CLR-03" (docs/spec.md)
    label: str                              # short name of the check, e.g. "Prior authorization"
    outcome: Outcome
    detail: str                             # one plain sentence with the values that decided it
    eye: Optional[Eye] = None
    evidence: list[Evidence] = []
    action: Optional[ProposedAction] = None


class ExtractedPlan(BaseModel):
    """What the model read in a clinic note. A proposal: code compares it with the structured plan."""
    eye: Eye
    drug: Optional[Drug] = None
    interval_weeks: Optional[int] = None
    quote: str                              # the exact span of the note it came from; must be a substring


class ModelCall(BaseModel):
    task: str                               # "extract_plan" | "draft_pa_request" | ...
    model: str                              # "stub" or a model id
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    ms: float = 0.0


class Draft(BaseModel):
    kind: str                               # "pa_request" | "appeal" | "outreach_sms" | "step_exception"
    subject: str
    body: str                               # rendered text; facts were inserted by code, not by the model
    facts: dict[str, str] = {}              # every fact the body may state, copied from the record
    verified: bool = False                  # verify_draft found no fact in the body that is not in `facts`
    problems: list[str] = []
    call: Optional[ModelCall] = None


class TraceStep(BaseModel):
    n: int
    kind: str                               # "code" | "model" | "person"
    name: str                               # e.g. "Eligibility"
    story: str                              # plain English: what was read and what it meant
    ms: float = 0.0
    model: Optional[str] = None
    tokens: int = 0
    cost_usd: float = 0.0


class WorkItem(BaseModel):
    id: str                                 # "<stage>:<subject id>", e.g. "clearance:appt-0031"
    stage: Stage
    subject_ref: str                        # "Appointment/appt-0031"
    patient_id: str
    patient_token: str
    patient_name: Optional[str] = None      # filled only for roles allowed to see it
    title: str                              # e.g. "Eylea HD · OS"
    subtitle: str = ""                      # e.g. payer / plan
    when: Optional[date] = None             # visit date, due date, date of service, or appeal deadline
    time: Optional[str] = None
    status: Status
    headline: str                           # the one reason, or "All checks passed"
    owner: Optional[Role] = None            # who acts next
    dollars_at_risk: float = 0.0
    checks: list[Check] = []
    draft: Optional[Draft] = None
    trace: list[TraceStep] = []
    facts: dict[str, str] = {}              # display facts for the detail panel (payer, eye, drug, dates)
    decision: Optional["Decision"] = None   # latest human decision, if any


class Decision(BaseModel):
    item_id: str
    action: str                             # "approve" | "edit" | "reject"
    role: Role
    note: str = ""
    at: str = ""                            # ISO timestamp, set by the store
    status_at_decision: Optional[Status] = None
    headline_at_decision: str = ""


WorkItem.model_rebuild()


# ---------------------------------------------------------------- pure helpers

def status_of(checks: list[Check]) -> Status:
    """Hold beats action beats clear. Notes and passes do not change the status."""
    outcomes = {c.outcome for c in checks}
    if Outcome.HOLD in outcomes:
        return Status.HOLD
    if Outcome.ACTION in outcomes:
        return Status.ACTION
    return Status.CLEAR


def next_due(last_injection: date, interval_weeks: int) -> date:
    return last_injection + timedelta(days=7 * interval_weeks)


def expected_drug_line(drug: Drug, policy: Optional[PayerPolicy]) -> tuple[Optional[str], Optional[int]]:
    """(HCPCS code, units) expected on the claim for one standard dose. Bevacizumab depends on the payer."""
    info = DRUGS[drug]
    if drug == Drug.BEVACIZUMAB:
        return (policy.bevacizumab_hcpcs, policy.bevacizumab_units) if policy else (None, None)
    return info.hcpcs, info.units_per_dose


def all_drug_codes(policies: list[PayerPolicy]) -> set[str]:
    codes = {i.hcpcs for i in DRUGS.values() if i.hcpcs}
    return codes | {p.bevacizumab_hcpcs for p in policies}


def dx_eyes(code: str) -> Optional[frozenset[Eye]]:
    """Eyes an ICD-10 code covers, or None when the code is not in the table (never guessed from the digits)."""
    entry = DX_CODES.get(code)
    return entry[1] if entry else None


def fmt_date(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def mask(item: WorkItem, role: Role, name: Optional[str]) -> WorkItem:
    """Minimum necessary: compliance sees the token only."""
    item.patient_name = None if role == Role.COMPLIANCE else name
    return item
