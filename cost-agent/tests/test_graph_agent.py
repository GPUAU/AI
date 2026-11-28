"""LangGraph 에이전트: LLM 판단자가 도구를 고르고, guard가 완료 조건을 지키고, 실패하면 기본 순서로 넘어간다."""
import json
import re

from app.agent.graph import TOOLS, GraphCostAgent
from app.agent.models import Answer, Goal, Step
from app.mapping.rules import RulesMapper
from app.mapping.templates import TemplateStore
from app.service import BillService
from tests.test_agent import assumptions, fake_backend


def hinted(calls, first=("analyze_bill", {}), premature_finish=True):
    """[현재 상태]의 '→ 도구(종류)' 안내를 읽고 고르는 가짜 판단자. 두 번째에는 일부러 finish를 일찍 부른다."""
    def decide(messages, tools):
        calls.append(messages)
        n = len(calls)
        if n == 1:
            name, args = first
        elif n == 2 and premature_finish:
            name, args = "finish", {}
        else:
            m = re.search(r"→ (\w+)(?:\((\w+)\))?", messages[-1]["content"])
            name, args = (m.group(1), {"kind": m.group(2)} if m.group(2) else {}) if m else ("finish", {})
            if name == "remap_bill":
                args = {"hypothesis": "소계 줄을 원가로 넣었을 수 있다"}
        return {"name": name, "args": {"reason": f"{name} 단계가 필요하다", **args}, "id": f"c{n}"}
    return decide


def agent_with(tmp_path, controller, sim_cost=2.8):
    backend, seen = fake_backend(sim_cost=sim_cost)
    return GraphCostAgent(BillService(RulesMapper(), TemplateStore(tmp_path)), backend, controller=controller), seen


def test_llm_controller_drives_tools_and_guard_blocks_early_finish(tmp_path, doc):
    calls = []
    agent, _ = agent_with(tmp_path, hinted(calls))
    s = agent.start(doc("c"), Goal(model_id="29", gpu_count=32))

    # 판단자가 finish를 일찍 불렀지만 guard가 이유와 함께 돌려보냈다
    refusal = next(m["content"] for m in calls[2] if m.get("role") == "tool" and m["content"].startswith("실행 불가"))
    assert "누락 비용 미처리: 운영·유지보수" in refusal and "가정값 미확정" in refusal
    assert s.step == Step.MISSING and s.question.title == "청구서에 없는 큰 비용이 1개 있어요"
    assert any(e.kind == "think" and e.message.startswith("에이전트 판단") for e in s.events)

    s = agent.answer(s.id, Answer(question_id=s.question.id, choice="amount",
                                  values={"amount": 1000000, "currency": "KRW", "period": "MONTH"}))
    assert s.step == Step.ASSUMPTIONS
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=assumptions()))
    assert s.step == Step.TERMS                             # 시장 범위 안 → 질문 없이 기간별 가격
    s = agent.answer(s.id, Answer(question_id=s.question.id, values={}))
    assert s.question.step == Step.REVIEW                   # 승인 검토 발견 사항
    s = agent.answer(s.id, Answer(question_id=s.question.id,
                                  values={f.name: "확인" for f in s.question.fields if f.name.startswith("reason:")}))
    assert s.step == Step.DONE and s.report_ready
    assert all(d["by"] == "llm" for d in s.decisions)
    assert [d["tool"] for d in s.decisions][:2] == ["analyze_bill", "finish"]
    assert agent.get(s.id).report_ready                    # 체크포인터에서 다시 읽어도 같다


def test_controller_never_sees_amounts_or_identifiers(tmp_path, doc):
    calls = []
    agent, _ = agent_with(tmp_path, hinted(calls))
    d = doc("c")
    s = agent.start(d, Goal(model_id="29", gpu_count=32))
    s = agent.answer(s.id, Answer(question_id=s.question.id, choice="amount",
                                  values={"amount": 1234567, "currency": "KRW", "period": "MONTH"}))
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=assumptions(acquisition_amount=7654321)))
    sent = json.dumps(calls, ensure_ascii=False)
    amounts = {v for r in d.rows for v in r.numbers.values() if abs(v) >= 1000}
    assert len(amounts) >= 5                                # 청구서 금액이 실제로 검사 대상에 들어갔다
    for a in amounts | {1234567, 7654321}:
        for form in (f"{a:.0f}", f"{a:,.0f}"):
            assert form not in sent, form
    assert d.file_name not in sent
    assert "$" not in sent                                  # 원가·제안가 금액도 보내지 않는다


