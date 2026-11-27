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
