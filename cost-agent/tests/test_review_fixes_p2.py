"""2순위 최종 리뷰 지적 재현 테스트 (feat/price-approval)."""
import pytest

from app.agent import verify as V
from app.agent.approval import verdict
from app.agent.graph import GraphCostAgent, _Refused
from app.agent.models import Answer, Goal, Step
from app.agent.orchestrator import AgentError, CostAgent
from app.mapping.rules import RulesMapper
from app.mapping.templates import TemplateStore
from app.service import BillService
from tests.test_agent import Agent, assumptions, real_backend, run_to, suggested, to_review  # noqa: F401 (Agent fixture)


def fe_values(q, **over):
    """FE readAnswer처럼: 화면의 값을 그대로, 숫자는 Number()로 (정수면 int)."""
    out = {}
    for f in q.fields:
        if f.value is None:
            continue
        v = f.value
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        out[f.name] = v
    out.update(over)
    return out


def at_assumptions(Agent, tmp_path, d, gpus=504):
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), real_backend()[0])
    s = agent.start(d, Goal(model_id="29", gpu_count=gpus))
    return agent, run_to(agent, s, Step.ASSUMPTIONS)


# 1 (Critical) FE가 보낸 정수 값이 제안값(실수)과 달라 '담당자 입력'으로 등급이 매겨짐
def test_untouched_example_from_fe_is_graded_example(Agent, tmp_path, detailed_doc):
    agent, s = at_assumptions(Agent, tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=fe_values(s.question)))
    g = s.assumptions["grades"]
    assert g["acquisition_amount"] == "예시값" and g["fx_krw_per_usd"] == "공개자료"


# 2 (Important) 가정값 카드를 다시 열면 담당자 값이 예시값으로 떨어짐
def test_revisited_card_keeps_manager_grade(tmp_path, detailed_doc):  # 세션을 직접 고치므로 워크플로 엔진으로
    Agent = CostAgent
    agent, s = at_assumptions(Agent, tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=fe_values(s.question, acquisition_amount=15_000_000)))
    assert s.assumptions["grades"]["acquisition_amount"] == "담당자 입력"
    q = agent._q_assumptions(s)
    s.question = q
    s = agent.answer(s.id, Answer(question_id=q.id, values=fe_values(q)))
    assert s.assumptions["grades"]["acquisition_amount"] == "담당자 입력"


# 3 (Important) 누락 비용 예시 금액이 조용히 실제 원가가 됨
def test_missing_example_amount_is_flagged(Agent, tmp_path, detailed_doc):
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), real_backend()[0])
    s = agent.start(detailed_doc("bill_r4_prorated_first_month_krw.xlsx"), Goal(model_id="29", gpu_count=160))
    s = run_to(agent, s, Step.MISSING)
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=fe_values(s.question)))
    assert s.missing_answers["네트워크"]["source"] == "예시값"
    for _ in range(10):
        if s.question is None or s.question.step == Step.REVIEW:
            break
        q = s.question
        s = agent.answer(s.id, Answer(question_id=q.id, values=assumptions(gpu_count=160)) if q.step == Step.ASSUMPTIONS
                         else suggested(q))
    assert "MISSING_EXAMPLE" in {f.code for f in s.approval.findings}
    assert any("네트워크" in r and "예시값" in r for r in V.risks(s))


# 4 (Important) 사유가 code로만 이어져 뜻이 바뀐 발견 사항에도 남음 / 고치기를 골라도 사유가 남음
def test_reason_not_carried_when_message_changes(Agent, tmp_path, detailed_doc):
    agent, s, _ = to_review(Agent, tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    cost = float(s.simulation["hourlyCost"])
    s.terms[1].price = cost * 0.95  # 5년가만 원가 아래
    agent._do_review(s)
    assert next(f for f in s.approval.findings if f.code == "PRICE_BELOW_COST")
    for f in s.approval.findings:
        f.reason = "확인"
    s.terms[0].price = cost * 0.9  # 3년가도 원가 아래로 → PRICE_BELOW_COST 문구가 바뀜
    agent._do_review(s)
    f = next(f for f in s.approval.findings if f.code == "PRICE_BELOW_COST")
    assert f.reason == ""


def test_fix_clears_reason(tmp_path, detailed_doc):  # 세션을 직접 고치므로 워크플로 엔진으로
    Agent = CostAgent
    agent, s, _ = to_review(Agent, tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    next(f for f in s.approval.findings if f.code == "NO_FINANCING").reason = "예전 사유"  # 앞서 사유를 적었던 발견 사항
    vals = {f.name: "확인" for f in s.question.fields if f.name.startswith("reason:")}
    vals["finding:NO_FINANCING"] = "fix"
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=vals))
    assert s.question.step == Step.ASSUMPTIONS
    for _ in range(8):
        if s.question is None or s.question.step == Step.REVIEW:
            break
        q = s.question
        s = agent.answer(s.id, Answer(question_id=q.id, values=assumptions(gpu_count=504)) if q.step == Step.ASSUMPTIONS
                         else suggested(q))
    f = next(f for f in s.approval.findings if f.code == "NO_FINANCING")
    assert f.reason == "" and s.question.step == Step.REVIEW