def test_falls_back_to_policy_when_controller_fails(tmp_path, doc):
    agent, _ = agent_with(tmp_path, lambda messages, tools: None)
    s = agent.start(doc("a"), Goal(model_id="29", gpu_count=8))
    assert s.step == Step.ASSUMPTIONS and all(d["by"] == "policy" for d in s.decisions)


def test_repeated_refusals_hand_over_to_policy(tmp_path, doc):
    def stubborn(messages, tools):            # 계속 원가 계산만 부르는 판단자
        return {"name": "simulate_cost", "args": {"reason": "바로 계산"}, "id": "x"}
    agent, seen = agent_with(tmp_path, stubborn)
    s = agent.start(doc("a"), Goal(model_id="29", gpu_count=8))
    assert s.step == Step.ASSUMPTIONS and not seen         # 가정값 없이 계산하지 않았다
    assert any(d["by"] == "policy" for d in s.decisions)
    assert any(e.kind == "warn" and "실행 불가" in e.message for e in s.events)


def test_tools_are_strict_schemas_with_reason():
    for t in TOOLS:
        f = t["function"]
        assert f["strict"] and "reason" in f["parameters"]["required"]
        assert f["parameters"]["additionalProperties"] is False


def test_graph_shape():
    from app.agent.graph import GraphCostAgent as G
    g = G(BillService(RulesMapper(), TemplateStore("/tmp/unused-templates")), fake_backend()[0])
    m = g.mermaid()
    assert "decide" in m and "act" in m and "wait" in m


def test_simulate_refused_before_interpret(tmp_path, detailed_doc):
    calls = []

    def eager(messages, tools):
        calls.append(messages)
        name = "analyze_bill" if len(calls) == 1 else "simulate_cost"
        return {"name": name, "args": {"reason": "바로 계산한다"}, "id": f"c{len(calls)}"}
    agent, _ = agent_with(tmp_path, eager)
    s = agent.start(detailed_doc("bill_r2_multisheet_thousand_krw.xlsx"), Goal(model_id="29", gpu_count=256))
    assert any(e.kind == "warn" and "청구서 해석" in e.message for e in s.events)
    assert s.question is not None and s.question.title.startswith("청구서를 이렇게 읽었어요")


def test_controller_sees_codes_not_messages(tmp_path, detailed_doc):
    calls = []
    agent, _ = agent_with(tmp_path, hinted(calls, premature_finish=False))
    s = agent.start(detailed_doc("bill_r2_multisheet_thousand_krw.xlsx"), Goal(model_id="29", gpu_count=256))
    assert s.question.title.startswith("청구서를 이렇게 읽었어요")
    sent = json.dumps(calls, ensure_ascii=False)
    assert "UNIT_SCALE" in sent
    assert "천원" not in sent and "1,000배" not in sent and "놓치면" not in sent


def test_finish_refused_until_review(tmp_path, detailed_doc):
    from tests.test_agent import real_backend, suggested
    calls = []
    backend, _ = real_backend()
    agent = GraphCostAgent(BillService(RulesMapper(), TemplateStore(tmp_path)), backend,
                           controller=hinted(calls, premature_finish=False))
    s = agent.start(detailed_doc("bill_r1_kr_metered_krw.xlsx"), Goal(model_id="29", gpu_count=504))
    for _ in range(12):
        if s.question is None or s.question.step == Step.REVIEW:
            break
        q = s.question
        s = agent.answer(s.id, Answer(question_id=q.id, values=assumptions(gpu_count=504)) if q.step == Step.ASSUMPTIONS
                         else suggested(q))
    assert s.question is not None and s.question.step == Step.REVIEW
    sent = json.dumps(calls, ensure_ascii=False)
    assert "NO_FINANCING" in sent and "손익분기" not in sent and "이자)이 원가에" not in sent
