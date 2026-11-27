import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.agent.market import margin_for_price, market_view, offer_prices
from app.agent.models import Answer, Goal, Status, Step
from app.agent.graph import GraphCostAgent
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


@pytest.fixture(params=["workflow", "graph"])
def Agent(request):
    """같은 시나리오를 워크플로와 LangGraph 에이전트(판단자 없음 = 기본 순서) 모두에서 확인한다."""
    return CostAgent if request.param == "workflow" else GraphCostAgent


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


def test_full_flow_with_questions_market_loop_and_report(Agent, tmp_path, doc):
    backend, seen = fake_backend(sim_cost=3.0)            # 마진 60%로 제안가 7.5가 시장 최고가(7.0)를 넘는 상황
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), backend)
    s = agent.start(doc("c"), Goal(model_id="29", gpu_count=32))

    # 1) 분석 끝 → 2) 운영비 누락 질문
    assert s.step == Step.MISSING and s.question.title == "청구서에 없는 큰 비용이 1개 있어요"
    assert "choice:운영·유지보수" in {f.name for f in s.question.fields}
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
    assert s.step == Step.REVIEW and s.question.step == Step.REVIEW        # 8) 승인 검토 카드
    s = agent.answer(s.id, suggested(s.question))
    assert s.step == Step.DONE and s.report_ready
    assert [round(t.price, 2) for t in s.terms] == [3.6, 3.2]
    assert all(b.status != Status.PENDING for b in s.board)
    assert any("마진을 조정" in line for line in s.summary)
    kinds = {e.kind for e in s.events}
    assert {"think", "tool", "ask", "answer", "result"} <= kinds

    from app.agent.report import build_report
    wb = load_workbook(io.BytesIO(build_report(s)))
    assert wb.sheetnames == ["승인 요약", "원가 구성", "민감도", "검토 결과", "요약", "원가 항목", "가정값", "청구서 해석",
                             "시장 비교", "진행 기록"]


def test_remap_loop_fixes_total_with_feedback(Agent, tmp_path, doc):
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
    agent = Agent(BillService(LLMMapper(llm), TemplateStore(tmp_path)), backend)
    s = agent.start(doc("b"), Goal())
    assert len(calls) == 2 and "[이전 분류 검토 결과]" in calls[1] and "Invoice!7: TOTAL" in calls[1]
    assert s.remap_tries == 1 and s.draft.reconciliation.matched is True
    assert "재분류 1회" in s.board[0].summary
    assert s.step == Step.ASSUMPTIONS                     # B는 큰 비용 누락이 없어 바로 가정값으로


def test_unfixable_total_asks_manager(Agent, tmp_path, doc):
    backend, _ = fake_backend()
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), backend)
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
    assert s["step"] == "REVIEW"                          # 승인 검토 카드: 사유를 적고 진행
    reasons = {f["name"]: "확인" for f in s["question"]["fields"] if f["name"].startswith("reason:")}
    s = c.post(f"/v1/agent/sessions/{s['id']}/answer", json={"question_id": s["question"]["id"], "values": reasons}).json()
    assert s["step"] == "DONE"
    r = c.get(f"/v1/agent/sessions/{s['id']}/report.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert c.post(f"/v1/agent/sessions/{s['id']}/answer", json={"question_id": "x"}).status_code == 422


def test_cost_above_market_asks_to_revisit_assumptions(Agent, tmp_path, doc):
    backend, seen = fake_backend(sim_cost=6.5)            # 원가가 중앙값(4.0)보다 높음
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), backend)
    s = agent.start(doc("a"), Goal(model_id="29", gpu_count=8))
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=assumptions(gpu_count=8)))
    assert s.step == Step.MARKET and [o.value for o in s.question.options] == ["assumptions", "keep"]
    s = agent.answer(s.id, Answer(question_id=s.question.id, choice="assumptions"))
    assert s.step == Step.ASSUMPTIONS and {f.name: f.value for f in s.question.fields}["gpu_count"] == 8


