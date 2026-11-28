"""청구서 엑셀·CSV를 행 단위 글자·숫자로 읽는다. 해석(어떤 항목인지)은 하지 않는다."""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from pathlib import PurePath

from openpyxl import load_workbook

from .models import Currency, ExtractedDocument, SheetRow

MAX_ROWS = 400
MAX_COLS = 30

_NUMBER = re.compile(r"^\(?-?[₩$]?\s*-?[0-9][0-9,]*(\.[0-9]+)?\s*(원|krw|usd|\$)?\)?$", re.I)


class UnsupportedFile(ValueError):
    pass


def parse_number(text: str) -> float | None:
    """'1,234,000', '₩1,234,000', '1,234,000원', '$12.50', '(5,000)'(음수) → 숫자."""
    s = text.strip().replace(" ", "")
    if not s or not _NUMBER.match(s):
        return None
    negative = s.startswith("(") and s.endswith(")") or "-" in s
    digits = re.sub(r"[^0-9.]", "", s)
    if not digits or digits == ".":
        return None
    value = float(digits)
    return -value if negative else value


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    return str(value).strip()


def _make_row(sheet: str, row_no: int, values: list) -> SheetRow | None:
    values = values[:MAX_COLS]
    cells = [_cell_text(v) for v in values]
    if not any(cells):
        return None
    numbers: dict[int, float] = {}
    for i, v in enumerate(values):
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            numbers[i] = float(v)
        elif isinstance(v, str):
            n = parse_number(v)
            if n is not None:
                numbers[i] = n
    # 끝의 빈 칸 정리
    while cells and not cells[-1]:
        cells.pop()
    return SheetRow(ref=f"{sheet}!{row_no}", sheet=sheet, row=row_no, cells=cells, numbers=numbers)


def detect_currency(rows: list[SheetRow]) -> Currency | None:
    text = " ".join(" ".join(r.cells) for r in rows).lower()
    krw = len(re.findall(r"₩|원\b|원\)|krw|\(원\)", text))
    usd = len(re.findall(r"\$|usd", text))
    if krw == usd == 0:
        return None
    return Currency.KRW if krw >= usd else Currency.USD


PDF_SHEET = "PDF"
SUMMARY_TITLE = ("요약", "summary", "총괄")
MSG_NEED_OCR = "스캔된 페이지가 있어 OCR이 필요해요. 텍스트가 있는 PDF나 엑셀로 올려 주세요"
MSG_ENCRYPTED = "암호가 걸린 PDF는 열 수 없어요. 암호를 풀고 올려 주세요"


