"""키워드 규칙 매퍼.

LLM 키가 없을 때의 대체 수단이자 테스트 기준선이다. 처음 보는 양식에서는 놓치는 항목이 생길 수 있어
confidence를 낮게 주고, 담당자 확인을 거쳐 템플릿으로 저장하게 한다.
"""

from __future__ import annotations

import re

from ..models import (
    DocumentMapping,
    ExtractedDocument,
    ItemType,
    LineRole,
    Period,
    RowMapping,
    SheetRow,
)

# 위에서부터 먼저 맞는 항목을 쓴다. (묶음 표기 "전력 포함"은 included 처리에서 따로 본다)
ITEM_KEYWORDS: list[tuple[ItemType, list[str]]] = [
    (ItemType.ELECTRICITY, ["전력", "전기", "동력", "kwh", "power", "electric"]),
    (ItemType.RACK_FACILITY, ["상면", "랙", "rack", "cabinet", "시설"]),
    (ItemType.RENT, ["임대", "공간", "층 사용", "colocation", "colo", "cage", "suite", "space"]),
    (
        ItemType.NETWORK_STORAGE,
        ["회선", "대역폭", "네트워크", "인터넷", "ix", "트래픽", "스토리지", "백업",
         "bandwidth", "transit", "cross connect", "cross-connect", "network", "storage", "backup", "ip"],
    ),
    (
        ItemType.OPERATIONS,
        ["운영", "관리", "기술지원", "유지보수", "보안관제", "관제", "모니터링", "remote hands",
         "smart hands", "support", "maintenance", "management", "monitoring"],
    ),
    (ItemType.GPU, ["gpu 임차", "gpu 리스", "gpu lease", "gpu rental"]),
    (ItemType.ACQUISITION, ["구매", "취득", "설치비", "구축비", "setup", "installation", "purchase"]),
]

ROLE_KEYWORDS: list[tuple[LineRole, list[str]]] = [
    (LineRole.TAX, ["부가세", "부가가치세", "세액", "vat", "tax"]),
    (LineRole.SUBTOTAL, ["소계", "공급가액", "subtotal", "sub-total", "sub total"]),
    (LineRole.TOTAL, ["합계", "총액", "총 청구", "청구금액", "청구 금액", "total", "amount due", "grand total"]),
    (LineRole.DISCOUNT, ["할인", "감면", "discount", "credit", "rebate"]),
]

AMOUNT_HEADERS = ["금액", "청구액", "청구금액", "amount", "charge", "total"]
ONE_TIME_WORDS = ["1회", "일회", "one-time", "one time", "setup", "설치비", "구축비"]
INCLUDED_WORDS = ["포함", "incl", "included", "including"]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _find_item(text: str) -> ItemType | None:
    for item, words in ITEM_KEYWORDS:
        if any(_word_in(w, text) for w in words):
            return item
    return None


def _word_in(word: str, text: str) -> bool:
    if re.fullmatch(r"[a-z]{1,3}", word):  # ix, ip 같은 짧은 영문은 단어 경계로
        return re.search(rf"(?<![a-z]){word}(?![a-z])", text) is not None
    return word in text


def split_included(text: str) -> tuple[str, str]:
    """항목명을 '주 항목' 부분과 '포함 표기' 부분으로 나눈다.

    '상면 임대료 (전력 10kW 포함)' → ('상면 임대료', '전력 10kw 포함')
    'colocation - power 40kw included' → ('colocation', 'power 40kw included')
    """
    included: list[str] = []

    def take(m: re.Match) -> str:
        if any(w in m.group(1) for w in INCLUDED_WORDS):
            included.append(m.group(1))
            return " "
        return m.group(0)

    main = re.sub(r"\(([^)]*)\)", take, text)
    parts = re.split(r"\s+[-/,]\s+|,\s*", main)
    keep = []
    for part in parts:
        (included if any(w in part for w in INCLUDED_WORDS) else keep).append(part)
    return _norm(" ".join(keep)), _norm(" ".join(included))


def _included_items(included_text: str, main: ItemType | None) -> list[ItemType]:
    found: list[ItemType] = []
    for item, words in ITEM_KEYWORDS:
        if item != main and any(_word_in(w, included_text) for w in words) and item not in found:
            found.append(item)
    return found


def amount_columns(doc: ExtractedDocument) -> dict[str, int]:
    """시트별 '금액' 머리글 열 번호."""
    result: dict[str, int] = {}
    for row in doc.rows:
        if row.sheet in result:
            continue
        for i, cell in enumerate(row.cells):
            c = _norm(cell)
            if c and any(h == c or c.startswith(h) or c.endswith(h) for h in AMOUNT_HEADERS) and i not in row.numbers:
                result[row.sheet] = i
                break
    return result


def pick_amount_col(row: SheetRow, preferred: int | None) -> int | None:
    if not row.numbers:
        return None
    if preferred is not None and preferred in row.numbers:
        return preferred
    return max(row.numbers)  # 가장 오른쪽 숫자 칸


def map_row(row: SheetRow, preferred_col: int | None) -> RowMapping:
    text = _norm(" ".join(row.cells))
    label = _norm(row.label)
    col = pick_amount_col(row, preferred_col)
    if col is None:
        return RowMapping(ref=row.ref, role=LineRole.IGNORE, confidence=0.9, reason="금액 칸이 없는 줄")
    for role, words in ROLE_KEYWORDS:
        if any(_word_in(w, label) for w in words):
            return RowMapping(ref=row.ref, role=role, amount_col=col, confidence=0.8, reason=f"'{role.value}' 표현")
    main_text, included_text = split_included(label)
    item = _find_item(main_text)
    period = Period.ONE_TIME if any(w in text for w in ONE_TIME_WORDS) else None
    if item is None:
        if not label:
            return RowMapping(ref=row.ref, role=LineRole.IGNORE, confidence=0.6, reason="항목명이 없는 숫자 줄")
        return RowMapping(
            ref=row.ref, role=LineRole.COST, item_type=ItemType.OTHER, amount_col=col, period=period,
            confidence=0.4, reason="알려진 키워드가 없어 기타로 분류",
        )
    return RowMapping(
        ref=row.ref,
        role=LineRole.COST,
        item_type=item,
        amount_col=col,
        included_items=_included_items(included_text, item),
        period=period,
        confidence=0.7,
        reason="키워드 일치",
    )


class RulesMapper:
    name = "rules"

    def map(self, doc: ExtractedDocument) -> DocumentMapping:
        cols = amount_columns(doc)
        rows = [map_row(r, cols.get(r.sheet)) for r in doc.rows]
        text = _norm(" ".join(" ".join(r.cells) for r in doc.rows[:15]))
        default_period = Period.YEAR if any(w in text for w in ["연간", "annual", "yearly"]) else Period.MONTH
        return DocumentMapping(currency=doc.currency_hint, default_period=default_period, rows=rows)
