"""견적 근거서 엑셀. 담당자가 보고서에 그대로 붙일 수 있게 결과·가정·근거를 한 파일로 정리한다."""

from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from ..validation import BILL_CORE_ITEMS  # noqa: F401  (항목 정의 참고)
from .models import AgentSession

HEAD = PatternFill("solid", fgColor="1F1F1F")
ITEM_KO = {
    "ACQUISITION": "서버·GPU 취득비", "GPU": "GPU 임차", "MEMORY": "별도 메모리", "NETWORK_STORAGE": "네트워크·스토리지",
    "RACK_FACILITY": "랙·전원·냉각 설비", "RENT": "임차료·코로케이션", "OPERATIONS": "운영·유지보수",
    "FINANCING": "조달 금리", "ELECTRICITY": "전기료", "OTHER": "기타",
}


def _header(ws, row, values):
    for i, v in enumerate(values, 1):
        c = ws.cell(row, i, v)
        c.font, c.fill = Font(bold=True, color="FFFFFF"), HEAD


def _approval_sheets(wb, s: AgentSession) -> None:
    """승인 검토 시트 4개를 맨 앞에 둔다: 승인 요약 · 원가 구성 · 민감도 · 검토 결과."""
    from . import verify as V
    ap, r, a = s.approval, s.simulation or {}, s.assumptions or {}
    ws = wb.create_sheet("승인 요약", 0)
    ws["A1"] = "GPU 요금 제안 검토"
    ws["A1"].font = Font(bold=True, size=15)
    rows = [("권고", ap.verdict if ap else "검토 전"), ("GPU 모델 · 수", f"{a.get('model_name', '')} · {a.get('gpu_count', '')}장"),
            ("제안가 (USD/GPU·시간)", r.get("candidatePrice")), ("원가 = 최저 수용가", r.get("hourlyCost"))]
    rows += [(f"{t.years}년 계약가", t.price) for t in s.terms]
    if ap:
        rows += [(f"손익분기 가동률 ({k})", round(v, 4)) for k, v in ap.breakeven.items()]
        rows += [("권고 이유", x) for x in ap.reasons]
    rows += [("리스크", x) for x in V.risks(s)]
    rows.append(("자동 검증", f"{len(s.checks_passed)}/{len(V.CHECKS)} 통과" + (f" · 실패: {', '.join(s.checks_failed)}" if s.checks_failed else "")))
    for i, (k, v) in enumerate(rows, 3):
        ws.cell(i, 1, k).font = Font(bold=True)
        ws.cell(i, 2, v)
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 28, 80

    ws = wb.create_sheet("원가 구성", 1)
    _header(ws, 1, ["묶음", "원가 항목", "연간 (USD)", "GPU·시간당 (USD)", "비중", "업계 기준 비중", "근거 줄"])
    bench = {b["group"]: b for b in (ap.benchmark if ap else [])}
    if ap and ap.breakdown_ok:
        for row in ap.breakdown:
            ws.append([row.group, ", ".join(ITEM_KO.get(t, t) for t in row.item_types), round(row.annual_usd, 2),
                       round(row.hourly_usd, 4), round(row.share, 4), bench.get(row.group, {}).get("ref_share"), ", ".join(row.refs)])
    else:
        ws.append(["분해 없음", "원가 항목 합계가 원가 서버 결과와 맞지 않아 싣지 않았습니다 (재검토 사유)"])

    ws = wb.create_sheet("민감도", 2)
    _header(ws, 1, ["가정 변화", "제안가 변화 (USD)", "출처 등급"])
    for x in (ap.sensitivity if ap else []):
        ws.append([x["label"], round(x["price_delta"], 4), x.get("grade", "")])

    ws = wb.create_sheet("검토 결과", 3)
    _header(ws, 1, ["코드", "수준", "내용", "담당자 사유"])
    for f in (ap.findings if ap else []):
        ws.append([f.code, f.level, f.message, f.reason])
    ws.append([])
    ws.append(["자동 검증", "", ", ".join(s.checks_passed) + " 통과", ", ".join(s.checks_failed) + (" 실패" if s.checks_failed else "")])
    for col, w in zip("ABCD", [20, 10, 70, 40]):
        ws.column_dimensions[col].width = w


