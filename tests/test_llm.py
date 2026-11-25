"""Model layer tests. All offline, all stub: the environment is forced before every test."""
from datetime import date

import pytest

from app import llm
from app.contracts import Coverage, ClinicNote, Drug, Eye, Patient

PATIENT = Patient(id="pt-0042", token="PT-0042", name="Margaret Okafor", birth_date=date(1948, 3, 7),
                  phone="(973) 555-0142")
COVERAGE = Coverage(id="cov-0042", patient_id="pt-0042", payer_id="ma-summit", plan_name="Summit Gold",
                    member_id="SUM-884213907", period_start=date(2026, 1, 1))

FULL_FACTS = {
    "patient_name": "Margaret Okafor", "birth_date": "Mar 7, 1948", "member_id": "SUM-884213907",
    "patient_token": "PT-0042", "payer_name": "Summit Medicare Advantage", "plan_name": "Summit Gold",
    "practice": "Garden State Retina (synthetic)", "physician": "Dr. Vatsal Doshi", "drug_brand": "Eylea HD",
    "drug_generic": "aflibercept 8 mg", "hcpcs": "J0177", "eye": "OD", "eye_words": "right eye",
    "diagnosis_code": "H35.3211", "diagnosis_label": "Exudative AMD, right eye, active CNV",
    "visit_date": "Nov 20, 2026", "last_injection_date": "Sep 25, 2026", "interval_weeks": "8",
    "auth_number": "AUTH-7731902", "claim_id": "clm-0107", "denial_code": "CO-197",
    "denial_reason": "Precertification or authorization absent", "denial_date": "Nov 10, 2026",
    "appeal_deadline": "May 9, 2027", "amount": "$2,410.00",
    "finding": "An approved authorization covering this drug, eye and date was on file.",
    "proposed_date": "Dec 4, 2026",
}
KINDS = ["pa_request", "appeal", "outreach_sms", "step_exception"]


@pytest.fixture(autouse=True)
def stub_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("AUTOPILOT_MODEL", "stub")
    llm.reset_log()
    yield
    llm.reset_log()


def note(text: str) -> ClinicNote:
    return ClinicNote(id="doc-1", patient_id="pt-0042", date=date(2026, 11, 1), author="Dr. Vatsal Doshi", text=text)


def extract(text: str):
    plans, call = llm.extract_plan(note(text), PATIENT, COVERAGE)
    return plans, call


def as_tuples(plans):
    return [(p.eye, p.drug, p.interval_weeks) for p in plans]


A2, A8, VAB = Drug.AFLIBERCEPT_2MG, Drug.AFLIBERCEPT_8MG, Drug.FARICIMAB
RAN, BEV = Drug.RANIBIZUMAB, Drug.BEVACIZUMAB
OD, OS = Eye.OD, Eye.OS


# ---------------------------------------------------------------- mode

def test_mode_is_stub_without_key():
    assert llm.mode() == "stub"


def test_mode_is_stub_when_forced_even_with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert llm.mode() == "stub"


def test_mode_is_a_model_id_with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("AUTOPILOT_MODEL")
    assert llm.mode() not in ("stub", "")


# ---------------------------------------------------------------- extraction grammar

GRAMMAR = [
    ("Plan: continue Eylea OD, extend to 10 weeks.", [(OD, A2, 10)]),
    ("Switch OS to Vabysmo, q4w x3.", [(OS, VAB, 4)]),
    ("Right eye: maintain aflibercept 8 mg every 12 weeks.", [(OD, A8, 12)]),
    ("Continue Eylea HD OS q 8 wk.", [(OS, A8, 8)]),
    ("Start Lucentis left eye, 6-week interval.", [(OS, RAN, 6)]),
    ("Continue avastin OD monthly.", [(OD, BEV, 4)]),
    ("MAINTAIN BEVACIZUMAB OD Q12W.", [(OD, BEV, 12)]),
    ("continue vabysmo od every 16 weeks", [(OD, VAB, 16)]),
    ("Continue faricimab left eye q8w.", [(OS, VAB, 8)]),
    ("Continue ranibizumab OS q 6 weeks.", [(OS, RAN, 6)]),
    ("Extend OD to q12w, continue Vabysmo.", [(OD, VAB, 12)]),
    ("Continue Eylea OU q8w.", [(OD, A2, 8), (OS, A2, 8)]),
    ("Continue Vabysmo both eyes every 16 weeks.", [(OD, VAB, 16), (OS, VAB, 16)]),
    ("Maintain Eylea HD OD, OS, q8w.", [(OD, A8, 8), (OS, A8, 8)]),
]