def test_backend_failure_reasks_with_previous_values(Agent, tmp_path, doc):
    def down(req):
        if req.url.path == "/api/v1/cost-simulations":
            return httpx.Response(400, json={"isSuccess": False, "message": "요청값을 확인해 주세요.",
                                             "errors": [{"field": "modelId", "reason": "선택한 GPU 모델이 없습니다."}]})
        return httpx.Response(200, json={"isSuccess": True, "result": {"models": [], "items": [], "status": "NONE"}})
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)),
                      BackendClient("http://be", transport=httpx.MockTransport(down)))
    s = agent.start(doc("a"), Goal())
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=assumptions(model_id="999", gpu_count=8)))
    assert s.step == Step.ASSUMPTIONS
    sim = next(b for b in s.board if b.key == Step.SIMULATE)
    assert sim.status == Status.ERROR and any("선택한 GPU 모델이 없습니다" in d for d in sim.details)
    vals = {f.name: f.value for f in s.question.fields}
    assert vals["gpu_count"] == 8 and vals["acquisition_amount"] == 1500000 and vals["utilization"] == 70


def test_unknown_currency_asks_manager(Agent, tmp_path, samples):
    from tests.conftest import make_samples
    from app.parsing import extract
    p = tmp_path / "bill_i.xlsx"
    make_samples.bill_i(p)
    backend, _ = fake_backend()
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path / "t")), backend)
    s = agent.start(extract(p.name, p.read_bytes()), Goal())
    assert s.step == Step.ANALYZE and s.question.title.startswith("청구서 금액의 통화")
    s = agent.answer(s.id, Answer(question_id=s.question.id, choice="currency:KRW"))
    assert s.draft.currency.value == "KRW" and not any(i.code == "NO_CURRENCY" for i in s.draft.issues)
    assert s.step == Step.ASSUMPTIONS        # 누락 없음 (보안관제·UPS 유지보수 → 운영)


def test_rules_recognize_gpu_lease_and_memory(tmp_path):
    from tests.conftest import make_samples
    from app.parsing import extract
    p = tmp_path / "g.csv"
    make_samples.bill_g(p)
    d = BillService(RulesMapper(), TemplateStore(tmp_path / "t")).ingest(extract(p.name, p.read_bytes()))
    kinds = {l.label: l.item_type.value for l in d.lines if l.item_type}
    assert kinds["GPU 서버 임차료 (H200 8장)"] == "GPU" and kinds["추가 메모리 증설"] == "MEMORY"
    assert "NETWORK_STORAGE" in [t.value for t in d.missing_items]


def test_ai_bundle_is_cross_checked_with_rules(tmp_path):
    from tests.conftest import make_samples
    from app.parsing import extract
    p = tmp_path / "h.xlsx"
    make_samples.bill_h(p)

    def llm(system, user, schema):   # AI가 묶음에서 전력만 잡은 상황
        rows = [{"ref": "청구서!3", "role": "COST", "item_type": "RACK_FACILITY", "amount_col": 2, "included_items": ["ELECTRICITY"]},
                {"ref": "청구서!4", "role": "COST", "item_type": "NETWORK_STORAGE", "amount_col": 2, "included_items": []},
                {"ref": "청구서!5", "role": "SUBTOTAL", "item_type": None, "amount_col": 2, "included_items": []},
                {"ref": "청구서!6", "role": "TAX", "item_type": None, "amount_col": 2, "included_items": []},
                {"ref": "청구서!7", "role": "TOTAL", "item_type": None, "amount_col": 2, "included_items": []}]
        for r in rows:
            r.update(period=None, confidence=0.9, reason="t")
        return json.dumps({"vendor": None, "currency": "KRW", "default_period": "MONTH", "rows": rows})
    d = BillService(LLMMapper(llm), TemplateStore(tmp_path / "t")).ingest(extract(p.name, p.read_bytes()))
    bundle = next(l for l in d.lines if l.ref == "청구서!3")
    assert {"ELECTRICITY", "NETWORK_STORAGE", "OPERATIONS"} <= {t.value for t in bundle.included_items}
    assert "규칙 교차 확인" in bundle.reason
    assert "OPERATIONS" not in [t.value for t in d.missing_items]


# ---------------------------------------------------------------- 청구서 해석 확인 카드 (설계 §4A-7)
from tests.conftest import BUILDERS, doc_from_rows  # noqa: E402


def start(Agent, tmp_path, d, gpu_count=256):
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), fake_backend()[0])
    return agent, agent.start(d, Goal(model_id="29", gpu_count=gpu_count))


def board(s, step):
    return next(b for b in s.board if b.key == step)


