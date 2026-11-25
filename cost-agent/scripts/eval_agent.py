"""워크플로(v1)와 LangGraph 에이전트(v2)를 샘플 청구서 9종으로 비교한다.

    python scripts/eval_agent.py            # .env의 OPENAI_API_KEY로 실제 LLM(분류·판단) 사용
    python scripts/eval_agent.py --no-llm   # LLM 없이 (규칙 분류 + 기본 순서)

원가 서버(BE)는 가짜로 대신한다: 판단 흐름을 비교하려는 것이라 원가 숫자는 정해진 값을 쓴다.
질문에는 정해진 답(누락 → 월 300만 원, 가정값 → H200 32장, 시장 이탈 → 첫 선택지, 할인율 → 제안값)을 넣는다.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.graph import GraphCostAgent, openai_controller  # noqa: E402
from app.agent.models import Answer, Goal, Step  # noqa: E402
from app.agent.orchestrator import CostAgent  # noqa: E402
from app.be_client import BackendClient  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import build_service  # noqa: E402
from app.mapping.rules import RulesMapper  # noqa: E402
from app.mapping.templates import TemplateStore  # noqa: E402
from app.parsing import extract  # noqa: E402
from app.service import BillService  # noqa: E402

OFFERS = [7.4, 9.9, 12.66, 18.0, 25.1]
# 샘플마다 원가(USD/GPU·시간)를 정해 시장 비교 분기가 둘 다 나오게 한다
SIM_COST = {"a": 6.3, "f": 6.3}  # 그 외 2.5 → 제안가가 시장 최저가보다 낮음


def fake_backend(sim_cost: float) -> BackendClient:
    def handler(req: httpx.Request):
        ok = lambda r: httpx.Response(200, json={"isSuccess": True, "result": r})
        p = req.url.path
        if p == "/api/v1/catalog":
            return ok({"models": [{"id": "29", "name": "H200"}]})
        if p == "/api/v1/exchange-rates/latest":
            return ok({"status": "AVAILABLE", "rate": "1400", "referenceDate": "2026-11-24"})
        if p == "/api/v1/market/offers":
            return ok({"items": [{"providerName": f"P{i}", "product": {"regionCode": "r", "gpuCount": 8},
                                  "lastSuccessfulObservation": {"collectedAt": "2026-11-24T00:00:00Z",
                                                                "effective": {"amount": str(v), "status": "OK"}}}
                                 for i, v in enumerate(OFFERS)]})
        if p == "/api/v1/cost-simulations":
            b = json.loads(req.content)
            r = b["pricing"]["targetRate"]
            cand = sim_cost / (1 - r) if b["pricing"]["method"] == "REVENUE_MARGIN" else sim_cost * (1 + r)
            return ok({"hourlyCost": str(sim_cost), "candidatePrice": str(cand), "missingItems": ["FINANCING"]})
        return httpx.Response(404, json={"isSuccess": False})
    return BackendClient("http://be", transport=httpx.MockTransport(handler))


def auto_answer(q) -> Answer:
    if q.step == Step.ANALYZE:
        return Answer(question_id=q.id, choice="currency:KRW" if q.options[0].value.startswith("currency") else "proceed")
    if q.step == Step.MISSING:
        return Answer(question_id=q.id, choice="amount", values={"amount": 3000000, "currency": "KRW", "period": "MONTH"})
    if q.step == Step.ASSUMPTIONS:
        return Answer(question_id=q.id, values={"model_id": "29", "gpu_count": 32, "acquisition_amount": 1500000,
                                                "acquisition_currency": "USD", "fx_krw_per_usd": 1400})
    if q.step == Step.MARKET:
        return Answer(question_id=q.id, choice=q.options[0].value)
    return Answer(question_id=q.id, values={})


def run(agent, path: Path) -> dict:
    doc = extract(path.name, path.read_bytes())
    t0 = time.perf_counter()
    s = agent.start(doc, Goal(model_id="29", gpu_count=32))
    asked = []
    while s.question and len(asked) < 12:
        asked.append(f"{s.question.step.value}" + (f":{s.question.options[0].value}" if s.question.step == Step.MARKET else ""))
        s = agent.answer(s.id, auto_answer(s.question))
    return {"asked": asked, "step": s.step.value, "remap": s.remap_tries, "secs": round(time.perf_counter() - t0, 1),
            "cand": round(float(s.simulation["candidatePrice"]), 2) if s.simulation else None,
            "decisions": s.decisions, "refused": sum(1 for e in s.events if e.kind == "warn" and "실행 불가" in e.message),
            "thoughts": [e.message for e in s.events if e.kind == "think" and e.message.startswith("에이전트 판단")]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    settings = Settings.from_env()
    use_llm = not args.no_llm and bool(settings.openai_api_key or settings.openai_base_url)
    tmp = Path("/tmp/cost-agent-eval-templates") if not settings.template_dir else Path(settings.template_dir) / "_eval"
    rows = []
    for path in sorted((ROOT / "samples").iterdir()):
        key = path.name.split("_")[1]
        if args.only and key not in args.only:
            continue
        backend = fake_backend(SIM_COST.get(key, 2.5))
        make = (lambda: build_service(settings)) if use_llm else (lambda: BillService(RulesMapper(), TemplateStore(tmp)))
        calls = []
        ctl = None
        if use_llm:
            base = openai_controller(settings.openai_api_key or "", settings.controller_model or settings.openai_model,
                                     base_url=settings.openai_base_url)
            ctl = lambda m, t, base=base: (calls.append(1), base(m, t))[1]
        v1 = run(CostAgent(make(), backend), path)
        v2 = run(GraphCostAgent(make(), backend, controller=ctl), path)
        rows.append((path.name, v1, v2, len(calls)))
        print(f"· {path.name}: v1 {v1['asked']} → v2 {v2['asked']}", file=sys.stderr)

    print(f"# 워크플로 vs LangGraph 에이전트 ({'LLM 분류·판단' if use_llm else '규칙 + 기본 순서'})\n")
    print("| 샘플 | v1 질문 순서 | v2 질문 순서 | 같은 결론 | v2 판단 (LLM/기본) | 실행 불가 | 판단자 호출 | 시간 v1→v2 |")
    print("|---|---|---|---|---|---|---|---|")
    same_n = 0
    for name, v1, v2, n_calls in rows:
        same = v1["asked"] == v2["asked"] and v1["step"] == v2["step"] and v1["cand"] == v2["cand"]
        same_n += same
        by = [d["by"] for d in v2["decisions"]]
        print(f"| {name} | {' → '.join(v1['asked']) or '-'} | {' → '.join(v2['asked']) or '-'} | {'O' if same else 'X'} | "
              f"{by.count('llm')}/{by.count('policy')} | {v2['refused']} | {n_calls} | {v1['secs']}s → {v2['secs']}s |")
    print(f"\n같은 결론: {same_n}/{len(rows)}\n")
    print("## 에이전트 판단 기록 (예시)\n")
    for name, _, v2, _ in rows[:3]:
        print(f"**{name}**")
        for t in v2["thoughts"]:
            print(f"- {t.removeprefix('에이전트 판단 · ')}")
        print()


if __name__ == "__main__":
    main()
