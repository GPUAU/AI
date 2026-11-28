"""한 장 승인 보고서 (인쇄용 HTML). C-level이 '이 가격으로 제안해도 되는가'를 한 장으로 판단하게 한다. (설계 §4B-4)

문장은 코드 템플릿이고 숫자는 세션 값에서 서식만 바꿔 넣는다 (LLM 서술 없음). 외부 리소스를 쓰지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from . import verify as V
from .models import AgentSession

CSS = """
*{box-sizing:border-box}body{font-family:-apple-system,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;color:#1f242b;
margin:0;padding:28px;font-size:13px;line-height:1.55}h1{font-size:19px;margin:0 0 2px}h2{font-size:14px;margin:18px 0 6px;
border-bottom:1px solid #d5d9df;padding-bottom:3px}.sub{color:#6b7280;margin:0 0 14px}.verdict{padding:10px 14px;border:1px solid #d5d9df;
border-radius:8px;margin-bottom:10px}.verdict b{font-size:16px}.v-ok{background:#eef8f1}.v-cond{background:#fff8e6}.v-rev{background:#fdeeee}
table{border-collapse:collapse;width:100%}td,th{padding:4px 6px;border-bottom:1px solid #eef0f3;text-align:left;vertical-align:top}
th{color:#6b7280;font-weight:500}.num{text-align:right;font-variant-numeric:tabular-nums}.bar{display:inline-block;height:9px;
background:#7f8ea3;border-radius:2px}ul{margin:4px 0;padding-left:18px}.foot{margin-top:16px;color:#6b7280;font-size:12px}
@media print{body{padding:14mm}@page{size:A4 portrait;margin:0}h2{break-after:avoid}}
"""


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def build_report_html(s: AgentSession, verified: tuple[list[str], list[str]] | None = None) -> str:
    """verified를 주면 하단 자동 검증 결과로 그 값을 쓴다 (verify가 이 함수를 부를 때 되돌아 부르지 않게)."""
    e = escape
    a, r, ap, m = s.assumptions or {}, s.simulation or {}, s.approval, s.market
    cost, cand = float(r.get("hourlyCost") or 0), float(r.get("candidatePrice") or 0)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    verdict = ap.verdict if ap else "검토 전"
    cls = {"승인 권고": "v-ok", "조건부": "v-cond", "재검토": "v-rev"}.get(verdict, "")
    parts = [f"<!DOCTYPE html><html lang=\"ko\"><head><meta charset=\"utf-8\"><title>GPU 요금 제안 검토</title>"
             f"<style>{CSS}</style></head><body>"]
    parts.append(f"<h1>GPU 요금 제안 검토 — {e(str(a.get('model_name', '')))} {e(str(a.get('gpu_count', '')))}장 · {today}</h1>")
    parts.append(f"<p class=\"sub\">청구서 {e(s.draft.file_name if s.draft else '')} · 입력 가정에 따른 시뮬레이션이며 확정 견적이 아닙니다.</p>")

    reasons = ap.reasons[:3] if ap else []
    parts.append(f"<div class=\"verdict {cls}\"><b>권고: {e(verdict)}</b>"
                 + ("<ul>" + "".join(f"<li>{e(x)}</li>" for x in reasons) + "</ul>" if reasons else "") + "</div>")

    terms = " · ".join(f"{t.years}년 {_usd(t.price)} (할인 {t.discount_rate:.0%})" for t in s.terms)
    margin = a.get("target_rate")
    parts.append("<h2>제안 요금</h2><table>"
                 f"<tr><th>제안가</th><td class=\"num\">{_usd(cand)} /GPU·시간</td><th>원가 (최저 수용가)</th><td class=\"num\">{_usd(cost)}</td></tr>"
                 f"<tr><th>마진</th><td class=\"num\">{(margin or 0):.0%} ({e(str(a.get('pricing_method', '')))})</td>"
                 f"<th>기간별</th><td>{e(terms) or '요청 없음'}</td></tr>")
    if ap and ap.breakeven:
        be = " · ".join(f"{'제안가' if k == 'candidate' else k.replace('y', '년가')} {v:.0%}" for k, v in ap.breakeven.items())
        parts.append(f"<tr><th>손익분기 가동률</th><td colspan=\"3\">{e(be)} (가정 {float(a.get('utilization') or 0):.0%})</td></tr>")
    parts.append("</table>")

    parts.append("<h2>원가 구성</h2>")
    if ap and ap.breakdown_ok and ap.breakdown:
        bench = {b["group"]: b for b in ap.benchmark}
        parts.append("<table><tr><th>묶음</th><th class=\"num\">연간 (USD)</th><th class=\"num\">GPU·시간당</th>"
                     "<th>비중</th><th>업계 기준</th></tr>")
        for row in sorted(ap.breakdown, key=lambda x: -x.share):
            ref = bench.get(row.group)
            ref_txt = f"{ref['ref_share']:.0%}" if ref else "-"
            parts.append(f"<tr><td>{e(row.group)}</td><td class=\"num\">{row.annual_usd:,.0f}</td>"
                         f"<td class=\"num\">{_usd(row.hourly_usd)}</td><td><span class=\"bar\" style=\"width:{row.share * 120:.0f}px\"></span> "
                         f"{row.share:.0%}</td><td>{ref_txt}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p>원가 항목 합계가 원가 서버 결과와 맞지 않아 항목별 분해를 싣지 않았어요. 재검토 사유입니다.</p>")

    parts.append("<h2>시장 위치</h2>")
    if m and m.count:
        parts.append(f"<p>해외 공개 요금 사업자 {m.providers}곳 · 공개가 {m.count}개: 최저 {_usd(m.min)} · 중앙 {_usd(m.median)} · "
                     f"최고 {_usd(m.max)} → 제안가는 {e(m.position)}</p>")
    else:
        parts.append("<p>비교할 시장 공개가격이 없습니다.</p>")

    if ap and ap.sensitivity:
        parts.append("<h2>결론을 바꿀 수 있는 가정</h2><table><tr><th>가정 변화</th><th class=\"num\">제안가 변화</th><th>출처 등급</th></tr>")
        for x in ap.sensitivity[:4]:
            parts.append(f"<tr><td>{e(x['label'])}</td><td class=\"num\">{x['price_delta']:+.2f}</td><td>{e(x.get('grade') or '-')}</td></tr>")
        parts.append("</table>")

    checks = []
    if s.draft:
        rec = s.draft.reconciliation
        checks.append("청구서 총액 " + ("일치" if rec.matched else "불일치(담당자 확인 후 진행)" if rec.matched is False else "대조 불가"))
        on = [i.message for i in s.draft.interpretations if i.applied]
        if on:
            checks.append(f"청구서 해석 {len(on)}건 적용: " + " / ".join(on))
    checks += [f"'{g}' {('별도 계약 금액 반영' if v.get('choice') == 'amount' else '다른 요금에 포함' if v.get('choice') == 'included' else '제외')}"
               for g, v in s.missing_answers.items()]
    parts.append("<h2>확인한 것</h2><ul>" + "".join(f"<li>{e(c)}</li>" for c in checks) + "</ul>")

    rs = V.risks(s)
    parts.append("<h2>리스크</h2>" + ("<ul>" + "".join(f"<li>{e(x)}</li>" for x in rs) + "</ul>" if rs else "<p>남은 리스크 없음</p>"))

    if ap and any(f.reason for f in ap.findings):
        parts.append("<h2>담당자 사유</h2><ul>" + "".join(
            f"<li>[{e(f.level)}] {e(f.message)} — {e(f.reason)}</li>" for f in ap.findings if f.reason) + "</ul>")

    passed, failed = verified if verified is not None else V.verify(s)
    parts.append(f"<p class=\"foot\">자동 검증 {len(passed)}/{len(V.CHECKS)} 통과"
                 + (f" · 실패: {e(', '.join(failed))}" if failed else "")
                 + " · 원가 계산은 원가 서버(BE) 공식, 문장은 코드 템플릿으로 만들었습니다.</p></body></html>")
    return "".join(parts)