def suggested(q):
    """제안대로 답한다 (선택지가 있으면 첫 번째, 입력칸은 제안값, 승인 검토는 사유를 적고 진행)."""
    if q.step == Step.REVIEW:
        return Answer(question_id=q.id, values={f.name: "테스트 진행" for f in q.fields if f.name.startswith("reason:")})
    if q.step == Step.ASSUMPTIONS:
        return Answer(question_id=q.id, values=assumptions(gpu_count=256))
    if q.step == Step.ANALYZE and q.options and q.options[0].value.startswith("currency"):
        return Answer(question_id=q.id, choice="currency:KRW")
    if q.step == Step.MISSING:
        return Answer(question_id=q.id, choice="included",
                      values={"parent": next((f.value for f in q.fields if f.name == "parent"), None) or "RACK_FACILITY"})
    return Answer(question_id=q.id, choice=q.options[0].value if q.options else None, values={})


def test_interpret_card_then_flow_continues(Agent, tmp_path, detailed_doc):
    agent, s = start(Agent, tmp_path, detailed_doc("bill_r2_multisheet_thousand_krw.xlsx"))
    assert s.step == Step.ANALYZE and s.question.title.startswith("청구서를 이렇게 읽었어요")
    f = {x.name: x for x in s.question.fields}
    assert f["interp:UNIT_SCALE"].value == "on" and "0.001배" in f["interp:UNIT_SCALE"].hint
    assert [o.value for o in f["interp:UNIT_SCALE"].options] == ["on", "off"]
    s = agent.answer(s.id, Answer(question_id=s.question.id, values={}))
    assert s.interpret_ok and (s.question is None or s.question.step != Step.ANALYZE)


def test_turning_off_interpretation_changes_draft(Agent, tmp_path, detailed_doc):
    agent, s = start(Agent, tmp_path, detailed_doc("bill_r2_multisheet_thousand_krw.xlsx"))
    s = agent.answer(s.id, Answer(question_id=s.question.id, values={"interp:UNIT_SCALE": "off"}))
    assert s.draft.unit_multiplier == 1 and s.interpret_ok


def test_bench_stop_ends_session(Agent, tmp_path):
    bad = doc_from_rows([["청구월", "2026년 10월분", "계약전력", "1,000kW"], ["항목", "금액(원)"],
                         ["상면 사용료", 1000], ["전력 사용료", 150_000_000], ["회선료", 1_000_000],
                         ["운영관리비", 1_000_000], ["합계", 152_001_000]])
    agent, s = start(Agent, tmp_path, bad)  # 상면 kW당 월 1원 → RENT_PER_KW
    assert "bench:RENT_PER_KW" in {f.name for f in s.question.fields}
    s = agent.answer(s.id, Answer(question_id=s.question.id, values={"bench:RENT_PER_KW": "stop"}))
    assert s.step == Step.DONE and board(s, Step.ANALYZE).status == Status.ERROR


@pytest.mark.parametrize("name", list(BUILDERS))
def test_analyze_cards_at_most_three(Agent, tmp_path, detailed_doc, name):
    agent, s = start(Agent, tmp_path, detailed_doc(name))
    analyze_cards = 0
    for _ in range(12):
        if s.question is None:
            break
        analyze_cards += s.question.step == Step.ANALYZE
        s = agent.answer(s.id, suggested(s.question))
    assert 1 <= analyze_cards <= 3
    assert s.interpret_ok


# ---------------------------------------------------------------- 사내 인건비·판관비 + 계산 후 기준선 (설계 §4A-5·6)
def run_to(agent, s, step):
    for _ in range(12):
        if s.question is None or s.question.step == step:
            return s
        s = agent.answer(s.id, suggested(s.question))
    return s


def r1_with_backend(Agent, tmp_path, detailed_doc, sim_cost=2.0):
    backend, seen = fake_backend(sim_cost=sim_cost)
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), backend)
    return agent, agent.start(detailed_doc("bill_r1_kr_metered_krw.xlsx"), Goal(model_id="29", gpu_count=504)), seen


def test_inhouse_field_suggested_from_contract(Agent, tmp_path, detailed_doc):
    agent, s, _ = r1_with_backend(Agent, tmp_path, detailed_doc)
    s = run_to(agent, s, Step.ASSUMPTIONS)
    f = {x.name: x for x in s.question.fields}["inhouse_annual"]
    assert f.value == 1_337_000_000 and f.required is False and "표 18" in f.hint


