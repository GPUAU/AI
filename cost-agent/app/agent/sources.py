"""가정값 출처 등급 — 보고서와 승인 판정이 '어떤 숫자를 얼마나 믿을 수 있는지' 보여주는 데 쓴다. (설계 §4B-2)"""

from __future__ import annotations

import re
from typing import Literal

Grade = Literal["공개자료", "리포트 가정", "예시값", "담당자 입력", "사내 문서"]

_PUBLIC = re.compile(r"수출입은행|한전|한국전력")
_REPORT = re.compile(r"보고서|리포트|표\s*\d+|미래에셋|현대차|하나증권")


def grade_of(name: str, source_text: str) -> Grade:
    t = source_text or ""
    if t == "담당자 입력":
        return "담당자 입력"
    if "예시값" in t:
        return "예시값"
    if "사내 문서" in t:  # 부서 문서(기준표)에서 온 값
        return "사내 문서"
    if _PUBLIC.search(t):
        return "공개자료"
    if _REPORT.search(t):
        return "리포트 가정"
    return "예시값"
