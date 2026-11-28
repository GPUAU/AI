"""샘플별 승인 판정 인수 시나리오 (설계 §6). 판정은 규칙 표(§4 ⑦)와 손으로 대조해 고정했다."""
import pytest

from app.agent.models import Answer, Goal, Step
from app.agent.orchestrator import CostAgent
from app.mapping.rules import RulesMapper
from app.mapping.templates import TemplateStore
from app.service import BillService
from tests.conftest import BUILDERS
from tests.test_agent import assumptions, fake_backend, real_backend, suggested

GPUS = {"bill_r1": 504, "bill_r2": 256, "bill_r3": 400, "bill_r4": 160, "bill_r5": 200}  # 계약전력 ÷ 2kW


def run(tmp_path, d, gpus, terms=None, backend=None, **assume):
    agent = CostAgent(BillService(RulesMapper(), TemplateStore(tmp_path)), backend or real_backend()[0])
    s = agent.start(d, Goal(model_id="29", gpu_count=gpus))
    for _ in range(14):
        if s.question is None:
            break
        q = s.question
        if q.step == Step.ANALYZE and q.options and q.options[0].value.startswith("currency"):
            ans = Answer(question_id=q.id, choice="currency:KRW")
        elif q.step == Step.ASSUMPTIONS:
            vals = {k: v for k, v in assumptions(gpu_count=gpus, **assume).items() if k != "acquisition_amount"}
            ans = Answer(question_id=q.id, values=vals)  # 취득비는 예시값(GPU 수 × $38,000) 그대로
        elif q.step == Step.TERMS and terms:
            ans = Answer(question_id=q.id, values={"discount_3": terms[0], "discount_5": terms[1]})
        else:
            ans = suggested(q)
        s = agent.answer(s.id, ans)
    return s


@pytest.mark.parametrize("name", list(BUILDERS))
def test_default_discounts_put_5y_below_cost(tmp_path, detailed_doc, name):
    # 기본 할인율 5년 25% > 목표 마진 20% → 5년가가 원가보다 낮다 (재검토), 3년가 손익분기 여유 부족, 금리 미반영
    s = run(tmp_path, detailed_doc(name), GPUS[name[:7]])
    assert s.step == Step.DONE and s.report_ready and s.checks_failed == []
    assert s.approval.verdict == "재검토"
    assert [f.code for f in s.approval.findings] == ["PRICE_BELOW_COST", "THIN_BREAKEVEN", "NO_FINANCING"]
    mix = {b["group"]: b["share"] for b in s.approval.benchmark}
    assert all(0.5 <= b["ratio"] <= 2 for b in s.approval.benchmark), mix  # 업계 구성비와 비슷 (ODD_MIX 없음)


def test_small_discounts_with_interest_is_approval(tmp_path, detailed_doc):
    s = run(tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"), 504, terms=(3, 5),
            interest_rate_type="ASSUMED", annual_interest_rate=5)
    assert s.approval.interest_applied and s.approval.verdict == "승인 권고", [f.code for f in s.approval.findings]


def test_small_discounts_without_interest_is_conditional(tmp_path, detailed_doc):
    s = run(tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"), 504, terms=(3, 5))
    assert s.approval.verdict == "조건부" and [f.code for f in s.approval.findings] == ["NO_FINANCING"]


def test_excluded_missing_cost_is_review(tmp_path, doc):
    agent = CostAgent(BillService(RulesMapper(), TemplateStore(tmp_path)), real_backend()[0])
    s = agent.start(doc("c"), Goal(model_id="29", gpu_count=32))
    s = agent.answer(s.id, Answer(question_id=s.question.id, values={"choice:운영·유지보수": "exclude"}))
    for _ in range(10):
        if s.question is None:
            break
        q = s.question
        s = agent.answer(s.id, Answer(question_id=q.id, values=assumptions()) if q.step == Step.ASSUMPTIONS else suggested(q))
    assert "MISSING_EXCLUDED" in {f.code for f in s.approval.findings} and s.approval.verdict == "재검토"
    assert s.report_ready and any("운영·유지보수" in r for r in __import__("app.agent.verify", fromlist=["risks"]).risks(s))
