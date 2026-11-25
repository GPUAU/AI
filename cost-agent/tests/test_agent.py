import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.agent.market import margin_for_price, market_view, offer_prices
from app.agent.models import Answer, Goal, Status, Step
from app.agent.orchestrator import AgentError, CostAgent
from app.be_client import BackendClient
from app.config import Settings
from app.main import create_app
from app.mapping.llm import LLMMapper
from app.mapping.rules import RulesMapper
from app.mapping.templates import TemplateStore
from app.service import BillService


def offer(provider, price, region="r1", status="OK"):
    return {"providerName": provider, "product": {"regionCode": region, "gpuCount": 8},
            "lastSuccessfulObservation": {"collectedAt": "2026-11-23T04:00:00Z",
                                          "effective": {"amount": str(price), "status": status}}}


OFFERS = [offer("A", 3.0), offer("B", 3.5), offer("C", 4.0), offer("D", 5.4), offer("E", 7.0), offer("X", 9, status="BAD")]


def fake_backend(sim_cost=2.0, seen=None):
    seen = seen if seen is not None else []

    def handler(req: httpx.Request):
        p = req.url.path
        ok = lambda r: httpx.Response(200, json={"isSuccess": True, "result": r})
        if p == "/api/v1/catalog":
            return ok({"models": [{"id": "29", "name": "H200"}, {"id": "4", "name": "H100"}]})
        if p == "/api/v1/exchange-rates/latest":
            return ok({"status": "AVAILABLE", "rate": "1400", "referenceDate": "2026-11-23"})
        if p == "/api/v1/market/offers":
            return ok({"items": OFFERS})
        if p == "/api/v1/cost-simulations":
            body = json.loads(req.content)
            seen.append(body)
            rate = body["pricing"]["targetRate"]
            cand = sim_cost / (1 - rate) if body["pricing"]["method"] == "REVENUE_MARGIN" else sim_cost * (1 + rate)
            return ok({"hourlyCost": str(sim_cost), "candidatePrice": str(cand), "missingItems": ["FINANCING"],
                       "costCompleteness": "INCOMPLETE", "warnings": []})
        return httpx.Response(404, json={"isSuccess": False})
    return BackendClient("http://be", transport=httpx.MockTransport(handler)), seen


def assumptions(**kw):
    v = {"model_id": "29", "gpu_count": 32, "acquisition_amount": 1500000, "acquisition_currency": "USD",
         "depreciation_years": 5, "utilization": 70, "pricing_method": "REVENUE_MARGIN", "target_rate": 20,
         "fx_krw_per_usd": 1400}
    v.update(kw)
    return v


def test_market_helpers():
    samples = offer_prices(OFFERS)
    assert len(samples) == 5                      # 상태가 OK가 아닌 값은 제외
    v = market_view("H200", samples, 3.8)
    assert (v.min, v.median, v.max) == (3.0, 4.0, 7.0)
    assert v.percentile == 0.4 and "중앙값 대비" in v.position and "5곳 중 2곳" in v.position
    # 한 사업자가 지역만 바꾼 같은 가격을 여러 개 내도 통계는 사업자당 하나
    skewed = offer_prices([offer("V", 7.0, f"r{i}") for i in range(10)] + [offer("N", 5.4), offer("H", 4.0)])
    sv = market_view("H200", skewed, 5.0)
    assert sv.count == 12 and sv.providers == 3 and sv.median == 5.4
    assert market_view("H200", skewed, 5.4).position.startswith("사업자 중앙값 수준")
    assert "최고가보다 높음" in market_view("H200", samples, 8).position
    assert round(margin_for_price(2.0, 4.0, "REVENUE_MARGIN"), 4) == 0.5
    assert round(margin_for_price(2.0, 4.0, "COST_MARKUP"), 4) == 1.0