@pytest.mark.parametrize("text,expected", GRAMMAR)
def test_grammar_forms(text, expected):
    plans, _ = extract(text)
    assert as_tuples(plans) == expected


@pytest.mark.parametrize("text,expected", [
    ("Continue Eylea OD q8w.", (OD, A2, 8)),
    ("Continue Eylea HD OD q8w.", (OD, A8, 8)),
    ("Continue EYLEA HD OD q8w.", (OD, A8, 8)),
    ("Continue eylea hd OD q8w.", (OD, A8, 8)),
    ("Continue aflibercept 2 mg OS q8w.", (OS, A2, 8)),
    ("Continue aflibercept 8 mg OS q8w.", (OS, A8, 8)),
    ("Continue aflibercept 8mg OS q8w.", (OS, A8, 8)),
    ("Continue Aflibercept 2 MG OS q8w.", (OS, A2, 8)),
    ("Continue aflibercept OS q8w.", (OS, None, 8)),   # dose not stated: never guessed
])
def test_near_miss_drugs(text, expected):
    plans, _ = extract(text)
    assert as_tuples(plans) == [expected]


def test_dose_is_not_read_as_interval():
    plans, _ = extract("Continue aflibercept 8 mg OD every 12 weeks.")
    assert as_tuples(plans) == [(OD, A8, 12)]


def test_eye_and_interval_without_drug_gives_none_drug():
    plans, _ = extract("OS q10w.")
    assert as_tuples(plans) == [(OS, None, 10)]


def test_eye_and_drug_without_interval_gives_none_interval():
    plans, _ = extract("Continue Vabysmo OD.")
    assert as_tuples(plans) == [(OD, VAB, None)]


def test_separate_sentences_for_each_eye():
    plans, _ = extract("Continue Eylea OD q8w. Switch OS to Vabysmo q4w.")
    assert as_tuples(plans) == [(OD, A2, 8), (OS, VAB, 4)]


def test_one_sentence_two_eyes_two_plans():
    plans, _ = extract("Continue Eylea OD q8w; switch OS to Vabysmo q4w.")
    assert as_tuples(plans) == [(OD, A2, 8), (OS, VAB, 4)]


def test_one_sentence_two_eyes_with_and():
    plans, _ = extract("Eylea HD OD every 12 weeks and Vabysmo OS every 8 weeks.")
    assert as_tuples(plans) == [(OD, A8, 12), (OS, VAB, 8)]


def test_each_eye_on_its_own_line():
    plans, _ = extract("Assessment: stable.\nOD: continue Eylea q8w\nOS: switch to Lucentis q6w\n")
    assert as_tuples(plans) == [(OD, A2, 8), (OS, RAN, 6)]


@pytest.mark.parametrize("text", [
    "Previously on Eylea OD q8w.",
    "Prior treatment: Vabysmo OS q4w.",
    "Was on Eylea OS every 6 weeks.",
    "S/P Eylea OD x6.",
    "s/p Lucentis OS monthly.",
    "History of Avastin OD q4w.",
])
def test_history_sentences_yield_nothing(text):
    plans, _ = extract(text)
    assert plans == []


