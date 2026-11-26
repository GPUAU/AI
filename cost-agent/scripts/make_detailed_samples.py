"""AI Factory 규모(수백 kW~1MW)의 가상 IDC 청구서 5종과 정답표를 만든다.

기존 samples/(a~i)는 랙 몇 개 규모의 단순 양식이다. 여기서는 실제 청구서에서 자주 보이는
열 구조(공급가액·부가세·합계 열, MRC/NRC 열, 일할 계산, 천원 단위, 분기 청구)와
원가를 틀리게 만드는 함정을 일부러 넣는다. 사업자 이름·금액은 모두 가상이다.

    python scripts/make_detailed_samples.py   → samples-detailed/*.xlsx, answer_key.json
"""

from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

OUT = Path(__file__).resolve().parent.parent / "samples-detailed"

BOLD = Font(bold=True)
TITLE = Font(bold=True, size=14)
HEAD_FILL = PatternFill("solid", fgColor="E7ECF2")
THIN = Side(style="thin", color="B7C0CC")
BOX = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)


def _head(ws, row: int, ncols: int) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = BOLD
        cell.fill = HEAD_FILL
        cell.border = BOX
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _widths(ws, widths: list[int]) -> None:
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(ord("A") + i)].width = w


def _money(ws, row: int, cols: list[int], fmt: str = "#,##0") -> None:
    for c in cols:
        ws.cell(row=row, column=c).number_format = fmt


def line(sheet, label, role, item_type=None, period=None, amount=None, note=""):
    """정답표의 한 줄. amount는 청구서에 적힌 그대로(단위 환산 전)."""
    return {"sheet": sheet, "label": label, "role": role, "item_type": item_type,
            "period": period, "amount": amount, "note": note}