def test_full_flow_with_questions_market_loop_and_report(tmp_path, doc):
    backend, seen = fake_backend(sim_cost=3.0)            # 마진 60%로 제안가 7.5가 시장 최고가(7.0)를 넘는 상황
    agent = CostAgent(BillService(RulesMapper(), TemplateStore(tmp_path)), backend)
    s = agent.start(doc("c"), Goal(model_id="29", gpu_count=32))

    # 1) 분석 끝 → 2) 운영비 누락 질문
    assert s.step == Step.MISSING and s.question.title.startswith("'운영·유지보수'")
    assert [o.value for o in s.question.options] == ["amount", "included", "exclude"]
    with pytest.raises(AgentError):
        agent.answer(s.id, Answer(question_id=s.question.id, choice="amount", values={"amount": "abc"}))
    assert s.question is not None                        # 잘못된 입력이면 같은 질문 유지
    s = agent.answer(s.id, Answer(question_id=s.question.id, choice="amount",
                                  values={"amount": 1000000, "currency": "KRW", "period": "MONTH"}))

    # 3) 가정값 질문: 근거가 붙은 제안값, BE 환율
    assert s.step == Step.ASSUMPTIONS
    fields = {f.name: f for f in s.question.fields}
    assert fields["model_id"].value == "29" and fields["gpu_count"].value == 32
    assert fields["fx_krw_per_usd"].value == 1400.0 and "수출입은행" in fields["fx_krw_per_usd"].hint
    assert "AI Factory" in fields["depreciation_years"].hint
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=assumptions(target_rate=60)))

    # 4) 원가 계산 → 5) 시장 최고가 초과 → 마진 조정 제안
    ops = next(i for i in seen[0]["items"] if i["itemType"] == "OPERATIONS")
    assert ops == {"itemType": "OPERATIONS", "status": "USER_INPUT", "amount": 12000000.0, "currency": "KRW", "period": "YEAR"}
    assert s.step == Step.MARKET and s.question.options[0].value == "median"
    s = agent.answer(s.id, Answer(question_id=s.question.id, choice="median"))
    assert len(seen) == 2 and seen[1]["pricing"]["targetRate"] == 0.25   # 마진을 60% → 25%로 낮춰 다시 계산
    assert abs(float(s.simulation["candidatePrice"]) - 4.0) < 0.01       # 중앙값에 맞춰짐

    # 6) 기간별 가격 → 7) 근거서
    assert s.step == Step.TERMS
    s = agent.answer(s.id, Answer(question_id=s.question.id, values={"discount_3": 10, "discount_5": 20}))
    assert s.step == Step.DONE and s.report_ready
    assert [round(t.price, 2) for t in s.terms] == [3.6, 3.2]
    assert all(b.status != Status.PENDING for b in s.board)
    assert any("마진을 조정" in line for line in s.summary)
    kinds = {e.kind for e in s.events}
    assert {"think", "tool", "ask", "answer", "result"} <= kinds

    from app.agent.report import build_report
    wb = load_workbook(io.BytesIO(build_report(s)))
    assert wb.sheetnames == ["요약", "원가 항목", "가정값", "시장 비교", "진행 기록"]


def test_remap_loop_fixes_total_with_feedback(tmp_path, doc):
    calls = []

    def llm(system, user, schema):
        calls.append(user)
        cost_role = "TOTAL" if len(calls) == 1 else "COST"     # 첫 시도는 일부러 틀리게
        rows = [
            {"ref": "Invoice!5", "role": "COST", "item_type": "RENT", "amount_col": 3, "included_items": ["ELECTRICITY"]},
            {"ref": "Invoice!6", "role": "COST", "item_type": "NETWORK_STORAGE", "amount_col": 3, "included_items": []},
            {"ref": "Invoice!7", "role": cost_role, "item_type": "NETWORK_STORAGE" if cost_role == "COST" else None, "amount_col": 3, "included_items": []},
            {"ref": "Invoice!8", "role": "COST", "item_type": "OPERATIONS", "amount_col": 3, "included_items": []},
            {"ref": "Invoice!9", "role": "TOTAL", "item_type": None, "amount_col": 3, "included_items": []},
        ]
        for r in rows:
            r.update(period=None, confidence=0.9, reason="t")
        return json.dumps({"vendor": None, "currency": "USD", "default_period": "MONTH", "rows": rows})

    backend, _ = fake_backend()
    agent = CostAgent(BillService(LLMMapper(llm), TemplateStore(tmp_path)), backend)
    s = agent.start(doc("b"), Goal())
    assert len(calls) == 2 and "[이전 분류 검토 결과]" in calls[1] and "Invoice!7: TOTAL" in calls[1]
    assert s.remap_tries == 1 and s.draft.reconciliation.matched is True
    assert "재분류 1회" in s.board[0].summary
    assert s.step == Step.ASSUMPTIONS                     # B는 큰 비용 누락이 없어 바로 가정값으로


