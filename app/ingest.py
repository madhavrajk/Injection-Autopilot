"""FHIR R4 Bundle -> Dataset (spec section 6 mapping).

Anything invalid goes to `Dataset.flags` and is never repaired. A record whose shape cannot be built (unknown drug or
eye code, bad date, missing field) is flagged and left out; a record with a dangling patient reference is kept as
stored and flagged. References from a denial to its claim are not checked here: triage (DEN-00) owns that case.
"""
from __future__ import annotations

import base64
import json
from datetime import date
from pathlib import Path

from app.contracts import (EYE_SNOMED, Appointment, Claim, ClaimLine, ClinicNote, Coverage, Dataset, Denial, Drug, Eye,
                           Flag, Injection, Patient, PayerPolicy, PlannedInjection, PriorAuth, TreatmentPlan)

URN = "urn:visentia:"
KNOWN = ("Patient", "Coverage", "ClaimResponse", "MedicationRequest", "Procedure", "MedicationAdministration",
         "Appointment", "DocumentReference", "Claim")
EYE_OF = {v: k for k, v in EYE_SNOMED.items()}
OUTCOME = {"complete": "approved", "queued": "pending", "error": "denied"}
APPT_STATUS = {"booked", "fulfilled", "cancelled", "noshow"}
CLAIM_STATUS = {"draft", "submitted", "paid", "denied"}
SHAPE_ERRORS = (KeyError, IndexError, TypeError, ValueError, AttributeError, StopIteration)


class Skip(Exception):
    """The problem was already flagged under another record's ref; drop this record quietly."""


class Bad(Exception):
    def __init__(self, field: str, value, reason: str):
        super().__init__(reason)
        self.field, self.value, self.reason = field, str(value), reason


def _date(v, field: str) -> date:
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise Bad(field, v, "not a valid date")


def _opt_date(v, field: str):
    return None if v is None else _date(v, field)


def _ext(r: dict, name: str) -> list[dict]:
    return [e for e in r.get("extension", []) if e.get("url") == URN + name]


def _one(r: dict, name: str) -> dict:
    return _ext(r, name)[0]


def _id(ref: dict, kind: str, field: str) -> str:
    t, _, i = str(ref["reference"]).partition("/")
    if t != kind or not i:
        raise Bad(field, ref["reference"], f"expected a {kind} reference")
    return i


def _drug(code, field: str) -> Drug:
    try:
        return Drug(code)
    except ValueError:
        raise Bad(field, code, "unknown drug code")


def _eye_sn(code, field: str) -> Eye:
    if code not in EYE_OF:
        raise Bad(field, code, "unknown eye code")
    return EYE_OF[code]


def _eye(code, field: str) -> Eye:
    try:
        return Eye(code)
    except ValueError:
        raise Bad(field, code, "unknown eye code")


