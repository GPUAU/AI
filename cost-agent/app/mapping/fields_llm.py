"""가린 사본으로 기준표 칸의 위치만 묻는 LLM 매핑.

LLM은 '이 칸의 값은 어느 줄 몇 번째 칸'만 답한다. 값은 코드가 원본 칸에서 읽는다 (docs.extract_fields).
보내는 내용: 칸 목록(키·뜻)과 가린 줄 (숫자 → #NUM/#, 회사·견적번호·사업장 → 표시어). 파일명은 보내지 않는다.
"""

from __future__ import annotations

import json

from ..costsheet import SPEC
from ..models import ExtractedDocument, SheetRow
from ..privacy import mask_cells, sensitive_terms
from .llm import Completion

SYSTEM_PROMPT = """너는 사내 부서 문서(견적서·명세서)에서 원가 기준표 칸의 '위치'를 찾는 도우미다.
입력: 찾을 칸 목록(key, 뜻)과 문서 줄 목록(id, cells, number_cols). 숫자는 가려져 있다("#NUM", "#"). 가려진 값을 추측하지 않는다.
각 칸마다 그 값이 들어 있는 줄 id와 칸 번호(0부터)를 답한다. 계산 근거(예: '#장 × #일')가 아니라 최종 제출값 칸을 고른다.
문서에 없는 칸은 답하지 않는다. 숫자를 만들거나 계산하지 않는다. JSON으로만 답한다."""

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fields"],
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "ref", "col"],
                "properties": {"key": {"type": "string"}, "ref": {"type": "string"}, "col": {"type": "integer"}},
            },
        }
    },
}


def build_prompt(doc: ExtractedDocument, rows: list[SheetRow], keys: list[str]) -> str:
    terms = sensitive_terms()
    fields = [{"key": k, "뜻": SPEC[k].label} for k in keys if k in SPEC]
    lines = [json.dumps({"id": r.ref, "cells": mask_cells(r.cells, set(r.numbers), terms),
                         "number_cols": sorted(r.numbers)}, ensure_ascii=False) for r in rows]
    return ("찾을 칸:\n" + json.dumps(fields, ensure_ascii=False) + f"\n쪽: {', '.join(doc.sheets)}"
            + "\n줄 목록(JSON Lines):\n" + "\n".join(lines))


def map_fields_llm(doc: ExtractedDocument, rows: list[SheetRow], keys: list[str],
                   complete: Completion) -> list[tuple[str, str, int]]:
    """(칸 키, 줄 ref, 열 번호). 문서에 없는 줄·요청하지 않은 칸·숫자 없는 칸은 버린다. 호출·형식 오류면 빈 목록."""
    try:
        raw = complete(SYSTEM_PROMPT, build_prompt(doc, rows, keys), RESPONSE_SCHEMA)
        data = json.loads(raw)
        answers = data.get("fields", []) if isinstance(data, dict) else []
    except Exception:  # noqa: BLE001 - LLM이 안 되면 규칙 결과만 쓴다
        return []
    known = {r.ref: r for r in rows}
    out = []
    for a in answers:
        if not isinstance(a, dict):
            continue
        key, ref, col = a.get("key"), a.get("ref"), a.get("col")
        r = known.get(ref)
        if key not in keys or r is None or not isinstance(col, int) or not 0 <= col < len(r.cells):
            continue
        if not any(ch.isdigit() for ch in r.cells[col]):
            continue
        out.append((key, ref, col))
    return out
