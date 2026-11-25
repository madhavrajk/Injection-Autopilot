"""Model layer (spec section 5). Stub-first: with no ANTHROPIC_API_KEY, or AUTOPILOT_MODEL=stub, a deterministic
rule-based stub runs, so tests and evals cost nothing and never touch the network.

Where the model stops: it reads a REDACTED note into proposals (checked by `_parse_extraction`) and writes a TEMPLATE
with {placeholders}. Code fills every fact (eye, drug, date, number) and re-checks the template (`verify_template`).
"""
from __future__ import annotations

import calendar
import json
import os
import re
import time
from dataclasses import dataclass, field
from string import Formatter
from typing import Callable, Optional

from app.contracts import (DRUGS, ClinicNote, Coverage, Draft, Drug, ExtractedPlan, Eye, ModelCall, Patient)

PROMPT_LOG: list[dict] = []     # every prompt, stub or real: {"task", "prompt"}; read by the leak check
SPENT_USD = 0.0                 # running real-mode spend; over the budget everything uses the stub

DEFAULT_EXTRACT_MODEL = "claude-haiku-5-5"
DEFAULT_DRAFT_MODEL = "claude-sonnet-5-5"

# USD per million tokens (input, output). Assumed list prices; the one place to edit them.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (1.0, 5.0),
    "claude-sonnet-5-5": (3.0, 15.0),
    "default": (3.0, 15.0),
}


def reset_log() -> None:
    PROMPT_LOG.clear()


def mode() -> str:
    """"stub" unless a key is set and AUTOPILOT_MODEL is not "stub"; otherwise the model id in use."""
    if not os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("AUTOPILOT_MODEL", "").strip().lower() == "stub":
        return "stub"
    return os.environ.get("AUTOPILOT_MODEL") or os.environ.get("AUTOPILOT_EXTRACT_MODEL") or DEFAULT_EXTRACT_MODEL


# ---------------------------------------------------------------- calling a model (the only network code)

def _complete_real(model: str, system: str, user: str, max_tokens: int) -> tuple[str, int, int]:
    """One Anthropic call. Returns (text, input tokens, output tokens). The SDK is imported here only."""
    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                      messages=[{"role": "user", "content": user}])
    return response.content[0].text, response.usage.input_tokens, response.usage.output_tokens


def _budget() -> float:
    try:
        return float(os.environ.get("AUTOPILOT_BUDGET_USD", "1.00"))
    except ValueError:
        return 1.0


def _cost(model: str, tokens_in: int, tokens_out: int) -> float:
    price_in, price_out = PRICES.get(model, PRICES["default"])
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000


def _complete(task: str, system: str, user: str, model_env: str, default_model: str, max_tokens: int,
              stub: Callable[[], str]) -> tuple[str, ModelCall]:
    """Log the prompt, then answer with the real model when allowed, else with `stub()`. Never raises."""
    global SPENT_USD
    prompt = f"{system}\n\n{user}"
    PROMPT_LOG.append({"task": task, "prompt": prompt})
    started = time.perf_counter()
    label, text, tokens_in, tokens_out, cost = "stub", None, 0, 0, 0.0
    if mode() != "stub":
        if SPENT_USD > _budget():
            label = "stub (budget exceeded)"
        else:
            model = os.environ.get(model_env) or default_model
            try:
                text, tokens_in, tokens_out = _complete_real(model, system, user, max_tokens)
                label, cost = model, _cost(model, tokens_in, tokens_out)
                SPENT_USD += cost
            except Exception as exc:  # any failure: the stub answers and the label says so
                text, label = None, f"stub (fallback: {type(exc).__name__})"
    if text is None:
        text = stub()
        tokens_in, tokens_out, cost = len(prompt) // 4, len(text) // 4, 0.0
    call = ModelCall(task=task, model=label, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost,
                     ms=(time.perf_counter() - started) * 1000)
    return text, call


# ---------------------------------------------------------------- redaction

