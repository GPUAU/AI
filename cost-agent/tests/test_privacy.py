import json

from app.mapping.llm import LLMMapper, build_user_prompt
from app.mapping.templates import TemplateStore
from app.privacy import mask_text
from app.service import BillService


def test_mask_text_hides_numbers_identifiers_and_terms(monkeypatch):
    monkeypatch.setenv("SENSITIVE_TERMS", "GPU AU, 가상IDC A")
    t = mask_text("가상IDC A 계약전력 40kW × 720h × 175원 문의 ops@idc.example https://idc.example/bill 고객 GPU AU")
    assert "40" not in t and "175" not in t and "720" not in t
    assert "[이메일]" in t and "[URL]" in t
    assert "GPU AU" not in t and "가상IDC A" not in t and t.count("[가림]") == 2
    assert "계약전력 #kW" in t


def test_prompt_contains_no_amounts_or_file_name(doc):
    d = doc("c")
    prompt = build_user_prompt(d)
    assert d.file_name not in prompt
    for row in d.rows:
        for value in row.numbers.values():
            if abs(value) < 1000:  # No·수량 같은 작은 수는 number_cols 번호와 겹칠 수 있어 금액만 본다
                continue
            assert f"{int(abs(value)):,}" not in prompt and str(int(abs(value))) not in prompt
    first = json.loads(prompt.splitlines()[2])
    assert "number_cols" in first
    assert "#NUM" in prompt and "동력비" in prompt


def test_unmasked_mode_sends_original(doc):
    d = doc("a")
    assert "5184000" in build_user_prompt(d, mask=False)


def test_draft_discloses_what_was_sent_and_missing_items(tmp_path, doc):
    from tests.test_llm_mapper import B_ROWS, fake_llm  # 같은 가짜 LLM 재사용
    s = BillService(LLMMapper(fake_llm(B_ROWS), destination="OpenAI (test)"), TemplateStore(tmp_path))
    draft = s.ingest(doc("b"))
    assert draft.llm.masked is True and draft.llm.destination == "OpenAI (test)"
    assert "18500" not in draft.llm.sent and "#NUM" in draft.llm.sent
    rules = BillService(__import__("app.mapping.rules", fromlist=["RulesMapper"]).RulesMapper(), TemplateStore(tmp_path / "r"))
    c = rules.ingest(doc("c"))
    assert c.llm is None
    assert [t.value for t in c.missing_items] == ["OPERATIONS"]


def test_period_comes_from_original_text_not_llm(tmp_path, doc):
    import json as _json

    def says_year(system, user, schema):
        return _json.dumps({"vendor": None, "currency": "KRW", "default_period": "YEAR", "rows": []})
    from app.models import Period
    s = BillService(LLMMapper(says_year), TemplateStore(tmp_path))
    assert s.ingest(doc("c")).period == Period.MONTH   # "2026년 10월분"
    assert s.ingest(doc("b")).period == Period.MONTH   # "Oct 2026"
    assert s.ingest(doc("a")).period == Period.MONTH   # "2026-10"
