"""한 장 승인 보고서 HTML과 근거서 시트 (설계 §4B-4)."""
import io

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.agent.report import build_report
from app.agent.report_html import build_report_html
from app.agent.verify import risks
from app.config import Settings
from app.main import create_app
from tests.test_agent import assumptions, real_backend
from tests.test_verify import done_session


def test_html_has_sections_and_numbers_from_session(tmp_path, detailed_doc):
    _, s = done_session(tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    h = build_report_html(s)
    cand = float(s.simulation["candidatePrice"])
    assert h.startswith("<!DOCTYPE html>") and "GPU 요금 제안 검토" in h
    assert f"권고: {s.approval.verdict}" in h and f"${cand:.2f}" in h
    assert all(f"${t.price:.2f}" in h for t in s.terms)
    assert "자동 검증 7/7 통과" in h and all(r in h for r in risks(s))
    assert "원가 구성" in h and "결론을 바꿀 수 있는 가정" in h and "@media print" in h
    assert "http" not in h.split("<body", 1)[0]  # 외부 리소스 없음


def test_html_escapes_bill_text(tmp_path, detailed_doc):
    _, s = done_session(tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    s.draft.file_name = "<script>alert(1)</script>.xlsx"
    h = build_report_html(s)
    assert "<script>alert" not in h and "&lt;script&gt;" in h


def test_html_has_pdf_button_hidden_in_print(tmp_path, detailed_doc):
    _, s = done_session(tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    h = build_report_html(s)
    assert "PDF로 저장" in h and "window.print()" in h and "get('print')==='1'" in h
    assert ".tools{display:none}" in h.split("@media print", 1)[1]


def test_html_marks_finding_levels(tmp_path, detailed_doc):
    from app.agent.models import Finding
    _, s = done_session(tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    s.approval.findings = [Finding(code="TERM_BELOW_COST", level="재검토", message="5년 계약가가 원가보다 낮아요"),
                           Finding(code="MARKET_LOW", level="조건부", message="시장 최저가보다 낮아요")]
    h = build_report_html(s)
    assert '<span class="lv lv-rev">재검토</span>5년 계약가가 원가보다 낮아요' in h
    assert '<span class="lv lv-cond">조건부</span>시장 최저가보다 낮아요' in h


def test_html_omits_breakdown_when_mismatch(tmp_path, detailed_doc):
    _, s = done_session(tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    s.approval.breakdown_ok = False
    assert "원가 서버 결과와 맞지 않아" in build_report_html(s)


def test_html_endpoint_409_before_ready(tmp_path, samples):
    backend, _ = real_backend()
    settings = Settings(openai_api_key=None, openai_model="x", openai_base_url=None, llm_mask=True, mapper="rules",
                        spring_url="http://be", template_dir=str(tmp_path / "t"), max_upload_bytes=1 << 20)
    c = TestClient(create_app(settings, backend=backend))
    with open(samples["a"], "rb") as f:
        s = c.post("/v1/agent/sessions", files={"file": ("a.xlsx", f)}, data={"model_id": "29", "gpu_count": "8"}).json()
    assert c.get(f"/v1/agent/sessions/{s['id']}/report.html").status_code == 409
    s = c.post(f"/v1/agent/sessions/{s['id']}/answer", json={"question_id": s["question"]["id"],
                                                             "values": assumptions(gpu_count=8)}).json()
    while s["question"]:
        q = s["question"]
        vals = {f["name"]: "확인" for f in q["fields"] if f["name"].startswith("reason:")}
        body = {"question_id": q["id"], "values": vals}
        if q["options"]:
            body["choice"] = q["options"][0]["value"]
        s = c.post(f"/v1/agent/sessions/{s['id']}/answer", json=body).json()
    r = c.get(f"/v1/agent/sessions/{s['id']}/report.html")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")


def test_xlsx_sheet_order(tmp_path, detailed_doc):
    _, s = done_session(tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    wb = load_workbook(io.BytesIO(build_report(s)))
    assert wb.sheetnames == ["요약", "원가 근거", "원가 구성·민감도", "시장 비교", "부록"]
    heads = [[c.value for c in r] for r in wb["원가 근거"].iter_rows() if r[0].value == "가정"]
    assert heads and "출처 등급" in heads[0]


def test_xlsx_summary_is_readable(tmp_path, detailed_doc):
    """첫 시트에 권고·제안 요금·확인할 점이 있고, 코드값 대신 한글·서식이 나온다."""
    from app.agent.models import Finding
    _, s = done_session(tmp_path, detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    s.approval.findings = [Finding(code="PRICE_BELOW_COST", level="재검토", message="5년가: 원가보다 낮아 손실이에요",
                                   fix_step="TERMS", reason="전략 가격")]
    s.approval.verdict = "재검토"
    wb = load_workbook(io.BytesIO(build_report(s)))
    ws = wb["요약"]
    cells = {c.coordinate: c for r in ws.iter_rows() for c in r if c.value is not None}
    text = [str(c.value) for c in cells.values()]
    assert cells["B4"].value == "재검토" and cells["B4"].fill.fgColor.rgb.endswith("FDECEE")
    assert abs(cells["B8"].value - float(s.simulation["hourlyCost"])) < 1e-9 and cells["B8"].number_format.startswith('"$"')
    assert cells["C9"].value.startswith("=IF($B$8=0")                      # 원가 대비는 수식
    row = next(c.row for c in cells.values() if c.value == "5년가: 원가보다 낮아 손실이에요")
    assert ws.cell(row, 1).value == "재검토" and ws.cell(row, 3).value == "기간별 가격" and ws.cell(row, 4).value == "전략 가격"
    assert not any(code in t for t in text for code in ("REVENUE_MARGIN", "USER_INPUT", "candidate"))
    assert ws.oddHeader.right.text == "내부용 · 대외 반출 금지"
    ev = [[c.value for c in r] for r in wb["원가 근거"].iter_rows()]
    assert not any(v in ("USER_INPUT", "NOT_INCLUDED", "REVENUE_MARGIN") for r in ev for v in r)
    util = next(r for r in wb["원가 근거"].iter_rows() if r[0].value == "유료 가동률")
    assert util[1].number_format.startswith("0%")
