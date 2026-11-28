"""승인 검토 계산 (설계 §4 ⑦, §4B-3)."""
import copy

import pytest

from app.agent.approval import breakdown

# BE 명세(기준_API_명세서) 응답 예시: 운영 34,000 + 취득 400,000 / 5년, GPU 8장, 가동률 70%
SPEC_REQ = {
    "modelId": "29",
    "items": [
        {"itemType": "ACQUISITION", "status": "USER_INPUT", "amount": 400000.0, "currency": "USD", "period": "ONE_TIME"},
        {"itemType": "OPERATIONS", "status": "USER_INPUT", "amount": 34000.0, "currency": "USD", "period": "YEAR"},
    ],
    "assumptions": {"gpuCount": 8, "depreciationYears": 5, "utilization": 0.7, "interestRateType": "NOT_APPLIED"},
    "pricing": {"method": "REVENUE_MARGIN", "targetRate": 0.2},
}
SPEC_RES = {"hourlyCost": "2.323874755382", "candidatePrice": "2.904843444227", "annualCost": "114000.000000",
            "billableGpuHoursPerYear": "49056.000000",
            "appliedAssumptions": {"interestWasProvided": False, "annualInterestRateUsed": "0"}}


def with_interest(annual_cost, provided):
    req = copy.deepcopy(SPEC_REQ)
    req["items"].append({"itemType": "FINANCING", "status": "USER_INPUT"})
    req["assumptions"].update(interestRateType="ASSUMED", annualInterestRate=0.05)
    res = dict(SPEC_RES, annualCost=str(annual_cost), hourlyCost=str(annual_cost / 49056),
               appliedAssumptions={"interestWasProvided": provided})
    return req, res


def test_breakdown_matches_spec_example():
    rows, ok, interest = breakdown(SPEC_REQ, SPEC_RES, fx=None)
    assert ok and not interest
    assert {r.group: round(r.annual_usd) for r in rows} == {"운영": 34000, "감가상각": 80000}
    assert sum(r.hourly_usd for r in rows) == pytest.approx(2.323874755382, rel=1e-6)
    assert sum(r.share for r in rows) == pytest.approx(1.0)


def test_breakdown_mismatch_flags_not_ok():
    rows, ok, _ = breakdown(SPEC_REQ, dict(SPEC_RES, annualCost="120000", hourlyCost=str(120000 / 49056)), fx=None)
    assert not ok


def test_interest_applied_goes_to_financing():
    rows, ok, interest = breakdown(*with_interest(125000, True), fx=None)
    assert ok and interest
    assert {r.group: round(r.annual_usd) for r in rows}["금융비용"] == 11000


def test_interest_not_applied_by_be_is_reported():  # Review Focus 1
    rows, ok, interest = breakdown(*with_interest(114000, False), fx=None)
    assert ok and not interest and "금융비용" not in {r.group for r in rows}


def test_krw_items_converted_with_fx():
    req = copy.deepcopy(SPEC_REQ)
    req["items"][1] = {"itemType": "ELECTRICITY", "status": "USER_INPUT", "amount": 34000 * 1400.0,
                       "currency": "KRW", "period": "YEAR"}
    rows, ok, _ = breakdown(req, SPEC_RES, fx=1400)
    assert ok and {r.group: round(r.annual_usd) for r in rows}["전력"] == 34000


def test_krw_items_without_fx_is_not_ok():
    req = copy.deepcopy(SPEC_REQ)
    req["items"][1]["currency"] = "KRW"
    assert breakdown(req, SPEC_RES, fx=None)[1] is False


# ---------------------------------------------------------------- 손익분기·업계 비교·민감도
from app.agent.approval import benchmark_shares, breakeven, sensitivity  # noqa: E402
from app.agent.models import BreakdownRow  # noqa: E402
from app.be_client import build_request  # noqa: E402
from app.models import CostItemDraft, Currency, ItemStatus, ItemType, Period, SimulationInput  # noqa: E402


def test_breakeven_formula():
    b = breakeven(0.7, 2.0, {"candidate": 2.5, "3y": 2.125, "bad": 0})
    assert b["candidate"] == pytest.approx(0.56) and b["3y"] == pytest.approx(0.6588, rel=1e-3)
    assert b["bad"] == 9.99


def test_benchmark_ratio_excludes_financing():
    rows = [BreakdownRow(group=g, annual_usd=v, hourly_usd=0, share=0) for g, v in
            [("감가상각", 36), ("공간", 34), ("전력", 16), ("운영", 14), ("금융비용", 50)]]
    out = {x["group"]: x for x in benchmark_shares(rows)}
    assert "금융비용" not in out and all(x["ratio"] == pytest.approx(1.0) for x in out.values())


BASE = SimulationInput(model_id="29", gpu_count=8, acquisition_amount=400000, depreciation_years=5,
                       utilization=0.7, pricing_method="REVENUE_MARGIN", target_rate=0.2)
ITEMS = [CostItemDraft(item_type=ItemType.OPERATIONS, status=ItemStatus.USER_INPUT, amount=34000,
                       currency=Currency.USD, period=Period.YEAR)]