# --------------------------------------------------------------------------
# R1. 국내 대형 IDC · 1MW 고밀도 구역 · 공급가액/부가세/합계 열이 줄마다 있음
# --------------------------------------------------------------------------
def bill_r1(path: Path) -> dict:
    wb = Workbook()
    ws = wb.active
    ws.title = "청구서"
    ws.append(["가상IDC 한누리 — 데이터센터 서비스 이용요금 청구서"])
    ws["A1"].font = TITLE
    ws.append(["고객사", "가상AI팩토리(주)", "", "계약번호", "HN-AIF-2026-007", "", "청구월", "2026년 10월분"])
    ws.append(["이용기간", "2026-10-01 ~ 2026-10-31", "", "계약전력", "1,000kW", "", "랙", "40kW급 25랙"])
    ws.append(["반입 장비", "GPU 서버 63대 (서버당 GPU 8장)", "", "금액 단위", "원 (VAT 별도 표기)"])
    ws.append([])
    head = ["No", "구분", "청구항목", "산정기준", "수량", "단위", "단가(원)", "공급가액(원)", "부가세(원)", "합계금액(원)"]
    ws.append(head)
    _head(ws, ws.max_row, len(head))
    it_kwh = 1000 * 0.80 * 744  # 계약 1MW, 평균 부하 80%, 10월 744시간
    common_kwh = it_kwh * 0.30  # PUE 1.3 → 공용설비 30%
    rows = [
        ("상면", "고밀도 랙 상면료 (40kW급)", "계약 25랙", 25, "랙", 4_200_000, "RACK_FACILITY", ""),
        ("상면", "전력용량 기반 상면 이용료", "계약전력 1,000kW", 1000, "kW", 245_000, "RACK_FACILITY",
         "kW당 받는 공간·용량 요금. 이름에 '전력'이 있지만 전기 사용료가 아님"),
        ("전력", "전력량 요금 (IT 부하 계량)", "당월 검침 IT 계량기", it_kwh, "kWh", 183, "ELECTRICITY", ""),
        ("전력", "공용설비 전력 (냉방·UPS 손실)", "IT 사용량 × 0.30 (PUE 1.3)", common_kwh, "kWh", 183, "ELECTRICITY",
         "PUE 할증분. 빼면 전기료를 23% 낮게 봄"),
        ("전력", "기후환경·연료비 조정액", "총 사용량 × 9원", it_kwh + common_kwh, "kWh", 9, "ELECTRICITY", ""),
        ("네트워크", "인터넷 전용회선 100Gbps", "이중화 2회선", 2, "회선", 9_500_000, "NETWORK_STORAGE", ""),
        ("네트워크", "크로스커넥트 (광, SMF)", "12포트", 12, "포트", 300_000, "NETWORK_STORAGE", ""),
        ("네트워크", "공인 IP 블록 (/26)", "1블록", 1, "블록", 400_000, "NETWORK_STORAGE", ""),
        ("운영", "24x7 관제·원격지원 (월 40시간 포함)", "월 정액", 1, "식", 12_000_000, "OPERATIONS", ""),
        ("운영", "원격지원 초과 사용", "초과 12시간", 12, "시간", 90_000, "OPERATIONS", ""),
        ("운영", "출입·물리보안 서비스", "월 정액", 1, "식", 2_500_000, "OPERATIONS", ""),
    ]
    key_lines = []
    supply_sum = 0
    for i, (grp, name, basis, qty, unit, price, item, note) in enumerate(rows, start=1):
        supply = round(qty * price)
        vat = round(supply * 0.1)
        supply_sum += supply
        ws.append([i, grp, name, basis, qty, unit, price, supply, vat, supply + vat])
        _money(ws, ws.max_row, [5, 7, 8, 9, 10])
        key_lines.append(line("청구서", name, "COST", item, "MONTH", supply, note))
    vat_sum = round(supply_sum * 0.1)
    ws.append([])
    for label, val, role in [("공급가액 합계", supply_sum, "SUBTOTAL"), ("부가세 합계", vat_sum, "TAX"),
                             ("당월 청구금액", supply_sum + vat_sum, "TOTAL"), ("전월 미납액", 0, "IGNORE"),
                             ("총 납부하실 금액", supply_sum + vat_sum, "TOTAL")]:
        ws.append(["", "", label, "", "", "", "", "", "", val])
        ws.cell(row=ws.max_row, column=3).font = BOLD
        _money(ws, ws.max_row, [10])
        key_lines.append(line("청구서", label, role, amount=val))
    ws.append([])
    ws.append(["※ 납부기한 2026-11-20 · 가상은행 000-0000-0000 · 문의 정산팀 (가상 번호)"])
    _widths(ws, [5, 9, 34, 26, 10, 7, 11, 15, 13, 15])
    wb.save(path)
    annual = {}
    for k in key_lines:
        if k["role"] == "COST":
            annual[k["item_type"]] = annual.get(k["item_type"], 0) + k["amount"] * 12
    return {
        "title": "국내 대형 IDC · 1MW 고밀도 구역 월 청구서",
        "layout": "머리글 블록(계약전력·랙·장비) + 세로 표. 줄마다 공급가액·부가세·합계 세 금액 열",
        "currency": "KRW", "period": "MONTH", "unit_multiplier": 1, "annualize": "× 12",
        "contract": {"power_kw": 1000, "racks": 25, "gpus": 504},
        "lines": key_lines, "expected_annual": annual,
        "expected_total_check": {"cost_sum": supply_sum, "matches": "공급가액 합계"},
        "traps": [
            "금액 열이 3개(공급가액·부가세·합계). 합계 열을 고르면 원가가 10% 높게 잡힘",
            "수량 열에 큰 숫자(744,000kWh)가 있어 금액 열로 착각하기 쉬움",
            "'전력용량 기반 상면 이용료'는 이름에 '전력'이 있지만 상면 요금. 전기료로 분류해도 총액은 맞음 → 총액 대조로는 못 잡는 오분류",
            "총액 줄이 2개(당월 청구금액, 총 납부하실 금액). 전월 미납액이 생기면 둘이 달라짐",
            "계약전력 1,000kW ÷ GPU 504장 ≈ 2kW/GPU → 담당자가 입력하는 GPU 수와 교차 확인 가능",
        ],
    }


