"""승인 보고서 (사내 보고용 A4, 4쪽). 결재자가 '이 가격으로 제안해도 되는가'를 1쪽에서 판단하고,
담당자는 2~4쪽에서 원가가 어떻게 계산됐는지 숫자로 따라가 검증한다. (설계 §4B-4)

1쪽 요약: 결재란 · 보고 요지(권고와 근거) · 제안 요금 · 승인 조건 · 의사결정 요청
2쪽 원가 산정 근거: 시장 위치 · 계산식과 이번 숫자로 푼 계산 단계 · 청구서 원본 대조
3쪽 구성과 가정: 원가 구성(업계 기준 비교) · 가정값과 출처 등급 · 민감도
4쪽 검증과 기록: 자동 검증 · 담당자 결정 · 리스크 · 유의 사항

문장은 코드 템플릿이고 숫자는 세션 값에서 서식만 바꿔 넣는다 (LLM 서술 없음).
SKT 로고와 Pretendard(OFL) 글꼴을 파일 안에 넣어 외부 리소스를 쓰지 않는다. 브라우저 인쇄 창의 'PDF로 저장'으로 PDF를 만든다.
"""

from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from html import escape as e
from pathlib import Path

from . import verify as V
from .models import AgentSession

ASSETS = Path(__file__).parent / "assets"
KST = timezone(timedelta(hours=9))
YEAR_HOURS = 8760
ITEM_KO = {"RACK_FACILITY": "랙·전원·냉각 설비", "ELECTRICITY": "전기료", "NETWORK_STORAGE": "네트워크·스토리지",
           "OPERATIONS": "운영·유지보수", "RENT": "임차료·코로케이션", "GPU": "GPU 임차", "MEMORY": "별도 메모리",
           "OTHER": "기타", "ACQUISITION": "서버·GPU 취득비", "FINANCING": "조달 금리"}
METHOD_KO = {"REVENUE_MARGIN": "매출 기준 이익률", "COST_MARKUP": "원가 가산율", "MARKUP": "원가 가산율"}
DECISION_KO = {"apply": "제안대로 고침", "proceed": "그대로 진행", "ack": "참고 확인", "fix": "돌아가서 고침"}
# 조건부·재검토 사유 → 승인 전에 할 일
CONDITION = {
    "NO_FINANCING": "조달 금리를 확정해 원가에 반영한 뒤 이익률을 다시 확인",
    "THIN_BREAKEVEN": "가동률 모니터링 조건을 붙이거나 할인율을 낮춰 손익분기 여유 확보",
    "MARKET_OUT_KEPT": "시장 범위 밖으로 제안하는 이유(전략적 진입 등)를 영업 계획에 명시",
    "ODD_MIX": "업계 기준과 크게 다른 비용 묶음이 빠짐없이 들어갔는지 담당 부서와 확인",
    "THIN_MARKET": "시장 비교 표본이 적어 추가 시장 조사 후 확정",
    "EXAMPLE_IN_TOP2": "결론을 크게 바꾸는 가정을 실제 값으로 교체",
    "MISSING_EXAMPLE": "예시값으로 넣은 누락 비용을 실제 계약 금액으로 교체",
    "MISSING_EXCLUDED": "제외한 비용을 확인해 원가에 넣은 뒤 다시 산정",
    "PLAN_MISMATCH": "사업기획팀 검산과의 차이 원인을 확인",
    "BENCH_WARN": "업계 기준과 크게 다른 값의 출처를 확인",
    "PRICE_BELOW_COST": "원가 아래인 기간별 가격의 할인율을 낮춰 원가 이상으로 조정",
    "COST_ABOVE_MARKET": "취득비·가동률 등 원가 가정을 다시 검토",
    "BREAKDOWN_MISMATCH": "원가 항목과 원가 서버 결과가 맞지 않는 원인을 확인",
}
FORMAL = [("이에요", "입니다"), ("예요", "입니다"), ("않았어요", "않았습니다"), ("달라요", "다릅니다"), ("없어요", "없습니다"),
          ("있어요", "있습니다"), ("했어요", "했습니다"), ("돼요", "됩니다"), ("해요", "합니다"), ("어요", "습니다"), ("아요", "습니다")]


def formal(text: str) -> str:
    """에이전트 화면 문장(해요체)을 보고서 문장(합니다체)으로. 리스크 문구 검증(verify)도 이 결과로 비교한다."""
    t = str(text or "")
    for a, b in FORMAL:
        t = re.sub(a + r"(?=[\s.,)·—]|$)", b, t)
    return t


@lru_cache(maxsize=1)
def _assets() -> tuple[str, str]:
    def b64(name: str) -> str:
        p = ASSETS / name
        return base64.b64encode(p.read_bytes()).decode() if p.is_file() else ""
    return b64("PretendardVariable-subset.woff2"), b64("skt-logo.png")


def _usd(v: float, p: int = 2) -> str:
    return f"${v:,.{p}f}"


def _krw(v: float) -> str:
    return f"{v:,.0f}원"


def _pct(v: float, p: int = 0) -> str:
    return f"{v * 100:.{p}f}%"


