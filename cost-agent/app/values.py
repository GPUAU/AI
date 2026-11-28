"""단위가 붙은 값 읽기 ('32장', '5년', '70%', '1,009,000,000원', '1USD=1,350KRW', 'H100 80GB')와 같은 값 비교.

부서 문서의 제출값은 숫자와 단위가 한 칸에 있다. 값은 늘 원문 글자에서 코드가 읽는다 (LLM은 위치만 알려 줌).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_NUM = r"[-+]?\d[\d,]*(?:\.\d+)?"
_MONEY = {"억원": 1e8, "만원": 1e4, "천원": 1e3, "원": 1.0, "KRW": 1.0}
_UNITS = ["GPU·시간", "GPU 시간", "GPU시간", "kWh", "개월", "시간", "세트", "회선", "장", "대", "년", "%", "USD"]
_FX = re.compile(rf"^(?:{_NUM})?\s*USD\s*=\s*({_NUM})\s*(?:KRW|원)$", re.I)
_MODEL = re.compile(r"^(?:NVIDIA\s*)?[A-Z]{1,3}\d{2,4}[A-Z]?(?:\s*\d+\s*GB)?(?:\s*(?:SXM|PCIe|NVL)\d?)?$", re.I)
_VALUE = re.compile(rf"^(?P<pre>[A-Za-z가-힣·]*\s*)(?P<num>{_NUM})\s*(?P<unit>{'|'.join(map(re.escape, list(_MONEY) + _UNITS))})?$")


@dataclass
class Value:
    number: float
    unit: str
    currency: str | None
    text: str


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_value(text: str) -> Value | None:
    """칸 하나의 글자 → 값. 숫자가 없거나 단위를 모르면 None (모델명은 number=nan, unit='model')."""
    raw = (text or "").strip()
    t = re.sub(r"\s+", " ", raw)
    t = re.sub(r"^약\s*", "", t)
    if not t:
        return None
    if m := _FX.match(t):
        return Value(_num(m.group(1)), "KRW/USD", "KRW", raw)
    if _MODEL.match(t):
        return Value(math.nan, "model", None, raw)
    m = _VALUE.match(t)
    if not m:
        return None
    pre, n, unit = m.group("pre").strip(), _num(m.group("num")), m.group("unit") or ""
    if pre and pre.upper() != "GPU":  # 'GPU 32장'만 허용, 다른 글자가 앞에 붙으면 값이 아님
        return None
    if unit in _MONEY:
        return Value(n * _MONEY[unit], "원", "KRW", raw)
    if unit == "USD":
        return Value(n, "USD", "USD", raw)
    if unit == "%":
        return Value(n / 100, "%", None, raw)
    if unit in ("GPU 시간", "GPU시간"):
        unit = "GPU·시간"
    return Value(n, unit, None, raw)


_IN_TEXT = re.compile(rf"(?:약\s*)?{_NUM}\s*(?:억원|만원|천원|원|KRW|USD|개월|년|%|장|대|kWh)(?![가-힣A-Za-z]*\s*×)")


def find_values(text: str) -> list[Value]:
    """문장 속 단위 붙은 값들 ('공급가액은 1,009,000,000원입니다' → [1,009,000,000원])."""
    out = []
    for m in _IN_TEXT.finditer(text or ""):
        if v := parse_value(m.group(0)):
            out.append(v)
    return out


def _canon(v: Value) -> tuple[float, str]:
    if v.unit == "개월":
        return v.number / 12, "년"
    return v.number, v.unit


def same_value(a: Value, b: Value, tol: float = 0.005) -> bool:
    """단위를 맞춘 뒤 상대 차이 ≤ tol(기본 0.5%)면 같은 값. 모델명은 글자 비교."""
    if a is None or b is None:
        return False
    if a.unit == "model" or b.unit == "model":
        norm = lambda s: re.sub(r"[\s·]", "", s).upper()
        return a.unit == b.unit and norm(a.text) == norm(b.text)
    (x, ux), (y, uy) = _canon(a), _canon(b)
    if ux != uy:
        return False
    if x == y:
        return True
    return abs(x - y) / max(abs(x), abs(y)) <= tol
