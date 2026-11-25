"""Independent full check: second reading of the raw bundle, evidence audit, answer key, leak scan.

`python -m app.verify` runs every check over everything (no sampling), prints one line per check, writes
`data/runtime/verify_report.json` and exits 1 when any check fails. The model stub is forced.

The raw bundle reader here is deliberately separate from `app.ingest`: plain dict access written from the FHIR mapping
table in docs/spec.md section 6. `ingest.load` is only the thing under test.

Evidence matching rule (check 2): a row's `ref` ("Type/id") must name a resource in bundle.json (or a payer in
policies.json as "PayerPolicy/<payer_id>" or "Organization/<payer_id>"). The row's `value` is accepted when
  1. it is non-empty, and
  2. it appears as a substring of the JSON dump of that resource (for a DocumentReference also of the base64-decoded
     text), or, for a composite value such as "OD aflibercept_2mg", "['LT', 'JZ']" or a repr of an enum, every
     word in it (after dropping the "Drug." / "Eye." enum prefixes) appears as a whole word, case-insensitively.
Three translations are also accepted, because the app quotes the contract value, not the FHIR spelling: a SNOMED eye
code reads as OD / OS, `outcome` reads as approved / pending / denied, a JSON boolean reads as True / False. And the
values "None" and "[]" are accepted when the named field (`period.end`, `item[3].modifier`) is absent from the resource.
Anything else is a value mismatch.
"""
from __future__ import annotations

import os

os.environ["AUTOPILOT_MODEL"] = "stub"

import base64  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from app import agent, ingest, llm, store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "data" / "runtime" / "verify_report.json"
CAP = 50
EYE_BY_CODE = {"8966001": "OD", "18944008": "OS"}   # right / left eye structure (SNOMED)
PA_STATUS = {"complete": "approved", "queued": "pending", "error": "denied"}


# ---------------------------------------------------------------- raw bundle access

def _ext(res: dict, name: str) -> list[dict]:
    return [e for e in res.get("extension", []) if e.get("url") == f"urn:visentia:{name}"]


def _ext1(res: dict, name: str, key: str, default=""):
    found = _ext(res, name)
    return found[0].get(key, default) if found else default


def _code(cc: dict) -> str:
    return cc["coding"][0]["code"]


def _ref_id(ref: dict) -> str:
    return ref["reference"].split("/", 1)[1]


def _num(x) -> str:
    return "" if x is None or x == "" else str(float(x))


def _s(x) -> str:
    return "" if x is None else str(x)


def load_raw(d: Path) -> dict[str, dict[str, dict]]:
    """type -> id -> resource, with ClaimResponse split into 'PriorAuth' and 'Denial' views."""
    bundle = json.loads((d / "bundle.json").read_text())
    raw: dict[str, dict[str, dict]] = {}
    for e in bundle["entry"]:
        r = e["resource"]
        raw.setdefault(r["resourceType"], {})[r["id"]] = r
    return raw


# ---------------------------------------------------------------- check 1: cross source

