"""청구서 해석: 매퍼(규칙·LLM·템플릿) 결과를 문서 정보·열 역할로 바로잡는다. (설계 §4A-2~4A-4)

총액 대조로는 잡히지 않는 오류(금액 단위, 부가세 포함 열, 1회성 열, 분기·일할 청구, 비원가 줄, 요약 시트 중복)를
코드가 찾아 '해석'으로 적용한다. 해석은 담당자가 하나씩 해제할 수 있고, 해제하면 처음부터 다시 계산한다.
문구는 코드 템플릿이다 (LLM에 보내지 않음).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .billmeta import BillMeta
from .columns import ColRole, header_text, preferred_col
from .models import DocumentMapping, ExtractedDocument, Interpretation, LineRole, Period, RowMapping


class InterpCode(str, Enum):
    UNIT_SCALE = "UNIT_SCALE"
    GROSS_COLUMN = "GROSS_COLUMN"
    NRC_ONE_TIME = "NRC_ONE_TIME"
    BILLING_MONTHS = "BILLING_MONTHS"
    PRORATED = "PRORATED"
    NON_COST_ROWS = "NON_COST_ROWS"
    SUMMARY_SHEET = "SUMMARY_SHEET"


NON_COST_WORDS = ["미납", "이월", "입금", "납부 완료", "previous balance", "payment received", "balance forward"]
SUMMARY_SHEET_WORDS = ["요약", "summary", "총괄"]
WRONG_AMOUNT_COLS = {ColRole.VAT, ColRole.GROSS, ColRole.QTY, ColRole.RATE, ColRole.BASE}
_AMOUNT_ROLES = (LineRole.COST, LineRole.DISCOUNT)


@dataclass
class Interpreted:
    mapping: DocumentMapping
    multiplier: float = 1.0
    months: float | None = None
    base: dict[str, float] = field(default_factory=dict)  # ref → 월 정액 (배수 적용 전)
    prorate: tuple[int, int] | None = None  # (사용 일수, 그 달 일수)
    found: list[Interpretation] = field(default_factory=list)


def _ignore(m: RowMapping, reason: str) -> RowMapping:
    return m.model_copy(update={"role": LineRole.IGNORE, "item_type": None, "included_items": [], "reason": reason})


def interpret(doc: ExtractedDocument, mapping: DocumentMapping, meta: BillMeta,
              roles: dict[str, dict[int, ColRole]], off: frozenset[str] = frozenset()) -> Interpreted:
    rows = {r.ref: r for r in doc.rows}
    heads = header_text(doc)
    maps = {m.ref: m for m in mapping.rows}
    found: dict[InterpCode, Interpretation] = {}

    def note(code: InterpCode, message: str, refs: list[str]) -> bool:
        found[code] = Interpretation(code=code.value, message=message, applied=code.value not in off, refs=refs)
        return code.value not in off

    # 원가가 아닌 줄 (미납·이월·입금) — 다른 해석보다 먼저
    non_cost = [ref for ref, m in maps.items() if m.role != LineRole.IGNORE and ref in rows
                and any(w in rows[ref].label.lower() for w in NON_COST_WORDS)]
    if non_cost and note(InterpCode.NON_COST_ROWS, f"원가가 아닌 줄 {len(non_cost)}개(전월 미납·이월·입금)는 원가에서 뺐어요",
                         non_cost):
        for ref in non_cost:
            maps[ref] = _ignore(maps[ref], "원가 아님 — 미납·이월·입금")

    # 요약 시트: 다른 시트에 원가 줄이 있을 때만 중복으로 본다
    cost_sheets = {ref.split("!")[0] for ref, m in maps.items() if m.role == LineRole.COST}
    for sheet in doc.sheets:
        if not any(w in sheet.lower() for w in SUMMARY_SHEET_WORDS) or not (cost_sheets - {sheet}):
            continue
        dup = [ref for ref, m in maps.items() if ref.split("!")[0] == sheet and m.role in _AMOUNT_ROLES]
        if dup and note(InterpCode.SUMMARY_SHEET, f"'{sheet}' 시트는 상세 시트를 요약한 것이라 원가에서 뺐어요", dup):
            for ref in dup:
                maps[ref] = _ignore(maps[ref], "요약 시트 — 상세 시트와 중복")

    # 금액 열: 부가세·합계·수량·단가·월 정액 칸을 골랐으면 공급가액(당월 청구·MRC) 칸으로
    moved: list[tuple[str, int, int]] = []
    for ref, m in maps.items():
        row = rows.get(ref)
        if row is None or m.role not in _AMOUNT_ROLES or m.amount_col is None:
            continue
        sheet_roles = roles.get(row.sheet, {})
        if sheet_roles.get(m.amount_col) in WRONG_AMOUNT_COLS:
            col = preferred_col(sheet_roles, row)
            if col is not None and col != m.amount_col:
                moved.append((ref, m.amount_col, col))
    if moved:
        ref0, old, new = moved[0]
        sheet0 = ref0.split("!")[0]
        h = heads.get(sheet0, {})
        if note(InterpCode.GROSS_COLUMN,
                f"'{h.get(old, '')}' 열은 부가세·수량·단가 열이라 '{h.get(new, '')}' 열을 원가로 썼어요",
                [ref for ref, _, _ in moved]):
            for ref, _, col in moved:
                maps[ref] = maps[ref].model_copy(update={"amount_col": col})

    # 1회성(NRC) 열에 금액이 있는 원가 줄 → 1회성
    nrc = [ref for ref, m in maps.items() if m.role == LineRole.COST and m.amount_col is not None and ref in rows
           and roles.get(rows[ref].sheet, {}).get(m.amount_col) == ColRole.NRC and m.period != Period.ONE_TIME]
    if nrc and note(InterpCode.NRC_ONE_TIME, f"1회성(NRC) 열의 {len(nrc)}줄은 초기 투자(취득비)로 옮겼어요", nrc):
        for ref in nrc:
            maps[ref] = maps[ref].model_copy(update={"period": Period.ONE_TIME})

    out = Interpreted(mapping=mapping.model_copy(update={"rows": list(maps.values())}))

    if meta.unit_multiplier != 1 and note(
            InterpCode.UNIT_SCALE,
            f"금액 단위가 {meta.unit_label}이에요 → 모든 금액에 {meta.unit_multiplier:,.0f}배를 적용했어요",
            [meta.unit_ref] if meta.unit_ref else []):
        out.multiplier = meta.unit_multiplier

    default_months = 12.0 if mapping.default_period == Period.YEAR else 1.0
    if meta.billing_months and meta.billing_months != default_months and not meta.prorate_days:
        mo = meta.billing_months
        if note(InterpCode.BILLING_MONTHS, f"{mo:g}개월 청구서라 ×{12 / mo:g}로 연간화했어요 ({meta.months_text[:40]})",
                [meta.months_ref] if meta.months_ref else []):
            out.months = mo

    if meta.prorate_days and meta.month_days:
        days = meta.prorate_days
        if note(InterpCode.PRORATED,
                f"{days}일 사용분(일할)이에요 → 월 정액이 있는 줄은 월 정액 × 12, 나머지는 ÷ {days}일 × 365로 연간화했어요",
                [meta.prorate_ref] if meta.prorate_ref else []):
            out.prorate = (days, meta.month_days)
            for ref, m in maps.items():
                row = rows.get(ref)
                if row is None or m.role != LineRole.COST:
                    continue
                base_col = next((i for i, r in roles.get(row.sheet, {}).items() if r == ColRole.BASE), None)
                if base_col is not None and base_col in row.numbers:
                    out.base[ref] = row.numbers[base_col]

    out.found = [found[c] for c in InterpCode if c in found]
    return out