_MONTHS = {m: [calendar.month_abbr[m], calendar.month_name[m]] + (["Sept"] if m == 9 else []) for m in range(1, 13)}


def _dob_patterns(d) -> list[str]:
    month = "(?:" + "|".join(_MONTHS[d.month]) + r")\.?"
    day = rf"0?{d.day}(?:st|nd|rd|th)?"
    return [
        rf"(?<!\d){d.year:04d}-{d.month:02d}-{d.day:02d}(?!\d)",
        rf"(?<!\d)0?{d.month}/0?{d.day}/(?:{d.year:04d}|{d.year % 100:02d})(?!\d)",
        rf"\b{month}\s+{day},?\s+{d.year:04d}\b",
        rf"\b{day}\s+{month},?\s+{d.year:04d}\b",
    ]


def _phone_pattern(phone: str) -> Optional[str]:
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 7:
        return None
    prefix = r"(?:\+?1[\s.-]*)?" if len(digits) == 10 else ""
    return r"(?<!\d)" + prefix + r"\(?" + r"[\s().-]*".join(digits) + r"(?!\d)"


def _name_pattern(name: str) -> Optional[str]:
    words = [w.strip(".,") for w in name.split()]
    parts = {p for w in words for p in [w] + w.split("-") if len(p) >= 2}
    if not parts:
        return None
    candidates = sorted(parts, key=len, reverse=True)
    if len(words) > 1:
        candidates.insert(0, r"\s+".join(re.escape(w) for w in words))
        candidates = [c if i == 0 else re.escape(c) for i, c in enumerate(candidates)]
    else:
        candidates = [re.escape(c) for c in candidates]
    return r"(?<!\w)(?:" + "|".join(candidates) + r")(?!\w)"


def redact(text: str, patient: Patient, coverage: Optional[Coverage] = None) -> str:
    """Replace name parts, birth date, phone and member ID. Drug names and clinical words are never touched."""
    if coverage and coverage.member_id:
        chars = [re.escape(c) for c in coverage.member_id if c.isalnum()]
        if chars:
            text = re.sub(r"(?<![A-Za-z0-9])" + r"[\s-]?".join(chars) + r"(?![A-Za-z0-9])", "[MEMBER]", text,
                          flags=re.I)
    phone = _phone_pattern(patient.phone) if patient.phone else None
    if phone:
        text = re.sub(phone, "[PHONE]", text)
    for pattern in _dob_patterns(patient.birth_date):
        text = re.sub(pattern, "[DOB]", text, flags=re.I)
    names = _name_pattern(patient.name)
    if names:
        text = re.sub(names, patient.token, text, flags=re.I)
    return text


# ---------------------------------------------------------------- note grammar (the stub's reader)

_EYE_RE = re.compile(r"\b(?P<ou>OU|both\s+eyes)\b|\b(?P<od>OD|right\s+eye)\b|\b(?P<os>OS|left\s+eye)\b", re.I)
_HISTORY = re.compile(r"\b(?:previously|prior|was\s+on|were\s+on|status\s+post|history\s+of|hx\s+of|formerly)\b"
                      r"|(?<!\w)s/p(?!\w)", re.I)
_PLAN_VERB = re.compile(r"\b(?:continue|switch|start|extend|maintain|change|resume|begin)\b", re.I)
_DELIM = re.compile(r"\s*(?:([;,:])|\band\b)\s*", re.I)
_ABBREV = {"dr", "mr", "mrs", "ms", "vs", "pt", "approx"}