def cross_source(raw: dict, ds) -> dict:
    mismatches: list[dict] = []
    n_fields = 0

    def cmp(ref: str, field: str, bundle_val, ds_val) -> None:
        nonlocal n_fields
        n_fields += 1
        b, v = _s(bundle_val), _s(ds_val)
        if b != v:
            mismatches.append({"ref": ref, "field": field, "bundle": b, "dataset": v})

    def pair(kind: str, bundle_ids: set, ds_objs: list, ref_type: str):
        """Count both ways; return {id: dataset object} for ids present on both sides."""
        by_id = {o.id: o for o in ds_objs}
        cmp(kind, "record count", len(bundle_ids), len(ds_objs))
        for i in sorted(bundle_ids - set(by_id)):
            mismatches.append({"ref": f"{ref_type}/{i}", "field": "record", "bundle": "present", "dataset": "missing"})
        for i in sorted(set(by_id) - bundle_ids):
            mismatches.append({"ref": f"{ref_type}/{i}", "field": "record", "bundle": "missing", "dataset": "present"})
        return {i: by_id[i] for i in bundle_ids if i in by_id}

    # Patient
    for i, p in pair("patients", set(raw.get("Patient", {})), ds.patients, "Patient").items():
        r = raw["Patient"][i]
        ref = f"Patient/{i}"
        cmp(ref, "name", r["name"][0]["given"][0] + " " + r["name"][0]["family"], p.name)
        cmp(ref, "birth_date", r["birthDate"], p.birth_date)
        token = [x["value"] for x in r["identifier"] if x["system"] == "urn:visentia:token"]
        cmp(ref, "token", token[0] if token else "", p.token)
        cmp(ref, "phone", (r.get("telecom") or [{}])[0].get("value", ""), p.phone)

    # Coverage
    for i, c in pair("coverages", set(raw.get("Coverage", {})), ds.coverages, "Coverage").items():
        r = raw["Coverage"][i]
        ref = f"Coverage/{i}"
        cmp(ref, "payer", r["payor"][0]["reference"].split("/", 1)[1], c.payer_id)
        cmp(ref, "member_id", r["subscriberId"], c.member_id)
        cmp(ref, "period_start", r["period"]["start"], c.period_start)
        cmp(ref, "period_end", r["period"].get("end", ""), c.period_end)

    # PriorAuth = ClaimResponse use=preauthorization; Denial = ClaimResponse use=claim
    crs = raw.get("ClaimResponse", {})
    pa_raw = {i: r for i, r in crs.items() if r.get("use") == "preauthorization"}
    den_raw = {i: r for i, r in crs.items() if r.get("use") == "claim"}
    for i, a in pair("prior_auths", set(pa_raw), ds.prior_auths, "ClaimResponse").items():
        r = pa_raw[i]
        ref = f"ClaimResponse/{i}"
        period = r.get("preAuthPeriod", {})
        cmp(ref, "auth_number", r.get("preAuthRef", ""), a.auth_number)
        cmp(ref, "drug", _ext1(r, "pa-drug", "valueCode"), a.drug.value)
        cmp(ref, "eyes", ",".join(sorted(e["valueCode"] for e in _ext(r, "pa-eye"))),
            ",".join(sorted(e.value for e in a.eyes)))
        cmp(ref, "status", PA_STATUS.get(r["outcome"], r["outcome"]), a.status)
        cmp(ref, "valid_from", period.get("start", ""), a.valid_from)
        cmp(ref, "valid_to", period.get("end", ""), a.valid_to)
        cmp(ref, "injections_approved", _ext1(r, "pa-injections-approved", "valueInteger", ""), a.injections_approved)
        cmp(ref, "injections_used", _ext1(r, "pa-injections-used", "valueInteger", 0), a.injections_used)
        cmp(ref, "step_exception", _ext1(r, "pa-step-exception", "valueBoolean", False), a.step_therapy_exception)

    # TreatmentPlan
    plan_raw = {i: r for i, r in raw.get("MedicationRequest", {}).items() if r.get("intent") == "plan"}
    for i, p in pair("plans", set(plan_raw), ds.plans, "MedicationRequest").items():
        r = plan_raw[i]
        ref = f"MedicationRequest/{i}"
        site = _code(r["dosageInstruction"][0]["site"])
        cmp(ref, "eye", EYE_BY_CODE.get(site, site), p.eye.value)
        cmp(ref, "drug", _code(r["medicationCodeableConcept"]), p.drug.value)
        cmp(ref, "interval_weeks", r["dosageInstruction"][0]["timing"]["repeat"]["period"], p.interval_weeks)
        cmp(ref, "diagnosis_code", _code(r["reasonCode"][0]), p.diagnosis_code)

    # Injection = Procedure + MedicationAdministration (linked by partOf)
    ma_by_proc: dict[str, dict] = {}
    for m in raw.get("MedicationAdministration", {}).values():
        ma_by_proc[_ref_id(m["partOf"][0])] = m
    for i, inj in pair("injections", set(raw.get("Procedure", {})), ds.injections, "Procedure").items():
        pr = raw["Procedure"][i]
        ref = f"Procedure/{i}"
        site = _code(pr["bodySite"][0])
        cmp(ref, "date", pr["performedDateTime"][:10], inj.date)
        cmp(ref, "eye", EYE_BY_CODE.get(site, site), inj.eye.value)
        ma = ma_by_proc.get(i)
        if ma is None:
            mismatches.append({"ref": ref, "field": "medication administration", "bundle": "missing",
                               "dataset": inj.medadmin_id})
            continue
        codings = ma["medicationCodeableConcept"]["coding"]
        ndc = next((c["code"] for c in codings if c.get("system") == "http://hl7.org/fhir/sid/ndc"), "")
        asite = _code(ma["dosage"]["site"])
        cmp(ref, "medadmin_id", ma["id"], inj.medadmin_id)
        cmp(ref, "admin_eye", EYE_BY_CODE.get(asite, asite), inj.admin_eye.value)
        cmp(ref, "drug", codings[0]["code"], inj.drug.value)
        cmp(ref, "dose_mg", _num(ma["dosage"]["dose"]["value"]), _num(inj.dose_mg))
        cmp(ref, "ndc", ndc, inj.ndc)
    ma_ids = {m["id"] for m in raw.get("MedicationAdministration", {}).values()}
    cmp("MedicationAdministration", "record count", len(ma_ids), len({i.medadmin_id for i in ds.injections}))
    for pid in sorted(set(ma_by_proc) - set(raw.get("Procedure", {}))):
        mismatches.append({"ref": f"MedicationAdministration/{ma_by_proc[pid]['id']}", "field": "procedure",
                           "bundle": f"Procedure/{pid} not in bundle", "dataset": ""})

    # Appointment
    for i, a in pair("appointments", set(raw.get("Appointment", {})), ds.appointments, "Appointment").items():
        r = raw["Appointment"][i]
        ref = f"Appointment/{i}"
        cmp(ref, "date", r["start"][:10], a.date)
        cmp(ref, "time", r["start"][11:16], a.time)
        cmp(ref, "kind", _code(r["appointmentType"]), a.kind)
        cmp(ref, "status", r["status"], a.status)
        planned = [(_ext1(e, "eye", "valueCode"),_ext1(e, "drug", "valueCode"))
                   for e in _ext(r, "planned-injection")]
        cmp(ref, "planned", sorted(planned), sorted((p.eye.value, p.drug.value) for p in a.planned))

    # ClinicNote
    for i, n in pair("notes", set(raw.get("DocumentReference", {})), ds.notes, "DocumentReference").items():
        r = raw["DocumentReference"][i]
        ref = f"DocumentReference/{i}"
        cmp(ref, "date", r["date"][:10], n.date)
        cmp(ref, "text", base64.b64decode(r["content"][0]["attachment"]["data"]).decode("utf-8"), n.text)

    # Claim
    claim_raw = {i: r for i, r in raw.get("Claim", {}).items() if r.get("use") == "claim"}
    for i, c in pair("claims", set(claim_raw), ds.claims, "Claim").items():
        r = claim_raw[i]
        ref = f"Claim/{i}"
        cmp(ref, "dos", r["billablePeriod"]["start"], c.dos)
        cmp(ref, "status", _ext1(r, "claim-status", "valueCode"), c.status)
        pre = (r.get("insurance") or [{}])[0].get("preAuthRef") or [""]
        cmp(ref, "auth_number", pre[0], c.auth_number)
        dx = {d["sequence"]: _code(d["diagnosisCodeableConcept"]) for d in r.get("diagnosis", [])}
        items = r.get("item", [])
        cmp(ref, "line count", len(items), len(c.lines))
        lines = {ln.seq: ln for ln in c.lines}
        for it in items:
            ln = lines.get(it["sequence"])
            lref = f"{ref}#{it['sequence']}"
            if ln is None:
                mismatches.append({"ref": lref, "field": "line", "bundle": "present", "dataset": "missing"})
                continue
            cmp(lref, "code", _code(it["productOrService"]), ln.code)
            cmp(lref, "modifiers", ",".join(_code(m) for m in it.get("modifier", [])), ",".join(ln.modifiers))
            cmp(lref, "units", int(it["quantity"]["value"]), ln.units)
            cmp(lref, "dx_codes", ",".join(dx.get(s, "?") for s in it.get("diagnosisSequence", [])),
                ",".join(ln.dx_codes))
            cmp(lref, "charge", _num(it["net"]["value"]), _num(ln.charge))
            cmp(lref, "ndc", _ext1(it, "ndc", "valueString"), ln.ndc)

    # Denial
    for i, d in pair("denials", set(den_raw), ds.denials, "ClaimResponse").items():
        r = den_raw[i]
        ref = f"ClaimResponse/{i}"
        adj = r["item"][0]["adjudication"][0]
        cmp(ref, "claim_id", _ref_id(r["request"]), d.claim_id)
        cmp(ref, "date", r["created"][:10], d.date)
        cmp(ref, "carc", _code(adj["reason"]), d.carc)
        cmp(ref, "amount", _num(adj["amount"]["value"]), _num(d.amount))

    n_records = sum(len(v) for v in raw.values())
    return {"ok": not mismatches, "records": n_records, "fields_compared": n_fields,
            "mismatches": mismatches[:CAP], "mismatch_count": len(mismatches)}


