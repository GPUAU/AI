"""최종 리뷰에서 나온 문제 (Critical 1 · Important 2~7) 재현 테스트."""

import pytest

from app.agent.models import Answer, Goal, Step
from app.costsheet import Source
from app.docs import DocKind, FieldHit, extract_fields
from app.mapping.rules import RulesMapper, detect_period
from app.mapping.templates import TemplateStore
from app.models import (BillDraft, CostItemDraft, Currency, ItemStatus, ItemType, Period, Reconciliation)
from app.service import BillService
from app.sheetmerge import build_sheet
from app.values import Value
from tests.conftest import doc_from_rows
from tests.test_agent import Agent, fake_backend, suggested  # noqa: F401
from tests.test_costsheet_agent import past_analyze, start_many


def _bill(ops: float, name="b") -> BillDraft:
    return BillDraft(id=name, file_name=name, fingerprint="x", mapper="rules", lines=[], issues=[], needs_review=False,
                     reconciliation=Reconciliation(cost_sum=0),
                     cost_items=[CostItemDraft(item_type=ItemType.OPERATIONS, status=ItemStatus.USER_INPUT, amount=ops,
                                               currency=Currency.KRW, period=Period.YEAR, source_refs=["s!3"])])


def _inhouse(v, doc):
    return FieldHit("operations", Value(v, "원", "KRW", f"{v:,.0f}원"), Source(doc=doc, row=8), "table")


# ---------------------------------------------------------------- Critical 1: 운영비 구성 요소별 충돌
def test_operations_inhouse_conflict_is_not_overwritten_by_bill_part():
    c = build_sheet([_inhouse(72_000_000, "05"), _inhouse(90_000_000, "05b")], [("bill", _bill(12_000_000))]).cells["operations"]
    assert c.status == "conflict" and c.value is None
    assert {x["part"] for x in c.candidates} == {"사내 인건비·판관비"}
    assert [x["name"] for x in c.components] == ["청구서 운영비"]


def test_operations_pick_keeps_other_part():
    from app.agent.sheetflow import _apply
    c = build_sheet([_inhouse(72_000_000, "05"), _inhouse(90_000_000, "05b")], [("bill", _bill(12_000_000))]).cells["operations"]
    _apply(c, {"choice": "pick", "doc": "05", "row": 8, "value": 72_000_000, "text": "72,000,000원"})
    assert c.status == "filled" and c.value == 84_000_000
    c2 = build_sheet([_inhouse(72_000_000, "05"), _inhouse(90_000_000, "05b")], [("bill", _bill(12_000_000))]).cells["operations"]
    _apply(c2, {"choice": "manual", "value": 80_000_000})
    assert c2.value == 92_000_000 and {x["name"] for x in c2.components} == {"청구서 운영비", "사내 인건비·판관비"}


def test_operations_bill_conflict_keeps_inhouse_part():
    from app.agent.sheetflow import _apply
    c = build_sheet([_inhouse(72_000_000, "05")], [("b1", _bill(12_000_000, "b1")), ("b2", _bill(20_000_000, "b2"))]).cells["operations"]
    assert c.status == "conflict" and {x["part"] for x in c.candidates} == {"청구서 운영비"}
    _apply(c, {"choice": "pick", "doc": "b1", "row": 3, "value": 12_000_000, "text": "12,000,000원"})
    assert c.value == 84_000_000


# ---------------------------------------------------------------- Important 2: 연간 청구서를 월로 읽지 않음
def test_annual_title_is_not_read_as_monthly():
    d = doc_from_rows([["연간 이용료 청구서"], ["월 사용료", "5,000,000원 × 12개월", 60_000_000]])
    assert detect_period(d) == Period.YEAR


def test_bimonthly_is_not_monthly():
    assert detect_period(doc_from_rows([["Bimonthly invoice"], ["Power", 100]])) != Period.MONTH


def test_quarter_row_with_monthly_basis_stays_quarter():
    from app.billmeta import read_meta
    m = read_meta(doc_from_rows([["가상센터 이용료"], ["3분기 청구 (월 정액 기준)"], ["상면", 300]]))
    assert m.billing_months == 3.0