def _pdf_rows(data: bytes, ocr, min_conf: float):
    """PDF → 행. 쪽마다 글자가 있으면 pdfplumber, 없으면 OCR. (행, 종류, 저신뢰 ref, 쪽 번호, 잘림)"""
    import pdfplumber

    from .ocr import page_image
    from .pdfread import align_columns, is_text_page, row_cells, text_page_words, words_to_rows

    try:
        pdf = pdfplumber.open(io.BytesIO(data))
        pages = list(pdf.pages)
    except Exception as exc:  # noqa: BLE001 - 손상·암호 PDF
        name = type(exc).__name__.lower() + " " + str(exc).lower()
        raise UnsupportedFile(MSG_ENCRYPTED if "password" in name or "encrypt" in name else "PDF 파일을 열 수 없습니다.") from exc
    kinds: set[str] = set()
    page_lines, meta = [], []  # meta: 줄마다 (쪽 번호, 숫자 칸의 최소 OCR 신뢰도)
    with pdf:
        for i, page in enumerate(pages, start=1):
            if is_text_page(page.extract_text() or ""):
                words, kind = text_page_words(page), "text"
            else:
                if ocr is None:
                    raise UnsupportedFile(MSG_NEED_OCR)
                words, kind = ocr.words(page_image(data, i - 1)), "ocr"
            kinds.add(kind)
            lines = words_to_rows(words)
            page_lines.append(lines)
            for line in lines:
                # 숫자만이 아니라 항목명도 본다: 깨진 한글은 분류 오류나 분기·개시일 같은 단서 손실로 이어진다
                meta.append((i, min(w.conf for cell in line for w in cell)))
    # 쪽마다 시트 하나 ('1쪽'). 첫 줄에 요약·summary·총괄이 있으면 '1쪽 요약' → 요약 시트 중복 판별이 그대로 동작
    sheet_of = {}
    for i, lines in enumerate(page_lines, start=1):
        first = " ".join(row_cells(lines[0])).lower() if lines else ""
        sheet_of[i] = f"{i}쪽 요약" if any(w in first for w in SUMMARY_TITLE) else f"{i}쪽"
    rows: list[SheetRow] = []
    low: list[str] = []
    page_of: dict[str, int] = {}
    truncated = False
    row_no: dict[int, int] = {}
    for cells, (page_no, conf) in zip(align_columns(page_lines), meta):
        if len(rows) >= MAX_ROWS:
            truncated = True
            break
        row_no[page_no] = row_no.get(page_no, 0) + 1
        r = _make_row(sheet_of[page_no], row_no[page_no], cells)
        if r:
            rows.append(r)
            page_of[r.ref] = page_no
            if conf < min_conf:
                low.append(r.ref)
    kind = "pdf-mixed" if kinds == {"text", "ocr"} else ("pdf-ocr" if kinds == {"ocr"} else "pdf-text")
    return rows, kind, low, page_of, truncated, [sheet_of[i] for i in sorted(sheet_of)]


def extract(file_name: str, data: bytes, ocr=None, ocr_min_conf: float = 0.8) -> ExtractedDocument:
    suffix = PurePath(file_name).suffix.lower()
    rows: list[SheetRow] = []
    sheets: list[str] = []
    truncated = False
    if suffix == ".xlsx":
        try:
            # data_only: 수식은 저장된 결과값으로 읽는다. 매크로는 실행되지 않는다.
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001 - 손상 파일
            raise UnsupportedFile("엑셀 파일을 열 수 없습니다.") from exc
        for ws in wb.worksheets:
            if ws.sheet_state != "visible":
                continue
            sheets.append(ws.title)
            for row_no, values in enumerate(ws.iter_rows(values_only=True), start=1):
                if len(rows) >= MAX_ROWS:
                    truncated = True
                    break
                r = _make_row(ws.title, row_no, list(values))
                if r:
                    rows.append(r)
        wb.close()
    elif suffix == ".pdf":
        rows, kind, low, page_of, truncated, pdf_sheets = _pdf_rows(data, ocr, ocr_min_conf)
        if not rows:
            raise UnsupportedFile("파일에 읽을 수 있는 내용이 없습니다.")
        return ExtractedDocument(file_name=file_name, sheets=pdf_sheets, rows=rows, currency_hint=detect_currency(rows),
                                 truncated=truncated, source_kind=kind, ocr_low_confidence=low, page_of=page_of)
    elif suffix == ".csv":
        text = data.decode("utf-8-sig", errors="replace")
        sheets.append("csv")
        for row_no, values in enumerate(csv.reader(io.StringIO(text)), start=1):
            if len(rows) >= MAX_ROWS:
                truncated = True
                break
            r = _make_row("csv", row_no, list(values))
            if r:
                rows.append(r)
    else:
        raise UnsupportedFile("xlsx, csv, pdf 파일만 올릴 수 있습니다.")
    if not rows:
        raise UnsupportedFile("파일에 읽을 수 있는 내용이 없습니다.")
    return ExtractedDocument(
        file_name=file_name,
        sheets=sheets,
        rows=rows,
        currency_hint=detect_currency(rows),
        truncated=truncated,
        source_kind="csv" if suffix == ".csv" else "xlsx",
    )