def fake_sim(calls):
    def simulate(req):
        calls.append(req)
        a = req["assumptions"]
        acq = next((i["amount"] for i in req["items"] if i["itemType"] == "ACQUISITION"), 0)
        annual = 34000 + acq / a["depreciationYears"] + acq * (a.get("annualInterestRate") or 0)
        cost = annual / (8760 * a["utilization"] * a["gpuCount"])
        return {"candidatePrice": str(cost / 0.8), "hourlyCost": str(cost)}
    return simulate


def test_sensitivity_sorted_and_isolated():
    calls = []
    base_req = build_request(ITEMS, BASE)
    base_price = float(fake_sim([])(base_req)["candidatePrice"])
    res = sensitivity(BASE, ITEMS, fake_sim(calls), {"utilization": "리포트 가정"}, has_krw=False, base_price=base_price)
    assert len(calls) == 6 and res[0]["key"] == "utilization_down" and res[0]["grade"] == "리포트 가정"
    assert abs(res[0]["price_delta"]) >= abs(res[-1]["price_delta"])
    assert BASE.utilization == 0.7 and build_request(ITEMS, BASE) == base_req  # 원래 입력은 그대로


def test_sensitivity_skips_failed_calls():
    def boom(req):
        raise RuntimeError("BE down")
    assert sensitivity(BASE, ITEMS, boom, {}, has_krw=True, base_price=3.0) == []


# ---------------------------------------------------------------- 발견 사항과 판정
from app.agent.approval import findings, verdict  # noqa: E402
from app.agent.models import AgentSession, Approval, Goal, MarketView, TermPrice  # noqa: E402


def session(cost=2.0, price=2.5, terms=(), market=(6, 2.0, 2.9, 4.0), util=0.7, missing=None):
    s = AgentSession(id="t", goal=Goal(), board=[])
    s.simulation = {"hourlyCost": str(cost), "candidatePrice": str(price)}
    s.assumptions = {"utilization": util, "grades": {}}
    s.terms = [TermPrice(years=y, discount_rate=d, price=price * (1 - d)) for y, d in terms]
    if market:
        n, lo, med, hi = market
        s.market = MarketView(model_name="H100", count=n * 2, providers=n, min=lo, median=med, max=hi, candidate=price)
    s.missing_answers = missing or {}
    return s


def clean_approval(**kw):
    rows = [BreakdownRow(group=g, annual_usd=v, hourly_usd=0, share=0) for g, v in
            [("감가상각", 36), ("공간", 34), ("전력", 16), ("운영", 14)]]
    ap = Approval(breakdown=rows, breakdown_ok=True, interest_applied=True,
                  sensitivity=[{"key": "utilization_down", "grade": "리포트 가정", "price_delta": 0.4}],
                  benchmark=benchmark_shares(rows))
    return ap.model_copy(update=kw)


def codes(s, ap):
    return {f.code for f in findings(s, ap)}


def test_clean_session_is_approval():
    s, ap = session(), clean_approval()
    assert codes(s, ap) == set() and verdict(findings(s, ap))[0] == "승인 권고"


def test_term_below_cost_is_review():  # Review Focus 5
    s = session(terms=[(3, 0.15), (5, 0.25)])  # 5년가 1.875 < 원가 2.0
    fs = findings(s, clean_approval())
    assert "PRICE_BELOW_COST" in {f.code for f in fs} and verdict(fs)[0] == "재검토"


def test_no_market_samples_is_conditional():  # Review Focus 3
    s = session(market=None)
    assert codes(s, clean_approval()) == {"THIN_MARKET"}


def test_review_reason_does_not_upgrade_verdict():
    fs = findings(session(missing={"네트워크": {"choice": "exclude"}}), clean_approval())
    for f in fs:
        f.reason = "알고 진행"
    assert verdict(fs)[0] == "재검토"


@pytest.mark.parametrize("kw, sess, code, level", [
    ({"breakdown_ok": False}, {}, "BREAKDOWN_MISMATCH", "재검토"),
    ({}, {"cost": 3.0, "price": 3.75}, "COST_ABOVE_MARKET", "재검토"),
    ({}, {"price": 2.2}, "THIN_BREAKEVEN", "조건부"),
    ({}, {"price": 4.5, "cost": 2.0}, "MARKET_OUT_KEPT", "조건부"),
    ({"interest_applied": False}, {}, "NO_FINANCING", "조건부"),
    ({"sensitivity": [{"key": "acquisition_up", "grade": "예시값", "price_delta": 1}]}, {}, "EXAMPLE_IN_TOP2", "조건부"),
    ({}, {"market": (2, 2.0, 2.9, 4.0)}, "THIN_MARKET", "조건부"),
])
def test_rule_table(kw, sess, code, level):
    fs = {f.code: f for f in findings(session(**sess), clean_approval(**kw))}
    assert fs[code].level == level


def test_odd_mix_and_bench_warn():
    rows = [BreakdownRow(group="공간", annual_usd=90, hourly_usd=0, share=0),
            BreakdownRow(group="전력", annual_usd=10, hourly_usd=0, share=0)]
    ap = clean_approval(breakdown=rows, benchmark=benchmark_shares(rows))
    s = session()
    s.checks = [__import__("app.models", fromlist=["BenchFinding"]).BenchFinding(
        code="KW_PER_GPU", message="x", value=1, low=1.2, high=3.5, source="s")]
    assert {"ODD_MIX", "BENCH_WARN"} <= codes(s, ap)