# --------------------------------------------------------------------------
# R2. 요약·상세·전력사용량 3시트 · 천원 단위 · 그룹 소계 · 일회성 공사비
# --------------------------------------------------------------------------
def bill_r2(path: Path) -> dict:
    wb = Workbook()
    summary = wb.active
    summary.title = "요약"
    detail = wb.create_sheet("상세내역")
    usage = wb.create_sheet("전력사용량")

    kw = 512
    it_kwh = kw * 0.78 * 744
    common_kwh = it_kwh * 0.30
    total_kwh = it_kwh + common_kwh
    groups = [
        ("공간", [("전용구역 임대료 (케이지 210㎡)", "월 정액", 120_000, "RENT", "MONTH", ""),
                  ("랙·시설 이용료 (40kW급 13랙)", "13랙 × 4,553.8", 59_200, "RACK_FACILITY", "MONTH", "")]),
        ("전력", [("전력 기본요금", f"계약 {kw}kW × 8.32", round(kw * 8.32), "ELECTRICITY", "MONTH", ""),
                  ("전력 사용요금", f"{total_kwh:,.0f}kWh × 0.183", round(total_kwh * 0.183), "ELECTRICITY", "MONTH",
                   "IT + 공용설비 합산"),
                  ("기후환경요금", f"{total_kwh:,.0f}kWh × 0.009", round(total_kwh * 0.009), "ELECTRICITY", "MONTH", "")]),
        ("네트워크", [("IX 접속료 (100G × 2포트)", "월 정액", 16_000, "NETWORK_STORAGE", "MONTH", ""),
                      ("백업 스토리지 (500TB)", "500TB × 15", 7_500, "NETWORK_STORAGE", "MONTH", "")]),
        ("운영", [("시설 유지보수 (UPS·항온항습)", "월 정액", 6_800, "OPERATIONS", "MONTH", ""),
                  ("보안관제", "월 정액", 2_200, "OPERATIONS", "MONTH", "")]),
        ("일회성", [("고밀도 랙 전환 공사 (1회)", "수냉 배관·분전반 증설", 45_000, "ACQUISITION", "ONE_TIME",
                     "1회성. ×12 하면 연 5.4억이 운영비로 잘못 잡힘")]),
    ]
    recurring = sum(a for g, items in groups if g != "일회성" for _, _, a, *_ in items)
    discount = -round(recurring * 0.03)

    detail.append(["가상데이터센터 미르 — 이용요금 상세내역 (2026년 10월분)"])
    detail["A1"].font = TITLE
    detail.append(["(단위: 천원, 부가세 별도)"])
    detail.append([])
    head = ["대분류", "세부 항목", "산정 근거", "금액"]
    detail.append(head)
    _head(detail, detail.max_row, len(head))
    key_lines = []
    supply = 0
    for g, items in groups:
        sub = 0
        for name, basis, amt, item, period, note in items:
            detail.append([g, name, basis, amt])
            _money(detail, detail.max_row, [4])
            key_lines.append(line("상세내역", name, "COST", item, period, amt, note))
            sub += amt
        detail.append(["", f"{g} 소계", "", sub])
        detail.cell(row=detail.max_row, column=2).font = BOLD
        _money(detail, detail.max_row, [4])
        key_lines.append(line("상세내역", f"{g} 소계", "SUBTOTAL", amount=sub,
                              note="그룹 소계. 원가 줄로 세면 그 그룹이 두 번 더해짐"))
        supply += sub
    detail.append(["할인", "장기계약 할인 (정기요금의 3%)", "3년 약정", discount])
    _money(detail, detail.max_row, [4])
    key_lines.append(line("상세내역", "장기계약 할인 (정기요금의 3%)", "DISCOUNT", amount=discount))
    supply += discount
    vat = round(supply * 0.1)
    for label, val, role in [("공급가액", supply, "SUBTOTAL"), ("부가세", vat, "TAX"), ("청구금액", supply + vat, "TOTAL")]:
        detail.append(["", label, "", val])
        detail.cell(row=detail.max_row, column=2).font = BOLD
        _money(detail, detail.max_row, [4])
        key_lines.append(line("상세내역", label, role, amount=val))
    _widths(detail, [10, 34, 30, 14])

    summary.append(["가상데이터센터 미르 이용요금 청구 요약"])
    summary["A1"].font = TITLE
    summary.append(["청구 기간", "2026년 10월분", "", "단위", "천원"])
    summary.append(["계약전력", f"{kw}kW", "", "랙", "13랙"])
    summary.append([])
    summary.append(["항목군", "금액"])
    _head(summary, summary.max_row, 2)
    for g, items in groups:
        summary.append([g, sum(a for _, _, a, *_ in items)])
        _money(summary, summary.max_row, [2])
        key_lines.append(line("요약", g, "IGNORE", amount=sum(a for _, _, a, *_ in items),
                              note="요약 시트의 항목군 합계. 상세와 같이 세면 원가가 두 배"))
    for label, val in [("할인", discount), ("공급가액", supply), ("세액", vat), ("청구금액", supply + vat)]:
        summary.append([label, val])
        _money(summary, summary.max_row, [2])
    _widths(summary, [14, 14, 4, 8, 8])

    usage.append(["10월 일별 전력 사용량 (kWh) — 참고용"])
    usage["A1"].font = BOLD
    head = ["일자", "IT 계량(kWh)", "공용설비(kWh)", "합계(kWh)"]
    usage.append(head)
    _head(usage, usage.max_row, len(head))
    day_it = it_kwh / 31
    for d in range(1, 32):
        wobble = 1 + ((d * 7) % 11 - 5) / 100
        a = round(day_it * wobble)
        b = round(a * 0.30)
        usage.append([f"2026-10-{d:02d}", a, b, a + b])
        _money(usage, usage.max_row, [2, 3, 4])
    usage.append(["합계", "", "", f"=SUM(D3:D33)"])
    _widths(usage, [14, 14, 14, 14])
    wb.save(path)

    annual = {}
    for k in key_lines:
        if k["role"] == "COST":
            mult = 1000 * (1 if k["period"] == "ONE_TIME" else 12)
            annual[k["item_type"]] = annual.get(k["item_type"], 0) + k["amount"] * mult
    return {
        "title": "요약·상세·전력사용량 3시트 · 천원 단위",
        "layout": "요약 시트(항목군 합계) + 상세 시트(대분류별 소계) + 일별 사용량 시트(31줄 숫자)",
        "currency": "KRW", "period": "MONTH", "unit_multiplier": 1000, "annualize": "정기 × 12, 일회성은 취득비로",
        "contract": {"power_kw": kw, "racks": 13, "gpus": None},
        "lines": key_lines, "expected_annual": annual,
        "expected_total_check": {"cost_sum": supply - discount, "matches": "원가 합계 − 할인 = 공급가액"},
        "traps": [
            "금액 단위가 천원. 머리글의 '(단위: 천원)'을 놓치면 원가가 1/1000로 잡힘 — 총액 대조는 그대로 맞아서 못 잡음",
            "대분류마다 소계 줄이 있음. 소계를 원가로 세면 해당 그룹이 두 번 들어감",
            "요약 시트의 항목군 합계와 상세 시트가 같은 금액을 반복. 둘 다 세면 두 배",
            "전력사용량 시트의 31줄은 kWh 숫자. 금액으로 읽으면 안 됨",
            "고밀도 랙 전환 공사는 1회성 → 취득비(초기 투자)로, ×12 금지",
            "할인은 정기요금 3% 약정 할인 → 계약 기간 내내 적용되는 할인이라 원가에서 빼는 게 맞을 수 있음 (담당자 판단)",
        ],
    }


