import json

from app.mapping.llm import RESPONSE_SCHEMA, LLMMapper
from app.mapping.templates import TemplateStore
from app.models import ItemStatus, ItemType
from app.service import BillService


def fake_llm(rows: list[dict], **doc):
    calls = []

    def complete(system, user, schema):
        calls.append(user)
        assert schema is RESPONSE_SCHEMA
        return json.dumps({"vendor": doc.get("vendor"), "currency": doc.get("currency", "USD"),
                           "default_period": "MONTH", "rows": rows})
    complete.calls = calls
    return complete


def row(ref, role, item=None, col=None, included=(), conf=0.9):
    return {"ref": ref, "role": role, "item_type": item, "amount_col": col, "included_items": list(included),
            "period": None, "confidence": conf, "reason": "test"}


B_ROWS = [
    row("Invoice!1", "IGNORE"), row("Invoice!2", "IGNORE"), row("Invoice!4", "IGNORE"),
    row("Invoice!5", "COST", "RENT", 3, ["ELECTRICITY"]),
    row("Invoice!6", "COST", "NETWORK_STORAGE", 3),
    row("Invoice!7", "COST", "NETWORK_STORAGE", 3),
    row("Invoice!8", "COST", "OPERATIONS", 3),
    row("Invoice!9", "TOTAL", None, 3),
]


def test_amounts_come_from_cells_not_from_llm(tmp_path, doc):
    llm = fake_llm(B_ROWS, vendor="Example Colo")
    s = BillService(LLMMapper(llm), TemplateStore(tmp_path))
    draft = s.ingest(doc("b"))
    assert draft.mapper == "llm" and draft.vendor == "Example Colo"
    rent = next(c for c in draft.cost_items if c.item_type == ItemType.RENT)
    assert rent.amount == 18_500 * 12
    elec = next(c for c in draft.cost_items if c.item_type == ItemType.ELECTRICITY)
    assert elec.status == ItemStatus.INCLUDED_IN_OTHER
    assert draft.reconciliation.matched is True
    # LLM에는 숫자가 있는 칸 번호를 알려 준다
    assert '"number_cols"' in llm.calls[0]


def test_unknown_rows_dropped_and_bad_amount_col_flagged(tmp_path, doc):
    rows = [r for r in B_ROWS if r["ref"] != "Invoice!8"]
    rows.append(row("Invoice!99", "COST", "GPU", 3))              # 문서에 없는 줄 → 버림
    rows.append(row("Invoice!8", "COST", "OPERATIONS", 0))        # 0번 칸은 글자 → 금액 없음
    s = BillService(LLMMapper(fake_llm(rows)), TemplateStore(tmp_path))
    draft = s.ingest(doc("b"))
    assert all(l.ref != "Invoice!99" for l in draft.lines)
    codes = {(i.code, i.ref) for i in draft.issues}
    assert ("NO_AMOUNT", "Invoice!8") in codes
    assert draft.reconciliation.matched is False


def test_malformed_llm_output_falls_back_to_rules(tmp_path, doc):
    s = BillService(LLMMapper(lambda *_: "not json"), TemplateStore(tmp_path))
    draft = s.ingest(doc("a"))
    assert draft.mapper == "rules"
    assert any(i.code == "LLM_FAILED" for i in draft.issues)
    assert draft.reconciliation.matched is True


def test_schema_is_strict():
    assert RESPONSE_SCHEMA["additionalProperties"] is False
    item = RESPONSE_SCHEMA["properties"]["rows"]["items"]
    assert set(item["required"]) == set(item["properties"])


def test_llm_api_error_falls_back_to_rules(tmp_path, doc):
    def boom(*_):
        raise RuntimeError("401 project archived")
    s = BillService(LLMMapper(boom), TemplateStore(tmp_path))
    draft = s.ingest(doc("b"))
    assert draft.mapper == "rules"
    msg = next(i.message for i in draft.issues if i.code == "LLM_FAILED")
    assert "RuntimeError" in msg and "archived" not in msg