_DRUG_PATTERNS: list[tuple[str, Optional[Drug]]] = [
    (r"eylea\s*-?\s*(?:hd|8\s*mg)", Drug.AFLIBERCEPT_8MG),                 # before plain Eylea
    (r"aflibercept\s*-?\s*\(?\s*(?:8\s*mg|hd)", Drug.AFLIBERCEPT_8MG),
    (r"aflibercept\s*-?\s*\(?\s*2\s*mg", Drug.AFLIBERCEPT_2MG),
    (r"eylea(?:\s*2\s*mg)?", Drug.AFLIBERCEPT_2MG),
    (r"aflibercept", None),                                                # dose not stated: never guessed
    (r"vabysmo|faricimab", Drug.FARICIMAB),
    (r"lucentis|ranibizumab", Drug.RANIBIZUMAB),
    (r"avastin|bevacizumab", Drug.BEVACIZUMAB),
]
_DRUG_SCAN = re.compile(r"\b(?:" + "|".join(f"(?P<d{i}>{p})" for i, (p, _) in enumerate(_DRUG_PATTERNS)) + r")\b", re.I)

_UNIT = r"(?:weeks?|wks?|w)"
# (regex, multiplier, fixed weeks, always preferred). Group 1 is the number when there is one.
_INTERVAL_PATTERNS: list[tuple[re.Pattern, int, Optional[int], bool]] = [
    (re.compile(rf"\bq\s*(\d{{1,2}})\s*{_UNIT}\b", re.I), 1, None, False),
    (re.compile(rf"\bevery\s+(\d{{1,2}})\s*-?\s*{_UNIT}\b", re.I), 1, None, False),
    (re.compile(rf"\b(\d{{1,2}})\s*-?\s*{_UNIT}\s*-?\s*(?:interval|dosing|schedule|cycle|regimen)s?\b", re.I), 1, None, False),
    (re.compile(rf"\binterval\s*(?:of|:|=)?\s*(\d{{1,2}})\s*-?\s*{_UNIT}\b", re.I), 1, None, False),
    (re.compile(rf"\b(?:extend(?:ed|ing)?|shorten(?:ed|ing)?|lengthen(?:ed|ing)?|increase|decrease|reduce|space)\b"
                rf"[^.;:]{{0,40}}?\bto\s+(?:every\s+)?(\d{{1,2}})\s*-?\s*{_UNIT}\b", re.I), 1, None, True),
    (re.compile(r"\bevery\s+(\d{1,2})\s*months?\b", re.I), 4, None, False),
    (re.compile(r"\bmonthly\b|\bevery\s+month\b", re.I), 1, 4, False),
]


def _eyes(text: str) -> set[Eye]:
    found: set[Eye] = set()
    for m in _EYE_RE.finditer(text):
        if m.group("ou"):
            found |= {Eye.OD, Eye.OS}
        elif m.group("od"):
            found.add(Eye.OD)
        else:
            found.add(Eye.OS)
    return found


def _only_eyes(clause: str) -> bool:
    """A clause that is nothing but eye words ("OS", "and both eyes")."""
    rest = re.sub(r"\b(?:the|and|or|both|eyes?|in|of|for)\b", " ", _EYE_RE.sub(" ", clause), flags=re.I)
    return bool(_eyes(clause)) and not re.search(r"\w", rest)


def _drug_of_match(m: re.Match) -> Optional[Drug]:
    return _DRUG_PATTERNS[int(m.lastgroup[1:])][1]


def _pick_drug(text: str) -> tuple[bool, Optional[Drug]]:
    """(any drug word seen, the drug). A drug after "to" wins ("switch from Eylea to Vabysmo"); a clash gives None."""
    seen = [(m.start(), _drug_of_match(m)) for m in _DRUG_SCAN.finditer(text)]
    if not seen:
        return False, None
    after_to = [d for pos, d in seen if re.search(r"\bto\s+$", text[max(0, pos - 8):pos], re.I)]
    values = set(after_to or [d for _, d in seen])
    return True, (values.pop() if len(values) == 1 else None)


def _pick_interval(text: str) -> Optional[int]:
    """The interval in weeks. One after "to" / "extend to" wins; otherwise the last one written."""
    found: list[tuple[int, int, bool]] = []
    for pattern, mult, fixed, preferred in _INTERVAL_PATTERNS:
        for m in pattern.finditer(text):
            weeks = fixed if fixed is not None else int(m.group(1)) * mult
            if 1 <= weeks <= 52:
                to_before = bool(re.search(r"\bto\s+$", text[max(0, m.start() - 8):m.start()], re.I))
                found.append((m.start(), weeks, preferred or to_before))
    if not found:
        return None
    pool = [f for f in found if f[2]] or found
    return max(pool)[1]


