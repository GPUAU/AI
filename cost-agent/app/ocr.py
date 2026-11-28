"""서버 안에서 도는 OCR — 스캔된 PDF 페이지를 글자 상자(글자·위치·신뢰도)로 읽는다. 문서는 밖으로 나가지 않는다.

엔진은 인터페이스 뒤에 둔다 (나중에 Docling·PP-StructureV3 등으로 바꿔 끼울 수 있게).
기본: RapidOCR(Apache-2.0) + PP-OCRv5 한국어 인식 모델(mobile), onnxruntime CPU.
"""

from __future__ import annotations

from typing import Protocol

from .pdfread import Word

RENDER_DPI = 200


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
