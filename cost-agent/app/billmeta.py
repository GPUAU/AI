"""청구서 머리글·안내문에서 금액 단위, 청구 기간, 일할 여부, 계약 정보를 읽는다.

가리기 전 원문 글자로 코드가 판단한다. 이 정보는 LLM에 보내지 않는다. (설계 §4A-1)
"""

from __future__ import annotations

import calendar
import re
from datetime import date

from pydantic import BaseModel, Field

from .mapping.rules import monthly_title, title_text
from .mapping.rules import YEAR_WORDS
from .models import ContractInfo, ExtractedDocument, SheetRow

HEAD_ROWS = 40  # 시트마다 앞쪽 몇 줄만 본다 (머리글·안내문)

_UNIT = [
    (re.compile(r"단위\s*[:：]?\s*천\s*원|\(\s*천\s*원\s*\)|in thousands", re.I), 1_000.0, "천원"),
    (re.compile(r"단위\s*[:：]?\s*백만\s*원|\(\s*백만\s*원\s*\)|in millions", re.I), 1_000_000.0, "백만원"),
]
# '단위: 원'이 명시되면 이후 주석의 '(천원)'이 금액 전체 단위를 바꾸지 못한다
_UNIT_WON = re.compile(r"단위\s*[:：]?\s*원(?![가-힣])")
# 계약기간·약정기간 같은 범위는 청구 기간이 아니다
_NOT_BILLING = re.compile(r"계약\s*기간|약정|contract\s*(?:term|period)|\bterm\b", re.I)
_DATE = r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})"
_RANGE = re.compile(_DATE + r"\s*[~–\-]\s*" + _DATE)
_START = re.compile(r"(?:개시일|개통일|start date)\D{0,10}" + _DATE, re.I)
_BILL_MONTH = re.compile(r"(20\d{2})\s*년\s*(\d{1,2})\s*월분")
_QUARTER = re.compile(r"\d\s*분기|quarter|\bQ[1-4]\b", re.I)
_HALF = re.compile(r"반기")

# '계약전력 40kW × 720h'처럼 계산식 안의 값은 계약 정보가 아니다
_POWER = re.compile(r"(?:계약\s*전력|committed\s+power)\s*[:：]?\s*([\d,]+(?:\.\d+)?)\s*kW(?!h)(?!\s*[×x*])", re.I)
_RACKS = [re.compile(r"(\d+)\s*랙"), re.compile(r"cabinets?\s*(\d+)", re.I)]
_GPUS = [re.compile(r"서버\s*(\d+)\s*대\s*\(\s*서버당\s*GPU\s*(\d+)\s*장\s*\)"), re.compile(r"GPU\s*([\d,]+)\s*장")]


class BillMeta(BaseModel):
    unit_multiplier: float = 1.0
    unit_label: str = ""
    unit_ref: str | None = None
    billing_months: float | None = None  # None = 판단 못함 → 기존 detect_period로
    months_text: str = ""
    months_ref: str | None = None
    prorate_days: int | None = None
    month_days: int | None = None
    prorate_ref: str | None = None
    contract: ContractInfo = Field(default_factory=ContractInfo)


def _text(row: SheetRow) -> str:
    return " ".join(c for c in row.cells if c)


def _head_rows(doc: ExtractedDocument) -> list[SheetRow]:
    return [r for r in doc.rows if r.row <= HEAD_ROWS]


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _full_months(a: date, b: date) -> int | None:
    """a가 1일, b가 말일이면 두 날짜 사이 달 수."""
    if a.day != 1 or b.day != calendar.monthrange(b.year, b.month)[1] or b < a:
        return None
    return (b.year - a.year) * 12 + b.month - a.month + 1


def read_meta(doc: ExtractedDocument) -> BillMeta:
    m = BillMeta()
    rows = _head_rows(doc)
    bill_month: tuple[int, int] | None = None
    unit_locked = False

    for r in rows:
        t = _text(r)
        if m.unit_ref is None and not unit_locked and _UNIT_WON.search(t):
            unit_locked = True
        if m.unit_ref is None and not unit_locked:
            for pat, mult, label in _UNIT:
                if pat.search(t):
                    m.unit_multiplier, m.unit_label, m.unit_ref = mult, label, r.ref
                    break
        if bill_month is None and (bm := _BILL_MONTH.search(t)):
            bill_month = (int(bm.group(1)), int(bm.group(2)))
        if m.contract.power_kw is None and (pw := _POWER.search(t)):
            m.contract.power_kw = _num(pw.group(1))
        for cell in r.cells:  # 랙·GPU 수는 한 칸 안의 표현만 본다 ('4 | 랙' 같은 수량 칸과 섞이지 않게)
            if m.contract.racks is None:
                for pat in _RACKS:
                    if rk := pat.search(cell):
                        m.contract.racks = int(rk.group(1))
                        break
            if m.contract.gpus is None:
                if g := _GPUS[0].search(cell):
                    m.contract.gpus = int(g.group(1)) * int(g.group(2))
                elif g := _GPUS[1].search(cell):
                    m.contract.gpus = int(_num(g.group(1)))

    # 청구 기간
    for r in rows:
        t = _text(r)
        if _NOT_BILLING.search(t):
            continue
        if rg := _RANGE.search(t):
            a = date(int(rg.group(1)), int(rg.group(2)), int(rg.group(3)))
            b = date(int(rg.group(4)), int(rg.group(5)), int(rg.group(6)))
            n = _full_months(a, b)
            if n and bill_month and n > 1:
                continue  # 'n월분'이 명시된 청구서에서 여러 달 범위는 청구 기간이 아니다
            if n:
                m.billing_months, m.months_text, m.months_ref = float(n), rg.group(0), r.ref
                bill_month = bill_month or (a.year, a.month)
                break
            if a.year == b.year and a.month == b.month:  # 한 달보다 짧은 기간 → 일할
                m.prorate_days = (b - a).days + 1
                m.month_days = calendar.monthrange(a.year, a.month)[1]
                m.prorate_ref = r.ref
                m.billing_months = 1.0
                break
    if m.billing_months is None and bill_month is None:
        title = title_text(doc)
        for r in rows:
            if r.numbers:  # 항목 줄('3분기 정산 회선')의 표현은 청구 기간이 아니다
                continue
            t = _text(r)
            low = t.lower()
            if _QUARTER.search(t):
                m.billing_months, m.months_text, m.months_ref = 3.0, t, r.ref
            elif _HALF.search(t):
                m.billing_months, m.months_text, m.months_ref = 6.0, t, r.ref
            elif any(w in low for w in YEAR_WORDS):
                m.billing_months, m.months_text, m.months_ref = 12.0, t, r.ref
            elif t == title and monthly_title(t):  # 제목이 '월 시설비 명세'처럼 월 단위 문서
                m.billing_months, m.months_text, m.months_ref = 1.0, t, r.ref
            if m.billing_months:
                break
    if m.billing_months is None and bill_month:
        m.billing_months = 1.0

    # 일할: 이번 청구월 안에서 1일이 아닌 날 개시
    if m.prorate_days is None and bill_month:
        for r in rows:
            if st := _START.search(_text(r)):
                d = date(int(st.group(1)), int(st.group(2)), int(st.group(3)))
                if (d.year, d.month) == bill_month and d.day != 1:
                    last = calendar.monthrange(d.year, d.month)[1]
                    m.prorate_days, m.month_days, m.prorate_ref = last - d.day + 1, last, r.ref
                break
    return m