def parse_bundle(bundle: dict, policies: list, as_of, practice: str = "") -> Dataset:
    if isinstance(as_of, str):
        as_of = date.fromisoformat(as_of)
    flags: list[Flag] = []
    by_type: dict[str, list[dict]] = {t: [] for t in KNOWN}
    for e in bundle.get("entry", []):
        r = e.get("resource") or {}
        t = r.get("resourceType")
        if t in by_type:
            by_type[t].append(r)
        else:
            flags.append(Flag(ref=f"{t}/{r.get('id')}", field="resourceType", value=str(t), reason="unknown resource type"))
    pids = {r.get("id") for r in by_type["Patient"]}
    pol = [PayerPolicy.model_validate(p) for p in policies]
    payers = {p.payer_id for p in pol}
    proc_ids = {r.get("id") for r in by_type["Procedure"]}
    appt_ids = {r.get("id") for r in by_type["Appointment"]}

    def flag(ref, field, value, reason):
        flags.append(Flag(ref=ref, field=field, value=str(value), reason=reason))

    def patient_ref(ref, r, field):
        pid = _id(r[field], "Patient", field)
        if pid not in pids:
            flag(ref, field, pid, "dangling reference: no such Patient")
        return pid

    def run(t, items, build):
        out = []
        for r in items:
            ref = f"{t}/{r.get('id')}"
            try:
                out.append(build(r, ref))
            except Skip:
                pass
            except Bad as b:
                flag(ref, b.field, b.value, b.reason)
            except SHAPE_ERRORS as ex:
                flag(ref, "shape", type(ex).__name__, "record is missing a field or has the wrong shape")
        return out

    def patient(r, ref):
        n = r["name"][0]
        token = next(i["value"] for i in r["identifier"] if i.get("system") == URN + "token")
        tel = r.get("telecom")
        return Patient(id=r["id"], token=token, name=f"{n['given'][0]} {n['family']}",
                       birth_date=_date(r["birthDate"], "birthDate"), phone=tel[0]["value"] if tel else None)

    def coverage(r, ref):
        pid = patient_ref(ref, r, "beneficiary")
        payer = _id(r["payor"][0], "Organization", "payor")
        if payer not in payers:
            flag(ref, "payor", payer, "dangling reference: no such payer policy")
        per = r["period"]
        return Coverage(id=r["id"], patient_id=pid, payer_id=payer, plan_name=r["class"][0]["name"],
                        member_id=r["subscriberId"], period_start=_date(per["start"], "period.start"),
                        period_end=_opt_date(per.get("end"), "period.end"))

    def auth(r, ref):
        pid = patient_ref(ref, r, "patient")
        if r["outcome"] not in OUTCOME:
            raise Bad("outcome", r["outcome"], "unknown outcome")
        per = r.get("preAuthPeriod", {})
        ints = lambda n: [e["valueInteger"] for e in _ext(r, n)]
        approved = ints("pa-injections-approved")
        req = _ext(r, "pa-requested-on")
        return PriorAuth(
            id=r["id"], patient_id=pid, payer_id=_id(r["insurer"], "Organization", "insurer"), auth_number=r["preAuthRef"],
            drug=_drug(_one(r, "pa-drug")["valueCode"], "pa-drug"),
            eyes=[_eye(e["valueCode"], "pa-eye") for e in _ext(r, "pa-eye")], status=OUTCOME[r["outcome"]],
            valid_from=_opt_date(per.get("start"), "preAuthPeriod.start"), valid_to=_opt_date(per.get("end"), "preAuthPeriod.end"),
            injections_approved=approved[0] if approved else None, injections_used=ints("pa-injections-used")[0],
            step_therapy_exception=_one(r, "pa-step-exception")["valueBoolean"],
            requested_on=_date(req[0]["valueDate"], "pa-requested-on") if req else None)

    def plan(r, ref):
        pid = patient_ref(ref, r, "subject")
        di = r["dosageInstruction"][0]
        rep = di["timing"]["repeat"]
        if rep["periodUnit"] != "wk":
            raise Bad("periodUnit", rep["periodUnit"], "interval is not in weeks")
        return TreatmentPlan(
            id=r["id"], patient_id=pid, eye=_eye_sn(di["site"]["coding"][0]["code"], "dosageInstruction.site"),
            diagnosis_code=r["reasonCode"][0]["coding"][0]["code"],
            drug=_drug(r["medicationCodeableConcept"]["coding"][0]["code"], "medicationCodeableConcept"),
            interval_weeks=int(rep["period"]), physician=r["requester"]["display"], updated_on=_date(r["authoredOn"], "authoredOn"))

    def denial(r, ref):
        a = r["item"][0]["adjudication"][0]
        seq = r["item"][0].get("itemSequence")
        grp = _ext(r, "carc-group")
        rarc = _ext(r, "rarc")
        return Denial(id=r["id"], claim_id=_id(r["request"], "Claim", "request"), date=_date(r["created"], "created"),
                      group=grp[0]["valueCode"] if grp else "CO", carc=a["reason"]["coding"][0]["code"],
                      rarc=rarc[0]["valueCode"] if rarc else None, amount=float(a["amount"]["value"]), line_seq=seq)

    def appointment(r, ref):
        pid = _id(r["participant"][0]["actor"], "Patient", "participant[0]")
        if pid not in pids:
            flag(ref, "participant[0]", pid, "dangling reference: no such Patient")
        kind = r["appointmentType"]["coding"][0]["code"]
        if r["status"] not in APPT_STATUS or kind not in ("injection", "exam"):
            raise Bad("status/type", f"{r['status']}/{kind}", "unknown appointment status or type")
        planned = [PlannedInjection(eye=_eye([x for x in e["extension"] if x["url"] == URN + "eye"][0]["valueCode"], "planned-injection.eye"),
                                    drug=_drug([x for x in e["extension"] if x["url"] == URN + "drug"][0]["valueCode"], "planned-injection.drug"))
                   for e in _ext(r, "planned-injection")]
        loc = _ext(r, "location")
        start = r["start"]
        return Appointment(id=r["id"], patient_id=pid, date=_date(start, "start"), time=start[11:16], kind=kind,
                           status=r["status"], planned=planned, provider=r["participant"][1]["actor"]["display"],
                           location=loc[0]["valueString"] if loc else "")

    def note(r, ref):
        pid = patient_ref(ref, r, "subject")
        return ClinicNote(id=r["id"], patient_id=pid, date=_date(r["date"], "date"), author=r["author"][0]["display"],
                          text=base64.b64decode(r["content"][0]["attachment"]["data"]).decode("utf-8"))

    def claim(r, ref):
        pid = patient_ref(ref, r, "patient")
        status = _one(r, "claim-status")["valueCode"]
        if status not in CLAIM_STATUS:
            raise Bad("claim-status", status, "unknown claim status")
        dx = {d["sequence"]: d["diagnosisCodeableConcept"]["coding"][0]["code"] for d in r.get("diagnosis", [])}
        lines = []
        for it in r["item"]:
            ndc = [e["valueString"] for e in it.get("extension", []) if e.get("url") == URN + "ndc"]
            lines.append(ClaimLine(
                seq=it["sequence"], code=it["productOrService"]["coding"][0]["code"],
                modifiers=[m["coding"][0]["code"] for m in it.get("modifier", [])], units=int(it["quantity"]["value"]),
                dx_codes=[dx[s] for s in it.get("diagnosisSequence", [])], charge=float(it["net"]["value"]),
                ndc=ndc[0] if ndc else None))
        pre = r["insurance"][0].get("preAuthRef")
        ap = _ext(r, "appointment")
        return Claim(id=r["id"], patient_id=pid, payer_id=_id(r["insurer"], "Organization", "insurer"),
                     dos=_date(r["billablePeriod"]["start"], "billablePeriod.start"), status=status,
                     auth_number=pre[0] if pre else None, lines=lines,
                     appointment_id=_id(ap[0]["valueReference"], "Appointment", "appointment") if ap else None)

    ds = Dataset(as_of=as_of, practice=practice, policies=pol)
    ds.patients = run("Patient", by_type["Patient"], patient)
    ds.coverages = run("Coverage", by_type["Coverage"], coverage)
    resp = by_type["ClaimResponse"]
    ds.prior_auths = run("ClaimResponse", [r for r in resp if r.get("use") == "preauthorization"], auth)
    ds.denials = run("ClaimResponse", [r for r in resp if r.get("use") == "claim"], denial)
    for r in resp:
        if r.get("use") not in ("preauthorization", "claim"):
            flag(f"ClaimResponse/{r.get('id')}", "use", r.get("use"), "unknown ClaimResponse use")
    ds.plans = run("MedicationRequest", [r for r in by_type["MedicationRequest"] if r.get("intent") == "plan"], plan)
    for r in by_type["MedicationRequest"]:
        if r.get("intent") != "plan":
            flag(f"MedicationRequest/{r.get('id')}", "intent", r.get("intent"), "not a treatment plan")

    # Injection = Procedure + MedicationAdministration joined by partOf
    admins: dict[str, dict] = {}
    for r in by_type["MedicationAdministration"]:
        try:
            pid = _id(r["partOf"][0], "Procedure", "partOf")
        except (Bad, *SHAPE_ERRORS):
            flag(f"MedicationAdministration/{r.get('id')}", "partOf", "", "no Procedure reference")
            continue
        if pid not in proc_ids:
            flag(f"MedicationAdministration/{r.get('id')}", "partOf", pid, "dangling reference: no such Procedure")
        admins[pid] = r

    def injection(r, ref):
        pid = patient_ref(ref, r, "subject")
        m = admins.get(r["id"])
        if m is None:
            raise Bad("partOf", r["id"], "no MedicationAdministration for this Procedure")
        mref = f"MedicationAdministration/{m['id']}"
        try:
            patient_ref(mref, m, "subject")
            codes = m["medicationCodeableConcept"]["coding"]
            drug = _drug(codes[0]["code"], "medicationCodeableConcept")
            admin_eye = _eye_sn(m["dosage"]["site"]["coding"][0]["code"], "dosage.site")
            dose = float(m["dosage"]["dose"]["value"])
        except Bad as b:
            flag(mref, b.field, b.value, b.reason)
            raise Skip()
        ndc = [c["code"] for c in codes if c.get("system") == "http://hl7.org/fhir/sid/ndc"]
        ap = _ext(r, "appointment")
        aid = _id(ap[0]["valueReference"], "Appointment", "appointment") if ap else None
        if aid and aid not in appt_ids:
            flag(ref, "appointment", aid, "dangling reference: no such Appointment")
        return Injection(id=r["id"], patient_id=pid, date=_date(r["performedDateTime"], "performedDateTime"),
                         eye=_eye_sn(r["bodySite"][0]["coding"][0]["code"], "bodySite"),
                         drug=drug, dose_mg=dose, admin_eye=admin_eye, medadmin_id=m["id"], appointment_id=aid, ndc=ndc[0] if ndc else None)

    ds.injections = run("Procedure", by_type["Procedure"], injection)
    ds.appointments = run("Appointment", by_type["Appointment"], appointment)
    ds.notes = run("DocumentReference", by_type["DocumentReference"], note)
    ds.claims = run("Claim", by_type["Claim"], claim)
    ds.flags = flags
    return ds


def load(dir: str = "data/synthetic") -> Dataset:
    d = Path(dir)
    bundle = json.loads((d / "bundle.json").read_text())
    pols = json.loads((d / "policies.json").read_text())
    mf = d / "manifest.json"
    practice = json.loads(mf.read_text()).get("practice", "") if mf.exists() else ""
    return parse_bundle(bundle, pols, str(bundle["timestamp"])[:10], practice)
