"""LLM 매퍼.

LLM에게는 '각 줄이 어떤 원가 항목인지, 금액이 몇 번째 칸에 있는지'만 묻는다.
금액 숫자는 LLM이 말하지 않고, 코드가 그 칸에서 직접 읽는다. (LLM이 숫자를 지어내거나 틀리게 옮기는 것을 막는다)
"""

from __future__ import annotations

import json
from typing import Callable, Protocol

from pydantic import ValidationError

from ..models import DocumentMapping, ExtractedDocument, ItemType, LineRole, Period
from ..privacy import mask_cells, sensitive_terms

MAX_ROWS_FOR_LLM = 250

ITEM_GUIDE = """\
- ELECTRICITY: 전력·전기료·동력비·kWh 사용료·Power
- RACK_FACILITY: 상면·랙·캐비닛·시설 사용료
- RENT: 공간·층 임대료, 코로케이션(Colocation), Cage/Suite
- NETWORK_STORAGE: 회선·대역폭·인터넷·IX·Cross Connect·트래픽·스토리지·백업·IP
- OPERATIONS: 운영·관리·기술지원·유지보수·보안관제·모니터링·Remote/Smart Hands
- GPU: GPU 임차·리스 요금
- MEMORY: 메모리 별도 요금
- ACQUISITION: 장비 구매·설치비·구축비 같은 1회성 투자비
- OTHER: 위 어디에도 해당하지 않는 비용 (억지로 끼워 맞추지 말 것)"""

SYSTEM_PROMPT = f"""너는 데이터센터(IDC) 청구서를 원가 항목으로 분류하는 도우미다.
입력은 엑셀에서 읽은 줄 목록이다. 각 줄: id, cells(칸 글자 배열, 0부터 번호), number_cols(숫자가 있는 칸 번호).
보안을 위해 숫자는 가려져 있다: 숫자 칸은 "#NUM", 글자 속 숫자는 "#", 고객·사업자명은 "[가림]". 가려진 값을 추측하지 않는다.
금액 칸은 머리글(금액·청구액·Amount 등)과 위치로 판단한다.

각 줄마다 판단해서 JSON으로만 답한다.
1. role: COST(원가 항목) / SUBTOTAL(소계·공급가액) / TOTAL(청구 총액) / TAX(부가세) / DISCOUNT(할인) / IGNORE(제목·머리글·메모·숫자 없는 줄). 전월 미납·이월·입금 줄은 IGNORE, '총 납부할 금액'은 TOTAL.
2. COST이면 item_type을 고른다:
{ITEM_GUIDE}
3. amount_col: 그 줄의 '청구 금액'이 들어 있는 칸 번호. 단가·수량·kW·kWh 칸이 아니라 최종 금액 칸을 고른다. 금액이 없으면 null.
4. included_items: 한 줄에 다른 항목이 묶여 포함되어 있으면 그 항목들 (예: '상면 임대료(전력 10kW 포함)' → item_type RACK_FACILITY, included_items [ELECTRICITY]). 없으면 [].
5. period: 그 줄이 1회성이면 ONE_TIME, 연간이면 YEAR, 월 청구면 MONTH, 알 수 없으면 null.
6. confidence: 0~1. 애매하면 낮게 준다. reason: 판단 근거 한 줄(한국어).

절대 금액 숫자를 새로 만들거나 계산하지 않는다. 문서에 없는 줄 id를 만들지 않는다.
문서 전체에 대해 vendor(사업자명, 모르면 null), currency(KRW/USD/null), default_period(MONTH/YEAR/null)도 답한다."""


def _enum(values: list[str]) -> dict:
    return {"type": ["string", "null"], "enum": [*values, None]}


RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["vendor", "currency", "default_period", "rows"],
    "properties": {
        "vendor": {"type": ["string", "null"]},
        "currency": _enum(["KRW", "USD"]),
        "default_period": _enum([p.value for p in Period]),
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["ref", "role", "item_type", "amount_col", "included_items", "period", "confidence", "reason"],
                "properties": {
                    "ref": {"type": "string"},
                    "role": {"type": "string", "enum": [r.value for r in LineRole]},
                    "item_type": _enum([i.value for i in ItemType if i != ItemType.FINANCING]),
                    "amount_col": {"type": ["integer", "null"]},
                    "included_items": {
                        "type": "array",
                        "items": {"type": "string", "enum": [i.value for i in ItemType if i != ItemType.FINANCING]},
                    },
                    "period": _enum([p.value for p in Period]),
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


class Completion(Protocol):
    def __call__(self, system: str, user: str, schema: dict) -> str: ...


class LLMMappingError(RuntimeError):
    pass


def openai_completion(api_key: str, model: str, timeout: float = 60.0, base_url: str | None = None) -> Completion:
    """OpenAI 또는 OpenAI 호환 서버(사내 sLLM: vLLM·Ollama 등)를 부른다. base_url이 있으면 그 서버로만 보낸다."""
    from openai import OpenAI

    from .. import tracing
    client = tracing.wrap_openai(OpenAI(api_key=api_key or "not-needed", timeout=timeout, base_url=base_url or None))

    def complete(system: str, user: str, schema: dict) -> str:
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "bill_mapping", "schema": schema, "strict": True},
            },
        )
        return resp.choices[0].message.content or ""

    return complete


def build_user_prompt(doc: ExtractedDocument, only_refs: set[str] | None = None, mask: bool = True) -> str:
    """LLM에 보낼 내용. mask=True면 금액·숫자·식별자를 가리고 파일명을 보내지 않는다."""
    terms = sensitive_terms() if mask else []
    lines = []
    for r in doc.rows[:MAX_ROWS_FOR_LLM]:
        if only_refs is not None and r.ref not in only_refs:
            continue
        cells = mask_cells(r.cells, set(r.numbers), terms) if mask else r.cells
        lines.append(json.dumps({"id": r.ref, "cells": cells, "number_cols": sorted(r.numbers)}, ensure_ascii=False))
    head = f"시트: {', '.join(doc.sheets)}" if mask else f"파일: {doc.file_name}\n시트: {', '.join(doc.sheets)}"
    return head + "\n줄 목록(JSON Lines):\n" + "\n".join(lines)


class LLMMapper:
    name = "llm"

    def __init__(self, complete: Completion | Callable[[str, str, dict], str], mask: bool = True,
                 destination: str = "OpenAI"):
        self._complete = complete
        self.mask = mask
        self.destination = destination

    def map(self, doc: ExtractedDocument, only_refs: set[str] | None = None,
            feedback: str | None = None) -> DocumentMapping:
        prompt = build_user_prompt(doc, only_refs, self.mask)
        if feedback:
            prompt += "\n\n[이전 분류 검토 결과]\n" + feedback
        try:
            raw = self._complete(SYSTEM_PROMPT, prompt, RESPONSE_SCHEMA)
        except Exception as exc:  # noqa: BLE001 - 인증·네트워크·한도 오류는 규칙 매퍼로 대체한다
            raise LLMMappingError(f"AI 호출 실패({type(exc).__name__})") from exc
        try:
            data = json.loads(raw)
            # 스키마의 "ref" 필드 = 입력의 id
            mapping = DocumentMapping.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise LLMMappingError(f"LLM 응답 형식 오류: {exc}") from exc
        known = {r.ref: r for r in doc.rows}
        cleaned = []
        for m in mapping.rows:
            row = known.get(m.ref)
            if row is None:
                continue  # 문서에 없는 줄은 버린다
            if m.amount_col is not None and m.amount_col not in row.numbers:
                # 숫자가 없는 칸을 금액 칸으로 고르면 무효 처리 → 검증 단계에서 확인 요청
                m = m.model_copy(update={"amount_col": None, "confidence": min(m.confidence, 0.3),
                                         "reason": m.reason + " (지정한 금액 칸에 숫자가 없음)"})
            cleaned.append(m)
        return mapping.model_copy(update={"rows": cleaned, "llm_input": prompt, "llm_destination": self.destination,
                                          "llm_masked": self.mask})
