"""문서별 결과(부서 문서 칸 + 청구서 원가 항목)를 원가 기준표 하나로 합친다.

- 같은 칸 여러 출처: 같은 값(±0.5%)이면 채우고 출처를 모두 남긴다. 다르면 conflict — 고르거나 더하지 않는다.
- 우선순위: 담당자 입력 > 사내 문서(표·청구서) > 공개값. 문장 속 값은 교차 확인에만 쓴다.
- OPERATIONS는 구성 요소(청구서 운영비, 사내 인건비·판관비)를 나눠 담고 합한다.
"""

from __future__ import annotations

import math

from .costsheet import KEY_OF_ITEM, SPEC, Cell, CostSheet, Source, add_component, empty_sheet
from .docs import FieldHit
from .models import BillDraft, ItemStatus, ItemType
from .values import Value, parse_value, same_value

INHOUSE = "사내 인건비·판관비"
BILL_OPS = "청구서 운영비"
FACILITY_KEYS = ("rack", "rent", "electricity", "network")


def _fmt(v: Value) -> str:
    if v.unit in ("model", "method"):
        return v.text
    if v.unit == "%":
        return f"{v.number * 100:g}%"
    n = f"{v.number:,.0f}" if float(v.number).is_integer() else f"{v.number:,.4g}"
    return n + ({"원": "원", "USD": " USD", "KRW/USD": "원/달러"}.get(v.unit, v.unit))


def _bill_hits(doc: str, draft: BillDraft) -> tuple[list[FieldHit], list[str]]:
    """청구서 원가 항목(이미 연간화) → 칸 후보. 포함 표시 항목은 따로."""
    hits, included = [], []
    for it in draft.cost_items:
        key = KEY_OF_ITEM.get(it.item_type)
        if key is None:
            continue
        if it.status == ItemStatus.INCLUDED_IN_OTHER:
            included.append(key)
            continue
        if it.status not in (ItemStatus.USER_INPUT, ItemStatus.PUBLIC_SOURCE) or it.amount is None:
            continue
        cur = it.currency.value if it.currency else None
        ref = it.source_refs[0] if it.source_refs else ""
        sheet, _, row = ref.partition("!")
        page = int(sheet[:-1]) if sheet.endswith("쪽") and sheet[:-1].isdigit() else None
        v = Value(float(it.amount), "원" if cur == "KRW" else "USD", cur, f"{it.amount:,.0f}")
        hits.append(FieldHit(key, v, Source(doc=doc, page=page, row=int(row) if row.isdigit() else 0, text=v.text), "bill"))
    return hits, included


def _set(cell: Cell, group: list[FieldHit], grade: str) -> None:
    first = group[0].value
    if all(same_value(first, h.value) for h in group[1:]):
        cell.value = first.text if first.unit in ("model", "method") else first.number
        if first.unit == "model":
            cell.value = first.text.strip()
        cell.unit, cell.currency, cell.status = first.unit, first.currency, "filled"
        cell.sources = [h.source for h in group]
        docs = sorted({h.source.doc for h in group})
        cell.grade = grade + (" · AI 위치 매핑" if any(h.via == "llm" for h in group) else "")
        if len(docs) > 1:
            cell.grade += f" · 교차 확인 {len(docs)}건"
    else:
        cell.value, cell.status = None, "conflict"
        cell.sources = [h.source for h in group]
        cell.candidates = [{"doc": h.source.doc, "value": None if math.isnan(h.value.number) else h.value.number,
                            "text": _fmt(h.value), "row": h.source.row,
                            "unit": h.value.unit, "currency": h.value.currency} for h in group]


