"""PDF 페이지의 단어(글자와 위치)를 줄·칸으로 묶는다. 텍스트 페이지(pdfplumber)와 OCR 페이지가 같은 함수를 쓴다.

- 같은 높이(세로 중심이 가까움)의 단어는 한 줄
- 한 줄 안에서 가로 간격이 크면 새 칸
- 칸을 페이지의 머리글 줄(숫자 없는 줄 중 칸이 가장 많은 줄)의 열에 맞춘다 → 금액 열 번호가 머리글과 같아져
  columns.column_roles(공급가액·부가세·합계 판별)가 그대로 동작한다
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import median

MIN_TEXT_CHARS = 20
MAX_GARBLED = 0.05
_CID = re.compile(r"\(cid:\d+\)")


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    conf: float = 1.0  # OCR 신뢰도 (텍스트 페이지는 1.0)


def is_text_page(text: str) -> bool:
    """글자 수 ≥ 20이고 깨진 글자(�, (cid:n)) 비율 < 5%면 텍스트 페이지."""
    t = re.sub(r"\s+", "", text or "")
    if len(t) < MIN_TEXT_CHARS:
        return False
    garbled = t.count("�") + sum(len(m) for m in _CID.findall(t))
    return garbled / len(t) < MAX_GARBLED


def _clean(text: str) -> str:
    """일부 PDF는 띄어쓰기를 제어 문자(\x01)로 넣는다 → 공백, 연속 공백은 하나로."""
    return re.sub(r"\s+", " ", text.replace("\x01", " ")).strip()


def text_page_words(page) -> list[Word]:
    """pdfplumber 페이지 → 단어 목록."""
    return [Word(text=_clean(w["text"]), x0=float(w["x0"]), x1=float(w["x1"]), top=float(w["top"]), bottom=float(w["bottom"]))
            for w in page.extract_words(keep_blank_chars=False, use_text_flow=False)]


def words_to_rows(words: list[Word]) -> list[list[list[Word]]]:
    """줄(위→아래) → 칸(왼→오른) → 단어."""
    if not words:
        return []
    height = median(w.bottom - w.top for w in words) or 1.0
    # 칸 사이 간격 기준: 글자 높이(≈ 글자 크기)의 0.6배. 칸 안의 띄어쓰기는 보통 0.25~0.35배, 열 간격은 그보다 크다.
    # (글자 폭 기준은 한글이 넓어서 좁은 열 간격을 한 칸으로 붙여 버린다)
    split_gap = 0.6 * height
    lines: list[list[Word]] = []
    for wd in sorted(words, key=lambda w: ((w.top + w.bottom) / 2, w.x0)):
        c = (wd.top + wd.bottom) / 2
        if lines and abs(c - _center(lines[-1])) <= 0.5 * height:
            lines[-1].append(wd)
        else:
            lines.append([wd])
    out = []
    for line in lines:
        cells: list[list[Word]] = []
        for wd in sorted(line, key=lambda w: w.x0):
            if cells and wd.x0 - cells[-1][-1].x1 <= split_gap:
                cells[-1].append(wd)
            else:
                cells.append([wd])
        out.append(cells)
    return out


def _center(line: list[Word]) -> float:
    return sum((w.top + w.bottom) / 2 for w in line) / len(line)


def row_cells(line: list[list[Word]]) -> list[str]:
    return [" ".join(w.text for w in cell) for cell in line]


def _span(cell: list[Word]) -> tuple[float, float]:
    return min(w.x0 for w in cell), max(w.x1 for w in cell)


def _is_number(text: str) -> bool:
    from .parsing import parse_number  # parsing이 이 모듈을 쓰므로 여기서 불러온다
    return parse_number(text) is not None


def _header(lines: list[list[list[Word]]]) -> list[tuple[float, float]] | None:
    """칸이 가장 많은 숫자 없는 줄 + 바로 아래·위 머리글 줄의 칸 중 겹치지 않는 것 (2줄 병합 머리글)."""
    def plain(line):
        texts = row_cells(line)
        return len(texts) >= 1 and not any(_is_number(t) for t in texts)

    best_i = None
    for i, line in enumerate(lines):
        if len(line) >= 2 and plain(line) and (best_i is None or len(line) > len(lines[best_i])):
            best_i = i
    if best_i is None:
        return None
    spans = [_span(c) for c in lines[best_i]]
    for j in (best_i - 1, best_i + 1):
        if 0 <= j < len(lines) and plain(lines[j]) and len(lines[j]) >= 2:
            for sp in (_span(c) for c in lines[j]):
                if all(min(sp[1], b) - max(sp[0], a) <= 0 for a, b in spans):
                    spans.append(sp)
    return sorted(spans)


def _place(cell: list[Word], anchors: list[tuple[float, float]]) -> int:
    a, b = _span(cell)
    overlaps = [max(0.0, min(b, y) - max(a, x)) for x, y in anchors]
    if max(overlaps) > 0:
        return overlaps.index(max(overlaps))
    mid = (a + b) / 2
    return min(range(len(anchors)), key=lambda i: abs((anchors[i][0] + anchors[i][1]) / 2 - mid))


def align_columns(pages: list[list[list[list[Word]]]]) -> list[list[str]]:
    """페이지마다 머리글 열에 칸을 맞춘 글자 행들 (모든 페이지를 이어서). 머리글이 없는 페이지는 앞 페이지 열을 쓴다."""
    out: list[list[str]] = []
    anchors: list[tuple[float, float]] | None = None
    for lines in pages:
        anchors = _header(lines) or anchors
        for line in lines:
            if not anchors:
                out.append(row_cells(line))
                continue
            row = [""] * len(anchors)
            for cell in line:
                i = _place(cell, anchors)
                while i < len(row) and row[i]:  # 같은 열에 두 칸이 오면 오른쪽 빈 열로
                    i += 1
                if i >= len(row):
                    row.append("")
                    i = len(row) - 1
                row[i] = " ".join(w.text for w in cell)
            out.append(row)
    return out