def test_inhouse_amount_added_to_operations(Agent, tmp_path, detailed_doc):
    agent, s, seen = r1_with_backend(Agent, tmp_path, detailed_doc)
    s = run_to(agent, s, Step.ASSUMPTIONS)
    bill_ops = next(c.amount for c in s.draft.cost_items if c.item_type.value == "OPERATIONS")
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=assumptions(gpu_count=504, inhouse_annual=1e9)))
    ops = next(i for i in seen[-1]["items"] if i["itemType"] == "OPERATIONS")
    assert ops["amount"] == pytest.approx(bill_ops + 1e9)


def test_negative_inhouse_is_rejected(Agent, tmp_path, detailed_doc):
    agent, s, _ = r1_with_backend(Agent, tmp_path, detailed_doc)
    s = run_to(agent, s, Step.ASSUMPTIONS)
    with pytest.raises(AgentError):
        agent.answer(s.id, Answer(question_id=s.question.id, values=assumptions(gpu_count=504, inhouse_annual=-1)))


def test_gpu_check_warns_without_blocking(Agent, tmp_path, detailed_doc):
    agent, s, seen = r1_with_backend(Agent, tmp_path, detailed_doc, sim_cost=0.5)
    s = run_to(agent, s, Step.ASSUMPTIONS)
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=assumptions(gpu_count=64)))
    assert {c.code for c in s.checks} == {"KW_PER_GPU", "COST_PER_GPU_HOUR"}
    assert seen  # 막지 않고 원가 계산까지 진행
    assert any(e.kind == "warn" and "계약전력 ÷ GPU 수" in e.message for e in s.events)


def test_inhouse_in_other_currency_keeps_existing_operations(Agent, tmp_path):
    usd = doc_from_rows([["Invoice period", "Oct 2026", "Currency", "USD"], ["Description", "Amount (USD)"],
                         ["Private Cage Colocation", 18_000], ["Metered Power", 6_000], ["IP Transit 10Gbps", 2_000],
                         ["Total Amount Due", 26_000]])
    backend, seen = fake_backend()
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), backend)
    s = agent.start(usd, Goal(model_id="29", gpu_count=8))
    assert s.step == Step.MISSING                       # 운영비 없음 → 원화로 별도 계약 금액 입력
    s = agent.answer(s.id, Answer(question_id=s.question.id, choice="amount",
                                  values={"amount": 1_000_000, "currency": "KRW", "period": "MONTH"}))
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=assumptions(gpu_count=8, inhouse_annual=5_000)))
    items = {i["itemType"]: i for i in seen[-1]["items"]}
    assert items["OPERATIONS"]["amount"] == 12_000_000 and items["OPERATIONS"]["currency"] == "KRW"
    assert items["OTHER"]["amount"] == 5_000 and items["OTHER"]["currency"] == "USD"


def test_report_has_bill_interpretation_sheet(Agent, tmp_path, detailed_doc):
    from app.agent.report import build_report
    agent, s = start(Agent, tmp_path, detailed_doc("bill_r2_multisheet_thousand_krw.xlsx"))
    for _ in range(12):
        if s.question is None:
            break
        s = agent.answer(s.id, suggested(s.question))
    assert s.step == Step.DONE
    ws = load_workbook(io.BytesIO(build_report(s)))["청구서 해석"]
    rows = [[c.value for c in r] for r in ws.iter_rows(min_row=2)]
    unit = next(r for r in rows if r[1] and "천원" in r[1])
    assert unit[0] == "해석" and unit[2] == "적용" and unit[3] == "0.001배"
    gpu = load_workbook(io.BytesIO(build_report(s)))["가정값"]
    assert any(r[0].value == "사내 인건비·판관비 (연간)" for r in gpu.iter_rows(min_row=2))


def test_report_without_interpretations_says_so(Agent, tmp_path, doc):
    from app.agent.report import build_report
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), fake_backend()[0])
    s = agent.start(doc("a"), Goal(model_id="29", gpu_count=32))
    for _ in range(12):
        if s.question is None:
            break
        s = agent.answer(s.id, suggested(s.question))
    ws = load_workbook(io.BytesIO(build_report(s)))["청구서 해석"]
    assert ws.cell(2, 1).value == "해석 변경 없음"