def _f(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ 그래프 (SVG)
def _market_svg(m, cost: float, cand: float) -> str:
    lo = min(m.min, cand, cost) * 0.85
    hi = max(m.max, cand) * 1.05
    W, L, R = 640, 18, 18
    x = lambda v: L + (v - lo) / ((hi - lo) or 1) * (W - L - R)  # noqa: E731
    return "".join([
        f'<svg viewBox="0 0 {W} 92" class="chart" role="img" aria-label="시장 가격 위치">',
        f'<line x1="{L}" x2="{W - R}" y1="46" y2="46" class="axis"/>',
        f'<rect x="{x(m.min):.1f}" y="38" width="{x(m.max) - x(m.min):.1f}" height="16" class="range"/>',
        f'<line x1="{x(m.median):.1f}" x2="{x(m.median):.1f}" y1="34" y2="58" class="median"/>',
        f'<text x="{x(m.min):.1f}" y="76" class="lbl" text-anchor="middle">최저 {_usd(m.min)}</text>',
        f'<text x="{x(m.median):.1f}" y="76" class="lbl" text-anchor="middle">중앙 {_usd(m.median)}</text>',
        f'<text x="{x(m.max):.1f}" y="76" class="lbl" text-anchor="middle">최고 {_usd(m.max)}</text>',
        f'<line x1="{x(cost):.1f}" x2="{x(cost):.1f}" y1="30" y2="62" class="cost"/>',
        f'<text x="{x(cost):.1f}" y="88" class="lbl cost-t" text-anchor="middle">원가 {_usd(cost)}</text>',
        f'<circle cx="{x(cand):.1f}" cy="46" r="7" class="me"/>',
        f'<text x="{x(cand):.1f}" y="22" class="lbl me-t" text-anchor="middle">제안가 {_usd(cand)}</text></svg>'])


MIX_COLOR = {"감가상각": "#2B3036", "운영": "#6B7280", "전력": "#A3A9B2", "공간": "#D3D7DD", "금융비용": "#EA002C"}


def _mix_svg(ap) -> str:
    W, x_from = 640, 92
    bench = {b["group"]: b for b in ap.benchmark}

    def bar(y: int, shares: list[tuple[str, float]], label: str) -> list[str]:
        out, x0 = [f'<text x="0" y="{y + 13}" class="lbl b">{label}</text>'], x_from
        for g, s in shares:
            w = (W - x_from) * s
            out.append(f'<rect x="{x0:.1f}" y="{y}" width="{max(w, 0):.1f}" height="18" style="fill:{MIX_COLOR.get(g, "#999")}"/>')
            if w > 44:
                fill = "#fff" if g in ("감가상각", "운영", "금융비용") else "#1A1D21"
                out.append(f'<text x="{x0 + 6:.1f}" y="{y + 13}" class="lbl" style="fill:{fill}">{e(g)} {s * 100:.0f}%</text>')
            x0 += w
        return out

    ours = [(b.group, b.share) for b in ap.breakdown]
    ref = [(g, bench[g]["ref_share"]) for g, _ in ours if g in bench]
    total = sum(s for _, s in ref) or 1
    rows = bar(4, ours, "이번 산정") + (bar(38, [(g, s / total) for g, s in ref], "업계 기준") if ref else [])
    return f'<svg viewBox="0 0 {W} {64 if ref else 30}" class="chart" role="img" aria-label="원가 구성 비교">' + "".join(rows) + "</svg>"


def _sens_svg(sens: list[dict]) -> str:
    xs = sorted(sens, key=lambda x: -abs(_f(x.get("price_delta"))))[:7]
    mx = max((abs(_f(x.get("price_delta"))) for x in xs), default=0) or 1
    W, row, mid, span = 640, 21, 455, 140
    parts = [f'<svg viewBox="0 0 {W} {row * len(xs) + 8}" class="chart" role="img" aria-label="민감도">',
             f'<line x1="{mid}" x2="{mid}" y1="0" y2="{row * len(xs) + 4}" class="axis"/>']
    for k, x in enumerate(xs):
        d = _f(x.get("price_delta"))
        y, w, up = 4 + k * row, abs(d) / mx * span, d > 0
        tx = mid + w + 6 if up else mid - w - 6
        parts += [f'<text x="0" y="{y + 13}" class="lbl">{e(x.get("label", x.get("key", "")))}</text>',
                  f'<text x="170" y="{y + 13}" class="lbl muted-t">{e(x.get("grade", ""))}</text>',
                  f'<rect x="{mid if up else mid - w:.1f}" y="{y + 3}" width="{w:.1f}" height="13" class="{"up" if up else "down"}"/>',
                  f'<text x="{tx:.1f}" y="{y + 13}" class="lbl num" text-anchor="{"start" if up else "end"}">'
                  f'{"+" if up else "−"}${abs(d):.3f}</text>']
    return "".join(parts) + "</svg>"


# ------------------------------------------------------------------ 스타일
def _css(font: str, doc_no: str) -> str:
    face = (f'@font-face{{font-family:"Pretendard";src:url(data:font/woff2;base64,{font}) format("woff2");'
            'font-weight:100 900;font-display:block}') if font else ""
    return face + f"""
@page{{size:A4;margin:22mm 16mm 18mm;
  @bottom-left{{content:"{doc_no} · 내부용 · 대외 반출 금지";font:7.5pt Pretendard,sans-serif;color:#8A9099}}
  @bottom-right{{content:counter(page) " / " counter(pages);font:7.5pt Pretendard,sans-serif;color:#8A9099}}}}
*{{box-sizing:border-box}}
html{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
body{{margin:0;font-family:Pretendard,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;font-size:9.2pt;line-height:1.55;color:#1A1D21;
  word-break:keep-all;font-feature-settings:"tnum"}}
.page{{break-after:page}}
.page:last-child{{break-after:auto}}
.runhead{{display:flex;justify-content:space-between;align-items:flex-end;padding-bottom:6px;border-bottom:1.6pt solid #EA002C;margin-bottom:14px}}
.runhead img{{height:22px}}.runhead span{{font-size:7.5pt;color:#6B7280}}
h1{{font-size:20pt;font-weight:700;letter-spacing:-.02em;line-height:1.3;margin:0}}
h2{{font-size:12pt;font-weight:700;margin:16px 0 7px;padding-left:8px;border-left:3pt solid #1A1D21;line-height:1.2}}
h2.first{{margin-top:0}}
h2 small{{font-weight:400;color:#6B7280;font-size:8.5pt;margin-left:6px}}
h3{{font-size:10pt;font-weight:600;margin:12px 0 6px}}h3.first{{margin-top:0}}
p{{margin:0 0 6px}}.muted{{color:#6B7280}}
.title-row{{display:grid;grid-template-columns:1fr auto;gap:18px;align-items:start;margin-bottom:12px}}
.meta{{margin:8px 0 0;font-size:8.3pt;color:#4B5159;display:grid;grid-template-columns:auto 1fr;column-gap:10px;row-gap:1px}}
.meta dt{{color:#8A9099}}.meta dd{{margin:0}}
.approval{{border-collapse:collapse;font-size:8pt;text-align:center}}
.approval th{{background:#F4F5F7;font-weight:600;border:.6pt solid #B8BDC4;padding:3px 0;width:62px}}
.approval td{{border:.6pt solid #B8BDC4;height:44px}}
.approval .side{{width:18px;background:#F4F5F7;font-weight:600;writing-mode:vertical-rl;letter-spacing:.3em}}
.summary{{border:.8pt solid #1A1D21;padding:12px 14px;margin:6px 0 4px}}
.summary .tag{{display:inline-block;font-size:8pt;font-weight:700;color:#fff;background:#1A1D21;padding:1px 7px;margin-bottom:6px}}
.summary .tag+.tag{{margin-left:4px}}
.summary .t-cond{{background:#9A4A00}}.summary .t-rev{{background:#B0102A}}.summary .t-ok{{background:#14663F}}
.summary .lead{{font-size:11.2pt;font-weight:600;line-height:1.55;margin:0 0 6px}}
.summary ul{{margin:0;padding-left:15px;color:#33383F}}
table.t{{width:100%;border-collapse:collapse;font-size:8.8pt}}
table.t th{{text-align:left;font-weight:600;color:#4B5159;background:#F4F5F7;border-top:.8pt solid #1A1D21;border-bottom:.6pt solid #C9CDD3;padding:4px 7px}}
table.t td{{border-bottom:.5pt solid #E1E4E8;padding:4px 7px;vertical-align:top}}
table.t .n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
table.t tr.total td{{border-top:.8pt solid #1A1D21;font-weight:700;background:#FAFAFB}}
table.t tr.hl td{{background:#FFF7F8}}
.neg{{color:#B0102A;font-weight:600}}.warn-t{{color:#9A4A00;font-weight:600}}.ok-t{{color:#14663F}}
.badge{{display:inline-block;font-size:7.6pt;font-weight:600;padding:0 5px;border:.6pt solid currentColor;line-height:1.5;white-space:nowrap}}
.b-cond{{color:#9A4A00}}.b-rev{{color:#B0102A}}.b-fix{{color:#14663F}}.b-g{{color:#6B7280}}
.cond ol{{margin:0;padding-left:16px}}.cond li{{margin-bottom:4px}}.cond li small{{color:#6B7280;display:block}}
.decide{{background:#F4F5F7;padding:10px 14px;margin-top:10px}}.decide b{{display:block;margin-bottom:3px}}
.chart{{width:100%;height:auto;display:block;font-family:Pretendard,sans-serif}}
.chart .axis{{stroke:#B8BDC4;stroke-width:1}}.chart .range{{fill:#E6E8EC}}.chart .median{{stroke:#1A1D21;stroke-width:1.5}}
.chart .cost{{stroke:#6B7280;stroke-width:1.2;stroke-dasharray:3 2}}.chart .me{{fill:#EA002C;stroke:#fff;stroke-width:2}}
.chart .lbl{{font-size:9.5px;fill:#4B5159}}.chart .lbl.b{{font-weight:600;fill:#1A1D21}}
.chart .me-t{{fill:#B0102A;font-weight:700}}.chart .cost-t{{fill:#4B5159;font-weight:600}}
.chart .up{{fill:#EA002C}}.chart .down{{fill:#6B7280}}.chart .muted-t{{fill:#8A9099}}.chart .num{{font-weight:600;fill:#1A1D21}}
.calc td.step{{width:22px;color:#EA002C;font-weight:700}}.calc td.f{{font-size:8.4pt;color:#4B5159}}
.formula{{border:.6pt solid #C9CDD3;background:#FAFAFB;padding:9px 12px;margin:4px 0 8px;font-size:9pt}}
.formula .eq{{display:grid;grid-template-columns:auto 1fr;column-gap:8px;row-gap:2px}}
.formula .eq span:nth-child(odd){{font-weight:600;white-space:nowrap}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.note{{font-size:7.8pt;color:#6B7280;margin-top:5px}}
ul.note{{font-size:8.3pt;padding-left:15px}}
.keep{{break-inside:avoid}}
.tools{{display:flex;justify-content:flex-end;gap:8px;width:min(210mm,calc(100% - 24px));margin:16px auto 0}}
.tools button{{font:inherit;padding:7px 16px;border:1px solid #c9ced6;background:#fff;cursor:pointer}}
.tools button.primary{{background:#ea002c;border-color:#ea002c;color:#fff}}
@media screen{{body{{background:#E9EBEE}}.page{{background:#fff;width:min(210mm,calc(100% - 24px));min-height:297mm;margin:12px auto;padding:20mm max(16px,min(16mm,4vw));
  box-shadow:0 1px 3px rgba(0,0,0,.12)}}}}
@media print{{.tools{{display:none}}.verdict,.badge{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}}}
"""


# ------------------------------------------------------------------ 본문
def build_report_html(s: AgentSession, verified: tuple[list[str], list[str]] | None = None) -> str:
    """verified를 주면 자동 검증 결과로 그 값을 쓴다 (verify가 이 함수를 부를 때 되돌아 부르지 않게)."""
    a, r, ap, m, d = s.assumptions or {}, s.simulation or {}, s.approval, s.market, s.draft
    aa = r.get("appliedAssumptions") or {}
    cost, cand = _f(r.get("hourlyCost")), _f(r.get("candidatePrice"))
    util, gpus, fx = _f(a.get("utilization")), int(_f(a.get("gpu_count"))), _f(a.get("fx_krw_per_usd"))
    margin, years = _f(a.get("target_rate")), int(_f(a.get("depreciation_years"), 0)) or None
    model = str(a.get("model_name", "") or "")
    method = METHOD_KO.get(a.get("pricing_method"), a.get("pricing_method") or "")
    hours = _f(r.get("billableGpuHoursPerYear")) or gpus * YEAR_HOURS * util
    annual = _f(r.get("annualCost")) or cost * hours
    verdict = ap.verdict if ap else "검토 전"
    found = list(ap.findings) if ap else []
    applied = list(ap.applied) if ap else []
    be = ap.breakeven if ap else {}
    now = datetime.now(KST)
    doc_no = f"GPU-COST-{now:%Y%m%d}-{s.id[:6].upper()}"
    font, logo = _assets()
    source = (f"부서 문서 {len(s.docs)}개" if s.sheet is not None and s.docs else f"청구서 {d.file_name}" if d else "")

    # 원가 항목 (원화·달러 연간). 사내 인건비·판관비는 운영비에 합쳐 들어가므로 따로 보인다.
    inhouse = _f(a.get("inhouse_annual"))
    rows_cost: list[tuple[str, str, float | None, float]] = []
    for it in s.final_items:
        if it.status.value != "USER_INPUT" or not it.amount or it.item_type.value == "ACQUISITION":
            continue
        amt = float(it.amount) * (12 if it.period and it.period.value == "MONTH" else 1)
        name = ITEM_KO.get(it.item_type.value, it.item_type.value)
        ref = ", ".join(x.split("!")[-1] + "행" for x in it.source_refs)
        where = f"{d.file_name if d else '청구서'} · {ref}" if ref else "담당자 입력"
        krw_amt = amt if it.currency and it.currency.value == "KRW" else None
        to_usd = (amt / fx if fx else 0.0) if krw_amt is not None else amt
        if it.item_type.value == "OPERATIONS" and inhouse and krw_amt is not None and amt >= inhouse:
            rows_cost.append((name + " (청구서)", where, amt - inhouse, (amt - inhouse) / fx if fx else 0.0))
            rows_cost.append(("사내 인건비·판관비", "담당자 입력 (연간)", inhouse, inhouse / fx if fx else 0.0))
        else:
            rows_cost.append((name, where, krw_amt, to_usd))
    operating = _f(aa.get("annualOperatingCostUsd")) or sum(x[3] for x in rows_cost)
    acq = _f(aa.get("acquisitionCostUsd")) or (_f(a.get("acquisition_amount")) / (fx if a.get("acquisition_currency") == "KRW" and fx else 1))
    capital = _f(aa.get("annualCapitalRecoveryUsd")) or (annual - operating)
    crf = _f(aa.get("capitalRecoveryFactor")) or (capital / acq if acq else 0.0)
    rate = _f(aa.get("annualInterestRateUsed"))
    krw_total = sum(x[2] for x in rows_cost if x[2] is not None)

    H: list[str] = [f'<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><title>GPU 요금 제안 검토 보고서 — {e(model)} {gpus}장</title>'
                    f'<style>{_css(font, doc_no)}</style></head><body>']
    # 인쇄 창의 'PDF로 저장'으로 내려받는다 (서버에 PDF 라이브러리를 두지 않는다). ?print=1이면 열자마자 인쇄 창을 띄운다.
    H.append('<div class="tools"><button type="button" class="primary" onclick="window.print()">PDF로 저장</button></div>'
             "<script>if(new URLSearchParams(location.search).get('print')==='1')"
             "addEventListener('load',function(){setTimeout(function(){window.print()},300)});</script>")
    logo_img = f'<img src="data:image/png;base64,{logo}" alt="SK telecom">' if logo else '<b>SK telecom</b>'
    runhead = f'<div class="runhead">{logo_img}<span>GPU 요금 제안 검토 보고서 · {doc_no}</span></div>'

    # ---------- 1쪽: 요약
    lead = (f"{e(model)} {gpus}장 GPU 서비스를 GPU·시간당 {_usd(cand)}({e(method)} {_pct(margin)})로 "
            f"제안하는 안에 대해 <b>‘{e(verdict)}’</b>을 권고합니다.")
    basis = [f"원가 {_usd(cost)} 대비 제안가 {_usd(cand)}로 이익률 {_pct(margin)}를 확보합니다"
             + (f" (손익분기 가동률 {_pct(be['candidate'])}, 가정 {_pct(util)})." if be.get("candidate") is not None else ".")]
    if m and m.count and m.min is not None:
        if cand < m.min:
            basis.append(f"제안가는 시장 공개가 최저 {_usd(m.min)}보다 {_pct(1 - cand / m.min)} 낮아, 가격 경쟁력은 높지만 이익 여지를 남겨 두는 안입니다.")
        elif m.max is not None and cand > m.max:
            basis.append(f"제안가는 시장 공개가 최고 {_usd(m.max)}보다 {_pct(cand / m.max - 1)} 높아 수주 경쟁력을 확인해야 합니다.")
        else:
            basis.append(f"제안가는 시장 공개가 범위({_usd(m.min)}~{_usd(m.max or m.min)}) 안이며, {e(m.position)}.")
    if not (ap and ap.interest_applied):
        basis.append("금융비용(이자)은 원가에 포함되지 않아, 실제 이익률은 위보다 낮을 수 있습니다.")
    vtag = {"재검토": "t-rev", "조건부": "t-cond", "승인 권고": "t-ok"}.get(verdict, "")

    H.append('<section class="page">' + runhead)
    H.append(f'''<div class="title-row"><div><h1>{e(model)} {gpus}장 GPU 서비스<br>요금 제안 검토 보고서</h1>
<dl class="meta"><dt>보고 일자</dt><dd>{now:%Y년 %m월 %d일}</dd><dt>대상</dt><dd>{e(model)} × {gpus}장 · 온디맨드 기준</dd>
<dt>원가 근거</dt><dd>{e(source)} · 가정값 {len(a.get("sources") or {})}건</dd><dt>작성</dt><dd>원가 에이전트 산정 · 담당자 검토</dd></dl></div>
<table class="approval"><tr><td class="side" rowspan="2">결재</td><th>담당</th><th>팀장</th><th>본부장</th></tr><tr><td></td><td></td><td></td></tr></table></div>''')
    H.append(f'<div class="summary keep"><span class="tag">보고 요지</span><span class="tag {vtag}">권고: {e(verdict)}</span>'
             f'<p class="lead">{lead}</p><ul>{"".join(f"<li>{x}</li>" for x in basis)}</ul></div>')

    H.append('<h2>제안 요금 <small>USD / GPU·시간, 부가세 별도</small></h2><table class="t keep"><tr><th>구분</th><th class="n">요금</th>'
             '<th class="n">할인</th><th class="n">원가 대비</th><th class="n">손익분기 가동률</th><th class="n">가정 가동률과 여유</th></tr>')
    H.append(f'<tr><td>원가 (최저 수용가)</td><td class="n">{_usd(cost, 3)}</td><td class="n">—</td><td class="n">—</td><td class="n">—</td><td class="n">—</td></tr>')
    prices = [("제안가", cand, be.get("candidate"), None)] + [(f"{t.years}년 계약가", t.price, be.get(f"{t.years}y"), t.discount_rate) for t in s.terms]
    for label, p, b, disc in prices:
        room = (util - b) * 100 if b is not None else None
        cls = "neg" if p < cost - 1e-9 else ("warn-t" if room is not None and room < 10 else "")
        vs = f"{(p / cost - 1) * 100:+.1f}%" if cost else "—"
        H.append(f'<tr class="{"hl" if label == "제안가" else ""}"><td><b>{label}</b></td><td class="n"><b>{_usd(p, 3)}</b> <small class="muted">{_usd(p)}</small></td>'
                 f'<td class="n">{"—" if disc is None else _pct(disc)}</td><td class="n {cls}">{vs}</td>'
                 f'<td class="n">{"—" if b is None else _pct(b, 1)}</td><td class="n {cls}">{"—" if room is None else f"{room:+.1f}%p"}</td></tr>')
    H.append('</table><p class="note">손익분기 가동률 = 가정 가동률 × 원가 ÷ 요금. 여유가 10%p 미만이면 주황, 원가보다 낮으면 빨강으로 표시합니다.</p>')

    H.append('<h2>승인 조건 <small>권고 판정의 근거와, 승인 전에 할 일</small></h2><div class="cond keep">')
    if found or applied:
        H.append("<ol>")
        for f in found:
            b = {"재검토": "b-rev", "조건부": "b-cond"}.get(f.level, "b-g")
            H.append(f'<li><span class="badge {b}">{e(f.level)}</span> {e(formal(f.message))}'
                     f'<small>→ {e(CONDITION.get(f.code, "사유를 확인하고 보고서에 남김"))}</small></li>')
        for x in applied:
            H.append(f'<li><span class="badge b-fix">고침</span> {e(formal(x["message"]))}'
                     f'<small>→ 승인 검토에서 제안대로 고침: {e(x["label"])} ({e(x["effect"])})</small></li>')
        H.append("</ol>")
    else:
        H.append('<p class="ok-t">승인 전에 확인할 점이 없습니다.</p>')
    H.append("</div>")
    terms_txt = f", {' · '.join(f'{t.years}년' for t in s.terms)} 약정가 {' / '.join(_usd(t.price) for t in s.terms)}" if s.terms else ""
    cond_txt = "위 승인 조건을 전제로 " if found else ""
    H.append(f'<div class="decide keep"><b>의사결정 요청</b>{e(model)} {gpus}장 서비스를 제안가 {_usd(cand)}{terms_txt}로 제안하는 것을 '
             f'{cond_txt}승인해 주시기 바랍니다.</div></section>')

    # ---------- 2쪽: 원가 산정 근거
    H.append('<section class="page">' + runhead)
    if m and m.count and m.min is not None and m.max is not None and m.median is not None:
        H.append(f'<h2 class="first">시장에서의 위치 <small>{e(m.model_name)} 온디맨드 공개가 {m.count}건 · 사업자 {m.providers}곳</small></h2>'
                 f'<div class="keep">{_market_svg(m, cost, cand)}<p class="note">{e(m.position)}. 리전·구성이 다른 참고 가격이며, 사업자별 대표값으로 통계를 냈습니다.</p></div>')
        H.append('<h2>원가는 이렇게 계산했습니다 <small>원가 서버(BE) 공식 · 이 보고서의 숫자를 그대로 넣음</small></h2>')
    else:
        H.append('<h2 class="first">원가는 이렇게 계산했습니다 <small>원가 서버(BE) 공식 · 이 보고서의 숫자를 그대로 넣음</small></h2>')
    cap_rule = "취득비 ÷ 감가상각 연수" if not rate else "취득비 × 자본회수계수(금리·연수로 계산)"
    price_rule = "원가 ÷ (1 − 목표 이익률)" if a.get("pricing_method") != "COST_MARKUP" else "원가 × (1 + 가산율)"
    H.append(f'''<div class="formula keep"><div class="eq">
<span>GPU·시간당 원가</span><span>= 연간 원가 ÷ 연간 유료 GPU 시간</span>
<span>연간 원가</span><span>= 연간 운영비 + 연간 자본회수액 (자본회수액 = {cap_rule})</span>
<span>연간 유료 GPU 시간</span><span>= GPU 수 × 8,760시간 × 유료 가동률</span>
<span>제안가</span><span>= {price_rule} · {e(method)}</span>
<span>기간별 계약가</span><span>= 제안가 × (1 − 약정 할인율)</span></div></div>''')
    steps: list[tuple[str, str, str]] = []
    if krw_total:
        steps.append(("연간 운영비 (원화)", "청구서 원가 항목" + (" + 사내 인건비·판관비" if inhouse else ""), _krw(krw_total)))
        steps.append(("연간 운영비 (달러)", f"{_krw(krw_total)} ÷ 환율 {fx:,.1f}원" + (" (+ 달러 항목)" if any(x[2] is None for x in rows_cost) else ""), _usd(operating, 0)))
    else:
        steps.append(("연간 운영비 (달러)", "청구서 원가 항목 합계", _usd(operating, 0)))
    crf_txt = f"회수계수 {crf:.4f}" + (f" (= 1 ÷ {years}년, 금리 0%)" if years and not rate else f" (금리 {_pct(rate, 2)}, {years}년)" if years else "")
    steps += [("연간 자본회수액", f"취득비 {_usd(acq, 0)} × {crf_txt}", _usd(capital, 0)),
              ("연간 원가", f"{_usd(operating, 0)} + {_usd(capital, 0)}", _usd(annual, 0)),
              ("연간 유료 GPU 시간", f"{gpus}장 × 8,760시간 × 가동률 {_pct(util)}", f"{hours:,.0f}시간"),
              ("GPU·시간당 원가", f"{_usd(annual, 0)} ÷ {hours:,.0f}시간", _usd(cost, 4)),
              ("제안가", f"{_usd(cost, 4)} ÷ (1 − {_pct(margin)})" if a.get("pricing_method") != "COST_MARKUP" else f"{_usd(cost, 4)} × (1 + {_pct(margin)})", _usd(cand, 4))]
    steps += [(f"{t.years}년 계약가", f"{_usd(cand, 4)} × (1 − {_pct(t.discount_rate)})", _usd(t.price, 4)) for t in s.terms]
    H.append('<table class="t calc keep"><tr><th></th><th>계산 단계</th><th>식 (이번 값)</th><th class="n">결과</th></tr>')
    for k, (name, formula_txt, val) in enumerate(steps, 1):
        hl = ' class="hl"' if name in ("GPU·시간당 원가", "제안가") else ""
        H.append(f'<tr{hl}><td class="step">{k}</td><td><b>{name}</b></td><td class="f">{e(formula_txt)}</td><td class="n"><b>{val}</b></td></tr>')
    H.append('</table><p class="note">자동 검증으로 위 식을 다시 계산해 원가 서버 결과와 맞는지 확인했습니다 (제안가 공식 · 기간별 가격 · 손익분기).</p>')

    H.append('<h3>원가 항목 원본 대조 <small class="muted">청구서 금액 → 연간 → 달러 환산</small></h3><table class="t keep">'
             '<tr><th>원가 항목</th><th>출처</th><th class="n">연간 (원)</th><th class="n">연간 (USD)</th></tr>')
    for name, where, k, u in rows_cost:
        H.append(f'<tr><td>{e(name)}</td><td class="muted">{e(where)}</td><td class="n">{"—" if k is None else f"{k:,.0f}"}</td><td class="n">{u:,.0f}</td></tr>')
    for it in s.final_items:
        if it.status.value == "INCLUDED_IN_OTHER":
            parent = ITEM_KO.get(it.included_in.value, "") if it.included_in else ""
            H.append(f'<tr><td class="muted">{e(ITEM_KO.get(it.item_type.value, it.item_type.value))}</td><td class="muted">{e(parent)}에 포함</td>'
                     '<td class="n muted">—</td><td class="n muted">—</td></tr>')
        elif it.status.value == "NOT_INCLUDED":
            H.append(f'<tr><td class="muted">{e(ITEM_KO.get(it.item_type.value, it.item_type.value))}</td><td class="warn-t">원가에서 제외</td>'
                     '<td class="n muted">—</td><td class="n muted">—</td></tr>')
    H.append(f'<tr class="total"><td>연간 운영비 합계</td><td></td><td class="n">{f"{krw_total:,.0f}" if krw_total else "—"}</td><td class="n">{operating:,.0f}</td></tr></table>')
    rec = d.reconciliation if d else None
    if rec is not None and rec.matched:
        H.append(f'<p class="note">청구서 총액 대조: {e(rec.basis or "원가 항목 합계와 청구 총액")} — 일치'
                 + (f" (원가 항목 {_krw(rec.cost_sum)}, 청구 총액 {_krw(rec.document_total)})" if rec.cost_sum and rec.document_total else "") + ".</p>")
    elif rec is not None and rec.matched is False:
        H.append('<p class="note warn-t">청구서 총액과 원가 항목 합계가 맞지 않아 담당자 확인 후 진행했습니다.</p>')
    H.append("</section>")

    # ---------- 3쪽: 구성과 가정
    H.append('<section class="page">' + runhead)
    if ap and ap.breakdown_ok and ap.breakdown:
        bench = {b["group"]: b for b in ap.benchmark}
        H.append(f'<h2 class="first">원가 구성 <small>업계 기준: 현대차증권 2026-09 표 18 GPUaaS 비용 구성</small></h2><div class="keep">{_mix_svg(ap)}</div>')
        H.append('<table class="t keep"><tr><th>묶음</th><th class="n">연간 (USD)</th><th class="n">GPU·시간당</th><th class="n">비중</th><th class="n">업계 기준</th></tr>')
        for b in ap.breakdown:
            ref = bench.get(b.group, {})
            odd = bool(ref) and not 0.5 <= ref.get("ratio", 1) <= 2.0
            H.append(f'<tr><td>{e(b.group)}</td><td class="n">{b.annual_usd:,.0f}</td><td class="n">{_usd(b.hourly_usd, 4)}</td>'
                     f'<td class="n {"warn-t" if odd else ""}">{_pct(b.share)}</td><td class="n muted">{_pct(ref["ref_share"]) if ref else "—"}</td></tr>')
        H.append(f'<tr class="total"><td>합계</td><td class="n">{sum(b.annual_usd for b in ap.breakdown):,.0f}</td><td class="n">{_usd(cost, 4)}</td><td class="n">100%</td><td></td></tr></table>')
        H.append('<h2>가정값과 출처')
    else:
        H.append('<h2 class="first">원가 구성</h2><p class="neg">원가 항목 합계가 원가 서버 결과와 맞지 않아 묶음별 분해를 싣지 않았습니다. 재검토 사유입니다.</p>')
        H.append('<h2>가정값과 출처')
    H.append(' <small>출처 등급: 사내 문서 · 공개자료 · 리포트 가정 · 예시값 · 담당자 입력</small></h2><table class="t keep">'
             '<tr><th>가정</th><th class="n">값</th><th>출처 등급</th><th>근거</th></tr>')
    src, grade = a.get("sources") or {}, a.get("grades") or {}
    acq_cur = a.get("acquisition_currency")
    spec = [("gpu_count", "GPU 수", f"{gpus}장"),
            ("acquisition_amount", "서버·GPU 취득비", _krw(_f(a.get("acquisition_amount"))) if acq_cur == "KRW" else _usd(_f(a.get("acquisition_amount")), 0)),
            ("depreciation_years", "감가상각 연수", f"{years}년" if years else "—"), ("utilization", "유료 가동률", _pct(util)),
            ("target_rate", "목표 이익률", _pct(margin)), ("fx_krw_per_usd", "환율", f"{fx:,.1f}원/달러" if fx else "—"),
            ("inhouse_annual", "사내 인건비·판관비", _krw(inhouse)),
            ("interest_rate_type", "조달 금리", _pct(rate, 2) if rate else "미반영")]
    for key, label, val in spec:
        g = grade.get(key, "")
        H.append(f'<tr><td>{label}</td><td class="n"><b>{val}</b></td><td><span class="badge {"b-cond" if g == "예시값" else "b-g"}">{e(g or "—")}</span></td>'
                 f'<td class="muted">{e(formal(src.get(key, "")))}</td></tr>')
    H.append('</table><p class="note">‘예시값’은 리포트의 예시 숫자를 그대로 쓴 값으로, 승인 전에 실제 견적·계약 값으로 바꿔야 합니다.</p>')
    if ap and ap.sensitivity:
        H.append('<h2>결론을 바꿀 수 있는 가정 <small>가정을 하나씩 바꿔 원가 서버로 다시 계산한 제안가 변화</small></h2>'
                 f'<div class="keep">{_sens_svg(ap.sensitivity)}</div>'
                 '<p class="note">빨강은 제안가가 오르는 방향(원가 상승), 회색은 내리는 방향입니다. 위쪽일수록 결론에 민감합니다.</p>')
    H.append("</section>")

    # ---------- 4쪽: 검증과 기록
    passed, failed = verified if verified is not None else V.verify(s)
    H.append('<section class="page">' + runhead + '<h2 class="first">검증과 결정 기록 <small>보고서 숫자를 다시 계산해 맞춰 본 결과와 담당자가 내린 결정</small></h2><div class="two">')
    H.append(f'<div><h3 class="first">자동 검증 {len(passed)}/{len(V.CHECKS)} 통과</h3><table class="t keep"><tr><th>검사</th><th>결과</th></tr>'
             + "".join(f'<tr><td>{e(c)}</td><td class="{"ok-t" if c in passed else "neg"}">{"통과" if c in passed else "실패" if c in failed else "—"}</td></tr>'
                       for c in V.CHECKS) + "</table></div>")
    dec_rows = [f'<tr><td>{e(formal(x["message"]))}</td><td class="ok-t">제안대로 고침<br><small class="muted">{e(x["label"])}</small></td></tr>' for x in applied]
    for f in found:
        dk = DECISION_KO.get(getattr(f, "decision", "") or "", "—")
        extra = f'<br><small class="muted">{e(f.reason)}</small>' if f.reason and f.reason != dk else ""
        dec_rows.append(f"<tr><td>{e(formal(f.message))}</td><td>{e(dk)}{extra}</td></tr>")
    H.append('<div><h3 class="first">담당자 결정 기록</h3><table class="t keep"><tr><th>항목</th><th>결정</th></tr>'
             + ("".join(dec_rows) or '<tr><td colspan="2" class="muted">결정할 항목이 없었습니다.</td></tr>') + "</table></div></div>")
    rs = V.risks(s)
    H.append("<h2>리스크</h2>" + ('<ul class="note">' + "".join(f"<li>{e(formal(x))}</li>" for x in rs) + "</ul>" if rs else '<p class="muted">남은 리스크 없음</p>'))
    if s.draft:
        on = [i.message for i in s.draft.interpretations if i.applied]
        if on:
            H.append("<h3>청구서 해석</h3><ul class=\"note\">" + "".join(f"<li>{e(formal(x))}</li>" for x in on) + "</ul>")
    H.append('''<h2>유의 사항</h2><ul class="note">
<li>이 보고서는 입력 가정에 따른 시뮬레이션이며 확정 견적이 아닙니다. 부가세는 포함하지 않았습니다.</li>
<li>원가 계산은 원가 서버(BE) 공식 한 곳에서 하고, 에이전트는 청구서 해석·가정값 확인·검토만 합니다. 문장은 코드 템플릿으로 만들었습니다 (AI 서술 없음).</li>
<li>청구서를 AI로 분류할 때는 금액·숫자·회사명을 가리고 항목명만 보냅니다.</li>
<li>시장 가격은 사업자 공개 요금을 수집한 참고값이며, 리전·상품 구성 차이를 포함합니다.</li></ul>''')
    H.append(f'<p class="note">자동 검증 {len(passed)}/{len(V.CHECKS)} 통과' + (f" · 실패: {e(', '.join(failed))}" if failed else "")
             + f" · 문서번호 {doc_no}</p>")
    H.append("</section></body></html>")
    return "".join(H)
