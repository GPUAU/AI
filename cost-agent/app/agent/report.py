"""견적 근거서 엑셀. 받은 사람이 첫 시트에서 '결론과 확인할 점'을 보고, 뒤 시트에서 근거를 따라가게 한다.

시트 5장: 요약 · 원가 근거 · 원가 구성·민감도 · 시장 비교 · 부록
- 숫자는 세션 값(원가 서버가 계산한 값)을 그대로 쓴다. 원가 대비 %·합계·비중처럼 표 안에서 셀 수 있는 것만 수식으로 둔다.
- 코드값(USER_INPUT, REVENUE_MARGIN, PLAN_MISMATCH …)은 한글로 바꾸고, 금액·비율에는 서식을 건다.
- 모든 시트 머리글에 '내부용 · 대외 반출 금지', A4 너비에 맞춰 인쇄.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone

from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.workbook.properties import CalcProperties
from openpyxl.worksheet.page import PageMargins

from .models import STEP_TITLES, AgentSession, Step

FONT = "Arial"
INK, MUTED = "1F242B", "6B7280"
RED, RED_BG = "C0122F", "FDECEE"
AMB, AMB_BG = "A65A00", "FFF4E5"
OK, OK_BG = "16784A", "EEF8F1"
HEAD_BG, LINE = "2B2F36", "D5D9DF"
LINE_SIDE = Side(style="thin", color=LINE)

KRW = '#,##0"원";-#,##0"원";"-"'
USD0 = '"$"#,##0;-"$"#,##0;"-"'
USD2 = '"$"#,##0.00;-"$"#,##0.00;"-"'
USD4 = '"$"#,##0.0000;-"$"#,##0.0000;"-"'
PCT0 = '0%;-0%;"-"'
PCT1 = '0.0%;-0.0%;"-"'
NUM = '#,##0;-#,##0;"-"'

ITEM_KO = {
    "ACQUISITION": "서버·GPU 취득비", "GPU": "GPU 임차", "MEMORY": "별도 메모리", "NETWORK_STORAGE": "네트워크·스토리지",
    "RACK_FACILITY": "랙·전원·냉각 설비", "RENT": "임차료·코로케이션", "OPERATIONS": "운영·유지보수",
    "FINANCING": "조달 금리", "ELECTRICITY": "전기료", "OTHER": "기타",
}
ITEM_STATUS_KO = {"USER_INPUT": "반영", "PUBLIC_SOURCE": "공개 자료", "NOT_INCLUDED": "제외", "INCLUDED_IN_OTHER": "다른 요금에 포함"}
PERIOD_KO = {"YEAR": "연간", "MONTH": "월", "ONE_TIME": "1회"}
METHOD_KO = {"REVENUE_MARGIN": "매출 기준 이익률", "MARKUP": "원가 가산율", "COST_PLUS": "원가 가산율"}
CELL_STATUS_KO = {"filled": "채움", "included": "다른 요금에 포함", "excluded": "제외", "empty": "빈칸", "conflict": "충돌"}
KIND_KO = {"think": "판단", "tool": "도구", "ask": "질문", "answer": "답변", "result": "결과", "warn": "주의"}
COMPLETENESS_KO = {"COMPLETE": "완전", "INCOMPLETE": "불완전 (빠진 항목 있음)"}
LEVEL_STYLE = {"재검토": (RED, RED_BG), "조건부": (AMB, AMB_BG), "참고": (MUTED, "F3F4F6")}
VERDICT_STYLE = {"재검토": (RED, RED_BG), "조건부": (AMB, AMB_BG), "승인 권고": (OK, OK_BG)}

SHEETS = ["요약", "원가 근거", "원가 구성·민감도", "시장 비교", "부록"]


# ------------------------------------------------------------------ 서식 도우미
def _f(size=10, bold=False, color=INK, italic=False) -> Font:
    return Font(name=FONT, size=size, bold=bold, color=color, italic=italic)


def _put(ws, row, col, value, fmt=None, font=None, fill=None, wrap=False, line=False):
    c = ws.cell(row, col, value)
    c.font = font or _f()
    if fmt:
        c.number_format = fmt
    if fill:
        c.fill = PatternFill("solid", fgColor=fill)
    c.alignment = Alignment(wrap_text=wrap, vertical="top")
    if line:
        c.border = Border(bottom=LINE_SIDE)
    return c


def _header(ws, row, labels):
    for i, v in enumerate(labels, 1):
        c = ws.cell(row, i, v)
        c.font, c.fill = _f(9, True, "FFFFFF"), PatternFill("solid", fgColor=HEAD_BG)
        c.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 20


def _line(ws, row, ncol):
    for col in range(1, ncol + 1):
        ws.cell(row, col).border = Border(bottom=LINE_SIDE)


def _section(ws, row, text):
    _put(ws, row, 1, text, font=_f(11, True))


def _sheet(wb, name, title, sub, widths, first=False):
    ws = wb.active if first else wb.create_sheet(name)
    ws.title = name
    _put(ws, 1, 1, title, font=_f(15, True))
    _put(ws, 2, 1, sub, font=_f(9, color=MUTED))
    ws.oddHeader.right.text, ws.oddHeader.right.size = "내부용 · 대외 반출 금지", 8
    ws.oddFooter.center.text = "&P / &N"
    ws.sheet_view.showGridLines = False
    ws.page_margins = PageMargins(left=0.5, right=0.5, top=0.7, bottom=0.6)
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    return ws


def _money_fmt(currency: str | None) -> str:
    return KRW if currency == "KRW" else USD0


def _kst(iso: str) -> str:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    return t.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")


def _step_ko(step) -> str:
    try:
        return STEP_TITLES.get(Step(step), str(step))
    except ValueError:
        return str(step)


def _term_label(key: str) -> str:
    return "제안가" if key == "candidate" else key.replace("y", "년 계약가")


def summary_tone(line: str, s: AgentSession) -> str:
    """판단 요약 한 줄의 색: bad(손실·재검토·불완전 원가) · warn(확인 필요). FE(cost-agent-ui.mjs summaryTone)와 같은 규칙."""
    cost = float((s.simulation or {}).get("hourlyCost") or 0)
    if m := re.match(r"^(\d+)년 계약가 \$([\d.]+)", line):
        return "bad" if float(m.group(2)) < cost else ""
    if m := re.search(r"자동 검증 (\d+)/(\d+)", line):
        return "bad" if int(m.group(1)) < int(m.group(2)) else ""
    if re.search(r"승인 검토 판정: 재검토|불완전 원가|손실", line):
        return "bad"
    if re.search(r"승인 검토 판정: 조건부|불일치|빠져 있습니다|예시값|검산과 차이", line):
        return "warn"
    mk, cand = s.market, float((s.simulation or {}).get("candidatePrice") or 0)
    if line.startswith("시장 공개가격") and mk and mk.count and mk.min is not None and (cand < mk.min or cand > (mk.max or cand)):
        return "warn"
    return ""


# ------------------------------------------------------------------ 1. 요약
def _summary(wb, s: AgentSession) -> None:
    from . import verify as V
    a, r, ap = s.assumptions or {}, s.simulation or {}, s.approval
    source = f"부서 문서 {len(s.docs)}개" if s.sheet is not None and s.docs else f"청구서 {s.draft.file_name if s.draft else ''}"
    ws = _sheet(wb, "요약", "GPU 원가·제안가 견적 근거서",
                f"{a.get('model_name', '')} {a.get('gpu_count', '')}장 · {source} · 입력 가정에 따른 시뮬레이션이며 확정 견적이 아닙니다.",
                {"A": 26, "B": 58, "C": 16, "D": 20, "E": 22}, first=True)

    verdict = ap.verdict if ap else "검토 전"
    color, bg = VERDICT_STYLE.get(verdict, (MUTED, "F3F4F6"))
    _put(ws, 4, 1, "권고", font=_f(10, True, MUTED))
    _put(ws, 4, 2, verdict, font=_f(18, True, color), fill=bg)
    note = {"재검토": "재검토 사유가 1개 이상이면 재검토. 사유를 적어도 판정은 바뀌지 않습니다.",
            "조건부": "조건부 사유는 아래 진행 사유와 함께 승인 보고서에 남습니다.",
            "승인 권고": "확인할 점이 없습니다."}.get(verdict, "승인 검토를 마치지 않은 세션입니다.")
    _put(ws, 4, 3, note, font=_f(9, color=MUTED), wrap=True)
    ws.merge_cells("C4:E4")
    ws.row_dimensions[4].height = 34

    # 제안 요금: 값은 원가 서버 결과, '원가 대비'와 '여유'만 수식
    util = float(a.get("utilization") or 0)
    _section(ws, 6, "제안 요금")
    _header(ws, 7, ["", "USD/GPU·시간", "원가 대비", "손익분기 가동률", f"가정 가동률({util:.0%})과 여유"])
    be = ap.breakeven if ap else {}
    rows = [("원가 (최저 수용가)", r.get("hourlyCost"), None),
            (f"제안가 · {METHOD_KO.get(a.get('pricing_method'), a.get('pricing_method') or '')} {float(a.get('target_rate') or 0):.0%}",
             r.get("candidatePrice"), be.get("candidate"))]
    rows += [(f"{t.years}년 계약가 · 할인 {t.discount_rate:.0%}", t.price, be.get(f"{t.years}y")) for t in s.terms]
    for i, (label, price, b) in enumerate(rows):
        row = 8 + i
        _put(ws, row, 1, label, font=_f(bold=i == 1))
        _put(ws, row, 2, float(price) if price is not None else None, USD4, _f(bold=i == 1))
        if i:
            _put(ws, row, 3, f"=IF($B$8=0,0,B{row}/$B$8-1)", '+0.0%;-0.0%;0.0%')
            if b is not None:
                _put(ws, row, 4, round(b, 4), PCT1)
                _put(ws, row, 5, f"=({util}-D{row})*100", '+0.0"%p";-0.0"%p";"0.0%p"')
        _line(ws, row, 5)
    last = 8 + len(rows) - 1
    if last > 8:
        ws.conditional_formatting.add(f"A9:E{last}", FormulaRule(formula=["$C9<0"], font=_f(bold=True, color=RED),
                                                                 fill=PatternFill("solid", fgColor=RED_BG)))
        ws.conditional_formatting.add(f"E9:E{last}", FormulaRule(formula=["AND($C9>=0,$E9<10)"], font=_f(bold=True, color=AMB)))
    row = last + 1
    _put(ws, row, 1, f"원가 완전성: {COMPLETENESS_KO.get(r.get('costCompleteness'), r.get('costCompleteness') or '-')}"
         + (f" · 원가 서버 미반영: {', '.join(ITEM_KO.get(x, x) for x in r.get('missingItems', []))}" if r.get("missingItems") else ""),
         font=_f(9, color=MUTED))

    # 확인할 점: 승인 검토 발견 사항 + 그 밖의 리스크
    row += 2
    _section(ws, row, "확인할 점")
    _header(ws, row + 1, ["등급", "내용", "관련 단계", "진행 사유", "코드"])
    row += 2
    found = list(ap.findings) if ap else []
    listed = {f.message for f in found}
    extra = [x for x in V.risks(s) if x not in listed and not any(x.startswith(m.split(" — ")[0]) for m in listed)]
    if not found and not extra:
        _put(ws, row, 1, "없음", font=_f(color=OK))
        row += 1
    for f in found:
        color, bg = LEVEL_STYLE.get(f.level, LEVEL_STYLE["참고"])
        _put(ws, row, 1, f.level, font=_f(9, True, color), fill=bg)
        _put(ws, row, 2, f.message, font=_f(bold=f.level == "재검토", color=RED if f.level == "재검토" else INK), wrap=True)
        _put(ws, row, 3, _step_ko(f.fix_step) if f.fix_step else "-")
        _put(ws, row, 4, f.reason or "", font=_f(color=MUTED), wrap=True)
        _put(ws, row, 5, f.code, font=_f(8, color=MUTED))
        _line(ws, row, 5)
        row += 1
    for x in extra:
        _put(ws, row, 1, "참고", font=_f(9, True, MUTED), fill="F3F4F6")
        _put(ws, row, 2, x, wrap=True)
        _line(ws, row, 5)
        row += 1
    passed, failed = s.checks_passed, s.checks_failed
    _put(ws, row, 1, f"자동 검증 {len(passed)}/{len(V.CHECKS)} 통과" + (f" · 실패: {', '.join(failed)}" if failed else ""),
         font=_f(9, bold=bool(failed), color=RED if failed else MUTED))

    # 시장 위치
    m = s.market
    if m and m.count:
        row += 2
        _section(ws, row, "시장 위치")
        _header(ws, row + 1, ["", "최저", "중앙", "최고", "표본"])
        _put(ws, row + 2, 1, f"{m.model_name} 온디맨드 공개가")
        for col, v in zip((2, 3, 4), (m.min, m.median, m.max)):
            _put(ws, row + 2, col, v, USD2)
        _put(ws, row + 2, 5, f"{m.count}개 · 사업자 {m.providers}곳")
        _line(ws, row + 2, 5)
        _put(ws, row + 3, 1, "제안가 위치")
        cand = float(r.get("candidatePrice") or 0)
        out = m.min is not None and m.max is not None and (cand < m.min or cand > m.max)
        _put(ws, row + 3, 2, m.position, font=_f(bold=True, color=AMB if out else INK))
        row += 3

    # 에이전트 판단 요약
    row += 2
    _section(ws, row, "에이전트 판단 요약")
    for line in s.summary:
        row += 1
        tone = summary_tone(line, s)
        _put(ws, row, 1, ("● " if tone else "· ") + line,
             font=_f(bold=tone == "bad", color=RED if tone == "bad" else AMB if tone == "warn" else INK))
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
    row += 2
    _put(ws, row, 1, "원가 계산은 원가 서버(BE) 공식, 문장은 코드 템플릿입니다 (LLM 서술 없음).", font=_f(8, color=MUTED))
    ws.print_area = f"A1:E{row}"


# ------------------------------------------------------------------ 2. 원가 근거
def _evidence(wb, s: AgentSession) -> None:
    a = s.assumptions or {}
    multi = s.sheet is not None
    ws = _sheet(wb, "원가 근거", "원가 근거",
                "부서 문서에서 채운 원가 기준표와 가정값. 칸마다 출처 문서·위치를 남깁니다." if multi
                else f"청구서 {s.draft.file_name if s.draft else ''}에서 읽은 원가 항목과 가정값.",
                {"A": 30, "B": 18, "C": 10, "D": 18, "E": 62, "F": 12, "G": 12, "H": 50})
    row = 4
    if multi:
        from ..costsheet import FIELDS
        _section(ws, row, "원가 기준표")
        _header(ws, row + 1, ["칸", "값", "단위", "상태", "출처 (문서 · 쪽 · 행 · 열)", "교차 확인 수", "담당자 수정", "메모"])
        row += 2
        for fd in FIELDS:
            c = s.sheet.cells[fd.key]
            if c.status == "empty" and fd.kind != "cost":
                continue
            where = " / ".join(f"{x.doc} · {x.page or '-'}쪽 · {x.row}행 · {x.col}열" if x.doc != "담당자 입력" else x.doc
                               for x in c.sources)
            docs = {x.doc for x in c.sources if x.doc != "담당자 입력"}
            value, fmt = c.value, None
            if c.unit == "원":
                fmt = KRW
            elif c.unit == "USD":
                fmt = USD2
            elif c.unit == "%":
                fmt = PCT0
            elif c.unit == "method":
                value = METHOD_KO.get(str(value), value)
            elif isinstance(value, (int, float)):
                fmt = '#,##0.##'
            _put(ws, row, 1, fd.label)
            _put(ws, row, 2, value, fmt)
            _put(ws, row, 3, {"%": "", "method": "", "model": "", "KRW/USD": "원/달러"}.get(c.unit, c.unit or ""))
            st = CELL_STATUS_KO.get(c.status, c.status)
            _put(ws, row, 4, st, font=_f(color=RED if c.status == "conflict" else MUTED if c.status in ("empty", "excluded") else INK))
            _put(ws, row, 5, where, wrap=True)
            _put(ws, row, 6, len(docs) if len(docs) > 1 else None, NUM)
            _put(ws, row, 7, "예" if fd.key in s.sheet_answers else "")
            _put(ws, row, 8, c.note or "", font=_f(9, color=MUTED), wrap=True)
            _line(ws, row, 8)
            row += 1
        for n in s.sheet.notices:
            _put(ws, row, 1, "알림", font=_f(bold=True, color=AMB))
            _put(ws, row, 2, n, wrap=True)
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=8)
            row += 1
    else:
        _section(ws, row, "청구서 원가 항목")
        _header(ws, row + 1, ["원가 항목", "금액", "통화", "상태", "출처", "기간", "포함된 상위 항목", ""])
        row += 2
        first = row
        for it in s.final_items:
            cur = it.currency.value if it.currency else ""
            src = (f"{s.draft.file_name if s.draft else '청구서'} · " + ", ".join(ref.split("!")[-1] + "행" for ref in it.source_refs)
                   if it.source_refs else ("담당자 입력" if it.amount is not None else ""))
            _put(ws, row, 1, ITEM_KO.get(it.item_type.value, it.item_type.value))
            _put(ws, row, 2, it.amount, _money_fmt(cur))
            _put(ws, row, 3, cur)
            status = ITEM_STATUS_KO.get(it.status.value, it.status.value)
            _put(ws, row, 4, status, font=_f(color=INK if it.status.value == "USER_INPUT" else MUTED))
            _put(ws, row, 5, src, wrap=True)
            _put(ws, row, 6, PERIOD_KO.get(it.period.value, it.period.value) if it.period else "")
            _put(ws, row, 7, ITEM_KO.get(it.included_in.value, "") if it.included_in else "")
            _line(ws, row, 7)
            row += 1
        currencies = {it.currency.value for it in s.final_items if it.amount is not None and it.currency}
        if len(currencies) == 1 and row > first:
            _put(ws, row, 1, "합계", font=_f(bold=True))
            _put(ws, row, 2, f"=SUM(B{first}:B{row - 1})", _money_fmt(currencies.pop()), _f(bold=True))
            row += 1
        rec = s.draft.reconciliation if s.draft else None
        if rec is not None:
            text = ("청구서 총액과 일치" if rec.matched else "청구서 총액과 불일치 — 담당자 확인 후 진행" if rec.matched is False
                    else "청구서에 총액 줄이 없어 대조하지 못함")
            _put(ws, row, 1, f"총액 대조: {text}", font=_f(9, color=MUTED if rec.matched else AMB))
            row += 1

    row += 2
    _section(ws, row, "가정값")
    _header(ws, row + 1, ["가정", "값", "단위", "출처 등급", "근거", "", "", ""])
    row += 2
    cur = a.get("acquisition_currency")
    spec = [("gpu_count", "GPU 수", NUM, "장"), ("acquisition_amount", "서버·GPU 취득비", _money_fmt(cur), cur or ""),
            ("depreciation_years", "감가상각 연수", NUM, "년"), ("utilization", "유료 가동률", PCT0, ""),
            ("fx_krw_per_usd", "환율 (원/달러)", "#,##0.0", "원/달러"), ("pricing_method", "마진 방식", None, ""),
            ("target_rate", "목표 마진", PCT0, ""), ("inhouse_annual", "사내 인건비·판관비 (연간)", KRW, "원")]
    sources, grades = a.get("sources", {}), a.get("grades", {})
    for key, label, fmt, unit in spec:
        value = a.get(key)
        if key == "pricing_method":
            value = METHOD_KO.get(value, value)
        g = grades.get(key, "")
        _put(ws, row, 1, label)
        _put(ws, row, 2, value, fmt)
        _put(ws, row, 3, unit)
        _put(ws, row, 4, g, font=_f(color=AMB if g == "예시값" else INK))
        _put(ws, row, 5, sources.get(key, "담당자 입력"), wrap=True)
        _line(ws, row, 5)
        row += 1


# ------------------------------------------------------------------ 3. 원가 구성·민감도
def _composition(wb, s: AgentSession) -> None:
    ap = s.approval
    ws = _sheet(wb, "원가 구성·민감도", "원가 구성과 민감도",
                "묶음별 원가와, 가정을 하나씩 바꿔 원가 서버로 다시 계산한 제안가 변화.",
                {"A": 16, "B": 34, "C": 16, "D": 18, "E": 12, "F": 16, "G": 40})
    _section(ws, 4, "묶음별 원가")
    _header(ws, 5, ["묶음", "원가 항목", "연간 (USD)", "GPU·시간당 (USD)", "비중", "업계 기준 비중", "근거 줄"])
    row = 6
    bench = {b["group"]: b for b in (ap.benchmark if ap else [])}
    if ap and ap.breakdown_ok and ap.breakdown:
        first = row
        for br in ap.breakdown:
            ref = bench.get(br.group, {})
            odd = ref and not 0.5 <= ref.get("ratio", 1) <= 2.0
            _put(ws, row, 1, br.group, font=_f(bold=True))
            _put(ws, row, 2, ", ".join(ITEM_KO.get(t, t) for t in br.item_types), wrap=True)
            _put(ws, row, 3, round(br.annual_usd, 2), USD0)
            _put(ws, row, 4, round(br.hourly_usd, 4), USD4)
            _put(ws, row, 5, f"=IF($C${{TOTAL}}=0,0,C{row}/$C${{TOTAL}})", PCT0, _f(bold=bool(odd), color=AMB if odd else INK))
            _put(ws, row, 6, ref.get("ref_share"), PCT0, _f(color=MUTED))
            _put(ws, row, 7, ", ".join(br.refs), font=_f(9, color=MUTED), wrap=True)
            _line(ws, row, 7)
            row += 1
        total = row
        for rr in range(first, total):
            c = ws.cell(rr, 5)
            c.value = c.value.replace("{TOTAL}", str(total))
        _put(ws, total, 1, "합계", font=_f(bold=True))
        _put(ws, total, 3, f"=SUM(C{first}:C{total - 1})", USD0, _f(bold=True))
        _put(ws, total, 4, f"=SUM(D{first}:D{total - 1})", USD4, _f(bold=True))
        _put(ws, total, 5, f"=SUM(E{first}:E{total - 1})", PCT0, _f(bold=True))
        _line(ws, total, 7)
        row = total + 1
        _put(ws, row, 1, f"원가 서버 GPU·시간당 원가 ${float((s.simulation or {}).get('hourlyCost') or 0):.4f}와 묶음 합계가 일치 (자동 검증 '분해 합계')."
             + ("" if ap.interest_applied else " 금융비용(이자)은 원가에 들어 있지 않습니다."), font=_f(9, color=MUTED))
    else:
        _put(ws, row, 1, "분해 없음", font=_f(bold=True, color=RED))
        _put(ws, row, 2, "원가 항목 합계가 원가 서버 결과와 맞지 않아 묶음별 분해를 싣지 않았습니다 (재검토 사유)." if ap
             else "승인 검토 전이라 묶음별 분해가 없습니다.", wrap=True)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=7)

    row += 3
    _section(ws, row, "결론을 바꿀 수 있는 가정 (제안가 변화가 큰 순)")
    _header(ws, row + 1, ["가정 변화", "", "제안가 변화 (USD)", "출처 등급", "", "", ""])
    row += 2
    sens = sorted(ap.sensitivity if ap else [], key=lambda x: -abs(float(x.get("price_delta") or 0)))
    if not sens:
        _put(ws, row, 1, "민감도 계산 없음", font=_f(color=MUTED))
    for x in sens:
        g = x.get("grade", "")
        _put(ws, row, 1, x.get("label", x.get("key", "")))
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
        _put(ws, row, 3, round(float(x.get("price_delta") or 0), 4), '+"$"0.0000;-"$"0.0000;"$"0.0000')
        _put(ws, row, 4, g, font=_f(color=AMB if g == "예시값" else INK))
        _line(ws, row, 4)
        row += 1


# ------------------------------------------------------------------ 4. 시장 비교
def _market(wb, s: AgentSession) -> None:
    m = s.market
    ws = _sheet(wb, "시장 비교", "시장 비교",
                f"{m.model_name if m else ''} 온디맨드 공개 가격 · USD/GPU·시간 · 수집 시각은 한국 시간",
                {"A": 18, "B": 18, "C": 12, "D": 16, "E": 18})
    if not m or not m.samples:
        _put(ws, 4, 1, "비교할 공개 가격이 없습니다.", font=_f(color=MUTED))
        return
    cand = float((s.simulation or {}).get("candidatePrice") or 0)
    _section(ws, 4, "사업자별 최저가")
    _header(ws, 5, ["사업자", "최저가", "표본 수", "제안가 − 최저가", ""])
    by: dict[str, list[float]] = {}
    for x in m.samples:
        by.setdefault(str(x.get("provider")), []).append(float(x.get("price") or 0))
    row = 6
    for p, prices in sorted(by.items(), key=lambda kv: min(kv[1])):
        _put(ws, row, 1, p)
        _put(ws, row, 2, min(prices), USD2)
        _put(ws, row, 3, len(prices), NUM)
        _put(ws, row, 4, round(cand - min(prices), 4), '+"$"0.00;-"$"0.00;"$"0.00')
        _line(ws, row, 4)
        row += 1
    if m.count > len(m.samples) or m.providers > len(by):
        _put(ws, row, 1, f"시장 통계는 표본 {m.count}개·사업자 {m.providers}곳 기준이고, 아래에는 저장된 {len(m.samples)}개만 싣습니다.",
             font=_f(9, color=AMB))
        row += 1
    row += 2
    _section(ws, row, "공개 가격 표본 (가격순)")
    _header(ws, row + 1, ["사업자", "지역", "GPU 수", "USD/GPU·시간", "수집 (KST)"])
    row += 2
    head = row
    for x in sorted(m.samples, key=lambda x: (float(x.get("price") or 0), str(x.get("provider")), str(x.get("region")))):
        _put(ws, row, 1, x.get("provider"))
        _put(ws, row, 2, x.get("region"))
        _put(ws, row, 3, x.get("gpu_count"), NUM)
        _put(ws, row, 4, x.get("price"), USD2)
        _put(ws, row, 5, _kst(x.get("collected_at", "")))
        row += 1
    ws.freeze_panes = f"A{head}"


# ------------------------------------------------------------------ 5. 부록
def _appendix(wb, s: AgentSession) -> None:
    from . import verify as V
    ws = _sheet(wb, "부록", "부록", "청구서 해석, 자동 검증, 에이전트 진행 기록",
                {"A": 18, "B": 90, "C": 10, "D": 18, "E": 30})
    _section(ws, 4, "청구서 해석")
    _header(ws, 5, ["구분", "내용", "적용", "해제 시 연간 원가", "근거"])
    row = 6
    d = s.draft
    interps = d.interpretations if d else []
    bench = (d.bench if d else []) + list(s.checks)
    for i in interps:
        _put(ws, row, 1, "해석")
        _put(ws, row, 2, i.message, wrap=True)
        _put(ws, row, 3, "적용" if i.applied else "해제")
        _put(ws, row, 4, f"{i.impact:.3g}배" if i.impact is not None else "-")
        _put(ws, row, 5, ", ".join(i.refs), font=_f(9, color=MUTED))
        _line(ws, row, 5)
        row += 1
    for b in bench:
        _put(ws, row, 1, "기준선 검사", font=_f(color=AMB))
        _put(ws, row, 2, b.message, wrap=True)
        _put(ws, row, 3, "-")
        _put(ws, row, 4, f"{b.low:,.2f}~{b.high:,.2f}")
        _put(ws, row, 5, b.source, font=_f(9, color=MUTED), wrap=True)
        _line(ws, row, 5)
        row += 1
    if not interps and not bench:
        _put(ws, row, 1, "해석 변경 없음")
        _put(ws, row, 2, "청구서를 기본 규칙대로 읽었습니다 (단위·금액 열·청구 기간 변경 없음).")
        row += 1

    row += 2
    _section(ws, row, "자동 검증")
    _header(ws, row + 1, ["검사", "결과", "", "", ""])
    row += 2
    for c in V.CHECKS:
        ok = c in s.checks_passed
        _put(ws, row, 1, c)
        _put(ws, row, 2, "통과" if ok else "실패" if c in s.checks_failed else "-", font=_f(bold=not ok, color=OK if ok else RED))
        _line(ws, row, 2)
        row += 1

    row += 2
    _section(ws, row, "진행 기록")
    _header(ws, row + 1, ["시각 (KST)", "내용", "구분", "단계", ""])
    row += 2
    for e in s.events:
        _put(ws, row, 1, _kst(e.at))
        _put(ws, row, 2, e.message, wrap=True, font=_f(color=AMB if e.kind == "warn" else INK))
        _put(ws, row, 3, KIND_KO.get(e.kind, e.kind), font=_f(color=MUTED))
        _put(ws, row, 4, _step_ko(e.step.value if hasattr(e.step, "value") else e.step))
        row += 1


def build_report(s: AgentSession) -> bytes:
    wb = Workbook()
    _summary(wb, s)
    _evidence(wb, s)
    _composition(wb, s)
    _market(wb, s)
    _appendix(wb, s)
    wb.active = 0
    wb.calculation = CalcProperties(fullCalcOnLoad=True)  # 원가 대비·합계 수식은 엑셀이 열 때 계산한다
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
