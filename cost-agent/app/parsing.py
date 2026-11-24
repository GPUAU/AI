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


def extract(file_name: str, data: bytes) -> ExtractedDocument:
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
        raise UnsupportedFile("xlsx 또는 csv 파일만 올릴 수 있습니다.")
    if not rows:
        raise UnsupportedFile("파일에 읽을 수 있는 내용이 없습니다.")
    return ExtractedDocument(
        file_name=file_name,
        sheets=sheets,
        rows=rows,
        currency_hint=detect_currency(rows),
        truncated=truncated,
    )
