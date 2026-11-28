import pytest

from app.docs import DocKind, classify_doc
from tests.conftest import BUILDERS, doc_from_rows


@pytest.mark.parametrize("code,kind", [("01", "QUOTE"), ("02", "MEMO"), ("03", "BILL"), ("04", "MEMO"),
                                       ("05", "MEMO"), ("06", "MEMO"), ("07", "MEMO"), ("08", "PLAN")])
def test_dept_doc_kinds(dept_docs, code, kind):
    assert classify_doc(dept_docs[code])[0].value == kind


def test_title_beats_body_mentions(dept_docs):
    # 02는 본문에 '견적'이 나오지만 제목은 '명세' → MEMO, 근거에 제목이 보인다
    kind, why = classify_doc(dept_docs["02"])
    assert kind == DocKind.MEMO and "명세" in why


@pytest.mark.parametrize("name", list(BUILDERS))
def test_detailed_bills_are_bills(detailed_doc, name):
    assert classify_doc(detailed_doc(name))[0] == DocKind.BILL


def test_sample_bills_are_bills(samples_all):
    assert all(classify_doc(d)[0] == DocKind.BILL for d in samples_all)


def test_unknown_doc_falls_back_to_bill_with_notice():  # Review Focus 4
    k, why = classify_doc(doc_from_rows([["", "1,000"]]))
    assert k == DocKind.BILL and "확인" in why


# ---------------------------------------------------------------- 칸 추출 (Task 4)
from app.docs import extract_fields  # noqa: E402


def _hits(dept_docs):
    out = {}
    for code, d in dept_docs.items():
        for h in extract_fields(d, classify_doc(d)[0], code):
            out.setdefault((h.key, h.role), []).append(h)
    return out


def test_dept_fields_match_answer_key(dept_docs, dept_key):
    hits = {k: v[0] for k, v in _hits(dept_docs).items()}
    assert hits[("acquisition", "table")].value.number == 1_009_000_000
    assert hits[("acquisition", "table")].value.currency == "KRW"
    assert hits[("gpu_count", "table")].value.number == 32 and hits[("depreciation_years", "table")].value.number == 5
    assert hits[("utilization", "table")].value.number == pytest.approx(0.7)
    assert hits[("fx_krw_per_usd", "table")].value.number == 1350
    assert hits[("operations", "table")].value.number == 72_000_000 and hits[("target_rate", "table")].value.number == 0.2
    assert hits[("gpu_model", "table")].value.text == "H100 80GB"
    assert hits[("pricing_method", "table")].value.text == "REVENUE_MARGIN"
    assert hits[("plan_hourly_cost", "result")].value.number == 1885
    assert hits[("plan_price", "result")].value.number == 2356
    assert hits[("plan_annual_cost", "result")].value.number == 369_800_000


def test_acquisition_found_in_quote_and_finance_memo(dept_docs):
    acq = _hits(dept_docs)[("acquisition", "table")]
    assert sorted(h.source.doc for h in acq) == ["01", "04"]
    q = next(h for h in acq if h.source.doc == "01")
    assert q.source.row == 10 and q.source.page == 1  # '공급가액 합계' 줄 (부가세 포함 합계가 아님)


def test_quote_gives_gpu_count_from_servers_times_gpus(dept_docs):
    g = [h for h in _hits(dept_docs)[("gpu_count", "table")] if h.source.doc == "01"]
    assert len(g) == 1 and g[0].value.number == 32


def test_calculation_basis_numbers_are_not_values(dept_docs):
    hits = _hits(dept_docs)
    assert all(h.value.number == 32 for h in hits[("gpu_count", "table")])  # '32장×365일×24시간'은 근거
    assert not any(h.value.number in (80_000_000, 120_000_000) for h in hits[("operations", "table")])


def test_operations_comes_from_management_accounting_total(dept_docs):
    ops = _hits(dept_docs)[("operations", "table")]
    assert len(ops) == 1 and ops[0].source.doc == "05" and ops[0].source.row == 8


def test_prose_values_are_marked_prose(dept_docs):
    hits = _hits(dept_docs)
    p = [h for h in hits[("acquisition", "prose")] if h.source.doc == "01"]
    assert p and p[0].value.number == 1_009_000_000 and p[0].source.row == 15
    fac = hits[("check_facility_annual", "prose")]
    assert fac[0].source.doc == "03" and fac[0].value.number == 96_000_000
    assert hits[("utilization", "prose")][0].value.number == pytest.approx(0.7)


def test_bill_doc_gives_only_prose(dept_docs):
    assert {h.role for h in extract_fields(dept_docs["03"], DocKind.BILL, "03")} == {"prose"}