# ---------------------------------------------------------------- check 2: evidence

_WORD = re.compile(r"[A-Za-z0-9_.\-]+")


def _value_found(value: str, text: str) -> bool:
    if not value or not value.strip():
        return False
    if value in text:
        return True
    words = [w for w in _WORD.findall(re.sub(r"\b(?:Drug|Eye)\.", "", value)) if w]
    if not words or words == [value]:
        return False
    return all(re.search(r"(?<![A-Za-z0-9_])" + re.escape(w) + r"(?![A-Za-z0-9_])", text, re.I) for w in words)


def _absent(res: dict, field: str) -> bool:
    """True when the dotted field path ("period.end", "item[3].modifier") does not exist in the resource."""
    node = res
    for part in field.split("."):
        m = re.fullmatch(r"(\w+)(?:\[(\d+)\])?", part)
        if not m or not isinstance(node, dict) or m.group(1) not in node:
            return True
        node = node[m.group(1)]
        if m.group(2):
            n = int(m.group(2))
            hit = [x for x in node if isinstance(x, dict) and x.get("sequence") == n] if isinstance(node, list) else []
            node = hit[0] if hit else (node[n - 1] if isinstance(node, list) and 0 < n <= len(node) else None)
            if node is None:
                return True
    return False


def _resource_text(res: dict) -> str:
    text = json.dumps(res, ensure_ascii=False)
    for code, eye in EYE_BY_CODE.items():   # translations the app applies when it quotes a value
        text = text.replace(f'"{code}"', f'"{code}" {eye}')
    text += " " + PA_STATUS.get(res.get("outcome", ""), "") + " " + text.replace("true", "True").replace("false", "False")
    if res.get("resourceType") == "DocumentReference":
        for c in res.get("content", []):
            data = c.get("attachment", {}).get("data")
            if data:
                text += "\n" + base64.b64decode(data).decode("utf-8")
    return text


