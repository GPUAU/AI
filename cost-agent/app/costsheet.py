"""원가 기준표: 스프링 원가 계산에 쓰이는 칸마다 값·출처·상태를 담는다.

칸은 스프링 원가 항목(ItemType)과 가정값만 다룬다. 유지보수·라이선스·보험처럼 스프링에 없는 요소는 칸이 없다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from .models import CostItemDraft, Currency, ItemStatus, ItemType, Period


class Source(BaseModel):
    doc: str
    page: int | None = None
    row: int = 0
    col: int = 0
    text: str = ""
    conf: float = 1.0


class Cell(BaseModel):
    key: str
    value: float | str | None = None
    unit: str = ""
    currency: str | None = None
    period: str | None = None  # YEAR | ONE_TIME | MONTH(원문)
    status: str = "empty"  # filled | included | excluded | empty | conflict
    sources: list[Source] = Field(default_factory=list)
    candidates: list[dict] = Field(default_factory=list)
    components: list[dict] = Field(default_factory=list)  # {"name","value","sources"} — OPERATIONS 구성 요소
    grade: str = ""
    owner: str = ""
    note: str = ""


class CostSheet(BaseModel):
    cells: dict[str, Cell]
    notices: list[str] = Field(default_factory=list)  # 문서 종류 확인 등 기준표 전체 알림
    checks: dict[str, dict] = Field(default_factory=dict)  # 교차 확인용 값 (기준표 칸 아님): {"value", "source"}


@dataclass(frozen=True)
class FieldSpecDef:
    key: str
    label: str
    kind: str  # cost | assumption | result
    be_item: ItemType | None = None
    period: str | None = None
    owner: str = ""


_FAC = "시설운영팀"
FIELDS: list[FieldSpecDef] = [
    FieldSpecDef("rack", "랙·상면 사용료", "cost", ItemType.RACK_FACILITY, "YEAR", _FAC),
    FieldSpecDef("rent", "공간 임대료", "cost", ItemType.RENT, "YEAR", _FAC),
    FieldSpecDef("electricity", "전력·냉각", "cost", ItemType.ELECTRICITY, "YEAR", _FAC),
    FieldSpecDef("network", "회선·스토리지", "cost", ItemType.NETWORK_STORAGE, "YEAR", _FAC),
    FieldSpecDef("operations", "운영비 (청구서 운영비 + 사내 인건비·판관비)", "cost", ItemType.OPERATIONS, "YEAR", "관리회계팀"),
    FieldSpecDef("gpu_lease", "GPU 임차", "cost", ItemType.GPU, "YEAR", "구매팀"),
    FieldSpecDef("memory", "메모리", "cost", ItemType.MEMORY, "YEAR", "구매팀"),
    FieldSpecDef("other", "기타", "cost", ItemType.OTHER, "YEAR", ""),
    FieldSpecDef("acquisition", "서버·GPU 취득비", "cost", ItemType.ACQUISITION, "ONE_TIME", "구매팀·재무회계팀"),
    FieldSpecDef("gpu_model", "GPU 모델", "assumption", owner="인프라운영팀"),
    FieldSpecDef("gpu_count", "GPU 수", "assumption", owner="인프라운영팀"),
    FieldSpecDef("depreciation_years", "감가상각 연수", "assumption", owner="재무회계팀"),
    FieldSpecDef("utilization", "유료 가동률", "assumption", owner="영업팀"),
    FieldSpecDef("pricing_method", "마진 방식", "assumption", owner="사업기획팀"),
    FieldSpecDef("target_rate", "목표 마진", "assumption", owner="사업기획팀"),
    FieldSpecDef("fx_krw_per_usd", "환율 (원/달러)", "assumption", owner="자금팀"),
    FieldSpecDef("interest_rate_type", "금리 종류", "assumption", owner="자금팀"),
    FieldSpecDef("annual_interest_rate", "연 금리", "assumption", owner="자금팀"),
    FieldSpecDef("discount_3", "3년 약정 할인", "assumption", owner="사업기획팀"),
    FieldSpecDef("discount_5", "5년 약정 할인", "assumption", owner="사업기획팀"),
    # 사업기획팀 검산 결과 (원가 계산에는 쓰지 않고 대조만)
    FieldSpecDef("plan_annual_cost", "검산 연간 원가", "result", owner="사업기획팀"),
    FieldSpecDef("plan_hourly_cost", "검산 시간당 원가", "result", owner="사업기획팀"),
    FieldSpecDef("plan_price", "검산 시간당 판매가", "result", owner="사업기획팀"),
    FieldSpecDef("plan_price_usd", "검산 달러 판매가", "result", owner="사업기획팀"),
]
SPEC = {f.key: f for f in FIELDS}
COST_KEYS = [f.key for f in FIELDS if f.kind == "cost"]
KEY_OF_ITEM = {f.be_item: f.key for f in FIELDS if f.be_item}
# 'included'(다른 항목에 포함) 칸이 어디에 포함되는지: 시설 칸은 랙·상면에, 그 밖은 운영비에
_INCLUDED_IN = {"rent": ItemType.RACK_FACILITY, "electricity": ItemType.RACK_FACILITY, "network": ItemType.RACK_FACILITY}


def empty_sheet() -> CostSheet:
    return CostSheet(cells={f.key: Cell(key=f.key, owner=f.owner, period=f.period) for f in FIELDS})


def add_component(sheet: CostSheet, key: str, name: str, value: float, sources: list[Source] | None = None) -> None:
    """구성 요소를 더한다 (같은 이름이면 바꾼다). 칸 값 = 구성 요소 합."""
    c = sheet.cells[key]
    c.components = [x for x in c.components if x["name"] != name] + [
        {"name": name, "value": float(value), "sources": [s.model_dump() for s in sources or []]}]
    c.value = sum(x["value"] for x in c.components)
    c.unit, c.currency, c.status = "원", c.currency or "KRW", "filled"


def to_cost_items(sheet: CostSheet) -> list[CostItemDraft]:
    """원가 칸 → BE 원가 항목. 충돌이 남은 칸이 있으면 보낼 수 없다."""
    out = []
    for key in COST_KEYS:
        c, spec = sheet.cells[key], SPEC[key]
        if c.status == "conflict":
            raise ValueError(f"'{spec.label}' 칸의 값이 문서마다 달라요. 먼저 골라 주세요.")
        refs = [f"{s.doc}!{s.row}" for s in c.sources]
        if c.status == "filled" and isinstance(c.value, (int, float)):
            period = Period.ONE_TIME if spec.period == "ONE_TIME" else Period.YEAR
            out.append(CostItemDraft(item_type=spec.be_item, status=ItemStatus.USER_INPUT, amount=float(c.value),
                                     currency=Currency(c.currency or "KRW"), period=period, source_refs=refs))
        elif c.status == "included":
            out.append(CostItemDraft(item_type=spec.be_item, status=ItemStatus.INCLUDED_IN_OTHER,
                                     included_in=_INCLUDED_IN.get(key, ItemType.OPERATIONS), source_refs=refs))
        else:  # excluded · empty
            out.append(CostItemDraft(item_type=spec.be_item, status=ItemStatus.NOT_INCLUDED, source_refs=refs))
    return out


_ASSUMPTION_NAMES = {"gpu_model": "model_name"}


def assumption_values(sheet: CostSheet) -> dict[str, Any]:
    """값이 정해진 가정값 칸 → 가정값 카드 미리 채우기 (비율은 0~1)."""
    out: dict[str, Any] = {}
    for f in FIELDS:
        c = sheet.cells.get(f.key)
        if f.kind != "assumption" or not c or c.status != "filled" or c.value is None:
            continue
        v = c.value
        if f.key in ("gpu_count", "depreciation_years") and isinstance(v, float) and v.is_integer():
            v = int(v)
        out[_ASSUMPTION_NAMES.get(f.key, f.key)] = v
    return out
