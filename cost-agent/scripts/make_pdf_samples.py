"""상세 청구서 샘플(엑셀)을 PDF로 만든다: 텍스트판, 스캔판(이미지만), 섞인 판(1쪽 텍스트 + 1쪽 스캔).

    python scripts/make_pdf_samples.py   → samples-detailed/pdf/*.pdf

시트마다 새 쪽에, 칸을 일정한 가로 간격으로 그린다 (실제 청구서 PDF처럼 글자만 있고 엑셀 구조는 없음).
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pypdfium2 as pdfium
from openpyxl import load_workbook
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
FONT = "KR"
FONT_SIZE = 8
ROW_H = 13
MARGIN = 36
GAP = 14  # 칸 사이 간격 (pt)
TTF_CANDIDATES = ["/System/Library/Fonts/Supplemental/AppleGothic.ttf",
                  "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"]


def _register_font() -> None:
    if FONT in pdfmetrics.getRegisteredFontNames():
        return
    for p in TTF_CANDIDATES:  # TTF를 쓰면 pdfplumber가 한글을 그대로 뽑는다
        if Path(p).exists():
            pdfmetrics.registerFont(TTFont(FONT, p))
            return
    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    pdfmetrics.registerFontFamily(FONT, normal="HYSMyeongJo-Medium")


def _font() -> str:
    return FONT if FONT in pdfmetrics.getRegisteredFontNames() else "HYSMyeongJo-Medium"


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer()):
        return f"{v:,.0f}"
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return str(v)


def to_pdf(xlsx_path: Path, pdf_path: Path) -> None:
    _register_font()
    font = _font()
    wb = load_workbook(xlsx_path, data_only=True)
    c = canvas.Canvas(str(pdf_path), pagesize=landscape(A4))
    w, h = landscape(A4)
    for ws in wb.worksheets:
        rows = [[_text(v) for v in r] for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(r)]
        ncol = max((len(r) for r in rows), default=0)
        widths = [max((pdfmetrics.stringWidth(r[i], font, FONT_SIZE) for r in rows if i < len(r)), default=0)
                  for i in range(ncol)]
        # 제목처럼 한 칸만 있는 긴 줄이 첫 열을 넓히지 않게, 첫 열 폭은 칸이 2개 이상인 줄 기준
        multi = [r for r in rows if sum(1 for x in r if x) >= 2]
        if multi and ncol:
            widths[0] = max(pdfmetrics.stringWidth(r[0], font, FONT_SIZE) for r in multi)
        # 표가 쪽 너비를 넘으면 글자·간격을 줄여 한 쪽에 맞춘다 (잘린 칸이 생기지 않게)
        total = sum(widths) + GAP * max(ncol - 1, 0)
        k = min(1.0, (w - 2 * MARGIN) / total) if total else 1.0
        size = FONT_SIZE * k
        xs, x = [], MARGIN
        for wd in widths:
            xs.append(x)
            x += (wd + GAP) * k
        y = h - MARGIN
        c.setFont(font, size)
        for r in rows:
            if y < MARGIN:
                c.showPage()
                c.setFont(font, size)
                y = h - MARGIN
            for i, t in enumerate(r):
                if t:
                    c.drawString(xs[i], y, t)
            y -= ROW_H
        c.showPage()
    c.save()


DARK_BG, DARK_FG = "#1F2937", "#4B5563"  # 짙은 배경 + 흐린 회색 글자 (흰 글자는 PP-OCRv5가 그대로도 잘 읽어 시험이 안 됨)


def to_scanned(pdf_path: Path, out_path: Path, dpi: int = 150, dark: bool = False) -> None:
    """각 쪽을 이미지로 바꿔 이미지만 든 PDF로 (글자 층 없음 = 스캔본). dark=True면 짙은 배경·흐린 글자 + 살짝 번짐."""
    from PIL import ImageFilter, ImageOps

    src = pdfium.PdfDocument(str(pdf_path))
    c = None
    for page in src:
        pw, ph = page.get_size()
        img = page.render(scale=dpi / 72).to_pil().convert("L")
        if dark:
            img = ImageOps.colorize(img, black=DARK_FG, white=DARK_BG).filter(ImageFilter.GaussianBlur(0.6)).convert("L")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        if c is None:
            c = canvas.Canvas(str(out_path), pagesize=(pw, ph))
        c.setPageSize((pw, ph))
        c.drawImage(ImageReader(buf), 0, 0, width=pw, height=ph)
        c.showPage()
    c.save()


def to_dark_scanned(pdf_path: Path, out_path: Path, dpi: int = 150) -> None:
    """짙은 배경 스캔판 (OCR 전처리 확인용)."""
    to_scanned(pdf_path, out_path, dpi, dark=True)


def to_mixed(text_pdf: Path, scanned_pdf: Path, out_path: Path) -> None:
    """1쪽은 텍스트판, 마지막 쪽은 스캔판의 첫 쪽이 아닌 다음 쪽 (텍스트 + 스캔이 섞인 문서)."""
    out = pdfium.PdfDocument.new()
    t, s = pdfium.PdfDocument(str(text_pdf)), pdfium.PdfDocument(str(scanned_pdf))
    out.import_pages(t, [0])
    out.import_pages(s, [min(1, len(s) - 1)])
    out.save(str(out_path))


def main() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import make_detailed_samples as mds

    src = ROOT / "samples-detailed"
    dst = src / "pdf"
    dst.mkdir(exist_ok=True)
    for name in mds.BUILDERS:
        x = src / name
        if not x.exists():
            mds.BUILDERS[name](x)
        to_pdf(x, dst / f"{x.stem}.pdf")
        to_scanned(dst / f"{x.stem}.pdf", dst / f"{x.stem}.scan.pdf")
        print("wrote", dst / f"{x.stem}.pdf")
    r2 = "bill_r2_multisheet_thousand_krw"
    to_mixed(dst / f"{r2}.pdf", dst / f"{r2}.scan.pdf", dst / "mixed_r2.pdf")


if __name__ == "__main__":
    main()
