"""여러 부서 문서 세션: 기준표 확인 카드, 기준표로 미리 채운 가정값, 여러 파일 API."""

import pytest
from fastapi.testclient import TestClient

from app.agent.models import Answer, Goal, Step
from app.mapping.rules import RulesMapper
from app.mapping.templates import TemplateStore
from app.models import SheetRow
from app.service import BillService
from tests.test_agent import Agent, fake_backend, suggested  # noqa: F401 - Agent fixture 재사용


def start_many(Agent, tmp_path, docs, seen=None):
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), fake_backend(seen=seen)[0])
    return agent, agent.start_many(docs, Goal(terms_years=[3, 5]))


def past_analyze(agent, s):
    """청구서 해석 카드는 제안대로 넘긴다."""
    for _ in range(5):
        if s.question is None or s.question.step != Step.ANALYZE:
            return s
        s = agent.answer(s.id, suggested(s.question))
    return s


def _conflict_docs(dept_docs):
    """04 재무회계팀 취득비를 1,100,000,000원으로 바꾼 변형."""
    d = dept_docs["04"]
    rows = [r.model_copy(update={"cells": [*r.cells[:3], "1,100,000,000원"], "numbers": {3: 1_100_000_000}})
            if r.row == 6 else r for r in d.rows]
    return [*(v for k, v in dept_docs.items() if k != "04"), d.model_copy(update={"rows": rows})]


def test_dept_docs_fill_sheet_and_ask_only_problems(Agent, tmp_path, dept_docs):
    agent, s = start_many(Agent, tmp_path, list(dept_docs.values()))
    s = past_analyze(agent, s)
    assert s.question.step == Step.ASSUMPTIONS  # 기준표 확인 카드 없이
    c = s.sheet.cells
    assert all(c[k].status in ("filled", "included") for k in ("rack", "electricity", "network", "operations", "acquisition"))
    assert len({x.doc for x in c["acquisition"].sources}) == 2 and len({x.doc for x in c["gpu_count"].sources}) == 2
    assert c["operations"].value == 72_000_000
    assert [d["kind"] for d in s.docs] == ["QUOTE", "MEMO", "BILL", "MEMO", "MEMO", "MEMO", "MEMO", "PLAN"]


def test_assumption_card_prefilled_from_sheet(Agent, tmp_path, dept_docs):
    seen = []
    agent, s = start_many(Agent, tmp_path, list(dept_docs.values()), seen)
    s = past_analyze(agent, s)
    f = {x.name: x for x in s.question.fields}
    assert f["fx_krw_per_usd"].value == 1350 and "07" in f["fx_krw_per_usd"].hint
    assert f["utilization"].value == 70 and f["target_rate"].value == 20 and f["gpu_count"].value == 32
    assert f["model_id"].value == "4"  # H100 80GB → BE 모델 H100
    assert f["acquisition_amount"].value == 1_009_000_000 and f["acquisition_currency"].value == "KRW"
    assert f["depreciation_years"].value == 5 and f["pricing_method"].value == "REVENUE_MARGIN"
    assert "inhouse_annual" not in f  # 사내 인건비는 기준표 운영비에 이미 있음
    s = agent.answer(s.id, Answer(question_id=s.question.id, values={}))
    assert s.assumptions["grades"]["fx_krw_per_usd"] == "사내 문서"
    items = {i["itemType"]: i for i in seen[-1]["items"]}
    assert items["OPERATIONS"]["amount"] == 72_000_000 and items["ELECTRICITY"]["amount"] == 57_600_000
    assert items["ACQUISITION"]["amount"] == 1_009_000_000 and items["ACQUISITION"]["currency"] == "KRW"
    assert sum(1 for i in seen[-1]["items"] if i["itemType"] == "ACQUISITION") == 1


def _questions(agent, s, limit=15):
    out = []
    for _ in range(limit):
        if s.question is None:
            break
        out.append((s.question.step, s.question.title))
        s = agent.answer(s.id, suggested(s.question))
    return out, s


def test_single_bill_upload_behaves_as_before(Agent, tmp_path, doc):  # Review Focus 3
    seen_a, seen_b = [], []
    a1 = Agent(BillService(RulesMapper(), TemplateStore(tmp_path / "a")), fake_backend(seen=seen_a)[0])
    a2 = Agent(BillService(RulesMapper(), TemplateStore(tmp_path / "b")), fake_backend(seen=seen_b)[0])
    q1, s1 = _questions(a1, a1.start(doc("c"), Goal(model_id="29", gpu_count=256)))
    q2, s2 = _questions(a2, a2.start_many([doc("c")], Goal(model_id="29", gpu_count=256)))
    assert q1 == q2 and seen_a == seen_b and s2.sheet is None


