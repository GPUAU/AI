"""PDF 인수 채점: 텍스트판은 엑셀과 같게, 스캔판은 조용한 오답 0건."""
import importlib.util

import pytest

from app.ocr import engine_from_settings
from tests.conftest import BUILDERS, ROOT

_spec = importlib.util.spec_from_file_location("score_detailed_samples", ROOT / "scripts" / "score_detailed_samples.py")
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
score = _m.score


@pytest.mark.parametrize("name", list(BUILDERS))
def test_text_pdf_scores_like_xlsx(name, pdf_samples, detailed, service):
    r = score(pdf_samples[name]["text"], detailed[name][1], service)
    assert r["errors"] == [], r["errors"]
    assert r["run_ratio"] == pytest.approx(1.0, abs=0.01)
    assert r["acq"][0] == pytest.approx(r["acq"][1], rel=0.01, abs=1)


@pytest.mark.parametrize("name", list(BUILDERS))
def test_scanned_pdf_never_silently_wrong(name, pdf_samples, detailed, service):
    eng = engine_from_settings("rapidocr")
    if eng is None:
        pytest.skip("rapidocr 미설치")
    r = score(pdf_samples[name]["scan"], detailed[name][1], service, ocr=eng)
    d = r["draft"]
    flagged = d.reconciliation.matched is False or bool(d.bench)  # 총액 불일치 또는 OCR 저신뢰·기준선 경고
    assert r["run_ratio"] == pytest.approx(1.0, abs=0.01) or flagged, (r["run_ratio"], r["errors"])
