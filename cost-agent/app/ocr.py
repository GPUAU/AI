"""서버 안에서 도는 OCR — 스캔된 PDF 페이지를 글자 상자(글자·위치·신뢰도)로 읽는다. 문서는 밖으로 나가지 않는다.

엔진은 인터페이스 뒤에 둔다 (나중에 Docling·PP-StructureV3 등으로 바꿔 끼울 수 있게).
기본: RapidOCR(Apache-2.0) + PP-OCRv5 한국어 인식 모델(mobile), onnxruntime CPU.
"""

from __future__ import annotations

from typing import Protocol

from .pdfread import Word

RENDER_DPI = 200
DARK_MEAN = 0.45  # 회색조 평균 밝기(0~1)가 이보다 낮으면 짙은 배경으로 보고 반전


def preprocess(image):
    """짙은 배경(흰 글자) 스캔본은 인식률이 떨어진다 → 반전해 밝은 배경·어두운 글자로, 이어서 명암 늘리기."""
    from PIL import ImageOps, ImageStat

    gray = image.convert("L")
    if ImageStat.Stat(gray).mean[0] / 255 < DARK_MEAN:
        gray = ImageOps.invert(gray)
    return ImageOps.autocontrast(gray).convert("RGB")


class OcrEngine(Protocol):
    name: str

    def words(self, image) -> list[Word]: ...


class RapidOcrEngine:
    name = "rapidocr"

    def __init__(self):
        from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

        self._ocr = RapidOCR(params={"Rec.lang_type": LangRec.KOREAN, "Rec.ocr_version": OCRVersion.PPOCRV5,
                                     "Rec.model_type": ModelType.MOBILE, "Global.log_level": "warning"})

    def words(self, image) -> list[Word]:
        """전처리(짙은 배경 반전·명암) 뒤 인식."""
        return self.words_raw(preprocess(image))

    def words_raw(self, image) -> list[Word]:
        res = self._ocr(image)
        out = []
        for box, text, score in zip(res.boxes if res.boxes is not None else [], res.txts or [], res.scores or []):
            xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
            out.append(Word(text=str(text), x0=min(xs), x1=max(xs), top=min(ys), bottom=max(ys),
                            conf=max(0.0, min(1.0, float(score)))))
        return out


def engine_from_settings(engine: str) -> OcrEngine | None:
    """'off'이거나 패키지가 없으면 None (스캔 페이지는 안내와 함께 거절된다)."""
    if (engine or "").lower() != "rapidocr":
        return None
    try:
        return RapidOcrEngine()
    except Exception:  # noqa: BLE001 - 설치 안 됨·모델 없음
        return None


def page_image(pdf_bytes: bytes, page_index: int, dpi: int = RENDER_DPI):
    """PDF 한 쪽을 PIL 이미지로 (pypdfium2)."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_bytes)
    return doc[page_index].render(scale=dpi / 72).to_pil()
