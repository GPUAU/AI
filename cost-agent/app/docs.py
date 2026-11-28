"""부서별 문서: 종류 판별(견적·청구·부서 명세·검산)과 기준표 칸 추출."""

from __future__ import annotations

import re
from enum import Enum

from .models import ExtractedDocument

UNKNOWN_NOTICE = "문서 종류를 확인해 주세요"
HEAD_ROWS = 8


class DocKind(str, Enum):
    QUOTE = "QUOTE"  # 견적서 (취득비)
    BILL = "BILL"  # 청구서·영수증 (기존 청구서 파이프라인)
    MEMO = "MEMO"  # 부서 명세·계획·기준 (가정값·사내 비용)
    PLAN = "PLAN"  # 사업기획 검산안 (대조만)


_RULES: list[tuple[DocKind, re.Pattern]] = [
    (DocKind.QUOTE, re.compile(r"견적|quot(e|ation)", re.I)),
    (DocKind.BILL, re.compile(r"청구|영수증|수납|시설비|이용료|invoice|bill", re.I)),
    (DocKind.PLAN, re.compile(r"검토안.*(판매가|시간당)|(판매가|시간당).*검토안")),
    (DocKind.MEMO, re.compile(r"명세|계획|기준")),
]


def _title(doc: ExtractedDocument) -> str:
    """앞 줄 중 글자 칸이 하나뿐인 첫 줄 (보고서 제목)."""
    for r in doc.rows[:HEAD_ROWS]:
        texts = [c for c in r.cells if c.strip()]
        if len(texts) == 1 and not r.numbers and len(texts[0]) <= 60:
            return texts[0].strip()
    return ""


def classify_doc(doc: ExtractedDocument) -> tuple[DocKind, str]:
    """(종류, 근거 문구). 제목을 먼저 보고, 제목으로 못 정하면 앞 8줄 전체를 본다. 끝내 모르면 청구서로."""
    title = _title(doc)
    head = " ".join(c for r in doc.rows[:HEAD_ROWS] for c in r.cells)
    for where, text in (("제목", title), ("앞부분", head)):
        if not text:
            continue
        for kind, rx in _RULES:
            if m := rx.search(text):
                return kind, f"{where} '{text[:40]}'의 '{m.group(0)}'"
    return DocKind.BILL, f"{UNKNOWN_NOTICE} — 제목·앞부분에서 견적·청구·명세를 찾지 못해 청구서로 읽었어요"


# ---------------------------------------------------------------- 칸 추출 (규칙)
import math  # noqa: E402
from dataclasses import dataclass  # noqa: E402

from .costsheet import Source  # noqa: E402
from .models import SheetRow  # noqa: E402
from .values import Value, find_values, parse_value  # noqa: E402


@dataclass
class FieldHit:
    key: str
    value: Value
    source: Source
    role: str  # table | prose | result
    via: str = "rule"  # rule | llm (가린 사본 위치 매핑)


# 표 줄 항목명 → 칸. 위에서부터 먼저 맞는 것 (긴 이름 먼저). 스프링 칸이 아닌 항목은 목록에 없어 버려진다.
# check_* 는 기준표 칸이 아니라 교차 확인용 (Task 6)
_MEMO_LABELS: list[tuple[str, str]] = [
    (r"GPU\s*모델", "gpu_model"),
    (r"서버\s*구성|GPU\s*수", "gpu_count"),
    (r"연간\s*감가\s*상각비", "check_annual_depreciation"),
    (r"취득비", "acquisition"),
    (r"내용\s*연수|감가\s*연수|상각\s*연수", "depreciation_years"),
    (r"유료\s*가동률|가동률", "utilization"),
    (r"합계\s*유료\s*판매|유료\s*판매\s*예상", "check_paid_hours"),
    (r"공급\s*가능\s*시간", "check_supply_hours"),
    (r"계획\s*환율|환율", "fx_krw_per_usd"),
    (r"대출\s*금리|차입\s*금리|조달\s*금리|연\s*금리|이자율|금리", "annual_interest_rate"),
    (r"마진\s*방식", "pricing_method"),
    (r"목표\s*마진", "target_rate"),
]
_PLAN_LABELS: list[tuple[str, str]] = [
    (r"달러.*판매가", "plan_price_usd"),
    (r"판매가", "plan_price"),
    (r"시간당\s*원가", "plan_hourly_cost"),
    (r"연간\s*원가", "plan_annual_cost"),
]
# 문장 속 값 → 칸 (교차 확인 전용). 단위가 맞는 값만
_PROSE: list[tuple[str, str, tuple[str, ...]]] = [
    (r"연간\s*시설비", "check_facility_annual", ("원",)),
    (r"인건비|판관비", "operations", ("원",)),
    (r"공급가액|취득비", "acquisition", ("원",)),
    (r"가동률", "utilization", ("%",)),
    (r"마진", "target_rate", ("%",)),
    (r"GPU\s*수", "gpu_count", ("장",)),
]
_MODEL_IN = re.compile(r"\b([A-Z]{1,3}\d{2,4}[A-Z]?\s*\d+\s*GB)\b")
_PER_SERVER = re.compile(r"×\s*(\d+)\s*장")