def build_report(s: AgentSession) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "요약"
    ws["A1"] = "GPU 원가·제안가 견적 근거서"
    ws["A1"].font = Font(bold=True, size=15)
    ws["A2"] = "원가 에이전트 생성 · 입력 가정에 따른 시뮬레이션이며 확정 견적이 아닙니다."
    ws["A2"].font = Font(color="808080")
    sim, a = s.simulation or {}, s.assumptions
    rows = [
        ("청구서", s.draft.file_name if s.draft else ""),
        ("GPU 모델 · 수", f"{a.get('model_name', '')} · {a.get('gpu_count', '')}장"),
        ("GPU·시간당 원가 (USD)", sim.get("hourlyCost")),
        ("제안가 (USD/GPU·시간)", sim.get("candidatePrice")),
        ("마진 방식 · 목표", f"{a.get('pricing_method')} · {a.get('target_rate')}"),
        ("원가 완전성", sim.get("costCompleteness")),
        ("BE 누락 항목", ", ".join(ITEM_KO.get(x, x) for x in sim.get("missingItems", []))),
    ]
    if s.market:
        m = s.market
        rows += [("시장 위치", m.position), ("시장 공개가격 (최저·중앙·최고)",
                 f"{m.min} · {m.median} · {m.max} (표본 {m.count}개, 사업자 {m.providers}곳)")]
    for t in s.terms:
        rows.append((f"{t.years}년 계약가 (할인 {t.discount_rate:.0%})", round(t.price, 4)))
    for i, (k, v) in enumerate(rows, 4):
        ws.cell(i, 1, k).font = Font(bold=True)
        ws.cell(i, 2, v)
    r = len(rows) + 6
    ws.cell(r, 1, "에이전트 판단 요약").font = Font(bold=True, size=12)
    for j, line in enumerate(s.summary, r + 1):
        ws.cell(j, 1, f"• {line}")
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 70

    ws = wb.create_sheet("원가 항목")
    _header(ws, 1, ["원가 항목", "상태", "금액", "통화", "기간", "포함된 상위 항목", "출처"])
    for i, it in enumerate(s.final_items, 2):
        src = "청구서 " + ", ".join(it.source_refs) if it.source_refs else ("담당자 입력" if it.amount is not None else "")
        ws.append([ITEM_KO.get(it.item_type.value, it.item_type.value), it.status.value, it.amount,
                   it.currency.value if it.currency else "", it.period.value if it.period else "",
                   ITEM_KO.get(it.included_in.value, "") if it.included_in else "", src])
    for col, w in zip("ABCDEFG", [22, 18, 16, 8, 10, 18, 30]):
        ws.column_dimensions[col].width = w

    ws = wb.create_sheet("가정값")
    _header(ws, 1, ["가정", "값", "근거", "출처 등급"])
    for k, label in [("gpu_count", "GPU 수"), ("acquisition_amount", "서버·GPU 취득비"), ("acquisition_currency", "취득비 통화"),
                     ("depreciation_years", "감가상각 연수"), ("utilization", "유료 가동률"), ("fx_krw_per_usd", "환율 (원/달러)"),
                     ("pricing_method", "마진 방식"), ("target_rate", "목표 마진"),
                     ("inhouse_annual", "사내 인건비·판관비 (연간)")]:
        ws.append([label, a.get(k), a.get("sources", {}).get(k, "담당자 입력"), a.get("grades", {}).get(k, "")])
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["C"].width = 60

    ws = wb.create_sheet("청구서 해석")
    _header(ws, 1, ["구분", "내용", "적용", "해제 시 연간 원가", "근거"])
    d = s.draft
    interps = d.interpretations if d else []
    bench = (d.bench if d else []) + list(s.checks)
    for i in interps:
        ws.append(["해석", i.message, "적용" if i.applied else "해제",
                   f"{i.impact:.3g}배" if i.impact is not None else "-", ", ".join(i.refs)])
    for b in bench:
        ws.append(["기준선 검사", b.message, "-", f"{b.low:,.2f}~{b.high:,.2f}", b.source])
    if not interps and not bench:
        ws.append(["해석 변경 없음", "청구서를 기본 규칙대로 읽었습니다 (단위·금액 열·청구 기간 변경 없음).", "", "", ""])
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 90
    ws.column_dimensions["E"].width = 30

    if s.market:
        ws = wb.create_sheet("시장 비교")
        _header(ws, 1, ["사업자", "지역", "GPU 수", "USD/GPU·시간", "수집 시각"])
        for x in s.market.samples:
            ws.append([x["provider"], x["region"], x["gpu_count"], x["price"], x["collected_at"]])
        ws.column_dimensions["A"].width = 16
        ws.column_dimensions["E"].width = 26

    _approval_sheets(wb, s)

    ws = wb.create_sheet("진행 기록")
    _header(ws, 1, ["시각", "단계", "구분", "내용"])
    for e in s.events:
        ws.append([e.at, e.step.value, e.kind, e.message])
    ws.column_dimensions["D"].width = 100
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
