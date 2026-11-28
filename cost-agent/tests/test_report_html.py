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
    assert wb.sheetnames[:5] == ["승인 요약", "원가 구성", "민감도", "검토 결과", "요약"]
    head = [c.value for c in next(wb["가정값"].iter_rows(min_row=1, max_row=1))]
    assert "출처 등급" in head
