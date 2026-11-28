"""로컬 OCR 엔진 (RapidOCR 한국어)."""
import pytest

from app.ocr import engine_from_settings, page_image


def test_engine_off_returns_none():
    assert engine_from_settings("off") is None


def test_rapidocr_reads_scanned_sample(pdf_samples):
    eng = engine_from_settings("rapidocr")
    if eng is None:
        pytest.skip("rapidocr 미설치")
    img = page_image(pdf_samples["bill_r1_kr_metered_krw.xlsx"]["scan"].read_bytes(), 0)
    words = eng.words(img)
    text = " ".join(w.text for w in words)
    assert "상면료" in text and "105,000,000" in text
    assert all(0 <= w.conf <= 1 for w in words) and all(w.x1 > w.x0 and w.bottom > w.top for w in words)