# --------------------------------------------------------------------------
# R3. 해외 코로케이션 영문 인보이스 · MRC/NRC 두 금액 열 · 이월 잔액 줄
# --------------------------------------------------------------------------
def bill_r3(path: Path) -> dict:
    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice"
    ws.append(["Example Hyperscale Colo LLC — Invoice"])
    ws["A1"].font = TITLE
    ws.append(["Invoice No.", "INV-2026-1043", "", "Invoice Date", "2026-11-01", "", "Currency", "USD"])
    ws.append(["Billing Period", "Oct 1 – Oct 31, 2026", "", "Site", "EX-WEST-2 (example)", "", "Account", "AC-00981"])
    ws.append(["Committed Power", "800 kW (2N)", "", "Cabinets", "20 × 40 kW"])
    ws.append([])
    head = ["Line", "Service ID", "Description", "Qty", "UoM", "Unit Rate", "MRC (USD)", "NRC (USD)"]
    ws.append(head)
    _head(ws, ws.max_row, len(head))
    kwh = 800 * 0.78 * 744 * 1.35
    rows = [
        ("SV-1101", "Private Suite – Space & Power Capacity (per committed kW)", 800, "kW", 135.00, "MRC", "RENT",
         "", ["RACK_FACILITY"]),
        ("SV-1102", "Metered Utility Power (PUE 1.35 applied)", round(kwh), "kWh", 0.078, "MRC", "ELECTRICITY", "", []),
        ("SV-2201", "Cross Connect – Single-mode Fiber", 16, "ea", 300.00, "MRC", "NETWORK_STORAGE", "", []),
        ("SV-2202", "Cross Connect Installation", 16, "ea", 500.00, "NRC", "ACQUISITION",
         "1회성 설치비. MRC 열이 비어 있음", []),
        ("SV-2301", "IP Transit 100G port (40G commit)", 1, "port", 9_500.00, "MRC", "NETWORK_STORAGE", "", []),
        ("SV-3101", "Remote Hands – beyond 10h block", 22, "hr", 175.00, "MRC", "OPERATIONS", "", []),
        ("SV-3102", "Managed Monitoring & Ticketing", 1, "mo", 2_500.00, "MRC", "OPERATIONS", "", []),
        ("SV-4101", "Cage Build-out (liquid-cooling manifold)", 1, "ea", 65_000.00, "NRC", "ACQUISITION",
         "1회성 공사비", []),
    ]
    key_lines = []
    mrc_sum = nrc_sum = 0.0
    for i, (sid, desc, qty, uom, rate, kind, item, note, inc) in enumerate(rows, start=1):
        amt = round(qty * rate, 2)
        mrc, nrc = (amt, None) if kind == "MRC" else (None, amt)
        mrc_sum += mrc or 0
        nrc_sum += nrc or 0
        ws.append([i, sid, desc, qty, uom, rate, mrc, nrc])
        _money(ws, ws.max_row, [4], "#,##0")
        _money(ws, ws.max_row, [6, 7, 8], "#,##0.00")
        k = line("Invoice", desc, "COST", item, "MONTH" if kind == "MRC" else "ONE_TIME", amt, note)
        k["included_items"] = inc
        key_lines.append(k)
    credit = -2_000.00
    ws.append([len(rows) + 1, "CR-0310", "SLA Credit – Oct 3 power event (one-time)", 1, "ea", credit, credit, None])
    _money(ws, ws.max_row, [6, 7], "#,##0.00")
    key_lines.append(line("Invoice", "SLA Credit – Oct 3 power event (one-time)", "DISCOUNT", amount=credit,
                          note="일회성 보상 크레딧. 원가에서 빼면 안 됨"))
    current = round(mrc_sum + nrc_sum + credit, 2)
    ws.append([])
    footer = [("Subtotal – Monthly Recurring", round(mrc_sum + credit, 2), "SUBTOTAL"),
              ("Subtotal – Non-Recurring", round(nrc_sum, 2), "SUBTOTAL"),
              ("Sales Tax (exempt)", 0.0, "TAX"),
              ("Total Current Charges", current, "TOTAL"),
              ("Previous Balance", 184_212.40, "IGNORE"),
              ("Payment Received – Thank you", -184_212.40, "IGNORE"),
              ("Balance Due", current, "TOTAL")]
    for label, val, role in footer:
        ws.append(["", "", label, "", "", "", val])
        ws.cell(row=ws.max_row, column=3).font = BOLD
        _money(ws, ws.max_row, [7], "#,##0.00")
        key_lines.append(line("Invoice", label, role, amount=val,
                              note="이월 잔액·입금 줄은 원가가 아님" if role == "IGNORE" else ""))
    ws.append([])
    ws.append(["Payment terms: Net 30. Wire to Example Bank (fictitious)."])
    _widths(ws, [6, 10, 52, 10, 7, 11, 15, 15])
    wb.save(path)
    annual = {}
    for k in key_lines:
        if k["role"] == "COST":
            mult = 1 if k["period"] == "ONE_TIME" else 12
            annual[k["item_type"]] = round(annual.get(k["item_type"], 0) + k["amount"] * mult, 2)
    return {
        "title": "해외 코로케이션 영문 인보이스 · MRC/NRC",
        "layout": "머리글 블록 + 표. 금액 열이 MRC(월 정기)와 NRC(1회성) 두 개",
        "currency": "USD", "period": "MONTH", "unit_multiplier": 1, "annualize": "MRC × 12, NRC는 취득비로",
        "contract": {"power_kw": 800, "racks": 20, "gpus": None},
        "lines": key_lines, "expected_annual": annual,
        "expected_total_check": {"cost_sum": round(mrc_sum + nrc_sum, 2), "matches": "원가 합계 − 크레딧 = Total Current Charges"},
        "traps": [
            "금액 열이 MRC·NRC 두 개. 열 이름이 아니라 열 위치로 기간(월/1회)을 판단해야 함",
            "Space & Power Capacity는 kW당 공간·용량 요금 (랙·시설 포함), 전기 사용료는 Metered Utility Power에 따로 있음",
            "SLA 크레딧은 일회성 보상. 원가에서 빼면 연간 원가가 $24,000 낮게 잡힘",
            "Previous Balance / Payment Received 줄은 원가가 아님. 더하면 총액 대조가 틀어짐",
            "Unit Rate 열(0.078 등)과 Qty 열(464,000kWh 등)에 숫자가 많아 금액 열 판단이 어려움",
        ],
    }


