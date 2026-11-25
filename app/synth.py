"""Deterministic synthetic world: a FHIR R4 Bundle, the payer policies and an answer-key manifest (spec section 6).

A clean baseline is built first (every item clear by spec sections 1 to 4), then defects are planted. Each planted
item's expectation is written by hand next to the defect, never computed by running the rules.
"""
from __future__ import annotations

import argparse
import base64
import json
import random
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.contracts import (DRUGS, EYE_MODIFIER, EYE_SNOMED, Appointment, ClaimLine, Claim, ClinicNote, Coverage,
                           Dataset, Denial, Drug, Eye, Injection, Patient, PayerPolicy, PlannedInjection, PriorAuth,
                           StepTherapy, TreatmentPlan, expected_drug_line)

PRACTICE = "Garden State Retina (synthetic)"
SITES = ["Somerset", "Edison"]
PHYSICIANS = ["Dr. Priya Raman", "Dr. Michael Okafor", "Dr. Elena Vasquez"]
PAYERS = ["medicare-ffs", "ma-summit", "ma-harbor", "com-northfield"]
FIRST = ["James", "Mary", "Robert", "Patricia", "John", "Linda", "Michael", "Barbara", "William", "Susan", "David",
         "Karen", "Richard", "Nancy", "Joseph", "Lisa", "Thomas", "Betty", "Charles", "Helen", "Daniel", "Sandra",
         "Paul", "Donna", "Mark", "Carol", "George", "Ruth", "Kenneth", "Sharon", "Steven", "Michelle", "Edward",
         "Laura", "Brian", "Sarah", "Ronald", "Kimberly", "Anthony", "Deborah", "Raj", "Anita", "Wei", "Mei", "Luis",
         "Carmen", "Omar", "Fatima", "Dmitri", "Irina"]
LAST = ["Alvarez", "Bennett", "Castillo", "Donovan", "Ellison", "Fitzgerald", "Gallagher", "Hartman", "Iverson",
        "Jennings", "Kowalski", "Lindqvist", "Mercer", "Nakamura", "Okonkwo", "Pennington", "Quigley", "Rosales",
        "Sullivan", "Tran", "Underwood", "Vasilev", "Whitaker", "Xu", "Yoder", "Zimmerman", "Abbott", "Brennan",
        "Calloway", "Dempsey", "Escobar", "Flanagan", "Grover", "Holloway", "Ingram", "Joshi", "Kaplan", "Lombardi",
        "Moreno", "Novak", "Patel", "Reyes", "Sharma", "Torres", "Ueda", "Volkov", "Wagner", "Yamamoto", "Zielinski",
        "Haddad"]
FAMS = {
    "AMD": {"OD": "H35.3211", "OS": "H35.3221", "OU": "H35.3231"},
    "DME": {"OD": "E11.3211", "OS": "E11.3212", "OU": "E11.3213"},
    "CRVO": {"OD": "H34.8110", "OS": "H34.8120"},
    "BRVO": {"OD": "H34.8310", "OS": "H34.8320"},
}
NDC = {Drug.AFLIBERCEPT_2MG: "61755000502", Drug.AFLIBERCEPT_8MG: "61755009201", Drug.FARICIMAB: "50242008701",
       Drug.RANIBIZUMAB: "50242008201", Drug.BEVACIZUMAB: "70121165701"}
PLAN_NAMES = {"medicare-ffs": "Medicare Part B", "ma-summit": "Summit Gold Medicare Advantage",
              "ma-harbor": "Harbor Advantage HMO", "com-northfield": "Northfield PPO Select"}
AUTH_PREFIX = {"medicare-ffs": "MCR", "ma-summit": "SUM", "ma-harbor": "HAR", "com-northfield": "NFC"}
HOLD_IDS = {"CLR-02", "CLR-11", "CLR-12", "CLM-01", "CLM-02", "DEN-00", "DEN-04", "DEN-06", "DEN-07", "DEN-12"}
SN = "http://snomed.info/sct"
BEV, AFL2, AFL8, FARI, RANI = (Drug.BEVACIZUMAB, Drug.AFLIBERCEPT_2MG, Drug.AFLIBERCEPT_8MG, Drug.FARICIMAB,
                               Drug.RANIBIZUMAB)
OD, OS = Eye.OD, Eye.OS
DRUG_NAMES = {AFL2: ("Eylea", "aflibercept 2 mg"), AFL8: ("Eylea HD", "aflibercept 8 mg"), FARI: ("Vabysmo", "faricimab"),
              RANI: ("Lucentis", "ranibizumab"), BEV: ("Avastin", "bevacizumab")}
FINDINGS = ["Exam stable.", "No new hemorrhage on dilated exam.", "OCT shows improved central subfield thickness.",
            "Patient tolerated the last treatment well.", "Fluid is resolved on OCT.",
            "Counseled on warning signs of endophthalmitis.", "Vision is stable per patient."]


def policies() -> list[PayerPolicy]:
    pa = [AFL2, AFL8, FARI, RANI]
    v = "2026.2"
    return [
        PayerPolicy(payer_id="medicare-ffs", name="Medicare Part B (illustrative)", kind="medicare_ffs", version=v, appeal_days=120),
        PayerPolicy(payer_id="ma-summit", name="Summit Medicare Advantage", kind="medicare_advantage", version=v,
                    pa_required=pa, step_therapy=StepTherapy(), appeal_days=60),
        PayerPolicy(payer_id="ma-harbor", name="Harbor Advantage HMO", kind="medicare_advantage", version=v,
                    pa_required=pa, bevacizumab_hcpcs="C9257", bevacizumab_units=5, appeal_days=60),
        PayerPolicy(payer_id="com-northfield", name="Northfield Commercial PPO", kind="commercial", version=v,
                    pa_required=[AFL8, FARI], bevacizumab_hcpcs="J3590", timely_filing_days=180),
    ]


