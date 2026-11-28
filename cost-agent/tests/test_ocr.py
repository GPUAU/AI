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


# ---------------------------------------------------------------- 짙은 배경 전처리 (Task 7)
from PIL import Image, ImageStat  # noqa: E402

from app.ocr import preprocess  # noqa: E402
from tests.conftest import make_pdf_samples  # noqa: E402

DARK_NAME = "bill_r1_kr_metered_krw.xlsx"


def _mean(img) -> float:
    return ImageStat.Stat(img.convert("L")).mean[0] / 255


def test_dark_background_is_inverted():
    img = Image.new("L", (40, 40), 51)  # 평균 밝기 0.2
    img.paste(230, (10, 10, 30, 14))  # 밝은 글자
    out = preprocess(img)
    assert _mean(out) > 0.5 and out.getpixel((15, 12)) < out.getpixel((2, 2))  # 글자가 배경보다 어두워짐


def test_light_page_is_not_inverted():
    img = Image.new("L", (40, 40), 240)
    img.paste(20, (10, 10, 30, 14))
    out = preprocess(img)
    assert out.getpixel((15, 12)) < out.getpixel((2, 2))


def test_dark_scan_reads_better_with_preprocess(pdf_samples, tmp_path):
    eng = engine_from_settings("rapidocr")
    if eng is None:
        pytest.skip("rapidocr 미설치")
    dark = tmp_path / "dark.pdf"
    make_pdf_samples.to_dark_scanned(pdf_samples[DARK_NAME]["text"], dark)
    img = page_image(dark.read_bytes(), 0)
    assert _mean(img) < 0.45
    import re

    import pdfplumber
    with pdfplumber.open(pdf_samples[DARK_NAME]["text"]) as pdf:
        truth = set(re.findall(r"\d[\d,]{3,}", pdf.pages[0].extract_text()))

    def score(words):
        nums = set(re.findall(r"\d[\d,]{3,}", " ".join(w.text for w in words)))
        return len(nums & truth), len(nums - truth)

    raw, pre = score(eng.words_raw(img)), score(eng.words(img))
    text = " ".join(w.text for w in eng.words(img))
    assert "상면료" in text.replace(" ", "") and "105,000,000" in text
    assert pre[0] > raw[0] and pre[1] <= raw[1]  # 맞힌 금액은 늘고 틀린 금액은 늘지 않음