def evidence(raw: dict, policies: dict[str, dict], items: list) -> dict:
    unresolved: list[dict] = []
    mismatched: list[dict] = []
    rows = n_unres = n_bad = 0
    for it in items:
        for ch in it.checks:
            for ev in ch.evidence:
                rows += 1
                where = {"item": it.id, "rule": ch.rule_id, "ref": ev.ref, "field": ev.field, "value": ev.value}
                rtype, _, rid = ev.ref.partition("/")
                if rtype in ("PayerPolicy", "Organization") and rid in policies:
                    res = policies[rid]
                else:
                    res = raw.get(rtype, {}).get(rid)
                if res is None:
                    n_unres += 1
                    unresolved.append(where)
                elif not (_value_found(ev.value, _resource_text(res))
                          or (ev.value in ("None", "[]") and _absent(res, ev.field))):
                    n_bad += 1
                    mismatched.append(where)
    return {"ok": not n_unres and not n_bad, "rows": rows, "unresolved": unresolved[:CAP],
            "unresolved_count": n_unres, "value_mismatches": mismatched[:CAP], "value_mismatch_count": n_bad}


# ---------------------------------------------------------------- check 3: answer key

def answer_key(items: list, manifest: dict) -> dict:
    planted = {p["item_id"]: p for p in manifest.get("planted", [])}
    out = {"items": len(items), "expected_not_clear": len(planted), "matched": 0, "wrong_clear": [],
           "false_alarm": [], "rule_mismatch": [], "missing_items": []}
    seen = set()
    for it in items:
        seen.add(it.id)
        got = (it.status.value, agent.flagged_rules(it))
        exp_p = planted.get(it.id)
        exp = (exp_p["expect_status"], sorted(exp_p["expect_rules"])) if exp_p else ("clear", [])
        row = {"item_id": it.id, "expected": {"status": exp[0], "rules": exp[1]},
               "got": {"status": got[0], "rules": got[1]}}
        if got == exp:
            out["matched"] += 1
        elif exp[0] != "clear" and got[0] == "clear":
            out["wrong_clear"].append(row)
        elif exp[0] == "clear":
            out["false_alarm"].append(row)
        else:
            out["rule_mismatch"].append(row)
    out["missing_items"] = sorted(set(planted) - seen)
    out["ok"] = not (out["wrong_clear"] or out["false_alarm"] or out["rule_mismatch"] or out["missing_items"])
    return out


