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
