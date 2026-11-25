"""외부 LLM으로 보내기 전에 사내 민감정보를 가린다.

원칙: LLM은 '이 줄이 어떤 비용인지'만 판단하면 되므로 금액·단가·수량·계약 조건 숫자가 필요 없다.
- 숫자 칸 → "#NUM" (몇 번째 칸에 숫자가 있는지는 number_cols로 따로 알려 준다)
- 글자 안의 숫자 → "#" (예: "계약전력 40kW × 175원" → "계약전력 #kW × #원")
- 이메일·URL·전화·사업자번호 같은 식별자 → 표시어로 치환
- SENSITIVE_TERMS에 등록한 고객·사업자·프로젝트명 → "[가림]"
- 파일명은 보내지 않는다.
"""

from __future__ import annotations

import os
import re

NUMBER_CELL = "#NUM"
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_DIGITS = re.compile(r"[0-9][0-9,.\-/:]*")


def sensitive_terms() -> list[str]:
    raw = os.getenv("SENSITIVE_TERMS", "")
    return sorted({t.strip() for t in raw.split(",") if t.strip()}, key=len, reverse=True)


def mask_text(text: str, terms: list[str] | None = None) -> str:
    if not text:
        return text
    t = _EMAIL.sub("[이메일]", text)
    t = _URL.sub("[URL]", t)
    for term in terms if terms is not None else sensitive_terms():
        t = re.sub(re.escape(term), "[가림]", t, flags=re.I)
    return _DIGITS.sub("#", t)


def mask_cells(cells: list[str], number_cols: set[int], terms: list[str] | None = None) -> list[str]:
    terms = sensitive_terms() if terms is None else terms
    return [NUMBER_CELL if i in number_cols else mask_text(c, terms) for i, c in enumerate(cells)]