# 5 (Important) 자동 검증 실패가 막다른 길
def test_verify_failure_offers_way_back(Agent, tmp_path, detailed_doc, monkeypatch):
    agent, s, _ = to_review(Agent, tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    real = V.verify
    monkeypatch.setattr(V, "verify", lambda ss: (V.CHECKS[:1], V.CHECKS[1:2]))
    s = agent.answer(s.id, suggested(s.question))
    assert s.step != Step.DONE and s.question is not None and s.question.step == Step.REPORT
    assert [o.value for o in s.question.options] == ["assumptions"]
    monkeypatch.setattr(V, "verify", real)
    s = agent.answer(s.id, Answer(question_id=s.question.id, choice="assumptions"))
    assert s.question.step == Step.ASSUMPTIONS


# 6 (Important) 검사 두 개가 실패할 수 없음
def test_verdict_check_recomputes_findings(Agent, tmp_path, detailed_doc):
    agent, s, _ = to_review(Agent, tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    s.terms[1].price = float(s.simulation["hourlyCost"]) * 0.95  # 5년가를 원가 아래로
    agent._do_review(s)
    s.approval.findings = [f for f in s.approval.findings if f.code != "PRICE_BELOW_COST"]
    s.approval.verdict = verdict(s.approval.findings)[0]
    assert "권고 일치" in V.verify(s)[1]


def test_risk_check_reads_rendered_report(Agent, tmp_path, detailed_doc, monkeypatch):
    from app.agent import report_html
    agent, s, _ = to_review(Agent, tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    real = report_html.build_report_html
    monkeypatch.setattr(report_html, "build_report_html",
                        lambda ss, **kw: real(ss, **kw).replace("금융비용(이자)이 원가에 반영되지", "금융비용 —"))
    assert "리스크 누락 금지" in V.verify(s)[1]


# (Minor → 반영) 손익분기 문구, BE가 금리를 무시한 경우, 그래프 오래된 검토 카드
def test_below_cost_message_is_not_loss_on_every_sale(Agent, tmp_path, detailed_doc):
    agent, s, _ = to_review(Agent, tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    s.terms[1].price = float(s.simulation["hourlyCost"]) * 0.95  # 5년가를 원가 아래로
    agent._do_review(s)
    m = next(f for f in s.approval.findings if f.code == "PRICE_BELOW_COST").message
    assert "팔수록" not in m and "가정 가동률" in m and "손익분기" in m


def test_be_ignored_interest_has_own_message(Agent, tmp_path, detailed_doc):
    backend, _ = real_backend(interest_supported=False)
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), backend)
    s = agent.start(detailed_doc("bill_r1_kr_metered_krw.xlsx"), Goal(model_id="29", gpu_count=504))
    for _ in range(12):
        if s.question is None or s.question.step == Step.REVIEW:
            break
        q = s.question
        s = agent.answer(s.id, Answer(question_id=q.id, values=assumptions(
            gpu_count=504, interest_rate_type="ASSUMED", annual_interest_rate=5)) if q.step == Step.ASSUMPTIONS else suggested(q))
    m = next(f for f in s.approval.findings if f.code == "NO_FINANCING").message
    assert "원가 서버가 금리를 반영하지 않았어요" in m


def test_graph_refuses_stale_review_card(tmp_path, detailed_doc):
    agent, s, _ = to_review(lambda b, be: GraphCostAgent(b, be), tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    s = agent.get(s.id)
    s.assumptions["utilization"] = 0.5  # 다시 계산하지 않은 상태
    with pytest.raises(_Refused):
        agent._tool_ask_user(s, "review")