def _page(r: SheetRow) -> int | None:
    m = re.match(r"(\d+)쪽", r.sheet)
    return int(m.group(1)) if m else None


def _src(doc_name: str, r: SheetRow, col: int) -> Source:
    return Source(doc=doc_name, page=_page(r), row=r.row, col=col, text=r.cells[col] if col < len(r.cells) else "")


def _filled(r: SheetRow) -> list[int]:
    return [i for i, c in enumerate(r.cells) if c.strip()]


def _is_prose(r: SheetRow) -> bool:
    f = _filled(r)
    return len(f) == 1 and len(r.cells[f[0]]) > 25


def _method(text: str) -> str | None:
    if re.search(r"매출\s*기준|이익률", text):
        return "REVENUE_MARGIN"
    if re.search(r"원가\s*가산|가산율|마크업", text):
        return "COST_MARKUP"
    return None


def _with_currency(v: Value, doc: ExtractedDocument) -> Value:
    """단위 없는 금액 칸(머리글이 '(원)')은 문서 통화로."""
    if v.unit == "" and doc.currency_hint is not None:
        cur = doc.currency_hint.value
        return Value(v.number, "원" if cur == "KRW" else "USD", cur, v.text)
    return v


def _prose_hits(doc: ExtractedDocument, doc_name: str) -> list[FieldHit]:
    out = []
    for r in doc.rows:
        if not _is_prose(r):
            continue
        col = _filled(r)[0]
        text = r.cells[col]
        for rx, key, units in _PROSE:
            if re.search(rx, text):
                vals = [v for v in find_values(text) if v.unit in units]
                if vals:
                    out.append(FieldHit(key, vals[-1], _src(doc_name, r, col), "prose"))
                break
    return out


def _memo_hits(doc: ExtractedDocument, doc_name: str, plan: bool) -> list[FieldHit]:
    out: list[FieldHit] = []
    labels = (_PLAN_LABELS if plan else []) + _MEMO_LABELS
    mgmt = bool(re.search(r"인건비|판관비", _title(doc)))  # 관리회계 문서: 합계 줄 = 사내 인건비·판관비
    for r in doc.rows:
        f = _filled(r)
        if len(f) < 2 or f[0] != 0:
            continue
        label, last = r.cells[0], f[-1]
        if re.fullmatch(r"항목|품목", label.strip()):
            continue
        key = "operations" if mgmt and re.fullmatch(r"합계", label.strip()) else None
        if key is None:
            key = next((k for rx, k in labels if re.search(rx, label)), None)
        if key is None:
            continue
        text = r.cells[last]
        role = "result" if key.startswith("plan_") else "table"
        if key == "pricing_method":
            if m := _method(text):
                out.append(FieldHit(key, Value(math.nan, "method", None, m), _src(doc_name, r, last), role))
            continue
        v = parse_value(text)
        if v is None:
            continue
        out.append(FieldHit(key, _with_currency(v, doc), _src(doc_name, r, last), role))
        if key == "annual_interest_rate":  # 대출·차입 금리면 실제 대출, 그 밖은 가정 금리
            kind = "ACTUAL_LOAN" if re.search(r"대출|차입", label) else "ASSUMED"
            out.append(FieldHit("interest_rate_type", Value(math.nan, "method", None, kind), _src(doc_name, r, 0), "table"))
        if key == "target_rate":  # '목표 마진 | 매출 기준 이익률 | 20%' → 마진 방식도
            basis = " ".join(r.cells[i] for i in f[1:-1])
            if m := _method(basis):
                out.append(FieldHit("pricing_method", Value(math.nan, "method", None, m), _src(doc_name, r, f[1]), "table"))
    # 유료 가동률 = 유료 판매 시간 ÷ 공급 가능 시간 (영업 문서는 시간만 표로 낸다)
    have = {h.key: h for h in out}
    if "utilization" not in have and "check_paid_hours" in have and "check_supply_hours" in have:
        paid, supply = have["check_paid_hours"], have["check_supply_hours"]
        if supply.value.number:
            out.append(FieldHit("utilization", Value(paid.value.number / supply.value.number, "%", None,
                                                     f"{paid.value.text} ÷ {supply.value.text}"), paid.source, "table"))
    return out


