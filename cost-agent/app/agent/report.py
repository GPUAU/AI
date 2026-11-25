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
    _header(ws, 1, ["가정", "값", "근거"])
    for k, label in [("gpu_count", "GPU 수"), ("acquisition_amount", "서버·GPU 취득비"), ("acquisition_currency", "취득비 통화"),
                     ("depreciation_years", "감가상각 연수"), ("utilization", "유료 가동률"), ("fx_krw_per_usd", "환율 (원/달러)"),
                     ("pricing_method", "마진 방식"), ("target_rate", "목표 마진")]:
        ws.append([label, a.get(k), a.get("sources", {}).get(k, "담당자 입력")])
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["C"].width = 60

    if s.market:
        ws = wb.create_sheet("시장 비교")
        _header(ws, 1, ["사업자", "지역", "GPU 수", "USD/GPU·시간", "수집 시각"])
        for x in s.market.samples:
            ws.append([x["provider"], x["region"], x["gpu_count"], x["price"], x["collected_at"]])
        ws.column_dimensions["A"].width = 16
        ws.column_dimensions["E"].width = 26

    ws = wb.create_sheet("진행 기록")
    _header(ws, 1, ["시각", "단계", "구분", "내용"])
    for e in s.events:
        ws.append([e.at, e.step.value, e.kind, e.message])
    ws.column_dimensions["D"].width = 100
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