# --------------------------------------------------------------------------
# R4. 신규 입주 첫 달 · 일할 계산 · 회선은 별도 계약(청구서에 없음)
# --------------------------------------------------------------------------
def bill_r4(path: Path) -> dict:
    wb = Workbook()
    ws = wb.active
    ws.title = "청구서"
    ws.append(["가상IDC 소백 — 신규 입주 첫 달 이용요금 청구서"])
    ws["A1"].font = TITLE
    ws.append(["청구월", "2026년 10월분", "", "서비스 개시일", "2026-10-15"])
    ws.append(["계약전력", "320kW", "", "랙", "40kW급 8랙", "", "금액 단위", "원(VAT 별도)"])
    ws.append([])
    head = ["항목", "월 정액(원)", "이용기간", "이용일수", "당월 청구액(원)", "비고"]
    ws.append(head)
    _head(ws, ws.max_row, len(head))
    days = 17
    kwh = round(320 * 0.75 * 24 * days * 1.3)
    rows = [
        ("랙 상면료 (40kW급 8랙)", 48_000_000, "10/15~10/31", days, round(48_000_000 * days / 31), "일할 계산",
         "RACK_FACILITY", "MONTH", 48_000_000 * 12),
        ("전력 사용료 (검침)", None, "10/15~10/31", days, kwh * 185, f"{kwh:,}kWh × 185원 (PUE 1.3 포함)",
         "ELECTRICITY", "MONTH", round(kwh * 185 / days * 365)),
        ("운영관리비 (관제·유지보수)", 4_000_000, "10/15~10/31", days, round(4_000_000 * days / 31), "일할 계산",
         "OPERATIONS", "MONTH", 4_000_000 * 12),
        ("입주 설치비 (1회)", None, "-", None, 3_000_000, "랙 설치·전원 인입, 1회 청구",
         "ACQUISITION", "ONE_TIME", 3_000_000),
    ]
    key_lines = []
    supply = 0
    for name, monthly, term, d, amt, note, item, period, annual_amt in rows:
        ws.append([name, monthly, term, d, amt, note])
        _money(ws, ws.max_row, [2, 5])
        k = line("청구서", name, "COST", item, period, amt, note)
        k["annual_amount"] = annual_amt
        key_lines.append(k)
        supply += amt
    vat = round(supply * 0.1)
    ws.append([])
    for label, val, role in [("공급가액", supply, "SUBTOTAL"), ("부가세", vat, "TAX"), ("청구금액", supply + vat, "TOTAL")]:
        ws.append([label, "", "", "", val])
        ws.cell(row=ws.max_row, column=1).font = BOLD
        _money(ws, ws.max_row, [5])
        key_lines.append(line("청구서", label, role, amount=val))
    ws.append([])
    ws.append(["※ 인터넷 회선·크로스커넥트는 고객사가 통신사와 직접 계약하여 본 청구서에 포함되지 않습니다."])
    ws.append(["※ 11월분부터는 월 정액 전액이 청구됩니다."])
    _widths(ws, [30, 15, 14, 9, 17, 34])
    wb.save(path)
    annual = {}
    for k in key_lines:
        if k["role"] == "COST":
            annual[k["item_type"]] = annual.get(k["item_type"], 0) + k["annual_amount"]
    return {
        "title": "신규 입주 첫 달 · 일할 계산 · 회선 별도 계약",
        "layout": "월 정액 열과 당월(일할) 청구액 열이 따로 있음. 전력은 월 정액 없이 검침액만",
        "currency": "KRW", "period": "MONTH", "unit_multiplier": 1,
        "annualize": "정액 항목은 '월 정액 × 12', 전력은 17일 사용액 ÷ 17 × 365, 설치비는 취득비로",
        "contract": {"power_kw": 320, "racks": 8, "gpus": None},
        "missing_expected": ["NETWORK_STORAGE"],
        "lines": key_lines, "expected_annual": annual,
        "expected_total_check": {"cost_sum": supply, "matches": "공급가액"},
        "traps": [
            "17일치 일할 금액을 × 12 하면 연간 원가가 약 45% 낮게 잡힘. 월 정액 열을 써야 함",
            "금액 열이 2개(월 정액, 당월 청구액). 총액 대조는 당월 청구액으로만 맞음",
            "네트워크가 청구서에 없음 → '별도 계약' 질문이 떠야 함. 비고에 이유가 적혀 있음",
            "입주 설치비는 1회성 → 취득비",
        ],
    }


