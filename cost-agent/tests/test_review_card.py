"""승인 검토 카드: 막는 것/참고 구분, 제안대로 고치기, 사유는 칩으로 (긴 사유 입력 없이)."""

import pytest

from app.agent.models import Answer, Goal, Step
from app.agent.orchestrator import AgentError
from tests.test_agent import Agent, start, suggested  # noqa: F401


def to_review(Agent, tmp_path, doc):
    agent, s = start(Agent, tmp_path, doc("c"))   # 가짜 BE: 원가 $2.0, 제안가 $2.5 · 할인 15/25% → 5년가 $1.875 < 원가
    for _ in range(12):
        if s.question is None or s.question.step == Step.REVIEW:
            break
        q = s.question
        ans = Answer(question_id=q.id, choice="keep") if q.step == Step.MARKET else suggested(q)  # 제안가 유지
        s = agent.answer(s.id, ans)
    assert s.question is not None and s.question.step == Step.REVIEW
    return agent, s


def _f(q):
    return {f.name: f for f in q.fields}


def decide(q, **values):
    """지정하지 않은 막는 것은 '이대로 진행 · 전략 가격'으로."""
    out = dict(values)
    for f in q.fields:
        if f.name.startswith("finding:") and f.meta.get("kind") == "block" and f.name not in out:
            code = f.name.split(":", 1)[1]
            out[f.name], out[f"reason:{code}"] = "proceed", "strategic"
    return Answer(question_id=q.id, values=out)


def test_blocking_and_reference_findings_are_split(Agent, tmp_path, doc):
    _, s = to_review(Agent, tmp_path, doc)
    f = _f(s.question)
    block = f["finding:PRICE_BELOW_COST"]
    assert block.meta["kind"] == "block" and block.value is None  # 막는 것은 담당자가 골라야 한다
    assert [o.value for o in block.options] == ["apply", "fix", "proceed"]
    notes = [x for x in s.question.fields if x.name.startswith("finding:") and x.meta.get("kind") == "note"]
    assert notes and all(x.value == "ack" for x in notes)  # 참고는 확인만
    assert f["reason:PRICE_BELOW_COST"].type == "select" and f["reason:PRICE_BELOW_COST"].show_for == ["proceed"]
    assert [o.label for o in f["reason:PRICE_BELOW_COST"].options] == ["전략 가격", "시장 진입용", "고객 협의 완료", "기타"]
    assert f["reason_text:PRICE_BELOW_COST"].show_for == ["other"]
    assert "reason:THIN_MARKET" not in f  # 참고에는 사유칸이 없다


def test_suggestion_is_specific(Agent, tmp_path, doc):
    _, s = to_review(Agent, tmp_path, doc)
    sug = _f(s.question)["finding:PRICE_BELOW_COST"].meta["suggestion"]
    assert sug["terms"] == {"5": 0.2}
    assert "5년 할인 25% → 20%" in sug["label"] and "$2.00" in sug["effect"]


def test_apply_fixes_in_place(Agent, tmp_path, doc):
    agent, s = to_review(Agent, tmp_path, doc)
    s = agent.answer(s.id, decide(s.question, **{"finding:PRICE_BELOW_COST": "apply"}))
    five = next(t for t in s.terms if t.years == 5)
    assert five.discount_rate == pytest.approx(0.2) and five.price >= float(s.simulation["hourlyCost"])
    assert "PRICE_BELOW_COST" not in {f.code for f in s.approval.findings}
    assert any("제안대로 고침" in e.message for e in s.events)


def test_proceed_with_chip_needs_no_typing(Agent, tmp_path, doc):
    agent, s = to_review(Agent, tmp_path, doc)
    s = agent.answer(s.id, decide(s.question, **{"finding:PRICE_BELOW_COST": "proceed", "reason:PRICE_BELOW_COST": "market_entry"}))
    by = {f.code: f for f in s.approval.findings}
    assert by["PRICE_BELOW_COST"].reason == "시장 진입용" and by["PRICE_BELOW_COST"].decision == "proceed"
    assert all(f.reason for f in s.approval.findings)  # 참고는 '참고 확인'으로 함께 처리
    assert {f.decision for f in s.approval.findings if f.level == "조건부"} == {"ack"}
    assert s.step == Step.DONE or (s.question is not None and s.question.step != Step.REVIEW)


def test_other_chip_needs_text(Agent, tmp_path, doc):
    agent, s = to_review(Agent, tmp_path, doc)
    with pytest.raises(AgentError):
        agent.answer(s.id, decide(s.question, **{"finding:PRICE_BELOW_COST": "proceed", "reason:PRICE_BELOW_COST": "other"}))
    s = agent.get(s.id)
    s = agent.answer(s.id, decide(s.question, **{
        "finding:PRICE_BELOW_COST": "proceed", "reason:PRICE_BELOW_COST": "other", "reason_text:PRICE_BELOW_COST": "본부장 승인"}))
    assert next(f for f in s.approval.findings if f.code == "PRICE_BELOW_COST").reason == "기타: 본부장 승인"


def test_undecided_block_is_rejected(Agent, tmp_path, doc):
    agent, s = to_review(Agent, tmp_path, doc)
    with pytest.raises(AgentError, match="막는 항목"):
        agent.answer(s.id, Answer(question_id=s.question.id, values={}))


def test_note_can_be_sent_back_to_fix(Agent, tmp_path, doc):
    agent, s = to_review(Agent, tmp_path, doc)
    note = next(x.name.split(":", 1)[1] for x in s.question.fields
                if x.name.startswith("finding:") and x.meta.get("kind") == "note" and any(o.value == "fix" for o in x.options))
    s = agent.answer(s.id, decide(s.question, **{f"finding:{note}": "fix"}))
    assert s.step != Step.DONE  # 고치러 돌아감


def test_decisions_are_recorded_in_report(Agent, tmp_path, doc):
    import io

    from openpyxl import load_workbook

    from app.agent.report import build_report
    from app.agent.report_html import build_report_html
    agent, s = to_review(Agent, tmp_path, doc)
    s = agent.answer(s.id, decide(s.question, **{"finding:PRICE_BELOW_COST": "apply"}))
    for _ in range(4):  # 다시 검토하면 남은 막는 것만 결정
        if s.question is None or s.question.step != Step.REVIEW:
            break
        s = agent.answer(s.id, decide(s.question))
    assert s.approval.applied and s.approval.applied[0]["code"] == "PRICE_BELOW_COST"
    ws = load_workbook(io.BytesIO(build_report(s)))["검토 결과"]
    assert [c.value for c in ws[1]] == ["코드", "수준", "내용", "담당자 결정", "사유"]
    rows = [[c.value for c in r] for r in ws.iter_rows(min_row=2)]
    assert any(r[0] == "PRICE_BELOW_COST" and r[3] == "제안대로 고침" and "20%" in (r[4] or "") for r in rows)
    assert any(r[3] == "그대로 진행" and r[4] == "전략 가격" for r in rows)
    assert "제안대로 고침" in build_report_html(s)
