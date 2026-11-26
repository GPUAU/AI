"""최종 리뷰 지적 재현 테스트 (feat/bill-interpretation)."""
import pytest

from app.agent.orchestrator import CostAgent
from app.billmeta import read_meta
from app.columns import ColRole, column_roles
from app.models import CorrectionRequest, ItemType, LineCorrection, LineRole
from tests.conftest import doc_from_rows
from tests.test_agent import fake_backend


def amount(d, t):
    return next(c.amount for c in d.cost_items if c.item_type == t)


# 1 (Critical) 계약기간 날짜 범위를 청구 기간으로 읽음
def test_contract_term_range_is_not_billing_period():
    m = read_meta(doc_from_rows([["청구월", "2026년 11월분"], ["계약기간", "2026-01-01 ~ 2026-12-31"],
                                 ["항목", "금액(원)"], ["전력 사용료", 100]]))
    assert m.billing_months == 1


# 2 (Critical) 항목명 속 '3분기'가 'n월분'을 덮어씀
def test_quarter_word_in_line_item_does_not_override_month():
    m = read_meta(doc_from_rows([["청구월", "2026년 10월분"], ["항목", "금액(원)"],
                                 ["3분기 정산 회선", 100], ["전력 사용료", 200]]))
    assert m.billing_months == 1


# 3 (Critical) 위쪽 요약 블록을 표 머리글로 잡음
def test_summary_block_above_table_is_not_the_header(service):
    d = service.ingest(doc_from_rows([["공급가액", "부가세", "합계금액"], [1000, 100, 1100],
                                      ["항목", "수량", "금액(원)"], ["전력 사용료", 4, 600], ["회선료", 1, 400]]))
    assert amount(d, ItemType.ELECTRICITY) == 600 * 12
    assert column_roles(doc_from_rows([["공급가액", "부가세", "합계금액"], [1000, 100, 1100],
                                       ["항목", "수량", "금액(원)"], ["전력 사용료", 4, 600],
                                       ["회선료", 1, 400]]))["s"][2] == ColRole.SUPPLY


# 4 (Important) '1회선'의 '1회'를 1회성으로 봄 + 운영원가 0일 때 '영향 없음'
def test_one_line_circuit_header_is_not_nrc():
    roles = column_roles(doc_from_rows([["항목", "수량", "1회선 금액"], ["전력 사용료", 1, 100], ["회선료", 2, 200]]))["s"]
    assert roles[2] == ColRole.SUPPLY


def test_unknown_impact_is_not_reported_as_no_effect():
    assert "영향이 없어요" not in CostAgent._impact_text(None)


# 5 (Important) 해석이 담당자 수정을 덮어씀
def test_user_correction_survives_non_cost_rule(service):
    d = service.ingest(doc_from_rows([["항목", "금액(원)"], ["전력 사용료", 100], ["이월 회선료", 50], ["합계", 150]]))
    ref = next(l.ref for l in d.lines if "이월" in l.label)
    d = service.correct(d.id, CorrectionRequest(corrections=[LineCorrection(ref=ref, role=LineRole.COST,
                                                                          item_type=ItemType.NETWORK_STORAGE)],
                                                save_template=False))
    assert next(l for l in d.lines if l.ref == ref).role == LineRole.COST


# 6 (Important) 재분류 뒤 새 해석이 카드 없이 적용됨
def test_new_interpretations_after_redraft_reset_confirmation(tmp_path, service, detailed_doc):
    agent = CostAgent(service, fake_backend()[0])
    s = agent.start(detailed_doc("bill_r1_kr_metered_krw.xlsx"), __import__("app.agent.models", fromlist=["Goal"]).Goal(
        model_id="29", gpu_count=504))
    s.interpret_ok = True
    other = service.ingest(detailed_doc("bill_r2_multisheet_thousand_krw.xlsx"))
    agent._set_draft(s, other)
    assert s.interpret_ok is False
    agent._set_draft(s, other)  # 같은 해석이면 다시 묻지 않는다
    s.interpret_ok = True
    agent._set_draft(s, other)
    assert s.interpret_ok is True


# 7 (Important) 요약 시트 중복 제거가 다른 곳에 없는 비용까지 지움
def test_summary_sheet_kept_when_detail_does_not_cover_it(service):
    rows = [["항목", "금액(원)"], ["상면 사용료", 300], ["전력 사용료", 200], ["회선료", 100], ["합계", 600]]
    d1 = doc_from_rows(rows, sheet="요약")
    d2 = doc_from_rows([["항목", "금액(원)"], ["전력 사용료", 200]], sheet="전력 상세")
    doc = d1.model_copy(update={"sheets": ["요약", "전력 상세"], "rows": d1.rows + d2.rows})
    d = service.ingest(doc)
    assert "SUMMARY_SHEET" not in {i.code for i in d.interpretations}
    assert amount(d, ItemType.RACK_FACILITY) == 300 * 12


def test_two_summary_sheets_do_not_remove_each_other(service):
    rows = [["항목", "금액(원)"], ["상면 사용료", 300], ["전력 사용료", 200], ["합계", 500]]
    a, b = doc_from_rows(rows, sheet="요약"), doc_from_rows(rows, sheet="총괄표")
    doc = a.model_copy(update={"sheets": ["요약", "총괄표"], "rows": a.rows + b.rows})
    d = service.ingest(doc)
    assert amount(d, ItemType.RACK_FACILITY) == 300 * 12


# 8 (Important) '단위: 원'을 무시하고 주석의 '(천원)'을 씀
def test_explicit_won_unit_locks_against_later_thousand_note():
    m = read_meta(doc_from_rows([["금액 단위", "원"], ["※ 보증금 별도 (천원)"], ["전력 사용료", 100]]))
    assert m.unit_multiplier == 1


# 9 (Important) '금액(VAT 포함)' 열을 공급가액으로 봄
def test_vat_included_amount_column_is_divided(service):
    d = service.ingest(doc_from_rows([["항목", "금액(VAT 포함)"], ["전력 사용료", 1100], ["회선료", 550], ["합계", 1650]]))
    assert amount(d, ItemType.ELECTRICITY) == pytest.approx(1000 * 12)
    assert d.reconciliation.matched is True and "GROSS_COLUMN" in {i.code for i in d.interpretations}


# 10 (Important) 비율로 찾은 합계 열이 실제로 바뀌지 않음
def test_ratio_found_gross_column_moves_amounts(service):
    d = service.ingest(doc_from_rows([["전력 사용료", 100, 110], ["회선료", 200, 220], ["운영관리비", 300, 330],
                                      ["상면 사용료", 400, 440]]))
    assert amount(d, ItemType.ELECTRICITY) == 100 * 12
