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
    hours = float(response.get("billableGpuHoursPerYear") or 0) or 8760 * float(a.get("utilization", 0)) * int(a.get("gpuCount", 0))
    # 명세상 필수지만 없으면 시간당 원가 × 연간 GPU 시간으로 본다 (설계 §7)
    annual = float(response.get("annualCost") or 0) or float(response.get("hourlyCost") or 0) * hours
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


# ---------------------------------------------------------------- 손익분기·업계 비교·민감도
# 현대차증권 표 18 GPUaaS MW당 비용 구성 (금융비용 제외)
REF_SHARES = {"감가상각": 0.36, "공간": 0.34, "전력": 0.16, "운영": 0.14}


def breakeven(utilization: float, hourly_cost: float, prices: dict[str, float]) -> dict[str, float]:
    """이 가격으로 팔 때 손익이 0이 되는 가동률 = 가정 가동률 × 원가 ÷ 가격. 가격이 0 이하면 9.99."""
    return {k: (utilization * hourly_cost / p if p > 0 else 9.99) for k, p in prices.items()}


def benchmark_shares(rows: list[BreakdownRow]) -> list[dict]:
    """원가 구성비를 리포트 기준과 비교한다. 금융비용은 기준에 없어 빼고 다시 100%로 나눈다."""
    use = [r for r in rows if r.group in REF_SHARES]
    total = sum(r.annual_usd for r in use) or 1.0
    out = []
    for g, ref in REF_SHARES.items():
        share = sum(r.annual_usd for r in use if r.group == g) / total
        out.append({"group": g, "share": share, "ref_share": ref, "ratio": share / ref})
    return out


def sensitivity(base_input, items, simulate, grades: dict[str, str], has_krw: bool, base_price: float) -> list[dict]:
    """가정 하나만 바꿔 BE를 다시 불러 제안가 변화를 잰다. 원래 입력·세션은 바꾸지 않는다. 실패한 호출은 건너뛴다."""
    from ..be_client import build_request

    u = base_input.utilization
    cases: list[tuple[str, str, str, dict]] = [
        ("utilization_down", f"가동률 {u:.0%} → {max(0.05, u - 0.1):.0%}", "utilization", {"utilization": max(0.05, u - 0.1)}),
        ("utilization_up", f"가동률 {u:.0%} → {min(1.0, u + 0.1):.0%}", "utilization", {"utilization": min(1.0, u + 0.1)}),
    ]
    if base_input.acquisition_amount:
        acq = base_input.acquisition_amount
        cases += [("acquisition_down", "취득비 −10%", "acquisition_amount", {"acquisition_amount": acq * 0.9}),
                  ("acquisition_up", "취득비 +10%", "acquisition_amount", {"acquisition_amount": acq * 1.1})]
    years = 6 if base_input.depreciation_years == 5 else 5
    cases.append(("depreciation_alt", f"감가상각 {base_input.depreciation_years}년 → {years}년", "depreciation_years",
                  {"depreciation_years": years}))
    if base_input.interest_rate_type == "NOT_APPLIED":
        cases.append(("interest_up", "금리 0% → 2% (가정)", "interest_rate_type",
                      {"interest_rate_type": "ASSUMED", "annual_interest_rate": 0.02}))
    else:
        r = base_input.annual_interest_rate or 0
        cases.append(("interest_up", f"금리 {r:.1%} → {r + 0.02:.1%}", "interest_rate_type", {"annual_interest_rate": r + 0.02}))
    if has_krw and base_input.fx_krw_per_usd:
        fx = base_input.fx_krw_per_usd
        cases += [("fx_down", "환율 −5%", "fx_krw_per_usd", {"fx_krw_per_usd": fx * 0.95}),
                  ("fx_up", "환율 +5%", "fx_krw_per_usd", {"fx_krw_per_usd": fx * 1.05})]
    out = []
    for key, label, grade_key, change in cases:
        try:
            res = simulate(build_request(items, base_input.model_copy(update=change)))
            delta = float(res["candidatePrice"]) - base_price
        except Exception:  # noqa: BLE001 - 민감도 하나가 실패해도 검토는 계속한다
            continue
        out.append({"key": key, "label": label, "price_delta": delta, "grade": grades.get(grade_key, "")})
    return sorted(out, key=lambda x: abs(x["price_delta"]), reverse=True)


# ---------------------------------------------------------------- 발견 사항과 판정 (설계 §4 ⑦ 규칙 표)
BREAKEVEN_MARGIN = 0.10  # 가정 가동률과 손익분기 가동률의 최소 여유 (10%p)
MIX_BAND = (0.5, 2.0)
MIN_PROVIDERS = 3