def test_unfixable_total_asks_manager(tmp_path, doc):
    backend, _ = fake_backend()
    agent = CostAgent(BillService(RulesMapper(), TemplateStore(tmp_path)), backend)
    d = doc("a")
    d = d.model_copy(update={"rows": [r for r in d.rows if "전력" not in r.label]})   # 줄을 빼서 총액 불일치
    s = agent.start(d, Goal())
    assert s.step == Step.ANALYZE and s.question.options[0].value == "proceed"
    s = agent.answer(s.id, Answer(question_id=s.question.id, choice="stop"))
    assert s.step == Step.DONE and s.board[0].status == Status.ERROR


def test_agent_api(tmp_path, samples):
    backend, _ = fake_backend(sim_cost=2.8)              # 제안가 3.5 → 시장 범위 안
    settings = Settings(openai_api_key=None, openai_model="x", openai_base_url=None, llm_mask=True, mapper="rules",
                        spring_url="http://be", template_dir=str(tmp_path / "t"), max_upload_bytes=1 << 20)
    c = TestClient(create_app(settings, backend=backend))
    with open(samples["a"], "rb") as f:
        s = c.post("/v1/agent/sessions", files={"file": ("a.xlsx", f)}, data={"model_id": "29", "gpu_count": "8"}).json()
    assert s["step"] == "ASSUMPTIONS"                     # A는 누락 없음
    assert c.get(f"/v1/agent/sessions/{s['id']}/report.xlsx").status_code == 409
    s = c.post(f"/v1/agent/sessions/{s['id']}/answer", json={"question_id": s["question"]["id"], "values": assumptions(gpu_count=8)}).json()
    assert s["step"] == "TERMS"                           # 시장 범위 안 → 질문 없이 통과
    s = c.post(f"/v1/agent/sessions/{s['id']}/answer", json={"question_id": s["question"]["id"], "values": {}}).json()
    assert s["step"] == "DONE"
    r = c.get(f"/v1/agent/sessions/{s['id']}/report.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert c.post(f"/v1/agent/sessions/{s['id']}/answer", json={"question_id": "x"}).status_code == 422


def test_cost_above_market_asks_to_revisit_assumptions(tmp_path, doc):
    backend, seen = fake_backend(sim_cost=6.5)            # 원가가 중앙값(4.0)보다 높음
    agent = CostAgent(BillService(RulesMapper(), TemplateStore(tmp_path)), backend)
    s = agent.start(doc("a"), Goal(model_id="29", gpu_count=8))
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=assumptions(gpu_count=8)))
    assert s.step == Step.MARKET and [o.value for o in s.question.options] == ["assumptions", "keep"]
    s = agent.answer(s.id, Answer(question_id=s.question.id, choice="assumptions"))
    assert s.step == Step.ASSUMPTIONS and {f.name: f.value for f in s.question.fields}["gpu_count"] == 8


def test_backend_failure_reasks_with_previous_values(tmp_path, doc):
    def down(req):
        if req.url.path == "/api/v1/cost-simulations":
            return httpx.Response(400, json={"isSuccess": False, "message": "요청값을 확인해 주세요.",
                                             "errors": [{"field": "modelId", "reason": "선택한 GPU 모델이 없습니다."}]})
        return httpx.Response(200, json={"isSuccess": True, "result": {"models": [], "items": [], "status": "NONE"}})
    agent = CostAgent(BillService(RulesMapper(), TemplateStore(tmp_path)),
                      BackendClient("http://be", transport=httpx.MockTransport(down)))
    s = agent.start(doc("a"), Goal())
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=assumptions(model_id="999", gpu_count=8)))
    assert s.step == Step.ASSUMPTIONS
    sim = next(b for b in s.board if b.key == Step.SIMULATE)
    assert sim.status == Status.ERROR and any("선택한 GPU 모델이 없습니다" in d for d in sim.details)
    vals = {f.name: f.value for f in s.question.fields}
    assert vals["gpu_count"] == 8 and vals["acquisition_amount"] == 1500000 and vals["utilization"] == 70
