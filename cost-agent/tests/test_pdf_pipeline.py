"""PDF·OCR로 읽은 청구서가 기존 파이프라인에서 어떻게 드러나는가."""
from tests.conftest import doc_from_rows

ROWS = [["청구월", "2026년 10월분"], ["항목", "금액(원)"], ["전력 사용료", "1,000,000"], ["상면 사용료", "2,000,000"],
        ["회선료", "500,000"], ["운영관리비", "300,000"], ["합계", "3,800,000"]]


def ocr_doc(rows=ROWS, low=("s!3",)):
    d = doc_from_rows(rows)
    return d.model_copy(update={"source_kind": "pdf-ocr", "ocr_low_confidence": list(low),
                                "page_of": {r.ref: 1 for r in d.rows}})


def test_low_confidence_numbers_become_bench_warning(service):
    d = service.ingest(ocr_doc())
    b = next(b for b in d.bench if b.code == "OCR_LOW_CONFIDENCE")
    assert b.value == 1 and "1쪽 3줄" in b.message and "원본과 비교" in b.message


def test_no_warning_for_text_pdf(service):
    d = service.ingest(ocr_doc(low=()))
    assert not any(b.code == "OCR_LOW_CONFIDENCE" for b in d.bench)


def test_misread_number_surfaces_as_mismatch(service):  # Review Focus 3: 쉼표를 마침표로 읽음
    rows = [r[:] for r in ROWS]
    rows[3][1] = "2.000.000"
    d = service.ingest(ocr_doc(rows, low=()))
    assert d.reconciliation.matched is False