def test_monthly_title_still_beats_annual_prose(dept_docs):
    assert detect_period(dept_docs["03"]) == Period.MONTH


# ---------------------------------------------------------------- Important 3: 공급가액 / 부가세 / 합계 견적
def test_quote_supply_amount_row_is_the_total():
    d = doc_from_rows([["장비 견적서"], ["품목", "수량", "금액(원)"], ["H100 80GB × 8장 서버", "4대", 1_000_000_000],
                       ["설치비", "4대", 9_000_000], ["공급가액", "", 1_009_000_000], ["부가세", "", 100_900_000],
                       ["합계", "", 1_109_900_000]])
    acq = [h for h in extract_fields(d, DocKind.QUOTE, "q") if h.key == "acquisition" and h.role == "table"]
    assert len(acq) == 1 and acq[0].value.number == 1_009_000_000


# ---------------------------------------------------------------- Important 4: 관리회계 문서가 없으면 사내 인건비를 묻는다
def test_inhouse_is_asked_when_no_management_accounting_doc(Agent, tmp_path, dept_docs):
    seen = []
    agent, s = start_many(Agent, tmp_path, [v for k, v in dept_docs.items() if k != "05"], seen)
    s = past_analyze(agent, s)
    if s.question.step == Step.MISSING:  # 운영비 칸이 비면 기준표 카드에서 '포함'으로
        s = agent.answer(s.id, Answer(question_id=s.question.id, values={"choice:operations": "excluded"}))
    f = {x.name: x for x in s.question.fields}
    assert "inhouse_annual" in f
    s = agent.answer(s.id, Answer(question_id=s.question.id, values={"inhouse_annual": 72_000_000}))
    items = {i["itemType"]: i for i in seen[-1]["items"]}
    assert items["OPERATIONS"]["amount"] == 72_000_000


# ---------------------------------------------------------------- Important 5: 시트 이름이 LLM에 가지 않음
def test_sheet_names_are_not_sent_to_llm():
    from app.mapping.fields_llm import map_fields_llm
    d = doc_from_rows([["품목", "금액"], ["공급가액 합계", 1_009_000_000]], sheet="솔빛컴퓨팅 울산 SIM-261126-02")
    sent = []

    def complete(system, user, schema):
        sent.append(user)
        return '{"fields":[{"key":"acquisition","ref":"L1","col":1}]}'
    out = map_fields_llm(d, d.rows, ["acquisition"], complete)
    assert "솔빛" not in sent[0] and "울산" not in sent[0] and "SIM" not in sent[0]
    assert out == [("acquisition", d.rows[1].ref, 1)]


# ---------------------------------------------------------------- Important 6: 재시작 뒤에도 부서 문서 값 유지
def test_sheet_survives_lost_memory(Agent, tmp_path, dept_docs):
    agent, s = start_many(Agent, tmp_path, list(dept_docs.values()))
    s = past_analyze(agent, s)
    agent._hits.clear()  # 프로세스 재시작으로 메모리 값이 사라진 상황
    s = agent.answer(s.id, Answer(question_id=s.question.id, values={}))
    assert s.sheet.cells["operations"].value == 72_000_000
    assert {i.item_type: i for i in s.final_items}[ItemType.OPERATIONS].amount == 72_000_000


# ---------------------------------------------------------------- Important 7: 두 번째 청구서의 문제도 보인다
def test_second_bill_issues_are_shown(Agent, tmp_path, doc):
    from app.parsing import extract
    from tests.conftest import ROOT
    p = next((ROOT / "samples").glob("bill_e*"))  # 총액 불일치 청구서
    agent, s = start_many(Agent, tmp_path, [doc("a"), extract(p.name, p.read_bytes())])
    warns = [e.message for e in s.events if e.kind == "warn"]
    assert any(w.startswith("bill_e") and "총액" in w for w in warns)
    assert any(n.startswith("bill_e") for n in s.sheet.notices)
