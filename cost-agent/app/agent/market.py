"""공개 가격으로 제안가의 시장 위치를 계산한다. (숫자 계산은 코드만 한다)"""

from __future__ import annotations

from statistics import median

from .models import MarketView


def offer_prices(offers: list[dict]) -> list[dict]:
    """BE /market/offers 항목 → USD/GPU·시간 표본. 정상(OK) 환산값만 쓴다."""
    out = []
    for o in offers:
        obs = o.get("lastSuccessfulObservation") or {}
        eff = obs.get("effective") or obs.get("nominal") or {}
        if eff.get("status") != "OK" or eff.get("amount") in (None, ""):
            continue
        try:
            price = float(eff["amount"])
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        prod = o.get("product") or {}
        out.append({
            "provider": o.get("providerName"), "region": prod.get("regionCode"), "gpu_count": prod.get("gpuCount"),
            "price": price, "collected_at": obs.get("collectedAt"),
        })
    return out


def market_view(model_name: str, samples: list[dict], candidate: float | None) -> MarketView:
    """사업자마다 지역·GPU 수만 다른 같은 가격이 여러 개 있으므로, 사업자별 대표값(중앙값)으로 통계를 낸다."""
    by_provider: dict[str, list[float]] = {}
    for x in samples:
        by_provider.setdefault(x["provider"] or "?", []).append(x["price"])
    reps = sorted(median(v) for v in by_provider.values())
    view = MarketView(model_name=model_name, count=len(samples), providers=len(by_provider), candidate=candidate,
                      samples=sorted(samples, key=lambda s: s["price"])[:30])
    if not reps:
        view.position = "비교할 공개 가격이 없습니다"
        return view
    view.min, view.median, view.max = reps[0], median(reps), reps[-1]
    if candidate is not None:
        view.percentile = round(sum(1 for p in reps if p < candidate) / len(reps), 3)
        if candidate < view.min:
            view.position = "시장 최저가보다 낮음"
        elif candidate > view.max:
            view.position = "시장 최고가보다 높음"
        else:
            gap = round((candidate - view.median) / view.median, 2)
            view.position = f"사업자 중앙값 대비 {gap:+.0%}" if gap else "사업자 중앙값 수준"
            view.position += f" · 사업자 {len(reps)}곳 중 {sum(1 for p in reps if p < candidate)}곳보다 비쌈"
    return view


def margin_for_price(hourly_cost: float, target_price: float, method: str) -> float | None:
    """원가에서 목표 가격이 되려면 필요한 마진. REVENUE_MARGIN: 1 − 원가/가격, COST_MARKUP: 가격/원가 − 1."""
    if hourly_cost <= 0 or target_price <= 0:
        return None
    if method == "REVENUE_MARGIN":
        return 1 - hourly_cost / target_price
    return target_price / hourly_cost - 1
