"""업계 기준선 검사 — 청구서 해석을 다 통과해도 값이 상식에서 크게 벗어나면 짚는다. (설계 §4A-6)

기준값은 증권사 리포트의 가정이다. 경고 범위가 넓어 환율·지역 차이는 결과를 바꾸지 않는다.
- 현대차증권 인터넷/SW 산업분석 (2026-09-28) 표 16 코로케이션 MW당 손익, 표 18 GPUaaS MW당 손익
- 하나증권 데이터센터 (2026-07-16) 도표 3·4 1GW AI DC 구성
"""

from __future__ import annotations

from .models import BenchFinding, ContractInfo, CostItemDraft, Currency, ItemStatus, ItemType

REPORT_FX = 1450.0  # 원/$ (표 18 E)
RENT_PER_KW_MONTH = (263_000.0, 350_000.0)  # 원/kW/월 (표 16 ③ 26.3만, 표 18 F 35만)
POWER_PER_KW_MONTH = 173_667.0  # 원/계약kW/월 = 730h × PUE 1.3 × 183원 (표 18 G·H)
KW_PER_GPU = 2.0  # GPU당 서버·네트워크 포함 (표 18 A)
COST_PER_GPU_HOUR_USD = 2.51  # 표 18 비용 합 127.7억 ÷ 350.4만 GPU·h ÷ 1,450
OTHER_OPEX_PER_MW_YEAR = 1_800_000_000.0  # 원/MW/년, 인건비·네트워크·판관 (표 18 J)

SRC_HD = "현대차증권 2026-09 표 16·18"

RENT_BAND = (RENT_PER_KW_MONTH[0] * 0.5, RENT_PER_KW_MONTH[1] * 2)
POWER_BAND = (POWER_PER_KW_MONTH * 0.3, POWER_PER_KW_MONTH * 1.5)
KW_PER_GPU_BAND = (1.2, 3.5)
COST_BAND = (COST_PER_GPU_HOUR_USD * 0.4, COST_PER_GPU_HOUR_USD * 2.5)


def _annual(items: list[CostItemDraft], *types: ItemType) -> float:
    return sum(c.amount or 0 for c in items if c.status == ItemStatus.USER_INPUT and c.item_type in types)


def _to_krw(amount: float, currency: Currency | None) -> float:
    return amount * REPORT_FX if currency == Currency.USD else amount


def _out(v: float, band: tuple[float, float]) -> bool:
    return v < band[0] or v > band[1]


def bill_checks(items: list[CostItemDraft], currency: Currency | None, contract: ContractInfo) -> list[BenchFinding]:
    kw = contract.power_kw
    if not kw or currency is None:
        return []
    out = []
    rent = _to_krw(_annual(items, ItemType.RACK_FACILITY, ItemType.RENT), currency) / 12 / kw
    if rent and _out(rent, RENT_BAND):
        out.append(BenchFinding(
            code="RENT_PER_KW", value=round(rent, 2), low=RENT_BAND[0], high=RENT_BAND[1], source=SRC_HD,
            message=f"상면·임대 단가가 계약 kW당 월 {rent:,.0f}원으로 계산돼요. 업계 기준 26.3만~35만원과 크게 달라요. "
                    "금액 단위나 금액 열을 확인해 주세요."))
    power = _to_krw(_annual(items, ItemType.ELECTRICITY), currency) / 12 / kw
    if power and _out(power, POWER_BAND):
        out.append(BenchFinding(
            code="POWER_PER_KW", value=round(power, 2), low=POWER_BAND[0], high=POWER_BAND[1], source=SRC_HD,
            message=f"전기료가 계약 kW당 월 {power:,.0f}원으로 계산돼요. 만실 기준 약 17.4만원(PUE 1.3·183원/kWh)과 크게 달라요."))
    return out


def gpu_checks(contract: ContractInfo, gpu_count: int) -> list[BenchFinding]:
    if not contract.power_kw or not gpu_count:
        return []
    v = contract.power_kw / gpu_count
    if not _out(v, KW_PER_GPU_BAND):
        return []
    return [BenchFinding(
        code="KW_PER_GPU", value=round(v, 3), low=KW_PER_GPU_BAND[0], high=KW_PER_GPU_BAND[1],
        source="현대차증권 표 18 A, 하나증권 2026-07 도표 3·4",
        message=f"계약전력 ÷ GPU 수가 {v:.1f}kW예요. GPU 한 장에 서버·네트워크 포함 약 2kW가 보통이라, "
                "GPU 수나 계약전력을 확인해 주세요.")]


def cost_check(hourly_cost_usd: float) -> list[BenchFinding]:
    if not _out(hourly_cost_usd, COST_BAND):
        return []
    return [BenchFinding(
        code="COST_PER_GPU_HOUR", value=round(hourly_cost_usd, 4), low=COST_BAND[0], high=COST_BAND[1],
        source="현대차증권 표 18",
        message=f"원가가 GPU·시간당 ${hourly_cost_usd:.2f}예요. 리포트 기준 약 $2.5와 크게 달라요. "
                "빠지거나 두 번 들어간 비용이 없는지 확인해 주세요.")]


def missing_suggestion(group: str, currency: Currency | None, contract: ContractInfo) -> float | None:
    """청구서에 없는 핵심 항목의 월 금액 예시값 (계약전력 기준). 그룹 이름은 validation.BILL_CORE_ITEMS의 키."""
    if not contract.power_kw:
        return None
    kw = contract.power_kw
    monthly = {
        "전력": POWER_PER_KW_MONTH * kw,
        "공간·상면": RENT_PER_KW_MONTH[0] * kw,
        "네트워크": OTHER_OPEX_PER_MW_YEAR / 4 / 12 * kw / 1000,
        "운영·유지보수": OTHER_OPEX_PER_MW_YEAR / 4 / 12 * kw / 1000,
    }.get(group)
    if monthly is None:
        return None
    if currency == Currency.USD:
        return float(round(monthly / REPORT_FX / 10) * 10)
    return float(round(monthly / 10_000) * 10_000)


def inhouse_suggestion(items: list[CostItemDraft], currency: Currency | None, contract: ContractInfo) -> float | None:
    """사내 인건비·판관비 예시값 = 표 18 기타 운영비 × MW − 청구서의 네트워크·운영 연간 금액."""
    if not contract.power_kw:
        return None
    usd = currency == Currency.USD
    total = OTHER_OPEX_PER_MW_YEAR * contract.power_kw / 1000 / (REPORT_FX if usd else 1)
    rest = max(0.0, total - _annual(items, ItemType.NETWORK_STORAGE, ItemType.OPERATIONS))
    step = 1_000 if usd else 1_000_000
    return float(round(rest / step) * step)