def _sentences(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for line in re.finditer(r"[^\n]+", text):
        s, base, pos = line.group(), line.start(), 0
        for m in re.finditer(r"(?<=[.!?])\s+", s):
            last_word = re.search(r"(\w+)\.$", s[pos:m.start()])
            if last_word and last_word.group(1).lower() in _ABBREV:
                continue
            spans.append((base + pos, base + m.start()))
            pos = m.end()
        spans.append((base + pos, base + len(s)))
    out = []
    for a, b in spans:
        seg = text[a:b]
        a, b = a + len(seg) - len(seg.lstrip()), b - (len(seg) - len(seg.rstrip()))
        if b > a:
            out.append((a, b))
    return out


def _clauses(text: str, a: int, b: int) -> list[tuple[int, int, str]]:
    out, pos = [], a
    for m in _DELIM.finditer(text, a, b):
        out.append((pos, m.start(), m.group(1) or "and"))
        pos = m.end()
    out.append((pos, b, ""))
    return [c for c in out if text[c[0]:c[1]].strip()]


@dataclass
class _Group:
    eyes: set
    start: int
    end: int
    dropped: bool = False


def _has_plan_content(text: str) -> bool:
    return _pick_drug(text)[0] or _pick_interval(text) is not None


def _groups(text: str, a: int, b: int) -> tuple[list[_Group], str]:
    """Split one sentence into per-eye groups of clauses. Returns (groups, text of leading eyeless clauses)."""
    groups: list[_Group] = []
    pending: set = set()
    pending_start = 0
    leading: list[str] = []
    force_drop = False
    for cs, ce, delim in _clauses(text, a, b):
        clause = text[cs:ce]
        eyes = _eyes(clause)
        if force_drop or _HISTORY.search(clause):
            groups.append(_Group(eyes, cs, ce, dropped=True))
            force_drop = delim == ":" and not force_drop      # "Prior treatment: ..." drops what follows
            pending = set()
        elif eyes and _only_eyes(clause):
            last = groups[-1] if groups else None
            if last and not last.dropped and _has_plan_content(text[last.start:last.end]):
                last.eyes |= eyes
                last.end = ce
            else:
                pending_start = pending_start if pending else cs
                pending |= eyes
        elif eyes:
            groups.append(_Group(eyes | pending, pending_start if pending else cs, ce))
            pending = set()
        elif pending:
            groups.append(_Group(set(pending), pending_start, ce))
            pending = set()
        elif groups:
            last = groups[-1]
            if last.dropped and last.eyes and _PLAN_VERB.search(clause):
                groups.append(_Group(set(last.eyes), cs, ce))   # "Was on Eylea OS; switch to Vabysmo q6w"
            else:
                last.end = ce
        else:
            leading.append(clause)
    return groups, " ".join(leading)


def _read_sentence(text: str, a: int, b: int) -> list[dict]:
    groups, leading = _groups(text, a, b)
    live = [g for g in groups if not g.dropped]
    _, default_drug = _pick_drug(leading)
    default_interval = _pick_interval(leading)
    out = []
    for g in live:
        chunk = text[g.start:g.end]
        _, drug = _pick_drug(chunk)
        interval = _pick_interval(chunk)
        drug = drug or default_drug
        interval = interval or default_interval
        if drug is None and interval is None:
            continue
        quote = text[a:b] if len(groups) == 1 else chunk.strip(" ,;:")
        for eye in sorted(g.eyes, key=lambda e: e.value):
            out.append({"eye": eye.value, "drug": drug.value if drug else None, "interval_weeks": interval,
                        "quote": quote})
    return out


def _stub_extract(redacted: str) -> str:
    entries: list[dict] = []
    for a, b in _sentences(redacted):
        entries += _read_sentence(redacted, a, b)
    return json.dumps(entries)


# ---------------------------------------------------------------- extraction (stub and real share the grounding check)

_EXTRACT_SYSTEM = (
    "You read one retina clinic note with identifiers removed and list its treatment plan. Reply with JSON only: a "
    "list of objects with the keys eye (OD or OS, one object per eye), drug (one of aflibercept_2mg, "
    "aflibercept_8mg, faricimab, ranibizumab, bevacizumab, or null), interval_weeks (an integer or null) and quote "
    "(the exact sentence or clause copied from the note). Eylea is aflibercept_2mg and Eylea HD is aflibercept_8mg. "
    "Skip history (previously, prior, was on, s/p, history of). Never guess a drug or an interval that is not written."
)


def _drug_from_value(value) -> Optional[Drug]:
    if value is None:
        return None
    try:
        return Drug(value)
    except ValueError:
        pass
    m = _DRUG_SCAN.fullmatch(str(value).strip())
    return _drug_of_match(m) if m else None


def _interval_from_value(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        weeks = int(value)
    except (TypeError, ValueError):
        return None
    return weeks if 1 <= weeks <= 52 else None


def _parse_extraction(text: str, redacted: str) -> list[ExtractedPlan]:
    """Defensive parse plus the grounding check: quote is a substring of the redacted note, an eye is present,
    drug and interval parse or become None."""
    try:
        items = json.loads(text[text.index("["):text.rindex("]") + 1])
    except ValueError:
        return []
    out: list[ExtractedPlan] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        quote = item.get("quote")
        if not isinstance(quote, str) or not quote or quote not in redacted:
            continue
        eyes = _eyes(str(item.get("eye", "")))
        drug, interval = _drug_from_value(item.get("drug")), _interval_from_value(item.get("interval_weeks"))
        for eye in sorted(eyes, key=lambda e: e.value):
            plan = ExtractedPlan(eye=eye, drug=drug, interval_weeks=interval, quote=quote)
            if plan not in out:
                out.append(plan)
    return out


def extract_plan(note: ClinicNote, patient: Patient,
                 coverage: Optional[Coverage] = None) -> tuple[list[ExtractedPlan], ModelCall]:
    redacted = redact(note.text, patient, coverage)
    text, call = _complete("extract_plan", _EXTRACT_SYSTEM, f"Clinic note:\n{redacted}", "AUTOPILOT_EXTRACT_MODEL",
                           DEFAULT_EXTRACT_MODEL, 1000, lambda: _stub_extract(redacted))
    return _parse_extraction(text, redacted), call


# ---------------------------------------------------------------- templates

_Option = tuple[tuple[str, ...], str]     # (fact keys needed, text); the first option whose keys exist is used
_BLANK: list[_Option] = [((), "")]


def _one(*options: _Option) -> list[_Option]:
    return list(options)


_PATIENT_LINES = [
    _one((("patient_name",), "Patient: {patient_name}")),
    _one((("birth_date",), "Date of birth: {birth_date}")),
    _one((("member_id",), "Member ID: {member_id}")),
    _one((("plan_name",), "Plan: {plan_name}")),
]
_DIAGNOSIS = _one((("diagnosis_label", "diagnosis_code"), "Diagnosis: {diagnosis_label} ({diagnosis_code})"),
                  (("diagnosis_label",), "Diagnosis: {diagnosis_label}"),
                  (("diagnosis_code",), "Diagnosis code: {diagnosis_code}"))

_TEMPLATES: dict[str, dict] = {
    "pa_request": {
        "subject": _one((("drug_brand", "eye"), "Prior authorization request: {drug_brand}, {eye}"),
                        (("drug_brand",), "Prior authorization request: {drug_brand}"),
                        ((), "Prior authorization request")),
        "lines": [
            _one((("payer_name",), "To: {payer_name}, Prior Authorization Department"),
                 ((), "To: Prior Authorization Department")),
            _one((("practice",), "From: {practice}")),
            _BLANK,
            _one(((), "Re: Request for prior authorization of intravitreal injection therapy")),
            *_PATIENT_LINES,
            _BLANK,
            _DIAGNOSIS,
            _one((("drug_brand", "drug_generic"), "Requested treatment: {drug_brand} ({drug_generic}), intravitreal injection"),
                 (("drug_brand",), "Requested treatment: {drug_brand}, intravitreal injection"),
                 (("drug_generic",), "Requested treatment: {drug_generic}, intravitreal injection")),
            _one((("hcpcs",), "HCPCS code: {hcpcs}")),
            _one((("eye_words", "eye"), "Eye: {eye_words} ({eye})"), (("eye",), "Eye: {eye}"),
                 (("eye_words",), "Eye: {eye_words}")),
            _one((("interval_weeks",), "Planned treatment interval: every {interval_weeks} weeks")),
            _one((("last_injection_date",), "Most recent injection: {last_injection_date}")),
            _one((("visit_date",), "Requested date of service: {visit_date}"),
                 (("proposed_date",), "Requested date of service: {proposed_date}")),
            _one((("auth_number",), "Existing authorization for reference: {auth_number}")),
            _one((("physician",), "Ordering physician: {physician}")),
            _BLANK,
            _one((("finding",), "Reason for this request: {finding}"),
                 ((), "Reason for this request: please see the enclosed chart documentation.")),
            _BLANK,
            _one(((), "Please review this request and confirm your determination at your earliest convenience. "
                      "Supporting clinic notes are available on request.")),
            _BLANK,
            _one((("practice",), "Sincerely,\nAuthorization Coordinator, {practice}"),
                 ((), "Sincerely,\nAuthorization Coordinator")),
        ],
    },
    "appeal": {
        "subject": _one((("denial_code", "claim_id"), "Appeal of denial {denial_code}: claim {claim_id}"),
                        (("claim_id",), "Appeal of denied claim {claim_id}"),
                        ((), "Appeal of denied claim")),
        "lines": [
            _one((("payer_name",), "To: {payer_name}, Appeals Department"), ((), "To: Appeals Department")),
            _one((("practice",), "From: {practice}")),
            _BLANK,
            _one(((), "Re: Request for reconsideration of a denied claim")),
            _one((("claim_id",), "Claim number: {claim_id}")),
            *_PATIENT_LINES,
            _one((("visit_date",), "Date of service: {visit_date}")),
            _one((("auth_number",), "Authorization number: {auth_number}")),
            _one((("denial_date",), "Date of denial: {denial_date}")),
            _one((("amount",), "Amount denied: {amount}")),
            _BLANK,
            _one((("denial_code", "denial_reason"), "The claim was denied with reason code {denial_code} ({denial_reason})."),
                 (("denial_code",), "The claim was denied with reason code {denial_code}."),
                 ((), "The claim was denied.")),
            _one((("finding",), "We believe this denial is incorrect. {finding}"),
                 ((), "We believe this denial is incorrect, as the enclosed documentation shows.")),
            _one(((), "We request that the claim be reprocessed and paid according to the member's benefits.")),
            _one((("appeal_deadline",), "Please note that the appeal filing deadline is {appeal_deadline}.")),
            _BLANK,
            _one(((), "Enclosures:")),
            _one(((), "- Copy of the denial notice")),
            _one(((), "- Clinic note and injection procedure record for the date of service")),
            _one((("auth_number",), "- Authorization approval")),
            _BLANK,
            _one((("practice",), "Sincerely,\nBilling Department, {practice}"), ((), "Sincerely,\nBilling Department")),
        ],
    },
    "step_exception": {
        "subject": _one((("drug_brand",), "Step therapy exception request: {drug_brand}"),
                        ((), "Step therapy exception request")),
        "lines": [
            _one((("payer_name",), "To: {payer_name}, Medical Director"), ((), "To: Medical Director")),
            _BLANK,
            _one(((), "Re: Request for a step therapy exception")),
            *_PATIENT_LINES,
            _DIAGNOSIS,
            _BLANK,
            _one((("drug_brand", "eye_words"), "I am writing to request an exception to the step therapy requirement "
                                                "so that my patient can receive treatment with {drug_brand} in the {eye_words}."),
                 (("drug_brand",), "I am writing to request an exception to the step therapy requirement "
                                   "so that my patient can receive treatment with {drug_brand}."),
                 ((), "I am writing to request an exception to the step therapy requirement for my patient.")),
            _one((("finding",), "Reason for this request: {finding}"),
                 ((), "My clinical rationale is documented in the enclosed chart notes.")),
            _one((("visit_date",), "The next treatment is planned for {visit_date}."),
                 (("proposed_date",), "The next treatment is planned for {proposed_date}.")),
            _one(((), "I ask that you approve this exception. I am available for a peer-to-peer discussion "
                      "at your convenience.")),
            _BLANK,
            _one((("physician", "practice"), "Sincerely,\n{physician}\n{practice}"),
                 (("physician",), "Sincerely,\n{physician}"),
                 (("practice",), "Sincerely,\nTreating physician, {practice}"),
                 ((), "Sincerely,\nTreating physician")),
        ],
    },
    "outreach_sms": {
        "subject": _one((("practice",), "Appointment reminder from {practice}"), ((), "Appointment reminder")),
        "lines": [
            _one((("practice", "patient_name"), "{practice}: Hello {patient_name}."),
                 (("practice",), "{practice}: Hello."),
                 (("patient_name",), "Hello {patient_name}."),
                 ((), "Hello.")),
            _one((("proposed_date",), "We would like to schedule your retina appointment and have a time "
                                      "available on {proposed_date}."),
                 ((), "We would like to schedule your retina appointment.")),
            _one(((), "Please call our office at your earliest convenience to confirm a time that works for you. "
                      "Reply STOP to opt out.")),
        ],
    },
}
KINDS = tuple(_TEMPLATES)
SMS_LIMIT = 300


def _pick(options: list[_Option], available: set[str]) -> Optional[str]:
    return next((text for keys, text in options if set(keys) <= available), None)


def _assemble(kind: str, available: set[str]) -> tuple[str, str]:
    spec = _TEMPLATES[kind]
    lines = [t for t in (_pick(opts, available) for opts in spec["lines"]) if t is not None]
    collapsed: list[str] = []
    for line in lines:                       # no leading, trailing or doubled blank lines
        if line == "" and (not collapsed or collapsed[-1] == ""):
            continue
        collapsed.append(line)
    while collapsed and collapsed[-1] == "":
        collapsed.pop()
    body = (" " if kind == "outreach_sms" else "\n").join(collapsed)
    return _pick(spec["subject"], available) or "", body


def _stub_template(kind: str, facts: dict[str, str]) -> str:
    available = set(facts)
    subject, body = _assemble(kind, available)
    if kind == "outreach_sms":               # a text message must stay short: shed optional facts until it fits
        for shed in ("proposed_date", "patient_name", "practice"):
            if len(_render(body, facts)) < SMS_LIMIT:
                break
            available.discard(shed)
            subject, body = _assemble(kind, available)
    return f"SUBJECT: {subject}\n\n{body}"


_DRAFT_SYSTEM = (
    "You write a letter or message template for a retina practice. Write prose only, with placeholders in curly "
    "braces named exactly after the available fact keys. Never write a digit, a drug name, an eye designation, a date "
    "or an authorization number yourself: use a placeholder, and leave a line out when its key is not available. "
    "Reply with a first line that starts with SUBJECT: followed by the subject, a blank line, then the body."
)
_DRAFT_GUIDE = {
    "pa_request": "A prior authorization request from the authorization coordinator to the payer: patient, "
                  "diagnosis, requested drug and eye, clinical rationale from the finding key, requested date.",
    "appeal": "An appeal of a denied claim from the biller to the payer: identify the claim, state the denial reason "
              "code and why it is incorrect using the finding key, request reprocessing, list enclosed documentation.",
    "step_exception": "A letter from the treating physician asking the payer for a step therapy exception.",
    "outreach_sms": "A text message to the patient: under three hundred characters, the practice name, the words "
                    "your retina appointment, and no diagnosis and no drug.",
}


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _render(template: str, facts: dict[str, str]) -> str:
    try:
        return template.format_map(_Safe(facts))
    except (ValueError, IndexError, AttributeError, KeyError):
        return template


# ---------------------------------------------------------------- verification of a template

_DRUG_WORDS = sorted({w for info in DRUGS.values() for w in re.findall(r"[A-Za-z]+", f"{info.brand} {info.generic}")},
                     key=str.lower)
_DRUG_RE = re.compile(r"\b(" + "|".join(_DRUG_WORDS) + r")\b", re.I)
_EYE_ABBR_RE = re.compile(r"\b(OD|OS|OU)\b")                                  # uppercase only: "os", "od" are words
_EYE_PHRASE_RE = re.compile(r"\b(?:right\s+eye|left\s+eye|both\s+eyes)\b", re.I)


def verify_template(template: str, facts: dict[str, str]) -> list[str]:
    """Problems in a template before rendering. Empty list = clean."""
    try:
        parsed = list(Formatter().parse(template))
    except ValueError as exc:
        return [f"malformed placeholder syntax: {exc}"]
    problems: list[str] = []

    def add(message: str) -> None:
        if message not in problems:
            problems.append(message)

    for literal, field_name, _spec, _conv in parsed:
        for m in re.finditer(r"\d+", literal):
            add(f"digit '{m.group()}' written outside a placeholder")
        for m in _DRUG_RE.finditer(literal):
            add(f"drug name '{m.group()}' written outside a placeholder")
        for m in list(_EYE_ABBR_RE.finditer(literal)) + list(_EYE_PHRASE_RE.finditer(literal)):
            add(f"eye word '{m.group()}' written outside a placeholder")
        if field_name is not None and field_name not in facts:
            add(f"unknown placeholder {{{field_name}}}: not a key of facts")
    return problems


# ---------------------------------------------------------------- drafting

def _split_subject(text: str, kind: str) -> tuple[str, str]:
    first, _, rest = text.strip().partition("\n")
    if first.upper().startswith("SUBJECT:"):
        return first[len("SUBJECT:"):].strip(), rest.strip()
    return kind.replace("_", " ").capitalize(), text.strip()


def draft(kind: str, facts: dict[str, str]) -> Draft:
    """The model (or stub) writes a template from fact KEYS only; code renders it with the values and re-checks it."""
    if kind not in _TEMPLATES:
        raise ValueError(f"unknown draft kind {kind!r}; expected one of {', '.join(KINDS)}")
    facts = {k: str(v) for k, v in facts.items() if v is not None and str(v) != ""}
    user = (f"Kind: {kind.replace('_', ' ')}\n{_DRAFT_GUIDE[kind]}\n"
            f"Available fact keys: {', '.join(sorted(facts))}")
    text, call = _complete(f"draft_{kind}", _DRAFT_SYSTEM, user, "AUTOPILOT_DRAFT_MODEL", DEFAULT_DRAFT_MODEL, 1500,
                           lambda: _stub_template(kind, facts))
    subject_t, body_t = _split_subject(text, kind)
    problems = verify_template(subject_t, facts) + [p for p in verify_template(body_t, facts)]
    problems = list(dict.fromkeys(problems))
    used_fields = {f for t in (subject_t, body_t) for _, f, _, _ in Formatter().parse(t) if f} if not any(
        p.startswith("malformed") for p in problems) else set()
    return Draft(kind=kind, subject=_render(subject_t, facts), body=_render(body_t, facts),
                 facts={k: facts[k] for k in sorted(used_fields) if k in facts}, verified=not problems,
                 problems=problems, call=call)
