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
# 견적·영수증 번호 (SIM-261126-02, SIM-OPS-1026)
_DOC_NO = re.compile(r"\b[A-Z]{2,}(?:-[A-Z]{2,})*-\d{4,}(?:-\d+)*\b")
# 회사명: '…컴퓨팅', '(주)…'/'…(주)', '(가상 업체)'·'(가상)' 앞 단어, '… Inc./Ltd./LLC'
_COMPANY = [
    re.compile(r"[가-힣A-Za-z]+컴퓨팅"),
    re.compile(r"\(주\)\s*[가-힣A-Za-z]+|[가-힣A-Za-z]+\s*\(주\)"),
    re.compile(r"[가-힣A-Za-z]+(?=\s*\(가상(?:\s*업체)?\))"),
    re.compile(r"(?:[A-Z][\w&-]*\s+){0,3}[A-Z][\w&-]*\s*(?:Inc\.|Ltd\.|LLC|Co\.)"),
]


def sensitive_terms() -> list[str]:
    raw = os.getenv("SENSITIVE_TERMS", "")
    return sorted({t.strip() for t in raw.split(",") if t.strip()}, key=len, reverse=True)


def site_names() -> list[str]:
    """사업장명 (설정 SITE_NAMES, 기본 '울산'). LLM에 보낼 때 '[사업장]'으로 가린다."""
    raw = os.getenv("SITE_NAMES", "울산")
    return sorted({t.strip() for t in raw.split(",") if t.strip()}, key=len, reverse=True)


def mask_text(text: str, terms: list[str] | None = None) -> str:
    if not text:
        return text
    t = _EMAIL.sub("[이메일]", text)
    t = _URL.sub("[URL]", t)
    t = _DOC_NO.sub("[번호]", t)
    for rx in _COMPANY:
        t = rx.sub("[회사]", t)
    for site in site_names():
        t = t.replace(site, "[사업장]")
    for term in terms if terms is not None else sensitive_terms():
        t = re.sub(re.escape(term), "[가림]", t, flags=re.I)
    return _DIGITS.sub("#", t)


def mask_cells(cells: list[str], number_cols: set[int], terms: list[str] | None = None) -> list[str]:
    terms = sensitive_terms() if terms is None else terms
    return [NUMBER_CELL if i in number_cols else mask_text(c, terms) for i, c in enumerate(cells)]