def build_sheet(hits: list[FieldHit], bills: list[tuple[str, BillDraft]],
                public: dict[str, Source] | None = None) -> CostSheet:
    sheet = empty_sheet()
    all_hits = list(hits)
    included: set[str] = set()
    for doc, draft in bills:
        h, inc = _bill_hits(doc, draft)
        all_hits += h
        included |= set(inc)
    prose: dict[str, list[FieldHit]] = {}
    groups: dict[str, list[FieldHit]] = {}
    for h in all_hits:
        if h.key.startswith("check_"):
            sheet.checks.setdefault(h.key, {"value": h.value.number, "unit": h.value.unit, "source": h.source.model_dump(),
                                            "role": h.role})
        elif h.role == "prose":
            prose.setdefault(h.key, []).append(h)
        elif h.key in SPEC:
            groups.setdefault(h.key, []).append(h)

    for key, group in groups.items():
        cell = sheet.cells[key]
        if key == "operations":  # 구성 요소별로 같은 규칙, 구성 요소끼리는 더한다. 한 구성 요소가 충돌이면 칸 전체가 충돌
            parts = {INHOUSE: [h for h in group if h.role != "bill"], BILL_OPS: [h for h in group if h.role == "bill"]}
            conflict = False
            for name, g in parts.items():
                if not g:
                    continue
                tmp = Cell(key=key)
                _set(tmp, g, "사내 문서")
                cell.sources += tmp.sources
                if tmp.status == "conflict":
                    conflict = True
                    cell.candidates += [{**x, "part": name} for x in tmp.candidates]
                else:
                    add_component(sheet, key, name, float(tmp.value), tmp.sources)
            cell.grade = "사내 문서"
            if conflict:  # 충돌이 아닌 구성 요소는 남겨 두고, 고른 값과 다시 더한다 (sheetflow._apply)
                cell.status, cell.value = "conflict", None
                cell.unit, cell.currency = "원", "KRW"
            continue
        _set(cell, group, "사내 문서")

    for key in included:
        if sheet.cells[key].status == "empty":
            sheet.cells[key].status = "included"

    for key, src in (public or {}).items():
        v = parse_value(src.text)
        cell = sheet.cells.get(key)
        if v is None or cell is None:
            continue
        if cell.status == "empty":
            cell.value, cell.unit, cell.status, cell.grade = v.number, v.unit, "filled", "공개값"
            cell.sources = [src]
        elif cell.status == "filled" and isinstance(cell.value, (int, float)) and v.number:
            diff = (cell.value - v.number) / v.number * 100
            cell.note = _join(cell.note, f"{src.doc} {src.text}과 {diff:+.1f}%")

    for key, ps in prose.items():
        cell = sheet.cells.get(key)
        if cell is None:
            continue
        for p in ps:
            if cell.status == "filled" and isinstance(cell.value, (int, float)):
                cur = Value(float(cell.value), cell.unit, cell.currency, "")
                ok = same_value(cur, p.value)
                cell.note = _join(cell.note, f"문장 교차 확인 {'일치' if ok else '불일치'} ({p.source.doc} {_fmt(p.value)})")
            elif cell.status == "empty":
                cell.note = _join(cell.note, f"문장에만 {_fmt(p.value)}이 있어요 ({p.source.doc}) — 표 값으로 확인해 주세요")

    fac = sheet.checks.get("check_facility_annual")
    if fac:
        total = _facility_total(sheet)
        if total is not None:
            ok = same_value(Value(total, "원", "KRW", ""), Value(fac["value"], "원", "KRW", ""))
            for k in FACILITY_KEYS:
                if sheet.cells[k].status == "filled":
                    sheet.cells[k].note = _join(sheet.cells[k].note,
                                                f"시설 문장 연간 시설비 {fac['value']:,.0f}원과 합계 {'일치' if ok else '불일치'}")
    return sheet


def _facility_total(sheet: CostSheet) -> float | None:
    vals = [sheet.cells[k].value for k in FACILITY_KEYS if sheet.cells[k].status == "filled"]
    nums = [float(v) for v in vals if isinstance(v, (int, float))]
    return sum(nums) if nums else None


def _join(a: str, b: str) -> str:
    return f"{a} · {b}" if a else b


def cross_checks(sheet: CostSheet) -> list[str]:
    """파생 관계 확인 결과 문구 (흐름은 막지 않는다)."""
    c, ch, out = sheet.cells, sheet.checks, []

    def num(key):
        cell = c.get(key)
        return float(cell.value) if cell and cell.status == "filled" and isinstance(cell.value, (int, float)) else None

    if (fac := ch.get("check_facility_annual")) and (total := _facility_total(sheet)) is not None:
        ok = same_value(Value(total, "원", "KRW", ""), Value(fac["value"], "원", "KRW", ""))
        out.append(f"연간 시설비: 기준표 시설 칸 합 {total:,.0f}원 · 문장 {fac['value']:,.0f}원 — {'일치' if ok else '불일치'}")
    if (dep := ch.get("check_annual_depreciation")) and (acq := num("acquisition")) and (yrs := num("depreciation_years")):
        ok = same_value(Value(acq / yrs, "원", "KRW", ""), Value(dep["value"], "원", "KRW", ""))
        out.append(f"연간 감가상각비: 취득비 ÷ {yrs:g}년 = {acq / yrs:,.0f}원 · 재무회계 {dep['value']:,.0f}원 — "
                   f"{'일치' if ok else '불일치'}")
    if (paid := ch.get("check_paid_hours")) and (sup := ch.get("check_supply_hours")) and (u := num("utilization")):
        r = paid["value"] / sup["value"] if sup["value"] else 0
        out.append(f"유료 가동률: 유료 시간 ÷ 공급 시간 = {r:.1%} · 기준표 {u:.1%} — {'일치' if abs(r - u) <= 0.005 else '불일치'}")
    if (sup := ch.get("check_supply_hours")) and (n := num("gpu_count")):
        ok = abs(sup["value"] - n * 8760) <= 0.005 * sup["value"]
        out.append(f"공급 가능 시간: GPU {n:g}장 × 8,760시간 = {n * 8760:,.0f} · 문서 {sup['value']:,.0f} — {'일치' if ok else '불일치'}")
    if (g := c.get("gpu_count")) and g.status == "filled" and len({s.doc for s in g.sources}) > 1:
        out.append(f"GPU 수 {g.value:g}장: {'·'.join(sorted({s.doc for s in g.sources}))} 문서 일치")
    return out
