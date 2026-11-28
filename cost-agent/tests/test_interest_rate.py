"""금리 가정: 한국은행 공시 금리(BE /interest-rates/latest)로 미리 채우고, 자금팀 문서 금리가 있으면 그것을 쓴다."""

import pytest

from app.agent.models import Answer, Goal, Step
from app.mapping.rules import RulesMapper
from app.mapping.templates import TemplateStore
from app.service import BillService
from tests.test_agent import Agent, fake_backend, suggested  # noqa: F401
from tests.test_costsheet_agent import past_analyze

ECOS = {"status": "AVAILABLE", "seriesName": "회사채(3년, AA-)", "annualRatePercent": "4.416", "annualRate": 0.04416,
        "referenceDate": "2026-11-27", "source": {"name": "한국은행 경제통계시스템(ECOS) 시장금리(일별)"}}


def _backend(seen, rate=ECOS):
    be, _ = fake_backend(seen=seen)
    be.latest_interest = lambda: rate if rate and rate.get("status") == "AVAILABLE" else None
    return be


def _to_assumptions(Agent, tmp_path, doc, be):
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), be)
    s = agent.start(doc("c"), Goal(model_id="29", gpu_count=256))
    for _ in range(6):
        if s.question is None or s.question.step == Step.ASSUMPTIONS:
            break
        s = agent.answer(s.id, suggested(s.question))
    return agent, s


def test_public_rate_prefills_interest_assumption(Agent, tmp_path, doc):
    seen = []
    agent, s = _to_assumptions(Agent, tmp_path, doc, _backend(seen))
    f = {x.name: x for x in s.question.fields}
    assert f["interest_rate_type"].value == "ASSUMED"
    assert f["annual_interest_rate"].value == pytest.approx(4.416)
    assert "한국은행" in f["annual_interest_rate"].hint and "2026-11-27" in f["annual_interest_rate"].hint
    vals = {k: v for k, v in suggested(s.question).values.items() if k not in ("interest_rate_type", "annual_interest_rate")}
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=vals))
    a = s.assumptions
    assert a["interest_rate_type"] == "ASSUMED" and a["annual_interest_rate"] == pytest.approx(0.04416)
    assert a["grades"]["annual_interest_rate"] == "공개자료"
    req = seen[-1]
    assert any(i["itemType"] == "FINANCING" for i in req["items"])
    assert req["assumptions"]["annualInterestRate"] == pytest.approx(0.04416)


def test_without_public_rate_nothing_changes(Agent, tmp_path, doc):
    agent, s = _to_assumptions(Agent, tmp_path, doc, _backend([], rate={"status": "NOT_AVAILABLE"}))
    f = {x.name: x for x in s.question.fields}
    assert f["interest_rate_type"].value == "NOT_APPLIED" and f["annual_interest_rate"].value is None


def test_rate_api_failure_does_not_break_the_card(Agent, tmp_path, doc):
    be, _ = fake_backend()
    from app.be_client import BackendError

    def boom():
        raise BackendError("down")
    be.latest_interest = boom
    agent, s = _to_assumptions(Agent, tmp_path, doc, be)
    assert {x.name: x for x in s.question.fields}["interest_rate_type"].value == "NOT_APPLIED"


def test_finance_team_rate_beats_public_rate(Agent, tmp_path, dept_docs):
    d = dept_docs["07"]
    extra = d.rows[-1].model_copy(update={"row": 99, "ref": f"{d.rows[-1].sheet}!99",
                                          "cells": ["대출 금리", "", "5.2%"], "numbers": {}})
    docs = [v for k, v in dept_docs.items() if k != "07"] + [d.model_copy(update={"rows": [*d.rows, extra]})]
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), _backend([]))
    s = past_analyze(agent, agent.start_many(docs, Goal(terms_years=[])))
    f = {x.name: x for x in s.question.fields}
    assert f["annual_interest_rate"].value == pytest.approx(5.2) and "07" in f["annual_interest_rate"].hint
    assert f["interest_rate_type"].value == "ACTUAL_LOAN"


def test_rate_row_is_extracted_from_department_memo():
    from app.docs import DocKind, extract_fields
    from tests.conftest import doc_from_rows
    d = doc_from_rows([["가격 검토용 금리 기준"], ["항목", "제출값"], ["차입 금리", "4.8%"]])
    hits = [h for h in extract_fields(d, DocKind.MEMO, "x") if h.key == "annual_interest_rate"]
    assert hits and hits[0].value.number == pytest.approx(0.048)