# --------------------------------------------------------------------------
# R5. 분기 청구 · 통화 표시 없음 · 2줄 병합 머리글 · 월별 전력 줄
# --------------------------------------------------------------------------
def bill_r5(path: Path) -> dict:
    wb = Workbook()
    ws = wb.active
    ws.title = "3분기"
    ws.append(["가상센터 다온 — 2026년 3분기(7~9월) 이용료 청구"])
    ws["A1"].font = TITLE
    ws.append(["계약전력 400kW · 40kW급 10랙 · 운영은 고객사 자체 인력"])
    ws.append([])
    ws.append(["항목", "산정", "금액", "", ""])
    ws.append(["", "", "공급가액", "부가세", "합계"])
    ws.merge_cells("A4:A5")
    ws.merge_cells("B4:B5")
    ws.merge_cells("C4:E4")
    _head(ws, 4, 5)
    _head(ws, 5, 5)

    def kwh(hours: int) -> int:
        return round(400 * 0.8 * hours * 1.3)

    rows = [
        ("상면 이용료 (10랙)", "3개월 × 140,000,000", 420_000_000, "RACK_FACILITY"),
        ("전력 기본요금", "400kW × 8,320 × 3개월", 400 * 8_320 * 3, "ELECTRICITY"),
        ("전력 사용요금 7월", f"{kwh(744):,}kWh × 190", kwh(744) * 190, "ELECTRICITY"),
        ("전력 사용요금 8월", f"{kwh(744):,}kWh × 195", kwh(744) * 195, "ELECTRICITY"),
        ("전력 사용요금 9월", f"{kwh(720):,}kWh × 180", kwh(720) * 180, "ELECTRICITY"),
        ("회선 이용료 (10G × 2)", "3개월 × 5,000,000", 15_000_000, "NETWORK_STORAGE"),
    ]
    key_lines = []
    supply = 0
    for name, basis, amt, item in rows:
        vat = round(amt * 0.1)
        ws.append([name, basis, amt, vat, amt + vat])
        _money(ws, ws.max_row, [3, 4, 5])
        key_lines.append(line("3분기", name, "COST", item, "QUARTER", amt))
        supply += amt
    vat = round(supply * 0.1)
    ws.append(["합계", "", supply, vat, supply + vat])
    ws.cell(row=ws.max_row, column=1).font = BOLD
    _money(ws, ws.max_row, [3, 4, 5])
    key_lines.append(line("3분기", "합계", "TOTAL", amount=supply,
                          note="한 줄에 공급가액·부가세·합계가 같이 있음"))
    ws.append([])
    ws.append(["※ 7~8월은 하계 요금 적용 · 분기 종료 후 일괄 청구"])
    _widths(ws, [26, 26, 16, 14, 16])
    wb.save(path)
    annual = {}
    for k in key_lines:
        if k["role"] == "COST":
            annual[k["item_type"]] = annual.get(k["item_type"], 0) + k["amount"] * 4
    return {
        "title": "분기 청구 · 통화 표시 없음 · 병합 머리글",
        "layout": "2줄 병합 머리글(금액 → 공급가액/부가세/합계), 월별 전력 줄이 따로",
        "currency": "KRW", "period": "QUARTER", "unit_multiplier": 1, "annualize": "× 4",
        "contract": {"power_kw": 400, "racks": 10, "gpus": None},
        "missing_expected": ["OPERATIONS"],
        "lines": key_lines, "expected_annual": annual,
        "expected_total_check": {"cost_sum": supply, "matches": "합계(공급가액 열)"},
        "traps": [
            "분기 청구. 지금 코드에는 분기(QUARTER) 기간이 없어 월로 보면 × 12 → 원가가 3배",
            "통화 기호가 전혀 없음 → 통화 질문이 떠야 함",
            "병합 머리글 2줄. '금액' 아래 공급가액·부가세·합계 중 공급가액 열을 골라야 함",
            "전력이 월별 3줄 + 기본요금 1줄로 쪼개져 있음 → 한 항목으로 합쳐야 함. 하계 요금으로 7~8월이 높아 한 달만 보고 × 12 하면 틀림",
            "운영비 없음(자체 인력) → '이번엔 제외'가 아니라 사내 인건비를 따로 넣어야 하는지 묻는 게 맞음",
        ],
    }


BUILDERS = {
    "bill_r1_kr_metered_krw.xlsx": bill_r1,
    "bill_r2_multisheet_thousand_krw.xlsx": bill_r2,
    "bill_r3_colo_mrc_nrc_usd.xlsx": bill_r3,
    "bill_r4_prorated_first_month_krw.xlsx": bill_r4,
    "bill_r5_quarterly_no_currency.xlsx": bill_r5,
}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    key = {}
    for name, build in BUILDERS.items():
        key[name] = build(OUT / name)
        print(f"wrote {OUT / name}")
    (OUT / "answer_key.json").write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT / 'answer_key.json'}")


if __name__ == "__main__":
    main()
