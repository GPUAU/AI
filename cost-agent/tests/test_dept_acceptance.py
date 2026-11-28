"""팀 목데이터 8종 인수 채점: 기준표 정확도 100%, 08 검산 일치, 마스킹 누출 0, 근거서 첫 시트 '원가 기준표'."""

import importlib.util
import io

from openpyxl import load_workbook

from app.agent.models import Answer, Goal, Step
from app.agent.orchestrator import CostAgent
from app.mapping.rules import RulesMapper
from app.mapping.templates import TemplateStore
from app.service import BillService
from tests.conftest import ROOT
from tests.test_agent import fake_backend, suggested

_spec = importlib.util.spec_from_file_location("score_dept_docs", ROOT / "scripts" / "score_dept_docs.py")
score_dept_docs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(score_dept_docs)


def test_dept_sheet_accuracy_is_100pct(dept_docs, dept_key, tmp_path):
    r = score_dept_docs.score(list(dept_docs.values()), dept_key, tmp_path)
    assert r["accuracy"] == 1.0, r["wrong"]
    assert r["plan_ok"] is True and abs(r["plan_hourly_krw"] - 1885) <= 19
    assert r["leaks"] == []
    assert all("일치" in m for m in r["cross_checks"])


def test_report_first_sheet_is_cost_sheet(dept_docs, tmp_path):
    from app.agent.report import build_report
    agent = CostAgent(BillService(RulesMapper(), TemplateStore(tmp_path)), fake_backend()[0])
    s = agent.start_many(list(dept_docs.values()), Goal(terms_years=[3]))
    for _ in range(15):
        if s.question is None:
            break
        q = s.question
        s = agent.answer(s.id, Answer(question_id=q.id, values={}) if q.step == Step.ASSUMPTIONS else suggested(q))
    wb = load_workbook(io.BytesIO(build_report(s)))
    assert wb.sheetnames[:2] == ["요약", "원가 근거"]
    ws = wb["원가 근거"]
    assert ws["A4"].value == "원가 기준표"
    assert [c.value for c in ws[5]] == ["칸", "값", "단위", "상태", "출처 (문서 · 쪽 · 행 · 열)", "교차 확인 수", "담당자 수정", "메모"]
    rows = {}
    for r in ws.iter_rows(min_row=6):
        rows.setdefault(r[0].value, r)  # 같은 이름이 아래 '가정값' 표에도 있다 — 기준표 줄을 쓴다
    acq = rows["서버·GPU 취득비"]
    assert acq[1].value == 1_009_000_000 and acq[5].value == 2 and "01_구매팀" in acq[4].value
    assert rows["운영비 (청구서 운영비 + 사내 인건비·판관비)"][1].value == 72_000_000


def test_single_bill_report_has_no_cost_sheet(tmp_path, doc):
    from app.agent.report import build_report
    agent = CostAgent(BillService(RulesMapper(), TemplateStore(tmp_path)), fake_backend()[0])
    s = agent.start(doc("c"), Goal(model_id="29", gpu_count=256, terms_years=[]))
    assert load_workbook(io.BytesIO(build_report(s)))["원가 근거"]["A4"].value == "청구서 원가 항목"