def _quote_hits(doc: ExtractedDocument, doc_name: str) -> list[FieldHit]:
    """견적서: '공급가액 합계'(부가세 제외)가 취득비. 없으면 품목 금액 합. 품목에서 GPU 모델·수 힌트."""
    out: list[FieldHit] = []
    items: list[tuple[SheetRow, int, Value]] = []
    total = None
    seen_vat = False
    for r in doc.rows:
        f = _filled(r)
        if len(f) < 2 or f[0] != 0 or not r.numbers:
            continue
        label, last = r.cells[0], f[-1]
        v = parse_value(r.cells[last])
        if v is None or math.isnan(v.number):
            continue
        v = _with_currency(v, doc)
        if re.search(r"부가세|VAT", label, re.I):
            seen_vat = True
            continue
        if re.search(r"공급가액", label):  # '공급가액'·'공급가액 합계' = 부가세 제외 합계
            total = total or FieldHit("acquisition", v, _src(doc_name, r, last), "table")
            continue
        if re.search(r"합계|총액|소계", label):
            if total is None and not seen_vat:  # 부가세 줄 뒤의 '합계'는 부가세 포함
                total = FieldHit("acquisition", v, _src(doc_name, r, last), "table")
            continue
        items.append((r, last, v))
        if m := _MODEL_IN.search(label):
            out.append(FieldHit("gpu_model", Value(math.nan, "model", None, m.group(1)), _src(doc_name, r, 0), "table"))
            per = _PER_SERVER.search(label)
            qty = next((parse_value(r.cells[i]) for i in f[1:-1] if (q := parse_value(r.cells[i])) and q.unit == "대"), None)
            if per and qty:
                out.append(FieldHit("gpu_count", Value(qty.number * int(per.group(1)), "장", None,
                                                       f"{qty.text} × {per.group(1)}장"), _src(doc_name, r, 0), "table"))
    if total is None and items:
        r, last, _ = items[-1]
        s = sum(v.number for _, _, v in items)
        total = FieldHit("acquisition", Value(s, items[0][2].unit, items[0][2].currency, "품목 합"), _src(doc_name, r, last), "table")
    if total:
        out.append(total)
    return out


def extract_fields(doc: ExtractedDocument, kind: DocKind, doc_name: str, complete=None) -> list[FieldHit]:
    """문서 → 기준표 칸 값들. 값은 늘 원문 칸에서 읽는다. 청구서는 문장 값만 (원가 줄은 청구서 파이프라인)."""
    if kind == DocKind.BILL:
        return _prose_hits(doc, doc_name)
    table = _quote_hits(doc, doc_name) if kind == DocKind.QUOTE else _memo_hits(doc, doc_name, kind == DocKind.PLAN)
    if complete is not None:
        table += _llm_hits(doc, doc_name, table, complete)
    return table + _prose_hits(doc, doc_name)


# LLM이 고른 칸의 단위가 칸에 맞는지 (단위가 다르면 위치를 잘못 고른 것 → 버림)
_UNIT_OK = {
    "cost": lambda v: v.currency is not None,
    "gpu_count": lambda v: v.unit in ("장", "대", ""),
    "depreciation_years": lambda v: v.unit in ("년", "개월"),
    "utilization": lambda v: v.unit == "%", "target_rate": lambda v: v.unit == "%",
    "discount_3": lambda v: v.unit == "%", "discount_5": lambda v: v.unit == "%",
    "annual_interest_rate": lambda v: v.unit == "%",
    "fx_krw_per_usd": lambda v: v.unit in ("KRW/USD", "", "원"),
    "gpu_model": lambda v: v.unit == "model",
}


def _llm_hits(doc: ExtractedDocument, doc_name: str, found: list[FieldHit], complete) -> list[FieldHit]:
    """규칙으로 못 찾은 칸만, 규칙이 쓰지 않은 숫자 줄만 가린 사본으로 묻는다."""
    from .costsheet import FIELDS
    from .mapping.fields_llm import map_fields_llm

    have = {h.key for h in found}
    used = {h.source.row for h in found}
    rows = [r for r in doc.rows if r.numbers and r.row not in used and len(_filled(r)) >= 2]
    keys = [f.key for f in FIELDS if f.kind in ("cost", "assumption") and f.key not in have
            and f.key not in ("pricing_method", "interest_rate_type")]
    if not rows or not keys:
        return []
    by_ref = {r.ref: r for r in rows}
    out = []
    for key, ref, col in map_fields_llm(doc, rows, keys, complete):
        r = by_ref[ref]
        v = parse_value(r.cells[col])
        if v is None:
            continue
        v = _with_currency(v, doc)
        kind = "cost" if key in {f.key for f in FIELDS if f.kind == "cost"} else key
        if not _UNIT_OK.get(kind, lambda _v: True)(v):
            continue
        out.append(FieldHit(key, v, _src(doc_name, r, col), "table", via="llm"))
    return out
