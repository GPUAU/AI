"""시트마다 금액 열의 역할을 정한다: 공급가액·부가세·합계, MRC/NRC, 월 정액/당월 청구, 수량·단가. (설계 §4A-2)

줄마다 금액 칸을 고르면 '합계(VAT 포함)'나 'NRC' 칸을 원가로 읽는 실수가 난다. 열 역할을 먼저 정하고,
해석 단계(interpret)가 매퍼가 고른 금액 칸을 이 역할에 맞게 바로잡는다.
"""

from __future__ import annotations

import re
from enum import Enum

from .models import ExtractedDocument, SheetRow


class ColRole(str, Enum):
    SUPPLY = "SUPPLY"  # 공급가액 (원가로 쓰는 기본 열)
    VAT = "VAT"
    GROSS = "GROSS"  # 부가세 포함 합계
    MRC = "MRC"  # 월 정기
    NRC = "NRC"  # 1회성
    BASE = "BASE"  # 월 정액 (일할 전)
    BILLED = "BILLED"  # 당월 청구액 (일할 후)
    QTY = "QTY"
    RATE = "RATE"


# 위에서 먼저 맞는 역할을 쓴다 ('합계금액'은 '금액'보다 '합계'가 먼저)
ROLE_WORDS: list[tuple[ColRole, list[str]]] = [
    (ColRole.NRC, ["nrc", "one-time", "setup", "1회"]),
    (ColRole.MRC, ["mrc", "monthly recurring"]),
    (ColRole.BASE, ["월 정액", "월정액"]),
    (ColRole.BILLED, ["당월 청구", "일할 청구"]),
    (ColRole.VAT, ["부가세", "세액", "vat", "tax"]),
    (ColRole.GROSS, ["합계금액", "합계", "total"]),
    (ColRole.SUPPLY, ["공급가액", "금액", "청구액", "amount"]),
    (ColRole.RATE, ["단가", "unit rate", "rate"]),
    (ColRole.QTY, ["수량", "qty", "일수", "사용량"]),
]
PREFERRED = [ColRole.BILLED, ColRole.SUPPLY, ColRole.MRC]
RATIO_TOL = 0.005
RATIO_MIN_ROWS = 3
RATIO_SHARE = 0.8


def _clean(text: str) -> str:
    t = re.sub(r"\([^)]*\)", " ", text.lower())  # (원)·(USD) 같은 단위 표기 제거
    return re.sub(r"\s+", " ", t).strip()


def _role_of(text: str) -> ColRole | None:
    t = _clean(text)
    if not t:
        return None
    for role, words in ROLE_WORDS:
        if any(w in t for w in words):
            return role
    return None


def _hits(row: SheetRow) -> int:
    return sum(1 for c in row.cells if _role_of(c))


def _header(rows: list[SheetRow]) -> dict[int, str]:
    """시트의 머리글: 숫자 없고 역할 단어가 2개 이상인 첫 줄 + 바로 위·아래의 머리글 조각(병합 머리글)."""
    by_no = {r.row: r for r in rows}
    for r in rows:
        if r.numbers or _hits(r) < 2:
            continue
        parts = [r]
        for n in (r.row - 1, r.row + 1):
            adj = by_no.get(n)
            if adj and not adj.numbers and _hits(adj) >= 1:
                parts.append(adj)
        parts.sort(key=lambda x: x.row)
        width = max(len(p.cells) for p in parts)
        return {i: " ".join(p.cells[i] for p in parts if i < len(p.cells) and p.cells[i]).strip()
                for i in range(width)}
    return {}


def _by_sheet(doc: ExtractedDocument) -> dict[str, list[SheetRow]]:
    out: dict[str, list[SheetRow]] = {}
    for r in doc.rows:
        out.setdefault(r.sheet, []).append(r)
    return out


def header_text(doc: ExtractedDocument) -> dict[str, dict[int, str]]:
    return {sheet: {i: t for i, t in _header(rows).items() if t} for sheet, rows in _by_sheet(doc).items()}


def _ratio_roles(rows: list[SheetRow], roles: dict[int, ColRole]) -> None:
    """머리글이 없어도 한 열이 다른 열의 1.1배면 합계(GROSS), 0.1배면 부가세(VAT)로 본다."""
    cols = sorted({c for r in rows for c in r.numbers})
    for a in cols:
        for b in cols:
            if a == b or b in roles:
                continue
            pairs = [(r.numbers[a], r.numbers[b]) for r in rows if a in r.numbers and b in r.numbers and r.numbers[a]]
            if len(pairs) < RATIO_MIN_ROWS:
                continue
            for factor, role in ((1.1, ColRole.GROSS), (0.1, ColRole.VAT)):
                ok = sum(1 for x, y in pairs if abs(y - x * factor) <= abs(x * factor) * RATIO_TOL)
                if ok / len(pairs) >= RATIO_SHARE:
                    roles[b] = role
                    break


def column_roles(doc: ExtractedDocument) -> dict[str, dict[int, ColRole]]:
    out: dict[str, dict[int, ColRole]] = {}
    for sheet, rows in _by_sheet(doc).items():
        roles = {i: role for i, t in _header(rows).items() if (role := _role_of(t))}
        _ratio_roles(rows, roles)
        out[sheet] = roles
    return out


def preferred_col(roles: dict[int, ColRole], row: SheetRow) -> int | None:
    """원가로 쓸 금액 칸: 당월 청구 > 공급가액 > 월 정기. 그 칸에 숫자가 있을 때만."""
    for want in PREFERRED:
        for i, role in sorted(roles.items()):
            if role == want and i in row.numbers:
                return i
    return None