def test_missing_core_cell_is_asked_with_owner(Agent, tmp_path, dept_docs):
    docs = [v for k, v in dept_docs.items() if k != "03"]  # 시설 영수증이 없으면 전력·상면·회선 칸이 빔
    agent, s = start_many(Agent, tmp_path, docs)
    s = past_analyze(agent, s)
    q = s.question
    assert q.step == Step.MISSING and q.title.startswith("기준표를 확인해 주세요 — 확인할 칸 3개")
    f = {x.name: x for x in q.fields}
    assert {"choice:electricity", "cell:electricity", "choice:rack", "choice:network"} <= set(f)
    assert "시설운영팀" in f["choice:electricity"].hint
    s = agent.answer(s.id, Answer(question_id=q.id, values={
        "choice:electricity": "amount", "cell:electricity": 57_600_000, "choice:rack": "included", "choice:network": "excluded"}))
    c = s.sheet.cells
    assert c["electricity"].value == 57_600_000 and c["electricity"].grade == "담당자 입력"
    assert c["rack"].status == "included" and c["network"].status == "excluded"
    assert s.question.step == Step.ASSUMPTIONS


def test_conflict_is_asked_and_pick_keeps_document_source(Agent, tmp_path, dept_docs):
    agent, s = start_many(Agent, tmp_path, _conflict_docs(dept_docs))
    s = past_analyze(agent, s)
    q = s.question
    f = {x.name: x for x in q.fields}
    assert q.step == Step.MISSING and f["cell:acquisition"].type == "select"
    opts = {o.value: o.label for o in f["cell:acquisition"].options}
    assert "1009000000" in opts and "1100000000" in opts and "manual" in opts
    s = agent.answer(s.id, Answer(question_id=q.id, values={"cell:acquisition": "1009000000"}))
    a = s.sheet.cells["acquisition"]
    assert a.status == "filled" and a.value == 1_009_000_000 and a.sources[0].doc.startswith("01")


def test_manual_value_on_conflict_is_flagged_against_sources(Agent, tmp_path, dept_docs):  # Review Focus 5
    agent, s = start_many(Agent, tmp_path, _conflict_docs(dept_docs))
    s = past_analyze(agent, s)
    s = agent.answer(s.id, Answer(question_id=s.question.id,
                                  values={"cell:acquisition": "manual", "amount:acquisition": 1_050_000_000}))
    a = s.sheet.cells["acquisition"]
    assert a.value == 1_050_000_000 and a.sources[0].doc == "담당자 입력" and a.grade == "담당자 입력"
    assert "1,009,000,000원" in a.note and "1,100,000,000원" in a.note and "다름" in a.note


def test_bad_manual_input_keeps_card(Agent, tmp_path, dept_docs):
    from app.agent.orchestrator import AgentError
    agent, s = start_many(Agent, tmp_path, _conflict_docs(dept_docs))
    s = past_analyze(agent, s)
    with pytest.raises(AgentError):
        agent.answer(s.id, Answer(question_id=s.question.id, values={"cell:acquisition": "manual", "amount:acquisition": "x"}))


def test_api_accepts_multiple_files(tmp_path, dept_docs):
    from app.config import Settings
    from app.main import create_app
    from tests.conftest import DEPTS
    settings = Settings(openai_api_key=None, openai_model="x", openai_base_url=None, llm_mask=True, mapper="rules",
                        spring_url="http://be", template_dir=str(tmp_path), max_upload_bytes=5 * 1024 * 1024,
                        agent_engine="workflow", ocr_engine="off")
    client = TestClient(create_app(settings, backend=fake_backend()[0]))
    files = [("files", (p.name, p.read_bytes(), "application/pdf")) for p in sorted(DEPTS.glob("*.pdf"))]
    r = client.post("/v1/agent/sessions", files=files, data={"terms": "3,5"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["docs"]) == 8 and body["sheet"]["cells"]["acquisition"]["value"] == 1_009_000_000


def test_bill_only_warnings_are_not_shown_when_sheet_covers_them(Agent, tmp_path, dept_docs):
    agent, s = start_many(Agent, tmp_path, list(dept_docs.values()))
    s = past_analyze(agent, s)
    analyze = next(b for b in s.board if b.key == Step.ANALYZE)
    assert not any("운영·유지보수" in d for d in analyze.details)  # 운영비는 관리회계팀 문서로 채워짐
    assert next(b for b in s.board if b.key == Step.MISSING).title == "기준표 확인"
