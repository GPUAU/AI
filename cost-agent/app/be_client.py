"""확정된 원가 항목으로 GPUAU/BE의 원가 계산 API(POST /api/v1/cost-simulations)를 부른다.

계산식은 BE에만 있다. 에이전트는 입력을 만들어 넘기고 결과를 받는다.
"""

from __future__ import annotations

import httpx

from .models import CostItemDraft, Currency, ItemStatus, ItemType, Period, SimulationInput


class BackendError(RuntimeError):
    def __init__(self, message: str, errors: list | None = None, status: int | None = None):
        super().__init__(message)
        self.errors = errors or []
        self.status = status


def build_request(items: list[CostItemDraft], inp: SimulationInput) -> dict:
    """BE CostSimulationRequestDto 형식. 모르는 필드를 넣으면 BE가 거부하므로 정해진 필드만 쓴다."""
    out: list[dict] = []
    has_acq = False
    for it in items:
        if it.item_type == ItemType.FINANCING:
            continue
        d: dict = {"itemType": it.item_type.value, "status": it.status.value}
        if it.status in (ItemStatus.USER_INPUT, ItemStatus.PUBLIC_SOURCE):
            amount = it.amount or 0
            currency = it.currency
            if it.item_type == ItemType.ACQUISITION and inp.acquisition_amount is not None:
                # 청구서의 1회성 비용 + 담당자가 입력한 취득비 (통화가 다르면 담당자 입력 통화 기준으로 바꾼다)
                amount = _to(amount, currency, inp.acquisition_currency, inp.fx_krw_per_usd) + inp.acquisition_amount
                currency = inp.acquisition_currency
                has_acq = True
            d.update({"amount": round(amount, 8), "currency": currency.value if currency else None,
                      "period": (it.period or Period.YEAR).value})
        if it.status == ItemStatus.INCLUDED_IN_OTHER and it.included_in:
            d["includedInItemType"] = it.included_in.value
        out.append(d)
    if inp.acquisition_amount is not None and not has_acq:
        out.append({"itemType": ItemType.ACQUISITION.value, "status": ItemStatus.USER_INPUT.value,
                    "amount": inp.acquisition_amount, "currency": inp.acquisition_currency.value,
                    "period": Period.ONE_TIME.value})
    interest = inp.interest_rate_type != "NOT_APPLIED"
    if interest:  # 금융비용은 금액이 아니라 금리로 받는다 (BE가 취득비에 이자를 얹어 계산)
        out.append({"itemType": ItemType.FINANCING.value, "status": ItemStatus.USER_INPUT.value})
    req: dict = {
        "modelId": inp.model_id,
        "items": out,
        "assumptions": {
            "gpuCount": inp.gpu_count,
            "depreciationYears": inp.depreciation_years,
            "utilization": inp.utilization,
            "interestRateType": inp.interest_rate_type,
        },
        "pricing": {"method": inp.pricing_method, "targetRate": inp.target_rate},
    }
    if interest:
        req["assumptions"]["annualInterestRate"] = inp.annual_interest_rate
    if inp.fx_krw_per_usd:
        req["inputFxKrwPerUsd"] = inp.fx_krw_per_usd
    return req


def _to(amount: float, src: Currency | None, dst: Currency, fx: float | None) -> float:
    if src is None or src == dst:
        return amount
    if not fx:
        raise BackendError("원화·달러가 섞여 있어 환율(fx_krw_per_usd)이 필요합니다.")
    return amount / fx if src == Currency.KRW else amount * fx


class BackendClient:
    def __init__(self, base_url: str, timeout: float = 15.0, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def get(self, path: str, params: dict | None = None) -> dict:
        try:
            resp = self._client.get(path, params=params)
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BackendError(f"BE 조회 실패({path}): {type(exc).__name__}") from exc
        if resp.status_code >= 400 or not body.get("isSuccess"):
            raise BackendError(body.get("message") or f"BE 조회 실패({path})", body.get("errors"), resp.status_code)
        return body["result"]

    def models(self) -> list[dict]:
        return self.get("/api/v1/catalog").get("models", [])

    def offers(self, model_id: str, contract_type: str = "ON_DEMAND") -> list[dict]:
        return self.get("/api/v1/market/offers", {"modelId": model_id, "contractType": contract_type}).get("items", [])

    def latest_fx(self) -> dict | None:
        r = self.get("/api/v1/exchange-rates/latest")
        return r if r.get("status") == "AVAILABLE" and r.get("rate") else None

    def latest_interest(self) -> dict | None:
        """한국은행 ECOS 회사채(3년, AA-) 최신 공시 금리 (BE). 없으면 None."""
        r = self.get("/api/v1/interest-rates/latest")
        return r if r.get("status") == "AVAILABLE" and r.get("annualRatePercent") else None

    def simulate(self, request: dict) -> dict:
        try:
            resp = self._client.post("/api/v1/cost-simulations", json=request)
        except httpx.HTTPError as exc:
            raise BackendError(f"원가 계산 서버에 연결하지 못했습니다: {exc}") from exc
        try:
            body = resp.json()
        except ValueError as exc:
            raise BackendError("원가 계산 서버 응답을 읽지 못했습니다.", status=resp.status_code) from exc
        if resp.status_code >= 400 or not body.get("isSuccess"):
            raise BackendError(body.get("message") or "원가 계산 실패", body.get("errors"), resp.status_code)
        return body["result"]