class World:
    def __init__(self, seed: int, as_of: date):
        self.seed, self.as_of = seed, as_of
        self.r = random.Random(seed)
        self.pols = policies()
        self.pol = {p.payer_id: p for p in self.pols}
        self.n: dict[str, int] = {}
        self.names: set[str] = set()
        self.patients, self.coverages, self.auths, self.plans, self.injs = [], [], [], [], []
        self.appts, self.notes, self.claims, self.denials = [], [], [], []
        self.planted, self.near = [], []

    # ------------------------------------------------------------ small helpers
    def d(self, n: int) -> date:
        return self.as_of + timedelta(days=n)

    def nid(self, prefix: str) -> str:
        self.n[prefix] = self.n.get(prefix, 0) + 1
        return f"{prefix}-{self.n[prefix]:04d}"

    def slot(self) -> str:
        return f"{self.r.randint(8, 16):02d}:{self.r.choice(['00', '15', '30', '45'])}"

    def plant(self, item: str, rules: list[str], why: str, hold: bool = False):
        status = "hold" if hold or any(x in HOLD_IDS for x in rules) else "action"
        self.planted.append({"item_id": item, "stage": item.split(":")[0], "expect_status": status,
                             "expect_rules": sorted(rules), "why": why})

    def near_miss(self, item: str, why: str):
        self.near.append({"item_id": item, "why": why})

    # ------------------------------------------------------------ records
    def new_patient(self) -> Patient:
        r = self.r
        while True:
            name = f"{r.choice(FIRST)} {r.choice(LAST)}"
            if name not in self.names:
                break
        self.names.add(name)
        i = len(self.patients) + 1
        p = Patient(id=f"pt-{i:04d}", token=f"PT-{i:04d}", name=name,
                    birth_date=date(r.randint(1936, 1962), r.randint(1, 12), r.randint(1, 28)),
                    phone=f"(732) 555-{r.randint(0, 9999):04d}")
        self.patients.append(p)
        return p

    def coverage(self, p: Patient, payer: str, start=None, end="auto") -> Coverage:
        r = self.r
        if end == "auto":
            end = date(2027, r.choice([3, 6, 9, 12]), 28) if r.random() < 0.25 else None
        c = Coverage(id=self.nid("cov"), patient_id=p.id, payer_id=payer, plan_name=PLAN_NAMES[payer],
                     member_id=f"{AUTH_PREFIX[payer]}{r.randint(10**8, 10**9 - 1)}",
                     period_start=start or date(r.randint(2022, 2024), r.randint(1, 12), 1), period_end=end)
        self.coverages.append(c)
        return c

    def appt(self, p, d, kind, status, planned, prov, loc) -> Appointment:
        a = Appointment(id=self.nid("appt"), patient_id=p.id, date=d, time=self.slot(), kind=kind, status=status,
                        planned=planned, provider=prov, location=loc)
        self.appts.append(a)
        return a

    # ------------------------------------------------------------ patient parameters
    def rp(self, payer=None, drug=None, eyes=None, w=None, fam=None) -> dict:
        r = self.r
        payer = payer or r.choices(PAYERS, [35, 20, 20, 25])[0]
        drug = drug or r.choices([BEV, AFL2, AFL8, FARI, RANI], [30, 30, 12, 18, 10])[0]
        bil = (len(eyes) == 2) if eyes else r.random() < 0.22
        fam = fam or (r.choice(["AMD", "AMD", "DME"]) if bil else r.choices(["AMD", "DME", "CRVO", "BRVO"], [45, 30, 10, 15])[0])
        if bil and fam in ("CRVO", "BRVO"):
            fam = "AMD"
        eyes = list(eyes) if eyes else ([OD, OS] if bil else [r.choice([OD, OS])])
        ou = bil and r.random() < 0.5
        kw = dict(payer=payer, drug=drug, eyes=eyes, w=w or r.choice([4, 6, 8, 8, 10, 12]),
                  dx={e: FAMS[fam]["OU" if ou else e.value] for e in eyes}, n_hist=r.randint(3, 4), bev_dates=0)
        if payer == "ma-summit" and drug != BEV and r.random() < 0.5:
            kw.update(n_hist=5, bev_dates=3)
        return kw

    # ------------------------------------------------------------ notes
    def drug_name(self, drug, brand):
        b, g = DRUG_NAMES[drug]
        name = b if brand or self.r.random() < 0.55 else g
        x = self.r.random()
        return name.upper() if x < 0.05 else name.lower() if x < 0.15 else name

    def one(self, drug, w, eyep, brand=False):
        r = self.r
        d = self.drug_name(drug, brand)
        iv = "monthly" if w == 4 and r.random() < 0.5 else r.choice([f"q{w}w", f"q {w} wk", f"every {w} weeks", f"{w}-week interval"])
        t = r.randrange(5)
        if t == 0:
            return f"Plan: continue {d} {eyep}, {iv}."
        if t == 1:
            return f"{eyep[0].upper() + eyep[1:]}: maintain {d} {iv}."
        if t == 2:
            return f"Continue {d} {eyep} {iv}."
        if t == 3:
            return f"Plan: continue {d} {eyep}, extend to {w} weeks."
        return f"Maintain {eyep} on {d}, {iv}."

    def note_text(self, P, day, over=None, brand=False, new=False, older=False):
        r = self.r
        dob = r.choice([P.p.birth_date.isoformat(), P.p.birth_date.strftime("%m/%d/%Y"),
                        f"{P.p.birth_date:%b} {P.p.birth_date.day}, {P.p.birth_date.year}"])
        parts = [f"Patient: {P.p.name}, DOB {dob}.", r.choice(FINDINGS)]
        spec = {e: (P.plans[e].drug, P.plans[e].interval_weeks) for e in P.eyes}
        spec.update(over or {})
        if older:
            spec = {e: (d, w - 2 if w >= 6 else w) for e, (d, w) in spec.items()}
        if P.bev_dates and not older:
            parts.append("Prior bevacizumab x3, completed.")
        words = {OD: ["OD", "right eye"], OS: ["OS", "left eye"]}
        if new:
            for e in P.eyes:
                d, w = spec[e]
                parts.append(f"Start {self.drug_name(d, brand)} {r.choice(words[e])} q{w}w.")
        elif len(P.eyes) == 2 and spec[OD] == spec[OS] and r.random() < 0.4:
            parts.append(self.one(*spec[OD], r.choice(["OU", "both eyes"]), brand))
        else:
            for e in P.eyes:
                parts.append(self.one(*spec[e], r.choice(words[e]), brand))
        parts.append(r.choice(FINDINGS))
        return " ".join(parts)

    # ------------------------------------------------------------ claims
    def make_claim(self, P, dos, status, specs, appt_id, em="auto", oct_="auto") -> Claim:
        r = self.r
        cl = Claim(id=self.nid("clm"), patient_id=P.p.id, payer_id=P.cov.payer_id, dos=dos, status=status,
                   appointment_id=appt_id)
        pol = self.pol[cl.payer_id]
        h = SimpleNamespace(inj={}, drug={}, em=None, oct=None)
        seq = 1
        for eye, drug, ndc in specs:
            dxc = P.dx[eye]
            h.inj[eye] = ClaimLine(seq=seq, code="67028", modifiers=[EYE_MODIFIER[eye]], units=1, dx_codes=[dxc],
                                   charge=105.0)
            code, units = expected_drug_line(drug, pol)
            h.drug[eye] = ClaimLine(seq=seq + 1, code=code, modifiers=[EYE_MODIFIER[eye], "JZ"], units=units,
                                    dx_codes=[dxc], charge=DRUGS[drug].est_allowed_usd, ndc=ndc)
            cl.lines += [h.inj[eye], h.drug[eye]]
            seq += 2
            if drug in pol.pa_required and P.auth:
                cl.auth_number = P.auth.auth_number
        first_dx = P.dx[specs[0][0]]
        if em == "auto":
            em = r.choice(["99213", "99214", "92014"]) if r.random() < 0.35 else None
        if em:
            h.em = ClaimLine(seq=seq, code=em, modifiers=["25"], units=1, dx_codes=[first_dx], charge=130.0)
            cl.lines.append(h.em)
            seq += 1
        if oct_ == "auto":
            oct_ = r.random() < 0.3
        if oct_:
            h.oct = ClaimLine(seq=seq, code="92134", modifiers=[], units=1, dx_codes=[first_dx], charge=45.0)
            cl.lines.append(h.oct)
        self.claims.append(cl)
        P.cl[dos], P.h[dos] = cl, h
        return cl

    # ------------------------------------------------------------ the baseline patient
    def build(self, *, payer, drug, eyes, w, last, dx, appt=True, n_hist=3, bev_dates=0, hist=None, auth="ok",
              exc=None, note_over=None, brand=False, prior_drug=None, updated_on=None, em="auto", oct_="auto",
              force_claim=False, noshow=False, cancelled=False):
        r = self.r
        pol = self.pol[payer]
        p = self.new_patient()
        P = SimpleNamespace(p=p, cov=self.coverage(p, payer), pol=pol, eyes=eyes, drug=drug, w=w, dx=dx, auth=None,
                            plans={}, dates=[], cl={}, h={}, inj_on={}, next=None, bev_dates=bev_dates)
        dates = hist if hist is not None else [last - timedelta(days=7 * w * k) for k in range(n_hist)] if last else []
        P.dates = dates
        prov, loc = r.choice(PHYSICIANS), r.choice(SITES)
        nonbev = len(dates) - bev_dates

        def drug_at(j):
            return BEV if j >= nonbev else (prior_drug or drug)

        used = sum(1 for j in range(len(dates)) if drug_at(j) == drug) * len(eyes)
        if drug in pol.pa_required and auth == "ok":
            first = dates[-1] if dates else self.d(-60)
            needs_exc = bool(pol.step_therapy) and drug != pol.step_therapy.first_line
            if exc is None:
                exc = needs_exc and bev_dates < 3
            P.auth = PriorAuth(id=self.nid("pa"), patient_id=p.id, payer_id=payer,
                               auth_number=f"{AUTH_PREFIX[payer]}-{r.randint(100000, 999999)}", drug=drug,
                               eyes=list(eyes), status="approved", valid_from=first - timedelta(days=30),
                               valid_to=self.d(r.randint(120, 300)), injections_approved=used + r.randint(5, 12),
                               injections_used=used, step_therapy_exception=bool(exc and needs_exc),
                               requested_on=first - timedelta(days=40))
            self.auths.append(P.auth)
        upd = updated_on or (dates[nonbev - 1] if dates and nonbev >= 1 else self.d(-5))
        for e in eyes:
            P.plans[e] = TreatmentPlan(id=self.nid("plan"), patient_id=p.id, eye=e, diagnosis_code=dx[e], drug=drug,
                                       interval_weeks=w, physician=prov, updated_on=upd)
            self.plans.append(P.plans[e])
        for j in reversed(range(len(dates))):
            dt, dj = dates[j], drug_at(j)
            ap = self.appt(p, dt, "injection", "fulfilled", [PlannedInjection(eye=e, drug=dj) for e in eyes], prov, loc)
            specs, P.inj_on[dt] = [], []
            for e in eyes:
                inj = Injection(id=self.nid("proc"), patient_id=p.id, date=dt, eye=e, drug=dj,
                                dose_mg=DRUGS[dj].dose_mg, admin_eye=e, medadmin_id=self.nid("ma"),
                                appointment_id=ap.id, ndc=NDC[dj])
                self.injs.append(inj)
                P.inj_on[dt].append(inj)
                specs.append((e, dj, NDC[dj]))
            age = (self.as_of - dt).days
            kw = dict(em=em, oct_=oct_) if j == 0 else {}
            if 1 <= age <= 10:
                self.make_claim(P, dt, "draft", specs, ap.id, **kw)
            elif age > 10 and (force_claim and j == 0 or r.random() < 0.55):
                self.make_claim(P, dt, "submitted" if age <= 45 else "paid", specs, ap.id, **kw)
            if j == 1 and r.random() < 0.4:
                nd = dates[1]
                self.notes.append(ClinicNote(id=self.nid("note"), patient_id=p.id, date=nd, author=prov,
                                             text=self.note_text(P, nd, older=True)))
        nd = dates[0] if dates else self.d(-5)
        self.notes.append(ClinicNote(id=self.nid("note"), patient_id=p.id, date=nd, author=prov,
                                     text=self.note_text(P, nd, note_over, brand, new=not dates)))
        due = dates[0] + timedelta(days=7 * w) if dates else None
        if appt is not None:
            P.next = self.appt(p, due if appt is True else appt, "injection", "booked",
                               [PlannedInjection(eye=e, drug=drug) for e in eyes], prov, loc)
        if noshow:
            self.appt(p, due, "injection", "noshow", [PlannedInjection(eye=e, drug=drug) for e in eyes], prov, loc)
        if cancelled:
            self.appt(p, due, "injection", "cancelled", [PlannedInjection(eye=e, drug=drug) for e in eyes], prov, loc)
        return P

    # ------------------------------------------------------------ patient kinds
    def win(self, off=None, rp=None, gap=None, **b):
        """A patient with a booked injection `off` days after as_of (inside the clearance window)."""
        kw = self.rp(**(rp or {}))
        kw.update(b)
        off = self.r.randint(0, 7) if off is None else off
        appt = self.d(off)
        kw.update(last=appt - timedelta(days=gap or 7 * kw["w"]), appt=appt)
        return self.build(**kw)

    def clm(self, k=None, rp=None, **b):
        """A patient whose newest injection (and draft claim) is k days old; next visit on time."""
        kw = self.rp(**(rp or {}))
        kw.update(b)
        k = k or self.r.randint(1, 10)
        kw.update(last=self.d(-k), appt=b.get("appt", True))
        return self.build(**kw)

    def far(self, due, rp=None, **b):
        """A patient whose next injection is due on day `due` (relative to as_of), nothing booked by default."""
        kw = self.rp(**(rp or {}))
        kw.update(b)
        kw.update(last=self.d(due) - timedelta(days=7 * kw["w"]))
        kw.setdefault("appt", None)
        return self.build(**kw)

    def den(self, carc, rp=None, **b):
        kw = self.rp(**dict(rp or {}, w=(rp or {}).get("w", 8)))
        kw.update(b)
        k = self.r.randint(26, min(60, 7 * kw["w"] - 8))
        kw.update(last=self.d(-k), appt=True, force_claim=True)
        P = self.build(**kw)
        return P, P.cl[P.dates[0]]

    def deny(self, claim, carc, amount=None, line=None, why="", rules=(), **plant):
        claim.status = "denied"
        line_obj = next((x for x in claim.lines if x.seq == line), None) if line else None
        den = Denial(id=self.nid("den"), claim_id=claim.id, date=claim.dos + timedelta(days=self.r.randint(12, 20)),
                     group="CO", carc=carc, rarc=self.r.choice([None, "N54", "M62", "N657"]),
                     amount=line_obj.charge if line_obj else claim.total, line_seq=line)
        self.denials.append(den)
        self.plant(f"denial:{den.id}", list(rules), why, **plant)
        return den

    # ------------------------------------------------------------ the planned world
    def run(self):
        jobs = []

        def add(tag, fn, *a, **k):
            jobs.append((tag, lambda: fn(*a, **k)))

        for fn in (self.j_clearance, self.j_near, self.j_cadence, self.j_claims, self.j_claim_near, self.j_denials):
            fn(add)
        nwin = sum(1 for t, _ in jobs if t == "win")
        nclm = sum(1 for t, _ in jobs if t == "clm")
        for _ in range(55 - nwin):
            add("win", self.win)
        for _ in range(58 - nclm):
            add("clm", self.clm)
        self.r.shuffle(jobs)
        for _, fn in jobs:
            fn()

    def ci(self, P):
        return f"clearance:{P.next.id}"

    def pi(self, P, eye=None):
        return f"cadence:{P.plans[eye or P.eyes[0]].id}"

    def j_clearance(self, add):
        W, ffs, har, sum_, nor = self.win, "medicare-ffs", "ma-harbor", "ma-summit", "com-northfield"

        def one(rp, rules, why, mut=None, cad=None, cad_why="", hold=False, **b):
            P = W(rp=rp, **b)
            if mut:
                mut(P)
            self.plant(self.ci(P), rules, why, hold=hold)
            for eye in (cad or []):
                self.plant(self.pi(P, eye), ["CAD-03"], cad_why)
            return P

        def no_cov_yet(P): P.cov.period_start, P.cov.period_end = P.next.date + timedelta(days=10), None
        def cov_ended(P): P.cov.period_end = self.d(-20)
        def extra_os(P): P.next.planned.append(PlannedInjection(eye=OS, drug=AFL2))
        def other_drug(P): P.next.planned[0] = PlannedInjection(eye=OD, drug=FARI)
        def auth_od(P): P.auth.eyes = [OD]
        def auth_vabysmo(P): P.auth.drug = FARI
        def expired(P): P.auth.valid_to = P.next.date - timedelta(days=4)
        def expired3(P): P.auth.valid_to = P.next.date - timedelta(days=3)
        def used_up(P): P.auth.injections_approved = P.auth.injections_used
        def pending(days):
            def f(P):
                a = P.auth
                a.status, a.valid_from, a.valid_to, a.injections_approved, a.injections_used = "pending", None, None, None, 0
                a.requested_on, a.auth_number = self.d(-days), f"REQ-{self.r.randint(100000, 999999)}"
            return f

        eylea = dict(drug=AFL2)
        add("win", one, dict(payer=har, **eylea, eyes=[OD]), ["CLR-01"], "Coverage starts after the visit date: no eligibility on the day", no_cov_yet)
        add("win", one, dict(payer=har, **eylea, eyes=[OD], w=8), ["CLR-01", "CLR-03"],
            "Coverage ended before the visit and the booked date is 21 days after the last injection (28 days used with no coverage)",
            cov_ended, cad=[OD], cad_why="Booked 21 days after the last injection, under the 28 day minimum", gap=21)
        add("win", one, dict(payer=ffs, **eylea, eyes=[OD]), ["CLR-02"], "Appointment plans OS but only OD has a treatment plan", extra_os)
        add("win", one, dict(payer=ffs, **eylea, eyes=[OD]), ["CLR-02"], "Appointment plans Vabysmo but the order is Eylea", other_drug)
        add("win", one, dict(payer=har, **eylea, eyes=[OS], w=8), ["CLR-03"], "Booked 21 days after the last injection, minimum is 28",
            cad=[OS], cad_why="Booked 21 days after the last injection, under the 28 day minimum", gap=21)
        add("win", one, dict(payer=har, **eylea, eyes=[OD], w=8), ["CLR-03", "CLR-04"],
            "Booked 21 days after the last injection and no authorization on file for a drug that needs one",
            cad=[OD], cad_why="Booked 21 days after the last injection, under the 28 day minimum", gap=21, auth="none")
        add("win", one, dict(payer=har, **eylea, eyes=[OD]), ["CLR-04"], "Payer requires authorization for Eylea and none exists", auth="none")
        add("win", one, dict(payer=har, **eylea, eyes=[OS]), ["CLR-05"], "Approved authorization covers OD only; visit is OS", auth_od)
        add("win", one, dict(payer=har, **eylea, eyes=[OD]), ["CLR-05"], "Approved authorization is for Vabysmo; visit is Eylea", auth_vabysmo)
        add("win", one, dict(payer=har, **eylea, eyes=[OD]), ["CLR-06"], "Authorization window ended 4 days before the visit", expired)
        add("win", one, dict(payer=sum_, drug=FARI, eyes=[OD]), ["CLR-06", "CLR-10"],
            "Authorization expired before the visit and only 1 bevacizumab injection on file for step therapy",
            expired3, n_hist=4, bev_dates=1, exc=False)
        add("win", one, dict(payer=nor, drug=FARI, eyes=[OS]), ["CLR-07"], "Authorization has no injections left", used_up)
        add("win", one, dict(payer=har, **eylea, eyes=[OD]), ["CLR-08"], "Authorization still pending; requested 2 days ago", pending(2))
        add("win", one, dict(payer=har, **eylea, eyes=[OS]), ["CLR-08"], "Authorization pending; requested 12 days ago, decision due before the visit", pending(12))
        add("win", one, dict(payer=sum_, **eylea, eyes=[OS]), ["CLR-10"], "Step therapy not met: 2 bevacizumab injections, no exception",
            n_hist=4, bev_dates=2, exc=False)
        add("win", one, dict(payer=ffs, **eylea, eyes=[OD], w=8), ["CLR-11"], "Plan diagnosis is a left-eye code but the eye is OD",
            dx={OD: "H35.3221"})
        add("win", one, dict(payer=ffs, **eylea, eyes=[OS], w=8), ["CLR-11", "CLR-12"],
            "Diagnosis code is not in the table and the note says Vabysmo while the order is Eylea",
            dx={OS: "H35.3299"}, note_over={OS: (FARI, 8)})
        add("win", one, dict(payer=ffs, **eylea, eyes=[OS], w=8), ["CLR-11"], "Plan diagnosis code E11.9 is not in the table", dx={OS: "E11.9"})
        add("win", one, dict(payer=nor, **eylea, eyes=[OD], w=8), ["CLR-12"], "Note switches OD to Vabysmo, order is Eylea",
            note_over={OD: (FARI, 8)})
        add("win", one, dict(payer=ffs, **eylea, eyes=[OD], w=8), ["CLR-12"], "Note extends to 10 weeks, order is every 8 weeks",
            note_over={OD: (AFL2, 10)})
        add("win", one, dict(payer=har, **eylea, eyes=[OD, OS], w=8), ["CLR-05"], "Bilateral visit; authorization covers OD only, OS line has none", auth_od)

    def j_near(self, add):
        W, ffs, har, sum_, nor = self.win, "medicare-ffs", "ma-harbor", "ma-summit", "com-northfield"

        def nm(rp, why, mut=None, cad=None, cad_why="", clr=True, **b):
            P = W(rp=rp, **b)
            if mut:
                mut(P)
            if clr:
                self.near_miss(self.ci(P), why)
            if cad:
                self.near_miss(self.pi(P), cad_why)
            return P

        def ends_on_visit(P): P.auth.valid_to = P.next.date
        def last_one(P): P.auth.injections_approved = P.auth.injections_used + 1

        add("win", nm, dict(payer=har, drug=AFL2, eyes=[OD]), "Authorization ending on the visit date still covers it (note only)", ends_on_visit)
        add("win", nm, dict(payer=har, drug=AFL2, eyes=[OS]), "This visit uses the last authorized injection (note only)", last_one)
        add("win", nm, dict(payer=har, drug=AFL2, eyes=[OD], w=8), "Interval exactly 28 days is allowed", cad=True,
            cad_why="Booked exactly 28 days after the last injection: no CAD-03, early is fine", gap=28)
        add("win", nm, dict(payer=ffs, drug=AFL8, eyes=[OD], w=8), "Visit 63 days after last injection, due +7 is on time", cad=True,
            cad_why="Booked exactly due + 7 days is on time", gap=63, off=4)
        add("win", self.n_new)
        add("win", self.n_hd)
        add("win", self.n_stale, "drug")
        add("win", self.n_stale, "interval")
        add("win", nm, dict(payer=ffs, drug=AFL2, eyes=[OD, OS]), "Bilateral same-day visit with two planned lines is clear")
        add("win", self.c_late_window)
        add("win", nm, dict(payer=sum_, drug=AFL2, eyes=[OD]), "Step therapy met by exactly 3 bevacizumab injections, no exception",
            n_hist=5, bev_dates=3, exc=False)
        add("win", nm, dict(payer=sum_, drug=FARI, eyes=[OS]), "Step therapy met by the exception on the authorization",
            n_hist=3, bev_dates=0, exc=True)

    def n_new(self):
        P = self.build(payer="medicare-ffs", drug=AFL2, eyes=[OD], w=8, last=None, dx={OD: "H35.3211"}, n_hist=0,
                       appt=self.d(self.r.randint(0, 7)))
        self.near_miss(self.ci(P), "First injection in this eye is allowed (no prior injection)")
        self.near_miss(self.pi(P), "Plan with no completed injection yet: no completed injection, note only")

    def n_hd(self):
        P = self.win(rp=dict(payer="com-northfield", drug=AFL8, eyes=[OD], w=8), brand=True)
        self.near_miss(self.ci(P), "Note says Eylea HD for an Eylea HD order: must not be read as Eylea")

    def n_stale(self, kind):
        w = 10 if kind == "interval" else 8
        kw = self.rp(payer="medicare-ffs", drug=FARI if kind == "drug" else AFL2, eyes=[OD], w=w)
        appt = self.d(self.r.randint(0, 7))
        last = appt - timedelta(days=7 * w)
        old = (AFL2, 8) if kind == "interval" else (AFL2, w)
        kw.update(last=last, appt=appt, updated_on=last + timedelta(days=3), note_over={OD: old},
                  prior_drug=AFL2 if kind == "drug" else None)
        P = self.build(**kw)
        self.near_miss(self.ci(P), f"Note ({kind}) predates a newer order; the order is newer so it is not compared")

    def c_late_window(self):
        P = self.win(3, rp=dict(payer="medicare-ffs", drug=AFL2, eyes=[OD], w=8), gap=67)
        self.plant(self.pi(P), ["CAD-04"], "Appointment is 11 days after the due date, more than 7 days late")
        self.near_miss(self.ci(P), "Late appointment is a cadence problem only; the clearance checks all pass")

    def j_cadence(self, add):
        ffs, har, nor = "medicare-ffs", "ma-harbor", "com-northfield"

        def p(P, rules, why, eye=None):
            self.plant(self.pi(P, eye), rules, why)

        def c4(P, d):
            p(P, ["CAD-04"], f"Booked {d} days after the due date")

        add("", lambda: c4(self.build(**{**self.rp(payer=ffs, drug=AFL2, eyes=[OD], w=6), "last": self.d(-30),
                                         "appt": self.d(22)}), 10))
        add("", lambda: c4(self.build(**{**self.rp(payer=nor, drug=AFL2, eyes=[OS], w=10), "last": self.d(-60),
                                         "appt": self.d(30)}), 20))
        add("", lambda: p(self.far(-20, rp=dict(payer=ffs, drug=AFL2, eyes=[OD], w=8), noshow=True), ["CAD-05"],
                          "Due 20 days ago, nothing booked (no-show on the due date)"))
        add("", lambda: p(self.far(-35, rp=dict(payer=har, drug=AFL2, eyes=[OS], w=8), noshow=True), ["CAD-05"],
                          "Due 35 days ago, nothing booked (no-show on the due date)"))

        def bilateral_lapsed():
            P = self.far(-70, rp=dict(payer=ffs, drug=AFL2, eyes=[OD, OS], w=8))
            for e in P.eyes:
                p(P, ["CAD-05"], "Both eyes due 70 days ago, nothing booked", e)
        add("", bilateral_lapsed)
        add("", lambda: p(self.far(-5, rp=dict(payer=ffs, drug=AFL2, eyes=[OD], w=8), cancelled=True), ["CAD-02"],
                          "Due 5 days ago, only a cancelled appointment"))
        add("", lambda: p(self.far(6, rp=dict(payer=har, drug=AFL2, eyes=[OS], w=8)), ["CAD-02"], "Due in 6 days, nothing booked"))
        add("", lambda: p(self.far(14, rp=dict(payer=ffs, drug=AFL2, eyes=[OD], w=8)), ["CAD-02"],
                          "Due exactly 14 days out, nothing booked: inside the booking horizon"))
        add("", lambda: p(self.far(-14, rp=dict(payer=ffs, drug=AFL2, eyes=[OS], w=8)), ["CAD-02"],
                          "Due exactly 14 days ago, nothing booked: overdue but not yet lapsed"))
        add("", lambda: self.near_miss(self.pi(self.far(15, rp=dict(payer=ffs, drug=AFL2, eyes=[OD], w=8))),
                                       "Due in 15 days with nothing booked: outside the 14 day horizon"))

        def early_outside():
            P = self.clm(9, rp=dict(payer=har, drug=AFL2, eyes=[OD], w=8), appt=self.d(12))
            p(P, ["CAD-03"], "Booked 21 days after the last injection (visit outside the clearance window)")
        add("clm", early_outside)
        # the clean draft claim on that patient is part of the baseline

    def j_claims(self, add):
        ffs, har, nor = "medicare-ffs", "ma-harbor", "com-northfield"

        def one(rules, why, rp, mut, hold=False, **b):
            P = self.clm(rp=rp, **b)
            mut(P, P.dates[0])
            self.plant(f"claim:{P.cl[P.dates[0]].id}", rules, why, hold=hold)

        def L(P, d, kind, eye=OD):
            return getattr(P.h[d], kind)[eye]

        def admin(P, d): P.inj_on[d][0].admin_eye = OS
        def lt(P, d): L(P, d, "inj").modifiers = ["LT"]
        def nomod(P, d): L(P, d, "inj").modifiers = []
        def hd(P, d): L(P, d, "drug").code = "J0177"
        def bevcode(P, d): L(P, d, "drug").code, L(P, d, "drug").units = "J7999", 1
        def nodrug(P, d): P.cl[d].lines.remove(L(P, d, "drug"))
        def units6(P, d): L(P, d, "drug").units = 6
        def nojz(P, d): L(P, d, "drug").modifiers = ["RT"]
        def nondc(P, d): L(P, d, "drug").ndc = None
        def baddx(P, d): L(P, d, "inj").dx_codes = ["H35.3221"]
        def typo(P, d): n = P.cl[d].auth_number; P.cl[d].auth_number = n[:-1] + str((int(n[-1]) + 1) % 10)
        def nonum(P, d): P.cl[d].auth_number = None
        def authgone(P, d): P.auth.valid_to = d - timedelta(days=10)
        def no25(P, d): P.h[d].em.modifiers = []
        def lt_ndc(P, d): lt(P, d); L(P, d, "drug").ndc = "6175500502"
        def jzjw(P, d): L(P, d, "drug").modifiers = ["RT", "JZ", "JW"]; P.h[d].em.modifiers = []

        eye = dict(eyes=[OD])
        add("clm", one, ["CLM-01"], "Procedure site is OD, MedicationAdministration site is OS", dict(payer=ffs, drug=AFL2, **eye), admin)
        add("clm", self.k_nodoc)
        add("clm", one, ["CLM-03"], "OD injection billed with the LT modifier", dict(payer=ffs, drug=AFL2, **eye), lt)
        add("clm", one, ["CLM-03"], "OD injection line has no eye modifier", dict(payer=ffs, drug=AFL2, **eye), nomod)
        add("clm", one, ["CLM-04"], "Eylea 2 mg visit billed with J0177 (Eylea HD)", dict(payer=ffs, drug=AFL2, **eye), hd)
        add("clm", one, ["CLM-04", "CLM-05"], "Harbor bevacizumab billed J7999 x1 instead of C9257 x5", dict(payer=har, drug=BEV, **eye), bevcode)
        add("clm", one, ["CLM-04"], "No drug line on a claim with an injection", dict(payer=ffs, drug=FARI, **eye), nodrug)
        add("clm", one, ["CLM-05"], "Vabysmo billed 6 units, expected 60", dict(payer=ffs, drug=FARI, **eye), units6)
        add("clm", one, ["CLM-06"], "Drug line has no JZ or JW modifier", dict(payer=ffs, drug=RANI, **eye), nojz)
        add("clm", one, ["CLM-07"], "Drug line has no NDC", dict(payer=ffs, drug=AFL8, **eye), nondc)
        add("clm", one, ["CLM-08"], "Injection line carries a left-eye diagnosis for an OD injection", dict(payer=ffs, drug=AFL2, **eye, fam="AMD"), baddx)
        add("clm", one, ["CLM-09"], "Claim authorization number differs from the approved one", dict(payer=har, drug=AFL2, **eye), typo)
        add("clm", one, ["CLM-09"], "Claim carries no authorization number", dict(payer=nor, drug=FARI, **eye), nonum)
        add("clm", one, ["CLM-09"], "Authorization required and none covers the date of service", dict(payer=har, drug=AFL2, **eye), authgone, hold=True)
        add("clm", one, ["CLM-10"], "Same-day exam billed without modifier 25", dict(payer=ffs, drug=AFL2, **eye), no25, em="99213")
        add("clm", one, ["CLM-03", "CLM-07"], "Wrong eye modifier and a 10 digit NDC", dict(payer=ffs, drug=AFL2, **eye), lt_ndc)
        add("clm", one, ["CLM-06", "CLM-10"], "Both JZ and JW, and 92014 without modifier 25", dict(payer=nor, drug=AFL2, **eye), jzjw, em="92014")

    def k_nodoc(self):
        P = self.build(**{**self.rp(payer="medicare-ffs", drug=AFL2, eyes=[OD], w=8), "last": self.d(-30), "appt": True})
        day = self.d(-4)
        ap = self.appt(P.p, day, "exam", "fulfilled", [], "Dr. Priya Raman", "Somerset")
        cl = self.make_claim(P, day, "draft", [(OD, AFL2, NDC[AFL2])], ap.id)
        self.plant(f"claim:{cl.id}", ["CLM-02"], "Injection billed for a visit that was an exam only; no injection on record")

    def j_claim_near(self, add):
        ffs, har, nor = "medicare-ffs", "ma-harbor", "com-northfield"

        def nm(why, rp, mut=None, **b):
            P = self.clm(rp=rp, **b)
            if mut:
                mut(P, P.dates[0])
            self.near_miss(f"claim:{P.cl[P.dates[0]].id}", why)

        def dashed(P, d):
            for i in P.inj_on[d]:
                i.ndc = "61755-0005-02"
            P.h[d].drug[OD].ndc = "61755-0005-02"
        def ends(P, d): P.auth.valid_to = d

        add("clm", nm, "Bilateral same-day visit with RT and LT lines", dict(payer=ffs, drug=AFL2, eyes=[OD, OS]))
        add("clm", nm, "NDC in the 5-4-2 dashed format is valid", dict(payer=ffs, drug=AFL2, eyes=[OD]), dashed)
        add("clm", nm, "E/M with modifier 25 and a 92134 OCT line are fine", dict(payer=ffs, drug=AFL2, eyes=[OD]), em="99214", oct_=True)
        add("clm", nm, "Harbor bevacizumab billed C9257 x5", dict(payer=har, drug=BEV, eyes=[OS]))
        add("clm", nm, "Northfield bevacizumab billed J3590 x1", dict(payer=nor, drug=BEV, eyes=[OD]))
        add("clm", nm, "Bilateral diagnosis code on a single-eye injection covers the eye",
            dict(payer=ffs, drug=AFL2, eyes=[OD], fam="AMD"), dx={OD: "H35.3231"})
        add("clm", nm, "Authorization ending on the date of service still covers it", dict(payer=har, drug=AFL2, eyes=[OS]), ends)

    def j_denials(self, add):
        ffs, har = "medicare-ffs", "ma-harbor"

        def case(carc, rp, rules, why, mut=None, line=None, whole=False, build=None, **plant):
            P, cl = self.den(carc, rp, **(build or {}))
            d0 = P.dates[0]
            if mut:
                mut(P, d0, cl)
            ln = None if whole else (line(P.h[d0]) if line else None)
            self.deny(cl, carc, line=ln.seq if ln else None, why=why, rules=rules, **plant)

        inj = lambda h: h.inj[OD]
        drg = lambda h: h.drug[OD]
        E = dict(payer=ffs, drug=AFL2, eyes=[OD])
        H = dict(payer=har, drug=AFL2, eyes=[OD])

        def wrong_num(P, d, cl): n = cl.auth_number; cl.auth_number = n[:-1] + str((int(n[-1]) + 1) % 10)
        def no_num(P, d, cl): cl.auth_number = None
        def lt(P, d, cl): P.h[d].inj[OD].modifiers = ["LT"]
        def dx(P, d, cl): P.h[d].inj[OD].dx_codes = ["H35.3221"]
        def nondc(P, d, cl): P.h[d].drug[OD].ndc = None
        def admin(P, d, cl): P.inj_on[d][0].admin_eye = OS
        def lapsed(P, d, cl):
            old = self.coverage(P.p, "ma-summit", start=date(2024, 1, 1), end=d - timedelta(days=5))
            P.cov.period_start = d - timedelta(days=4)
            cl.payer_id = "ma-summit"

        def missing():
            P = self.build(**{**self.rp(**E, w=8), "last": self.d(-40), "appt": True})
            den = Denial(id=self.nid("den"), claim_id="clm-9001", date=self.d(-20), group="CO", carc="16", amount=105.0)
            self.denials.append(den)
            self.plant(f"denial:{den.id}", ["DEN-00"], "Denial refers to a claim that is not in the data")

        def short_gap():
            kw = self.rp(**E, w=8)
            last = self.d(-self.r.randint(26, 48))
            kw.update(last=last, appt=True, force_claim=True, hist=[last, last - timedelta(days=21), last - timedelta(days=77)])
            P = self.build(**kw)
            self.deny(P.cl[last], "151", why="Frequency denial and the previous OD injection was 21 days earlier", rules=["DEN-07"])

        add("", missing)
        add("", case, "197", H, ["DEN-02"], "Authorization exists; claim has no number", no_num, whole=True)
        add("", case, "197", H, ["DEN-02"], "Authorization exists; claim carries a different number", wrong_num, whole=True)
        add("", case, "197", H, ["DEN-03"], "Claim already carries the approved authorization number", whole=True)
        add("", case, "197", E, ["DEN-04"], "No authorization on file for this date and eye", whole=True)
        add("", case, "4", E, ["DEN-05"], "Modifier problem found on the claim itself", lt, inj)
        add("", case, "11", E, ["DEN-05"], "Diagnosis does not match the eye", dx, inj)
        add("", case, "16", E, ["DEN-05"], "Missing NDC on the drug line", nondc, drg)
        add("", case, "4", E, ["DEN-06"], "Modifier denial but the claim looks right", None, inj)
        add("", case, "16", E, ["DEN-06"], "Chart sources disagree on the eye", admin, drg)
        add("", short_gap)
        add("", case, "151", E, ["DEN-08"], "Frequency denial but the previous injection was 8 weeks earlier", whole=True)
        add("", case, "50", E, ["DEN-09"], "Medical necessity denial", whole=True)
        add("", case, "50", H, ["DEN-09"], "Medical necessity denial", whole=True)
        add("", case, "27", E, ["DEN-10"], "Coverage was active on the date of service", whole=True)
        add("", case, "27", dict(payer=ffs, drug=BEV, eyes=[OS]), ["DEN-11"], "Claim payer's coverage had ended before the date of service", lapsed, whole=True)
        add("", case, "29", E, ["DEN-12"], "Timely filing denial: code not handled by the triage", whole=True)
        add("", case, "96", E, ["DEN-12"], "Unlisted denial code", whole=True)

    # ------------------------------------------------------------ outputs
    def dataset(self) -> Dataset:
        return Dataset(as_of=self.as_of, practice=PRACTICE, patients=self.patients, coverages=self.coverages,
                       policies=self.pols, prior_auths=self.auths, plans=self.plans, injections=self.injs,
                       appointments=self.appts, notes=self.notes, claims=self.claims, denials=self.denials)

    def manifest(self) -> dict:
        lo, hi = self.as_of, self.as_of + timedelta(days=7)
        draft = [c for c in self.claims if c.status == "draft" and self.as_of - timedelta(days=10) <= c.dos < self.as_of]
        win = [a for a in self.appts if a.kind == "injection" and a.status == "booked" and lo <= a.date <= hi]
        return {"as_of": self.as_of.isoformat(), "seed": self.seed, "practice": PRACTICE,
                "counts": {"patients": len(self.patients), "plans": len(self.plans), "injections": len(self.injs),
                           "appointments": len(self.appts), "clearance_appointments": len(win),
                           "claims": len(self.claims), "draft_claims": len(draft), "denials": len(self.denials),
                           "planted": len(self.planted), "near_misses": len(self.near),
                           "planted_by_stage": {s: sum(1 for p in self.planted if p["stage"] == s)
                                                for s in ("cadence", "clearance", "claim", "denial")}},
                "planted": sorted(self.planted, key=lambda p: (p["stage"], p["item_id"])),
                "near_misses": sorted(self.near, key=lambda p: p["item_id"])}

    def bundle(self) -> dict:
        ref = lambda t, i: {"reference": f"{t}/{i}"}
        iso = lambda d: d.isoformat()
        sn = lambda e: {"coding": [{"system": SN, "code": EYE_SNOMED[e]}]}
        drug_cc = lambda d: {"system": "urn:visentia:drug", "code": d.value}
        res: list[dict] = []
        for p in self.patients:
            first, family = p.name.split(" ", 1)
            r = {"resourceType": "Patient", "id": p.id, "identifier": [{"system": "urn:visentia:token", "value": p.token}],
                 "name": [{"family": family, "given": [first]}], "birthDate": iso(p.birth_date)}
            if p.phone:
                r["telecom"] = [{"system": "phone", "value": p.phone}]
            res.append(r)
        for c in self.coverages:
            period = {"start": iso(c.period_start)}
            if c.period_end:
                period["end"] = iso(c.period_end)
            res.append({"resourceType": "Coverage", "id": c.id, "status": "active", "beneficiary": ref("Patient", c.patient_id),
                        "payor": [ref("Organization", c.payer_id)], "class": [{"type": {"coding": [{"code": "plan"}]}, "name": c.plan_name}],
                        "subscriberId": c.member_id, "period": period})
        for a in self.auths:
            r = {"resourceType": "ClaimResponse", "id": a.id, "status": "active", "type": {"coding": [{"code": "professional"}]},
                 "use": "preauthorization", "patient": ref("Patient", a.patient_id), "insurer": ref("Organization", a.payer_id),
                 "outcome": {"approved": "complete", "pending": "queued", "denied": "error"}[a.status],
                 "preAuthRef": a.auth_number, "created": iso(a.requested_on or self.as_of)}
            if a.valid_from or a.valid_to:
                r["preAuthPeriod"] = {k: iso(v) for k, v in (("start", a.valid_from), ("end", a.valid_to)) if v}
            r["extension"] = ([{"url": "urn:visentia:pa-drug", "valueCode": a.drug.value}]
                              + [{"url": "urn:visentia:pa-eye", "valueCode": e.value} for e in a.eyes]
                              + ([{"url": "urn:visentia:pa-injections-approved", "valueInteger": a.injections_approved}]
                                 if a.injections_approved is not None else [])
                              + [{"url": "urn:visentia:pa-injections-used", "valueInteger": a.injections_used},
                                 {"url": "urn:visentia:pa-step-exception", "valueBoolean": a.step_therapy_exception}]
                              + ([{"url": "urn:visentia:pa-requested-on", "valueDate": iso(a.requested_on)}] if a.requested_on else []))
            res.append(r)
        for t in self.plans:
            res.append({"resourceType": "MedicationRequest", "id": t.id, "status": "active", "intent": "plan",
                        "subject": ref("Patient", t.patient_id), "medicationCodeableConcept": {"coding": [drug_cc(t.drug)]},
                        "authoredOn": iso(t.updated_on), "requester": {"display": t.physician},
                        "reasonCode": [{"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": t.diagnosis_code}]}],
                        "dosageInstruction": [{"site": sn(t.eye), "timing": {"repeat": {"period": t.interval_weeks, "periodUnit": "wk"}}}]})
        for i in self.injs:
            pr = {"resourceType": "Procedure", "id": i.id, "status": "completed", "subject": ref("Patient", i.patient_id),
                  "performedDateTime": iso(i.date), "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": "67028"}]},
                  "bodySite": [sn(i.eye)]}
            if i.appointment_id:
                pr["extension"] = [{"url": "urn:visentia:appointment", "valueReference": ref("Appointment", i.appointment_id)}]
            res.append(pr)
        for i in self.injs:
            coding = [drug_cc(i.drug)] + ([{"system": "http://hl7.org/fhir/sid/ndc", "code": i.ndc}] if i.ndc else [])
            res.append({"resourceType": "MedicationAdministration", "id": i.medadmin_id, "status": "completed",
                        "subject": ref("Patient", i.patient_id), "effectiveDateTime": iso(i.date),
                        "medicationCodeableConcept": {"coding": coding}, "partOf": [ref("Procedure", i.id)],
                        "dosage": {"dose": {"value": i.dose_mg, "unit": "mg"}, "site": sn(i.admin_eye)}})
        for a in self.appts:
            ext = [{"url": "urn:visentia:planned-injection",
                    "extension": [{"url": "urn:visentia:eye", "valueCode": pl.eye.value},
                                  {"url": "urn:visentia:drug", "valueCode": pl.drug.value}]} for pl in a.planned]
            ext.append({"url": "urn:visentia:location", "valueString": a.location})
            res.append({"resourceType": "Appointment", "id": a.id, "status": a.status,
                        "appointmentType": {"coding": [{"code": a.kind}]}, "start": f"{iso(a.date)}T{a.time}:00",
                        "participant": [{"actor": ref("Patient", a.patient_id), "status": "accepted"},
                                        {"actor": {"display": a.provider}, "status": "accepted"}], "extension": ext})
        for n in self.notes:
            res.append({"resourceType": "DocumentReference", "id": n.id, "status": "current", "subject": ref("Patient", n.patient_id),
                        "date": iso(n.date), "author": [{"display": n.author}],
                        "content": [{"attachment": {"contentType": "text/plain", "data": base64.b64encode(n.text.encode()).decode()}}]})
        cov_of = {}
        for c in self.coverages:
            cov_of.setdefault((c.patient_id, c.payer_id), c.id)
        for c in self.claims:
            dx: list[str] = []
            for l in c.lines:
                dx += [x for x in l.dx_codes if x not in dx]
            items = []
            for l in c.lines:
                it = {"sequence": l.seq, "productOrService": {"coding": [{"system": "urn:visentia:code", "code": l.code}]},
                      "quantity": {"value": l.units}, "net": {"value": l.charge}}
                if l.modifiers:
                    it["modifier"] = [{"coding": [{"code": m}]} for m in l.modifiers]
                if l.dx_codes:
                    it["diagnosisSequence"] = [dx.index(x) + 1 for x in l.dx_codes]
                if l.ndc:
                    it["extension"] = [{"url": "urn:visentia:ndc", "valueString": l.ndc}]
                items.append(it)
            ins = {"sequence": 1, "focal": True, "coverage": ref("Coverage", cov_of.get((c.patient_id, c.payer_id), ""))}
            if c.auth_number:
                ins["preAuthRef"] = [c.auth_number]
            ext = [{"url": "urn:visentia:claim-status", "valueCode": c.status}]
            if c.appointment_id:
                ext.append({"url": "urn:visentia:appointment", "valueReference": ref("Appointment", c.appointment_id)})
            res.append({"resourceType": "Claim", "id": c.id, "status": "active", "type": {"coding": [{"code": "professional"}]},
                        "use": "claim", "patient": ref("Patient", c.patient_id), "created": iso(c.dos),
                        "insurer": ref("Organization", c.payer_id), "provider": {"display": PRACTICE}, "priority": {"coding": [{"code": "normal"}]},
                        "billablePeriod": {"start": iso(c.dos)}, "insurance": [ins], "extension": ext,
                        "diagnosis": [{"sequence": k + 1, "diagnosisCodeableConcept": {"coding": [{"code": x}]}} for k, x in enumerate(dx)],
                        "item": items})
        by_claim = {c.id: c for c in self.claims}
        for d in self.denials:
            c = by_claim.get(d.claim_id)
            item = {"adjudication": [{"category": {"coding": [{"code": "denied"}]},
                                      "reason": {"coding": [{"system": "urn:visentia:carc", "code": d.carc}]},
                                      "amount": {"value": d.amount}}]}
            if d.line_seq is not None:
                item["itemSequence"] = d.line_seq
            ext = [{"url": "urn:visentia:carc-group", "valueCode": d.group}]
            if d.rarc:
                ext.append({"url": "urn:visentia:rarc", "valueCode": d.rarc})
            res.append({"resourceType": "ClaimResponse", "id": d.id, "status": "active", "type": {"coding": [{"code": "professional"}]},
                        "use": "claim", "patient": ref("Patient", c.patient_id if c else self.patients[0].id),
                        "created": iso(d.date), "insurer": ref("Organization", c.payer_id if c else "medicare-ffs"),
                        "request": ref("Claim", d.claim_id), "outcome": "error", "item": [item], "extension": ext})
        return {"resourceType": "Bundle", "type": "collection", "timestamp": f"{self.as_of.isoformat()}T00:00:00Z",
                "entry": [{"fullUrl": f"{r['resourceType']}/{r['id']}", "resource": r} for r in res]}


def build_world(seed: int = 7, as_of: date = date(2026, 11, 18)) -> World:
    w = World(seed, as_of)
    w.run()
    return w


def generate(seed: int = 7, as_of: date = date(2026, 11, 18)) -> tuple[dict, list, dict]:
    w = build_world(seed, as_of)
    return w.bundle(), [p.model_dump(mode="json") for p in w.pols], w.manifest()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.synth")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--as-of", default="2026-11-18")
    ap.add_argument("--out", default="data/synthetic")
    a = ap.parse_args(argv)
    bundle, pols, manifest = generate(a.seed, date.fromisoformat(a.as_of))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, obj in (("bundle.json", bundle), ("policies.json", pols), ("manifest.json", manifest)):
        (out / name).write_text(json.dumps(obj, indent=1) + "\n")
    print(json.dumps(manifest["counts"]))


if __name__ == "__main__":
    main()