def test_history_clause_dropped_current_clause_kept():
    plans, _ = extract("Was on Eylea OS q4w, now switch to Vabysmo OS q6w.")
    assert as_tuples(plans) == [(OS, VAB, 6)]


def test_history_then_plan_in_same_sentence():
    plans, _ = extract("Previously on Eylea OS; switch to Vabysmo q6w.")
    assert as_tuples(plans) == [(OS, VAB, 6)]


def test_history_sentence_before_plan_sentence():
    plans, _ = extract("Previously Eylea OD q8w. Plan: switch OD to Vabysmo q6w.")
    assert as_tuples(plans) == [(OD, VAB, 6)]


VITALS = ("VA 20/40 OD, 20/30 OS. OCT central thickness 312 microns OD, 288 microns OS. IOP 15 OU. "
          "Dilated exam 1 week ago. Return in 4 weeks.")


def test_vitals_are_not_intervals():
    plans, _ = extract(VITALS)
    assert plans == []


def test_vitals_next_to_a_plan():
    plans, _ = extract(VITALS + " Plan: continue Eylea OD, extend to 10 weeks.")
    assert as_tuples(plans) == [(OD, A2, 10)]


def test_note_without_plan_gives_empty_list():
    plans, call = extract("Patient doing well. No new complaints.")
    assert plans == [] and call.task == "extract_plan"


def test_quotes_are_substrings_of_redacted_text():
    text = ("Margaret Okafor seen 03/07/2026. " + VITALS + "\nPlan: continue Eylea OD q8w; switch OS to Vabysmo, q4w x3.\n"
            "Previously on Lucentis OS monthly. Right eye: maintain aflibercept 8 mg every 12 weeks.")
    redacted = llm.redact(text, PATIENT, COVERAGE)
    plans, _ = extract(text)
    assert plans
    for p in plans:
        assert p.quote and p.quote in redacted
        assert p.quote in redacted.replace("\n", "\n")   # exact span, not re-joined text


def test_quote_is_the_plan_sentence():
    plans, _ = extract("Doing well. Plan: continue Eylea OD, extend to 10 weeks. Return soon.")
    assert plans[0].quote == "Plan: continue Eylea OD, extend to 10 weeks."


def test_extraction_works_on_redacted_text():
    plans, _ = extract("Margaret Okafor: continue Eylea OD q8w.")
    assert "Okafor" not in plans[0].quote and "PT-0042" in plans[0].quote


def test_ou_entries_share_quote():
    plans, _ = extract("Continue Eylea OU q8w.")
    assert [p.eye for p in plans] == [OD, OS] and plans[0].quote == plans[1].quote


# ---------------------------------------------------------------- redaction

def red(text):
    return llm.redact(text, PATIENT, COVERAGE)


def test_redact_full_name_and_parts():
    out = red("Margaret Okafor was seen. Margaret returns. OKAFOR said hi. okafor.")
    assert "Margaret" not in out and "Okafor" not in out.replace("PT-0042", "") and "OKAFOR" not in out
    assert out.count("PT-0042") == 4


def test_redact_last_first_order():
    out = red("Okafor, Margaret is a 78 year old woman.")
    assert "Okafor" not in out and "Margaret" not in out and "PT-0042" in out


def test_redact_name_is_whole_word_only():
    out = red("Margarets and Okaforian are not the patient.")
    assert "Margarets" in out and "Okaforian" in out


@pytest.mark.parametrize("dob", ["1948-03-07", "03/07/1948", "Mar 7, 1948", "March 7, 1948", "3/7/1948"])
def test_redact_dob_forms(dob):
    out = red(f"DOB {dob}. Seen today.")
    assert "1948" not in out and "[DOB]" in out


def test_redact_phone_forms():
    for phone in ["(973) 555-0142", "973-555-0142", "973.555.0142", "9735550142"]:
        out = red(f"Call {phone} to confirm.")
        assert "555" not in out and "[PHONE]" in out, phone


