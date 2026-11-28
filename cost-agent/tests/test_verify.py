"""보고서 자동 검증 관문 (설계 §4B-5)."""
import pytest

from app.agent import verify as V
from app.agent.models import Answer, Step
from app.agent.orchestrator import CostAgent
from app.mapping.rules import RulesMapper
from app.mapping.templates import TemplateStore
from app.service import BillService
from tests.test_agent import assumptions, real_backend, suggested

CHECKS = ["분해 합계", "제안가 공식", "기간별 가격", "손익분기", "시장 통계", "리스크 누락 금지", "권고 일치"]


def done_session(tmp_path, d, **assume):
    backend, _ = real_backend()
    agent = CostAgent(BillService(RulesMapper(), TemplateStore(tmp_path)), backend)
    from app.agent.models import Goal
    s = agent.start(d, Goal(model_id="29", gpu_count=504))
    for _ in range(14):
        if s.question is None:
            break
        q = s.question
        vals = {k: v for k, v in assumptions(gpu_count=504, **assume).items() if v is not None}  # None = 제안값 그대로
        s = agent.answer(s.id, Answer(question_id=q.id, values=vals)
                         if q.step == Step.ASSUMPTIONS else suggested(q))
    return agent, s


def test_clean_session_passes_all_seven(tmp_path, detailed_doc):
    _, s = done_session(tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    passed, failed = V.verify(s)
    assert failed == [] and passed == CHECKS and s.report_ready
    assert s.checks_passed == CHECKS


def test_tampered_term_price_fails(tmp_path, detailed_doc):
    _, s = done_session(tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    s.terms[0].price += 0.5
    assert "기간별 가격" in V.verify(s)[1]


def test_missing_risk_fails(tmp_path, detailed_doc, monkeypatch):
    _, s = done_session(tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    real = V.risks
    monkeypatch.setattr(V, "risks", lambda ss: [r for r in real(ss) if "금융비용" not in r])
    assert "리스크 누락 금지" in V.verify(s)[1]


def test_verdict_mismatch_fails(tmp_path, detailed_doc):
    _, s = done_session(tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    s.approval.verdict = "승인 권고"
    assert "권고 일치" in V.verify(s)[1]


def test_cost_markup_formula_verified(tmp_path, detailed_doc):  # Review Focus 2
    _, s = done_session(tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"), pricing_method="COST_MARKUP")
    assert V.verify(s)[1] == []
    s.simulation["candidatePrice"] = str(float(s.simulation["hourlyCost"]) / (1 - s.assumptions["target_rate"]))
    assert "제안가 공식" in V.verify(s)[1]


def test_report_not_ready_when_verify_fails(tmp_path, detailed_doc, monkeypatch):
    agent, s = done_session(tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    monkeypatch.setattr(V, "risks", lambda ss: [])
    s.report_ready = False
    agent._do_report(s)
    assert not s.report_ready and "리스크 누락 금지" in s.checks_failed
    assert next(b for b in s.board if b.key == Step.REPORT).status.value == "ERROR"


def test_risks_list_expected_items(tmp_path, detailed_doc):
    _, s = done_session(tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"), acquisition_amount=None)
    text = " ".join(V.risks(s))
    assert "금융비용" in text and "예시값" in text


def test_disclosed_breakdown_mismatch_does_not_block(tmp_path, doc):
    from app.agent.models import Goal
    from tests.test_agent import fake_backend
    agent = CostAgent(BillService(RulesMapper(), TemplateStore(tmp_path)), fake_backend(sim_cost=2.8)[0])
    s = agent.start(doc("a"), Goal(model_id="29", gpu_count=8))
    for _ in range(8):
        if s.question is None:
            break
        q = s.question
        s = agent.answer(s.id, Answer(question_id=q.id, values=assumptions(gpu_count=8)) if q.step == Step.ASSUMPTIONS
                         else suggested(q))
    assert "BREAKDOWN_MISMATCH" in {f.code for f in s.approval.findings} and s.approval.verdict == "재검토"
    assert s.report_ready and "분해 합계" in s.checks_passed