# ---------------------------------------------------------------- check 4: leak

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _identifiers(raw: dict) -> list[tuple[str, str, str, bool]]:
    """(patient token, kind, text, whole_word) for every identifier in the raw bundle. Never reported."""
    members: dict[str, list[str]] = {}
    for c in raw.get("Coverage", {}).values():
        members.setdefault(_ref_id(c["beneficiary"]), []).append(c["subscriberId"])
    out = []
    for pid, p in raw.get("Patient", {}).items():
        token = next((x["value"] for x in p["identifier"] if x["system"] == "urn:visentia:token"), pid)
        given, family = p["name"][0]["given"][0], p["name"][0]["family"]
        names = [("full name", f"{given} {family}"), ("given name", given), ("family name", family)]
        out += [(token, k, v, True) for k, v in names if len(v) >= 3]
        y, m, d = (int(x) for x in p["birthDate"].split("-"))
        out += [(token, "birth date", p["birthDate"], False), (token, "birth date", f"{m:02d}/{d:02d}/{y}", False),
                (token, "birth date", f"{_MONTHS[m - 1]} {d}, {y}", False)]
        phone = (p.get("telecom") or [{}])[0].get("value")
        if phone:
            out.append((token, "phone", phone, False))
        out += [(token, "member id", mid, False) for mid in members.get(pid, [])]
    return out


def leak(raw: dict, items: list) -> dict:
    texts: list[tuple[str, str]] = [(f"prompt[{i}]:{p.get('task', '')}", f"{p.get('prompt', '')}")
                                    for i, p in enumerate(llm.PROMPT_LOG)]
    n_prompts = len(texts)
    n_log = 0
    if store.DECISIONS.exists():
        for n, line in enumerate(store.DECISIONS.read_text().splitlines(), 1):
            if line.strip():
                n_log += 1
                texts.append((f"decision log line {n}", line))
    n_steps = 0
    for it in items:
        n_steps += len(it.trace)
        texts.append((f"trace of {it.id}", json.dumps([s.model_dump(mode="json") for s in it.trace])))
    ids = _identifiers(raw)
    compiled = [(tok, kind, re.compile((r"(?<!\w)" + re.escape(txt) + r"(?!\w)") if word else re.escape(txt), re.I))
                for tok, kind, txt, word in ids]
    hits, seen = [], set()
    for where, text in texts:
        for tok, kind, rx in compiled:
            if rx.search(text) and (where, kind, tok) not in seen:
                seen.add((where, kind, tok))
                hits.append({"where": where, "identifier_kind": kind, "patient_token": tok})
    return {"ok": not hits, "prompts": n_prompts, "trace_steps": n_steps, "log_lines": n_log,
            "identifiers": len(ids), "hits": hits[:CAP], "hit_count": len(hits)}


# ---------------------------------------------------------------- driver

def run(dir: str = "data/synthetic") -> dict:
    t0 = time.time()
    d = Path(dir)
    raw = load_raw(d)
    policies = {p["payer_id"]: p for p in json.loads((d / "policies.json").read_text())}
    manifest = json.loads((d / "manifest.json").read_text()) if (d / "manifest.json").exists() else {}
    ds = ingest.load(str(d))
    llm.reset_log()
    items = agent.build_items(ds)
    report = {"cross_source": cross_source(raw, ds), "evidence": evidence(raw, policies, items),
              "answer_key": answer_key(items, manifest), "leak": leak(raw, items)}
    report = {"ok": all(v["ok"] for v in report.values()), "seconds": round(time.time() - t0, 2),
              "as_of": str(ds.as_of), **report}
    return report


def _print(r: dict) -> None:
    cs, ev, ak, lk = r["cross_source"], r["evidence"], r["answer_key"], r["leak"]
    flag = lambda c: "PASS" if c["ok"] else "FAIL"  # noqa: E731
    print(f"cross_source  {flag(cs)}  {cs['records']} records, {cs['fields_compared']} fields, "
          f"{cs['mismatch_count']} mismatches")
    print(f"evidence      {flag(ev)}  {ev['rows']} rows, {ev['unresolved_count']} unresolved, "
          f"{ev['value_mismatch_count']} value mismatches")
    print(f"answer_key    {flag(ak)}  {ak['matched']}/{ak['items']} match, {len(ak['wrong_clear'])} wrong clears, "
          f"{len(ak['false_alarm'])} false alarms, {len(ak['rule_mismatch'])} rule mismatches, "
          f"{len(ak['missing_items'])} missing")
    print(f"leak          {flag(lk)}  {lk['prompts']} prompts, {lk['trace_steps']} trace steps, "
          f"{lk['log_lines']} log lines, {lk['identifiers']} identifiers, {lk['hit_count']} hits")
    print(f"full check {'PASS' if r['ok'] else 'FAIL'} · {cs['mismatch_count']} field mismatches · "
          f"{ev['unresolved_count'] + ev['value_mismatch_count']} bad evidence rows · "
          f"{ak['matched']}/{ak['items']} items match the key · {lk['hit_count']} leaks")


def main() -> int:
    r = run()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(r, indent=1))
    _print(r)
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
