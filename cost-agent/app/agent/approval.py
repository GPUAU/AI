"""가격 승인 검토 계산 — 원가 분해, 손익분기, 업계 비교, 민감도, 판정 규칙. (설계 §4 ⑦, §4B-3)

숫자는 BE 응답과 이미 보낸 요청에서만 만든다. 문구는 코드 템플릿이며 LLM에 보내지 않는다.
"""

from __future__ import annotations

from .models import BreakdownRow

GROUPS: dict[str, tuple[str, ...]] = {
    "공간": ("RACK_FACILITY", "RENT"),
    "전력": ("ELECTRICITY",),
    "운영": ("OPERATIONS", "NETWORK_STORAGE", "MEMORY", "OTHER", "GPU"),
}
TOLERANCE = 0.005  # 분해 합계 대조 0.5%


def _usd(item: dict, fx: float | None) -> float | None:
    amount = float(item.get("amount") or 0)
    if item.get("currency") == "KRW":
        return amount / fx if fx else None
    return amount


def breakdown(request: dict, response: dict, fx: float | None) -> tuple[list[BreakdownRow], bool, bool]:
    """(묶음별 행, 합계 대조 통과, 금리 반영). 금리가 반영됐을 때만 '금융비용' 행이 생긴다."""
    fx = fx or request.get("inputFxKrwPerUsd")
    a = request.get("assumptions", {})
    annual = float(response.get("annualCost") or 0)
    hours = float(response.get("billableGpuHoursPerYear") or 0) or 8760 * float(a.get("utilization", 0)) * int(a.get("gpuCount", 0))
    interest = bool(response.get("appliedAssumptions", {}).get("interestWasProvided")) and any(
        i.get("itemType") == "FINANCING" and i.get("status") == "USER_INPUT" for i in request.get("items", []))
    sums: dict[str, float] = {g: 0.0 for g in GROUPS}
    types: dict[str, set[str]] = {g: set() for g in GROUPS}
    acq = 0.0
    ok = True
    for it in request.get("items", []):
        if it.get("status") not in ("USER_INPUT", "PUBLIC_SOURCE") or it.get("itemType") == "FINANCING":
            continue
        usd = _usd(it, fx)
        if usd is None:  # 원화 항목인데 환율이 없으면 나눌 수 없다
            ok = False
            continue
        if it.get("itemType") == "ACQUISITION":
            acq += usd
            continue
        for g, members in GROUPS.items():
            if it.get("itemType") in members:
                sums[g] += usd
                types[g].add(it["itemType"])
    dep = acq / max(int(a.get("depreciationYears") or 1), 1)
    ops = sum(sums.values())
    fin = max(0.0, annual - ops - dep) if interest else 0.0
    if not annual or not hours:
        ok = False
    elif interest:
        ok = ok and annual - ops - dep >= -annual * TOLERANCE
    else:
        ok = ok and abs(ops + dep - annual) <= annual * TOLERANCE
    hourly = float(response.get("hourlyCost") or 0)
    if ok and hourly and abs(annual / hours - hourly) > hourly * TOLERANCE:
        ok = False
    parts = [(g, sorted(types[g]), v) for g, v in sums.items() if v > 0]
    if dep > 0:
        parts.append(("감가상각", ["ACQUISITION"], dep))
    if fin > 0:
        parts.append(("금융비용", ["FINANCING"], fin))
    total = sum(v for _, _, v in parts) or 1.0
    rows = [BreakdownRow(group=g, item_types=t, annual_usd=round(v, 6), hourly_usd=v / hours if hours else 0.0,
                         share=v / total) for g, t, v in parts]
    return rows, ok, interest