def test_redact_member_id():
    out = red("Member SUM-884213907 on Summit Gold; also sum-884213907.")
    assert "884213907" not in out and out.count("[MEMBER]") == 2


def test_redact_leaves_drugs_and_clinical_words_alone():
    text = "Eylea HD OD q8w, aflibercept 8 mg, Vabysmo OS, Lucentis, Avastin, ranibizumab, VA 20/40, OCT 312 microns."
    assert red(text) == text


def test_redact_ignores_one_character_name_parts():
    p = Patient(id="pt-1", token="PT-0001", name="J Smith", birth_date=date(1950, 1, 1))
    out = llm.redact("J plan: Smith, continue Eylea OD. Option J.", p)
    assert out == "J plan: PT-0001, continue Eylea OD. Option J."


def test_redact_without_coverage_or_phone():
    p = Patient(id="pt-1", token="PT-0001", name="Ana Diaz", birth_date=date(1950, 1, 1))
    assert llm.redact("Ana Diaz q8w", p) == "PT-0001 q8w"


# ---------------------------------------------------------------- leak check

LEAKY_NOTE = ("Margaret Okafor, DOB 03/07/1948 (Mar 7, 1948; 1948-03-07). Phone 973-555-0142. Member SUM-884213907.\n"
              "Okafor, Margaret: plan continue Eylea OD q8w; switch OS to Vabysmo, q4w x3.")


def test_no_phi_in_prompt_log():
    plans, call = extract(LEAKY_NOTE)
    assert plans
    llm.draft("appeal", FULL_FACTS)
    llm.draft("outreach_sms", FULL_FACTS)
    assert len(llm.PROMPT_LOG) == 3
    for entry in llm.PROMPT_LOG:
        assert set(entry) == {"task", "prompt"}
        for secret in ["Margaret", "Okafor", "1948", "03/07", "555", "0142", "884213907", "973"]:
            assert secret not in entry["prompt"], (entry["task"], secret)
    assert "PT-0042" in llm.PROMPT_LOG[0]["prompt"]


def test_reset_log_empties_it():
    extract("Continue Eylea OD q8w.")
    assert llm.PROMPT_LOG
    llm.reset_log()
    assert llm.PROMPT_LOG == []


# ---------------------------------------------------------------- draft

@pytest.mark.parametrize("kind", KINDS)
def test_draft_full_facts(kind):
    d = llm.draft(kind, FULL_FACTS)
    assert d.kind == kind and d.verified and d.problems == []
    assert "{" not in d.body and "}" not in d.body and "{" not in d.subject
    assert d.subject.strip() and d.body.strip()
    assert d.facts and set(d.facts) <= set(FULL_FACTS)
    assert all(d.facts[k] == FULL_FACTS[k] for k in d.facts)
    assert d.call and d.call.task == f"draft_{kind}" and d.call.model == "stub" and d.call.cost_usd == 0.0


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("facts", [{}, {"practice": "Garden State Retina (synthetic)"},
                                   {"practice": "Garden State Retina (synthetic)", "patient_name": "Margaret Okafor"}])
def test_draft_minimal_facts(kind, facts):
    d = llm.draft(kind, facts)
    assert d.verified and d.problems == []
    assert "{" not in d.body and "}" not in d.body and "{" not in d.subject
    assert d.body.strip() and d.subject.strip()
    assert set(d.facts) <= set(facts)


def test_draft_prompt_has_keys_but_no_values():
    llm.draft("pa_request", FULL_FACTS)
    prompt = llm.PROMPT_LOG[-1]["prompt"]
    assert llm.PROMPT_LOG[-1]["task"] == "draft_pa_request"
    for key in FULL_FACTS:
        assert key in prompt
    for key, value in FULL_FACTS.items():
        assert value not in prompt, key
    keys_line = [k for k in sorted(FULL_FACTS)]
    assert ", ".join(keys_line) in prompt   # sorted list of keys


