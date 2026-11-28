"""보고서 자동 검증 관문 — 보고서의 숫자·권고·리스크가 계산 결과와 맞는지 코드가 다시 확인한다. (설계 §4B-5)

하나라도 실패하면 보고서를 내보내지 않는다. 문구는 코드 템플릿이다.
"""

from __future__ import annotations

from html import escape

from .approval import breakeven, findings, verdict
from .market import market_view

CHECKS = ["분해 합계", "제안가 공식", "기간별 가격", "손익분기", "시장 통계", "리스크 누락 금지", "권고 일치"]
FORMULA_TOL = 0.001


def _risk_conditions(s) -> list[tuple[str, str]]:
    """(조건 키, 리스크 문구). 보고서 리스크 칸에 반드시 있어야 하는 것들."""
    out: list[tuple[str, str]] = []
    a = s.assumptions or {}
    if (a.get("grades") or {}).get("acquisition_amount") == "예시값":
        out.append(("acq_example", "서버·GPU 취득비가 리포트 예시값이에요 — 견적서 값으로 바꿔야 해요"))
    if not (s.approval and s.approval.interest_applied):
        out.append(("no_financing", "금융비용(이자)이 원가에 반영되지 않았어요 — 마진이 실제보다 높게 보일 수 있어요"))
    for b in list(getattr(s.draft, "bench", []) if s.draft else []) + list(s.checks):
        out.append((f"bench:{b.code}", f"업계 기준선 경고: {b.message}"))
    for g, rec in s.missing_answers.items():
        if rec.get("choice") == "exclude":
            out.append((f"excluded:{g}", f"'{g}' 비용을 빼고 계산했어요 (불완전 원가)"))
        elif rec.get("choice") == "amount" and rec.get("source") == "예시값":
            out.append((f"missing_example:{g}", f"'{g}' 금액이 리포트 예시값이에요 — 실제 계약 금액으로 바꿔야 해요"))
    for i in getattr(s.draft, "interpretations", []) if s.draft else []:
        if not i.applied:
            out.append((f"interp_off:{i.code}", f"담당자가 청구서 해석을 해제했어요: {i.message}"))
    if s.mismatch_ok:
        out.append(("mismatch", "청구서 총액과 원가 합계가 맞지 않는 것을 알고 진행했어요"))
    return out


def risks(s) -> list[str]:
    return [text for _, text in _risk_conditions(s)]


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= max(abs(b) * tol, 1e-9)


def verify(s) -> tuple[list[str], list[str]]:
    """(통과한 검사, 실패한 검사)."""
    ap, r, a = s.approval, s.simulation or {}, s.assumptions or {}
    cost, cand = float(r.get("hourlyCost") or 0), float(r.get("candidatePrice") or 0)
    ok: dict[str, bool] = {}
    # 분해가 BE와 맞거나, 맞지 않는다는 것이 재검토 사유로 드러나 있으면(보고서에 분해를 싣지 않음) 통과
    ok["분해 합계"] = bool(ap) and (ap.breakdown_ok or any(f.code == "BREAKDOWN_MISMATCH" for f in ap.findings))
    t = float(a.get("target_rate") or 0)
    expected = cost * (1 + t) if a.get("pricing_method") == "COST_MARKUP" else (cost / (1 - t) if t < 1 else 0)
    ok["제안가 공식"] = bool(cand) and _close(cand, expected, FORMULA_TOL)
    ok["기간별 가격"] = all(abs(x.price - round(cand * (1 - x.discount_rate), 4)) <= 1e-4 for x in s.terms)
    prices = {"candidate": cand, **{f"{x.years}y": x.price for x in s.terms}}
    again = breakeven(float(a.get("utilization") or 0), cost, prices)
    ok["손익분기"] = bool(ap) and set(again) == set(ap.breakeven) and all(_close(again[k], ap.breakeven[k], 1e-9) for k in again)
    m = s.market
    if m and m.count and m.count <= len(m.samples):  # 표본이 다 저장된 경우에만 다시 계산할 수 있다
        v = market_view(m.model_name, m.samples, cand)
        ok["시장 통계"] = (v.min, v.median, v.max) == (m.min, m.median, m.max)
    else:
        ok["시장 통계"] = True
    # 리스크는 실제로 만든 보고서(HTML)에 실렸는지 본다
    from . import report_html as RH
    rendered = RH.build_report_html(s, verified=([], []))
    ok["리스크 누락 금지"] = all(escape(RH.formal(text)) in rendered for _, text in _risk_conditions(s))  # 보고서는 합니다체로 싣는다
    # 권고는 세션 상태로 발견 사항을 다시 만들어 저장된 것과 비교한다
    if ap:
        fresh = findings(s, ap)
        ok["권고 일치"] = ([(f.code, f.level) for f in fresh] == [(f.code, f.level) for f in ap.findings]
                        and verdict(fresh)[0] == ap.verdict)
    else:
        ok["권고 일치"] = False
    passed = [c for c in CHECKS if ok[c]]
    return passed, [c for c in CHECKS if not ok[c]]