def findings(s, ap) -> list:
    """세션과 검토 계산으로 발견 사항을 만든다. 문구는 화면·보고서용 (판단자 LLM에는 code만 보낸다)."""
    from .models import Finding

    out: list[Finding] = []

    def add(code: str, level: str, message: str, fix: str | None) -> None:
        out.append(Finding(code=code, level=level, message=message, fix_step=fix))

    r = s.simulation or {}
    cost = float(r.get("hourlyCost") or 0)
    cand = float(r.get("candidatePrice") or 0)
    util = float((s.assumptions or {}).get("utilization") or 0)
    prices = {"제안가": cand, **{f"{t.years}년가": t.price for t in s.terms}}
    m = s.market

    excluded = [g for g, rec in s.missing_answers.items() if rec.get("choice") == "exclude"]
    if excluded:
        add("MISSING_EXCLUDED", "재검토", f"{', '.join(excluded)} 비용을 빼고 계산했어요 (불완전 원가)", "MISSING")
    below = [k for k, p in prices.items() if p < cost]
    if below:
        be = util * cost / prices[below[0]] if prices[below[0]] > 0 else 9.99
        add("PRICE_BELOW_COST", "재검토",
            f"{', '.join(below)}: 원가 ${cost:.2f}보다 낮아 가정 가동률 {util:.0%}에서는 손실이에요 (손익분기 가동률 {be:.0%})",
            "TERMS")
    pc = getattr(s, "plan_check", None)
    if pc and not pc.get("ok"):
        add("PLAN_MISMATCH", "조건부", pc["message"], "ASSUMPTIONS")
    examples = [g for g, rec in s.missing_answers.items() if rec.get("choice") == "amount" and rec.get("source") == "예시값"]
    if examples:
        add("MISSING_EXAMPLE", "조건부", f"{', '.join(examples)} 금액이 리포트 예시값 그대로예요 — 실제 계약 금액으로 바꿔야 해요", "MISSING")
    if m and m.count and m.median is not None and cost >= m.median:
        add("COST_ABOVE_MARKET", "재검토",
            f"원가 ${cost:.2f}가 시장 중앙값 ${m.median:.2f} 이상이에요 — 마진 조정으로는 맞출 수 없어요", "ASSUMPTIONS")
    if not ap.breakdown_ok:
        add("BREAKDOWN_MISMATCH", "재검토", "원가 항목 합계가 원가 서버 결과와 맞지 않아요", "ASSUMPTIONS")
    thin = [k for k, p in prices.items() if p >= cost and util - util * cost / p < BREAKEVEN_MARGIN]
    if thin:
        k = thin[0]
        add("THIN_BREAKEVEN", "조건부",
            f"{k} 기준 가동률이 {util * cost / prices[k]:.0%} 아래로 떨어지면 손실이에요 (가정 {util:.0%})", "TERMS")
    if m and m.count and m.min is not None and m.max is not None and (cand < m.min or cand > m.max):
        add("MARKET_OUT_KEPT", "조건부", f"제안가 ${cand:.2f}가 시장 범위 ${m.min:.2f}~${m.max:.2f} 밖이에요", "MARKET")
    if not ap.interest_applied:
        if (s.assumptions or {}).get("interest_rate_type", "NOT_APPLIED") != "NOT_APPLIED":
            # 금리를 넣었는데 BE가 반영하지 않음 — 담당자가 고칠 수 없으므로 사유만 받는다
            add("NO_FINANCING", "조건부", "금리를 입력했지만 원가 서버가 금리를 반영하지 않았어요 — 금융비용(이자)이 원가에 빠져 있어요", None)
        else:
            add("NO_FINANCING", "조건부", "금융비용(이자)이 원가에 반영되지 않았어요", "ASSUMPTIONS")
    top = [x for x in ap.sensitivity[:2] if x.get("grade") == "예시값"]
    if top:
        add("EXAMPLE_IN_TOP2", "조건부", f"결론을 크게 바꾸는 '{top[0]['label'] if 'label' in top[0] else top[0]['key']}'의 가정이 아직 예시값이에요",
            "ASSUMPTIONS")
    if not m or not m.count:
        add("THIN_MARKET", "조건부", "비교할 시장 공개가격이 없어 시장 위치를 확인하지 못했어요", None)
    elif m.providers < MIN_PROVIDERS:
        add("THIN_MARKET", "조건부", f"시장 공개가격이 사업자 {m.providers}곳뿐이에요", None)
    odd = [b for b in ap.benchmark if not MIX_BAND[0] <= b["ratio"] <= MIX_BAND[1]]
    if odd:
        b = odd[0]
        add("ODD_MIX", "조건부", f"원가 중 {b['group']} 비중 {b['share']:.0%}가 업계 기준 {b['ref_share']:.0%}와 크게 달라요",
            "ASSUMPTIONS")
    bench = list(getattr(s.draft, "bench", []) if s.draft else []) + list(s.checks)
    if bench:
        add("BENCH_WARN", "조건부", f"업계 기준선과 크게 다른 값이 {len(bench)}개 있어요", "ANALYZE")
    return out


def verdict(fs: list) -> tuple[str, list[str]]:
    """재검토가 하나라도 있으면 재검토(사유를 적어도), 조건부만 있으면 조건부, 없으면 승인 권고."""
    if any(f.level == "재검토" for f in fs):
        level = "재검토"
    elif fs:
        level = "조건부"
    else:
        level = "승인 권고"
    return level, [f.message for f in fs]