def test_pa_request_content():
    d = llm.draft("pa_request", FULL_FACTS)
    for needle in ["Margaret Okafor", "H35.3211", "Eylea HD", "right eye", "Nov 20, 2026", FULL_FACTS["finding"]]:
        assert needle in d.body


def test_appeal_content():
    d = llm.draft("appeal", FULL_FACTS)
    for needle in ["clm-0107", "CO-197", "Precertification or authorization absent", FULL_FACTS["finding"],
                   "reprocess", "Enclos"]:
        assert needle in d.body


def test_step_exception_content():
    d = llm.draft("step_exception", FULL_FACTS)
    assert "exception" in d.body.lower() and "Dr. Vatsal Doshi" in d.body and FULL_FACTS["finding"] in d.body


def test_outreach_sms_is_a_text_message():
    d = llm.draft("outreach_sms", FULL_FACTS)
    assert len(d.body) < 300 and "Garden State Retina" in d.body and "retina appointment" in d.body
    for forbidden in ["Eylea", "aflibercept", "H35", "AMD", "Exudative", "right eye", "OD", "J0177", "AUTH", "SUM-"]:
        assert forbidden not in d.body, forbidden


def test_draft_drops_lines_for_missing_keys():
    d = llm.draft("pa_request", {"practice": "Garden State Retina (synthetic)", "drug_brand": "Eylea", "eye": "OS"})
    assert "Eylea" in d.body and "OS" in d.body
    assert "Date of birth" not in d.body and "Member ID" not in d.body and "Diagnosis" not in d.body


def test_draft_unknown_kind_raises():
    with pytest.raises(ValueError):
        llm.draft("poem", FULL_FACTS)