# ---------------------------------------------------------------- 2순위: 누락 확인 카드 한 장 (설계 §4B-1)
def test_missing_groups_in_one_card(Agent, tmp_path, detailed_doc):  # r4: 네트워크만 빠짐 + 비고 '직접 계약'
    agent, s = start(Agent, tmp_path, detailed_doc("bill_r4_prorated_first_month_krw.xlsx"))
    s = run_to(agent, s, Step.MISSING)
    f = {x.name: x for x in s.question.fields}
    assert s.question.title == "청구서에 없는 큰 비용이 1개 있어요"
    assert f["choice:네트워크"].value == "amount" and "직접 계약" in f["choice:네트워크"].hint
    assert [o.value for o in f["choice:네트워크"].options] == ["amount", "included", "exclude"]
    assert f["amount:네트워크"].value == 12_000_000  # 18억/4/12 × 0.32MW, 만원 반올림
    assert f["amount:네트워크"].show_for == ["amount"] and f["parent:네트워크"].show_for == ["included"]


def test_missing_card_answers_all_groups(Agent, tmp_path, doc):
    backend, seen = fake_backend()
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), backend)
    s = agent.start(doc("c"), Goal(model_id="29", gpu_count=32))
    assert s.step == Step.MISSING and "choice:운영·유지보수" in {f.name for f in s.question.fields}
    s = agent.answer(s.id, Answer(question_id=s.question.id, values={
        "choice:운영·유지보수": "amount", "amount:운영·유지보수": 1_000_000, "currency:운영·유지보수": "KRW",
        "period:운영·유지보수": "MONTH"}))
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=assumptions()))
    ops = next(i for i in seen[0]["items"] if i["itemType"] == "OPERATIONS")
    assert ops["amount"] == 12_000_000.0


def test_missing_card_rejects_bad_amount(Agent, tmp_path, doc):
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), fake_backend()[0])
    s = agent.start(doc("c"), Goal(model_id="29", gpu_count=32))
    with pytest.raises(AgentError):
        agent.answer(s.id, Answer(question_id=s.question.id, values={"choice:운영·유지보수": "amount",
                                                                    "amount:운영·유지보수": "abc"}))
    assert s.question is not None and s.missing_answers == {}


# ---------------------------------------------------------------- 2순위: 가정값 (설계 §4B-2)
def test_acquisition_example_and_grades(Agent, tmp_path, detailed_doc):
    agent, s, seen = r1_with_backend(Agent, tmp_path, detailed_doc)
    s = run_to(agent, s, Step.ASSUMPTIONS)
    f = {x.name: x for x in s.question.fields}
    assert f["acquisition_amount"].value == 504 * 38_000 and "예시값" in f["acquisition_amount"].hint
    assert [o.value for o in f["interest_rate_type"].options] == ["NOT_APPLIED", "ASSUMED", "ACTUAL_LOAN"]
    assert f["annual_interest_rate"].show_for == ["ASSUMED", "ACTUAL_LOAN"]
    vals = {k: v for k, v in assumptions(gpu_count=504).items() if k != "acquisition_amount"}
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=vals))
    assert s.assumptions["grades"]["acquisition_amount"] == "예시값"
    assert s.assumptions["grades"]["fx_krw_per_usd"] == "담당자 입력"  # 테스트 값 1400이 제안값(BE 1400)과 같으면 공개자료


def test_interest_requires_rate(Agent, tmp_path, doc):
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), fake_backend()[0])
    s = agent.start(doc("a"), Goal(model_id="29", gpu_count=32))
    with pytest.raises(AgentError):
        agent.answer(s.id, Answer(question_id=s.question.id, values=assumptions(interest_rate_type="ASSUMED")))


def test_interest_sent_to_backend(Agent, tmp_path, doc):
    backend, seen = fake_backend()
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), backend)
    s = agent.start(doc("a"), Goal(model_id="29", gpu_count=32))
    agent.answer(s.id, Answer(question_id=s.question.id,
                              values=assumptions(interest_rate_type="ASSUMED", annual_interest_rate=5)))
    assert seen[0]["assumptions"]["annualInterestRate"] == 0.05


