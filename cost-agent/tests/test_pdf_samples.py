"""샘플 PDF 생성기 (텍스트판·스캔판·섞인 판)."""
import pdfplumber


def test_text_pdf_has_korean_text(pdf_samples):
    with pdfplumber.open(pdf_samples["bill_r1_kr_metered_krw.xlsx"]["text"]) as p:
        t = p.pages[0].extract_text()
    assert "고밀도 랙 상면료" in t and "537,141,920" in t


def test_scanned_pdf_has_no_text(pdf_samples):
    with pdfplumber.open(pdf_samples["bill_r1_kr_metered_krw.xlsx"]["scan"]) as p:
        assert (p.pages[0].extract_text() or "").strip() == ""


def test_mixed_pdf_has_text_then_scan(mixed_pdf):
    with pdfplumber.open(mixed_pdf) as p:
        assert len(p.pages) >= 2
        assert (p.pages[0].extract_text() or "").strip() and not (p.pages[-1].extract_text() or "").strip()
