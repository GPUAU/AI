import pytest

from app.costsheet import Source
from app.docs import DocKind, FieldHit, classify_doc, extract_fields
from app.mapping.rules import RulesMapper
from app.mapping.templates import TemplateStore
from app.models import Currency, ItemStatus, ItemType, Period
from app.service import BillService
from app.sheetmerge import build_sheet, cross_checks
from app.values import Value


@pytest.fixture
def dept_inputs(dept_docs, tmp_path):
    svc = BillService(RulesMapper(), TemplateStore(tmp_path))
    hits, bills = [], []
    for code, d in dept_docs.items():
        kind, _ = classify_doc(d)
        hits += extract_fields(d, kind, code)
        if kind == DocKind.BILL:
            bills.append((code, svc.ingest(d)))
    return hits, bills


def test_dept_facility_bill_reads_monthly(dept_inputs):
    _, bills = dept_inputs
    b = dict(bills)["03"]
    items = {c.item_type: c for c in b.cost_items if c.status == ItemStatus.USER_INPUT}
    assert b.billing_months == 1.0
    assert items[ItemType.ELECTRICITY].amount == 57_600_000 and items[ItemType.RACK_FACILITY].amount == 28_800_000
    assert items[ItemType.NETWORK_STORAGE].amount == 9_600_000  # '1회선 월 정액'은 1회성이 아님
    assert ItemType.OTHER not in items and ItemType.ACQUISITION not in items  # 수납액은 원가가 아님


def test_dept_sheet_matches_answer_key(dept_inputs, dept_key):
    hits, bills = dept_inputs
    s = build_sheet(hits, bills)
    c = s.cells
    for k, v in dept_key["cells"].items():
        assert c[k].status == "filled", k
        assert c[k].value == (pytest.approx(v) if isinstance(v, (int, float)) else v), k
    assert sorted(x.doc for x in c["acquisition"].sources) == ["01", "04"]
    assert sorted(x.doc for x in c["gpu_count"].sources) == ["01", "02"]
    assert c["operations"].components[0]["name"] == "사내 인건비·판관비"
    assert c["electricity"].sources[0].doc == "03" and c["electricity"].grade.startswith("사내 문서")
    assert c["plan_hourly_cost"].value == 1885
    assert c["rent"].status in ("included", "empty")


def test_conflicting_departments_make_conflict(dept_inputs):
    hits, bills = dept_inputs
    changed = [FieldHit(h.key, Value(1_100_000_000, "원", "KRW", "1,100,000,000원"), h.source, h.role)
               if (h.key, h.source.doc, h.role) == ("acquisition", "04", "table") else h for h in hits]
    a = build_sheet(changed, bills).cells["acquisition"]
    assert a.status == "conflict" and a.value is None and len(a.candidates) == 2
    assert {x["doc"] for x in a.candidates} == {"01", "04"}


def test_same_cost_in_two_docs_is_not_added_twice():  # Review Focus 1
    from app.models import BillDraft, CostItemDraft, Reconciliation
    bill = BillDraft(id="b", file_name="f", fingerprint="x", mapper="rules", lines=[],
                     reconciliation=Reconciliation(cost_sum=0), issues=[], needs_review=False,
                     cost_items=[CostItemDraft(item_type=ItemType.ACQUISITION, status=ItemStatus.USER_INPUT, amount=8_000_000,
                                               currency=Currency.KRW, period=Period.ONE_TIME, source_refs=["1쪽!9"])])
    quote = FieldHit("acquisition", Value(1_009_000_000, "원", "KRW", "1,009,000,000"), Source(doc="견적", row=10, col=3), "table")
    a = build_sheet([quote], [("시설 청구서", bill)]).cells["acquisition"]
    assert a.status == "conflict" and a.value is None  # 1,017,000,000으로 더하지 않음


def test_internal_fx_beats_public_fx(dept_inputs):
    hits, bills = dept_inputs
    fx = build_sheet(hits, bills, public={"fx_krw_per_usd": Source(doc="한국수출입은행", text="1,450")}).cells["fx_krw_per_usd"]
    assert fx.value == 1350 and fx.sources[0].doc == "07"
    assert "수출입은행 1,450" in fx.note and "-6.9%" in fx.note


def test_public_value_fills_only_when_no_internal():
    fx = build_sheet([], [], public={"fx_krw_per_usd": Source(doc="한국수출입은행", text="1,450")}).cells["fx_krw_per_usd"]
    assert fx.status == "filled" and fx.value == 1450 and fx.grade == "공개값"


def test_prose_annual_facility_cross_checks_table(dept_inputs):
    hits, bills = dept_inputs
    s = build_sheet(hits, bills)
    assert "일치" in s.cells["electricity"].note and s.cells["electricity"].value == 57_600_000  # 연간화 한 번
    msgs = cross_checks(s)
    assert any("연간 시설비" in m and "일치" in m for m in msgs)
    assert any("감가상각" in m and "일치" in m for m in msgs)


def test_prose_only_value_does_not_fill():
    h = FieldHit("acquisition", Value(5_000_000, "원", "KRW", "5,000,000원"), Source(doc="x", row=3), "prose")
    a = build_sheet([h], []).cells["acquisition"]
    assert a.status == "empty" and "5,000,000원" in a.note