# ---------------------------------------------------------------- 2순위: 승인 검토 단계 (설계 §4 ⑦)
def real_backend(seen=None, interest_supported=True):
    """요청으로 원가를 실제로 계산하는 가짜 BE (명세의 공식: 연간 총원가 ÷ (8760 × 가동률 × GPU 수))."""
    seen = seen if seen is not None else []

    def handler(req: httpx.Request):
        p = req.url.path
        ok = lambda r: httpx.Response(200, json={"isSuccess": True, "result": r})
        if p != "/api/v1/cost-simulations":
            return fake_backend()[0]._client._transport.handle_request(req)
        b = json.loads(req.content)
        seen.append(b)
        fx = b.get("inputFxKrwPerUsd") or 1400
        a = b["assumptions"]
        usd = lambda i: (i.get("amount") or 0) / (fx if i.get("currency") == "KRW" else 1)
        ops = sum(usd(i) for i in b["items"] if i.get("period") == "YEAR" and i["status"] == "USER_INPUT")
        acq = sum(usd(i) for i in b["items"] if i["itemType"] == "ACQUISITION" and i["status"] == "USER_INPUT")
        rate = a.get("annualInterestRate") or 0
        applied = interest_supported and any(i["itemType"] == "FINANCING" for i in b["items"]) and rate > 0
        annual = ops + acq / a["depreciationYears"] + (acq * rate if applied else 0)
        hours = 8760 * a["utilization"] * a["gpuCount"]
        cost = annual / hours
        t = b["pricing"]["targetRate"]
        cand = cost / (1 - t) if b["pricing"]["method"] == "REVENUE_MARGIN" else cost * (1 + t)
        return ok({"hourlyCost": str(cost), "candidatePrice": str(cand), "annualCost": str(annual),
                   "billableGpuHoursPerYear": str(hours), "missingItems": [] if applied else ["FINANCING"],
                   "appliedAssumptions": {"interestWasProvided": applied}, "costCompleteness": "COMPLETE", "warnings": []})
    return BackendClient("http://be", transport=httpx.MockTransport(handler)), seen


def to_review(Agent, tmp_path, d, **assume):
    backend, seen = real_backend()
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), backend)
    s = agent.start(d, Goal(model_id="29", gpu_count=504))
    for _ in range(12):
        if s.question is None or s.question.step == Step.REVIEW:
            break
        q = s.question
        ans = (Answer(question_id=q.id, values=assumptions(gpu_count=504, **assume)) if q.step == Step.ASSUMPTIONS
               else suggested(q))
        s = agent.answer(s.id, ans)
    return agent, s, seen


def test_review_card_after_terms(Agent, tmp_path, detailed_doc):
    agent, s, _ = to_review(Agent, tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    assert s.question.step == Step.REVIEW and s.question.title.startswith("승인 전에 확인할 점이")
    names = {f.name for f in s.question.fields}
    assert "finding:NO_FINANCING" in names and "reason:NO_FINANCING" in names
    assert s.approval.breakdown_ok and s.approval.verdict in ("조건부", "재검토")
    assert {r.group for r in s.approval.breakdown} >= {"공간", "전력", "운영", "감가상각"}


def test_proceed_needs_reason(Agent, tmp_path, detailed_doc):
    agent, s, _ = to_review(Agent, tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    with pytest.raises(AgentError):
        agent.answer(s.id, Answer(question_id=s.question.id, values={}))
    vals = {f.name: "보고서에 명시" for f in s.question.fields if f.name.startswith("reason:")}
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=vals))
    assert s.step == Step.DONE and all(f.reason for f in s.approval.findings)


def test_fix_reruns_review(Agent, tmp_path, detailed_doc):  # Review Focus 4
    agent, s, _ = to_review(Agent, tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    vals = {f.name: "확인" for f in s.question.fields if f.name.startswith("reason:")}
    vals["finding:NO_FINANCING"] = "fix"
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=vals))
    assert s.question.step == Step.ASSUMPTIONS
    s = agent.answer(s.id, Answer(question_id=s.question.id, values=assumptions(
        gpu_count=504, interest_rate_type="ASSUMED", annual_interest_rate=5)))
    for _ in range(6):
        if s.question is None or s.question.step == Step.REVIEW:
            break
        s = agent.answer(s.id, suggested(s.question))
    assert s.approval.interest_applied and "NO_FINANCING" not in {f.code for f in s.approval.findings}