def test_draft_with_bad_real_template_is_unverified(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("AUTOPILOT_MODEL")
    bad = "SUBJECT: Request\n\nPlease approve 3 doses of Eylea for {patient_name} in the right eye. {invented}"
    monkeypatch.setattr(llm, "_complete_real", lambda *a, **k: (bad, 100, 50))
    d = llm.draft("pa_request", FULL_FACTS)
    assert not d.verified and len(d.problems) >= 4
    assert d.call.model != "stub"


# ---------------------------------------------------------------- verify_template

FACTS = {"patient_name": "X", "finding": "Y"}


def test_verify_clean_template():
    assert llm.verify_template("Dear payer, re {patient_name}: {finding}. Please reprocess.", FACTS) == []


def test_verify_flags_digit():
    p = llm.verify_template("Please approve within 30 days for {patient_name}.", FACTS)
    assert len(p) == 1 and "digit" in p[0].lower()


def test_verify_digits_inside_placeholders_are_fine():
    assert llm.verify_template("Re {patient_name} {finding}", {"patient_name": "a", "finding": "b"}) == []


@pytest.mark.parametrize("word", ["Eylea", "eylea", "VABYSMO", "aflibercept", "Faricimab", "Lucentis", "ranibizumab",
                                  "Avastin", "bevacizumab"])
def test_verify_flags_drug_names(word):
    p = llm.verify_template(f"We request {word} for {{patient_name}}.", FACTS)
    assert len(p) == 1 and "drug" in p[0].lower()


@pytest.mark.parametrize("phrase", ["OD", "OS", "OU", "right eye", "Left Eye", "both eyes"])
def test_verify_flags_eye_words(phrase):
    p = llm.verify_template(f"Treat the {phrase} of {{patient_name}}.", FACTS)
    assert len(p) == 1 and "eye" in p[0].lower()


def test_verify_does_not_flag_ordinary_words():
    assert llm.verify_template("The modified odds, the cost, our house, and the eye clinic. {patient_name}", FACTS) == []


def test_verify_flags_unknown_placeholder():
    p = llm.verify_template("Hello {patient_name} {nonsense}", FACTS)
    assert len(p) == 1 and "nonsense" in p[0]


def test_verify_flags_malformed_braces():
    assert llm.verify_template("Hello {patient_name", FACTS)


def test_verify_reports_every_problem():
    p = llm.verify_template("Give 3 doses of Eylea OD to {who}.", FACTS)
    assert len(p) == 4


# ---------------------------------------------------------------- telemetry, determinism, real mode

def test_stub_telemetry():
    _, call = extract("Continue Eylea OD q8w.")
    assert call.model == "stub" and call.cost_usd == 0.0 and call.ms >= 0
    assert call.tokens_in == len(llm.PROMPT_LOG[0]["prompt"]) // 4 and call.tokens_out >= 0


def test_determinism():
    a, _ = extract(LEAKY_NOTE)
    b, _ = extract(LEAKY_NOTE)
    assert a == b
    for kind in KINDS:
        x, y = llm.draft(kind, FULL_FACTS), llm.draft(kind, FULL_FACTS)
        assert (x.subject, x.body, x.facts, x.verified) == (y.subject, y.body, y.facts, y.verified)


def test_prices_table_has_default():
    assert "default" in llm.PRICES and all(len(v) == 2 for v in llm.PRICES.values())


def _real_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("AUTOPILOT_MODEL")
    monkeypatch.setattr(llm, "SPENT_USD", 0.0)


def test_real_mode_failure_falls_back_to_stub(monkeypatch):
    _real_env(monkeypatch)

    def boom(*a, **k):
        raise TimeoutError("network down")
    monkeypatch.setattr(llm, "_complete_real", boom)
    plans, call = extract("Continue Eylea OD q8w.")
    assert call.model == "stub (fallback: TimeoutError)"
    assert as_tuples(plans) == [(OD, A2, 8)]


def test_real_mode_output_is_grounded(monkeypatch):
    _real_env(monkeypatch)
    reply = ('Here you go: [{"eye": "OD", "drug": "aflibercept_8mg", "interval_weeks": 12, "quote": "Continue Eylea HD OD q12w."},'
             '{"eye": "OS", "drug": "vabysmo", "interval_weeks": "8", "quote": "not in the note"},'
             '{"eye": "XX", "drug": null, "interval_weeks": 4, "quote": "Continue Eylea HD OD q12w."},'
             '{"eye": "OS", "drug": "unobtainium", "interval_weeks": 999, "quote": "Continue Eylea HD OD q12w."}]')
    monkeypatch.setattr(llm, "_complete_real", lambda *a, **k: (reply, 1_000_000, 100_000))
    plans, call = extract("Continue Eylea HD OD q12w.")
    assert as_tuples(plans) == [(OD, A8, 12), (OS, None, None)]
    assert call.model == "claude-haiku-5-5" and call.tokens_in == 1_000_000
    price_in, price_out = llm.PRICES["claude-haiku-5-5"]
    assert call.cost_usd == pytest.approx(price_in + 0.1 * price_out)
    assert llm.SPENT_USD == pytest.approx(call.cost_usd)


def test_real_mode_model_ids_from_env(monkeypatch):
    _real_env(monkeypatch)
    monkeypatch.setenv("AUTOPILOT_EXTRACT_MODEL", "my-extract")
    monkeypatch.setattr(llm, "_complete_real", lambda *a, **k: ("[]", 10, 10))
    _, call = extract("Continue Eylea OD q8w.")
    assert call.model == "my-extract"


def test_budget_guard_forces_stub(monkeypatch):
    _real_env(monkeypatch)
    monkeypatch.setattr(llm, "SPENT_USD", 5.0)

    def forbidden(*a, **k):
        raise AssertionError("real model must not be called over budget")
    monkeypatch.setattr(llm, "_complete_real", forbidden)
    plans, call = extract("Continue Eylea OD q8w.")
    assert call.model == "stub (budget exceeded)" and as_tuples(plans) == [(OD, A2, 8)]
    d = llm.draft("outreach_sms", FULL_FACTS)
    assert d.call.model == "stub (budget exceeded)" and d.verified
