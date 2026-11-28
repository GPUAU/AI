import pytest

from app.models import Currency
from app.parsing import UnsupportedFile, extract, parse_number


@pytest.mark.parametrize("text,expected", [
    ("1,234,000", 1234000), ("₩1,234,000", 1234000), ("1,234,000원", 1234000),
    ("$12.50", 12.5), ("(5,000)", -5000), ("-500,000", -500000),
    ("2026-10", None), ("40kW", None), ("", None), ("합계", None),
])
def test_parse_number(text, expected):
    assert parse_number(text) == expected


def test_extract_xlsx_reads_numbers_and_currency(doc):
    d = doc("a")
    assert d.currency_hint == Currency.KRW
    row = next(r for r in d.rows if "전력 사용료" in r.label)
    assert row.numbers[5] == 28_800 * 180


def test_extract_csv():
    d = extract("bill.csv", "항목,금액(원)\n전력 사용료,\"1,000,000\"\n합계,\"1,000,000\"\n".encode())
    assert d.rows[1].numbers == {1: 1_000_000}


def test_rejects_other_types():
    with pytest.raises(UnsupportedFile):
        extract("bill.pdf", b"%PDF")
    with pytest.raises(UnsupportedFile):
        extract("bill.xlsx", b"not a zip")


# ---------------------------------------------------------------- PDF (텍스트 우선, 스캔은 로컬 OCR)
import pytest  # noqa: E402

from app.ocr import engine_from_settings  # noqa: E402
from app.parsing import UnsupportedFile  # noqa: E402


@pytest.fixture(scope="module")
def ocr_engine():
    eng = engine_from_settings("rapidocr")
    if eng is None:
        pytest.skip("rapidocr 미설치")
    return eng


def test_text_pdf_extracts_like_xlsx(pdf_samples):
    d = extract("r1.pdf", pdf_samples["bill_r1_kr_metered_krw.xlsx"]["text"].read_bytes())
    assert d.source_kind == "pdf-text" and d.sheets == ["1쪽"]
    assert any("고밀도 랙 상면료" in r.label for r in d.rows)
    first = next(r for r in d.rows if "고밀도 랙 상면료" in r.label)
    assert 105_000_000 in first.numbers.values() and d.page_of[first.ref] == 1


def test_scanned_pdf_without_engine_is_refused(pdf_samples):
    with pytest.raises(UnsupportedFile, match="OCR이 필요해요"):
        extract("s.pdf", pdf_samples["bill_r1_kr_metered_krw.xlsx"]["scan"].read_bytes(), ocr=None)


def test_scanned_pdf_with_engine(pdf_samples, ocr_engine):
    d = extract("s.pdf", pdf_samples["bill_r1_kr_metered_krw.xlsx"]["scan"].read_bytes(), ocr=ocr_engine)
    assert d.source_kind == "pdf-ocr" and any("상면료" in r.label for r in d.rows)


def test_mixed_pdf_kind(mixed_pdf, ocr_engine):
    d = extract("m.pdf", mixed_pdf.read_bytes(), ocr=ocr_engine)
    assert d.source_kind == "pdf-mixed" and set(d.page_of.values()) == {1, 2}


def test_encrypted_pdf_is_refused(tmp_path):
    from reportlab.pdfgen import canvas
    p = tmp_path / "locked.pdf"
    c = canvas.Canvas(str(p), encrypt="secret")
    c.drawString(100, 700, "locked invoice text that is long enough")
    c.save()
    with pytest.raises(UnsupportedFile, match="암호가 걸린 PDF"):
        extract("locked.pdf", p.read_bytes())
