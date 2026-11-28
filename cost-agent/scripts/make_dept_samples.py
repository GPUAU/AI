"""부서별 목데이터 PDF(8종 + 충돌 변형)를 다시 만든다 — Pretendard 글꼴, SKT 로고 머리글, 표 테두리.

    PRETENDARD_DIR=/path/to/fonts python scripts/make_dept_samples.py   → samples-depts/*.pdf, samples-depts/variants/*.pdf

글꼴은 Pretendard TTF(Regular·Bold)가 필요하다 (OTF는 reportlab이 넣지 못함). PRETENDARD_DIR 또는 아래 후보 경로에서 찾는다.
문서 내용(항목·값·문장)은 팀 목데이터와 같다. 모두 가상 시나리오의 내부 검토용 모의 자료다.

배치 규칙 (추출 규칙과 맞춤):
- 머리글 줄은 칸 2개(왼쪽 사업장, 오른쪽 검토명) → 제목으로 오인되지 않게
- 제목은 혼자 있는 첫 한 칸 줄 (문서 종류 판별이 제목을 먼저 본다)
- 제출·수신 줄도 칸 2개, 표 머리글은 숫자 없는 줄 중 칸이 가장 많게
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
LOGO = ROOT / "scripts" / "assets" / "skt-logo.png"
RED, INK, MUTED, LINE, SOFT = HexColor("#EA002C"), HexColor("#19212E"), HexColor("#6B7280"), HexColor("#D9DEE5"), HexColor("#F4F6F9")
W, H = A4
M = 48  # 좌우 여백
DATE = "2026.11.27"
SITE = "울산 데이터센터"
REVIEW = "GPU 사업 원가 검토 · 가상 시나리오"
NOTE = "내부 검토용 모의 자료 · 가상 시나리오"
FONT_CANDIDATES = [os.getenv("PRETENDARD_DIR", ""), str(Path.home() / "Library/Fonts"), "/usr/share/fonts/truetype/pretendard",
                   "/usr/local/share/fonts"]


def register_fonts() -> None:
    for d in FONT_CANDIDATES:
        reg, bold = Path(d) / "Pretendard-Regular.ttf", Path(d) / "Pretendard-Bold.ttf"
        if d and reg.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont("Pretendard", str(reg)))
            pdfmetrics.registerFont(TTFont("Pretendard-Bold", str(bold)))
            return
    sys.exit("Pretendard TTF(Regular·Bold)를 찾지 못했어요. PRETENDARD_DIR=<글꼴 폴더>로 알려 주세요.")


# 문서 정의: 제목, 부서, 소개 문장, 표 (머리글, 줄들, 열 너비 비율, 굵게 할 줄), 문장들
DOCS: list[dict] = [
    {"file": "01_구매팀_장비 도입 견적 명세.pdf", "dept": "구매팀", "title": "장비 도입 견적 명세",
     "intro": ["솔빛컴퓨팅(가상 업체)의 견적 SIM-261126-02를 기준으로 울산 데이터센터 H100 서버 4대의",
               "구매 금액을 제출합니다."],
     "head": ["품목", "수량", "단가(원)", "공급가액(원)"], "widths": [0.40, 0.14, 0.22, 0.24], "num_cols": [2, 3],
     "rows": [["H100 80GB × 8장 서버", "4대", "250,000,000", "1,000,000,000"], ["장착용 레일 키트", "4세트", "250,000", "1,000,000"],
              ["설치 및 시운전", "4대", "2,000,000", "8,000,000"], ["공급가액 합계", "", "", "1,009,000,000"],
              ["부가세", "", "", "100,900,000"], ["부가세 포함 합계", "", "", "1,109,900,000"]], "bold": [3, 5],
     "notes": ["견적일 2026.11.26 · 유효 기한 2026.12.10. 발주 후 8주 이내 납품하며, 계약금 30%와 검수 후",
               "잔금 70% 조건입니다.", "취득비 통화는 KRW, 공급가액은 1,009,000,000원입니다."]},
    {"file": "02_인프라운영팀_GPU 구성 및 공급 용량 명세.pdf", "dept": "인프라운영팀", "title": "GPU 구성 및 공급 용량 명세",
     "intro": ["구매팀의 H100 도입 견적을 기준으로 서비스에 투입할 장비 구성과 연간 공급 가능 시간을 산정했습니다."],
     "head": ["항목", "산출 근거", "제출값"], "widths": [0.30, 0.40, 0.30], "num_cols": [2],
     "rows": [["GPU 모델", "견적 SIM-261126-02", "H100 80GB"], ["서버 구성", "4대 × 대당 GPU 8장", "GPU 32장"],
              ["연간 공급 가능 시간", "32장 × 365일 × 24시간", "280,320 GPU·시간"]], "bold": [],
     "notes": ["전체 서버가 1년간 설치·가동된다는 계획 가정입니다.", "GPU 모델과 GPU 수 입력란에 H100 80GB와 32장을 적용합니다."]},
    {"file": "03_시설운영팀_월 시설비 명세 및 모의 수납 영수증.pdf", "dept": "시설운영팀", "title": "월 시설비 명세 및 모의 수납 영수증",
     "intro": ["동일 규모 장비군의 2026년 10월 정산 사례를 가정해 신규 서비스의 시설비 산정 근거를 제출합니다."],
     "head": ["항목", "계산 근거", "공급가액(원)"], "widths": [0.30, 0.40, 0.30], "num_cols": [2],
     "rows": [["전력·냉각", "24,000 kWh × 200원", "4,800,000"], ["상면 임차", "랙 2개 × 1,200,000원", "2,400,000"],
              ["전용 회선", "1회선 월 정액", "800,000"], ["월 합계", "", "8,000,000"], ["부가세", "", "800,000"],
              ["수납액", "", "8,800,000"]], "bold": [3, 5],
     "notes": ["모의 영수증 SIM-OPS-1026 · 수납처 울산시설서비스(가상) · 납부 부서 시설운영팀 · 2026.11.10",
               "계좌이체 수납 가정.", "연간 시설비는 월 공급가액 × 12개월인 96,000,000원입니다. 청구서 항목에 한 번만 반영합니다."]},
    {"file": "04_재무회계팀_장비 취득비 및 감가상각 명세.pdf", "dept": "재무회계팀", "title": "장비 취득비 및 감가상각 명세",
     "intro": ["사업 원가 검토에 사용할 H100 서버 4대의 감가상각 가정을 제출합니다."],
     "head": ["항목", "산정 기준", "제출값"], "widths": [0.30, 0.40, 0.30], "num_cols": [2],
     "rows": [["취득비", "장비·레일·설치비 공급가액", "1,009,000,000원"], ["상각 방식", "잔존가치 0원 가정", "정액법"],
              ["내용연수", "사업 검토 가정", "5년"], ["연간 감가상각비", "취득비 ÷ 5년", "201,800,000원"]], "bold": [],
     "notes": ["취득비는 구매팀의 모의 견적 SIM-261126-02를 기준으로 했습니다.",
               "화면에 취득비와 상각 연수를 입력하면 연간 감가상각비가 원가에 반영됩니다."]},
    {"file": "05_관리회계팀_사내 인건비 및 판관비 배부 명세.pdf", "dept": "관리회계팀", "title": "사내 인건비 및 판관비 배부 명세",
     "intro": ["울산 H100 장비군 32장에 귀속할 연간 사내 비용을 운영 인력 투입률과 공통비 배부율로 산정했습니다."],
     "head": ["항목", "배부 기준", "연간 배부액"], "widths": [0.30, 0.40, 0.30], "num_cols": [2],
     "rows": [["운영 인건비", "80,000,000원 × 투입률 60%", "48,000,000원"], ["영업·관리 공통비", "120,000,000원 × 배부율 20%", "24,000,000원"],
              ["합계", "", "72,000,000원"]], "bold": [2],
     "notes": ["인건비 총액과 투입률, 공통비와 배부율은 모의 예산 기준입니다.",
               "사내 인건비·판관비(연간)에 72,000,000원을 입력합니다. 시설 청구 비용은 별도 집계합니다."]},
    {"file": "06_영업팀_유료 사용시간 및 가동률 계획.pdf", "dept": "영업팀", "title": "유료 사용시간 및 가동률 계획",
     "intro": ["계약 및 단기 판매 예상 물량을 연간 GPU 공급 가능 시간 대비 비율로 제출합니다."],
     "head": ["항목", "산출 근거", "연간 GPU·시간"], "widths": [0.30, 0.40, 0.30], "num_cols": [2],
     "rows": [["공급 가능 시간", "32장 × 365일 × 24시간", "280,320"], ["장기계약 예상", "공급 가능 시간 × 50%", "140,160"],
              ["단기 판매 예상", "공급 가능 시간 × 20%", "56,064"], ["합계 유료 판매 예상", "장기계약 + 단기 판매", "196,224"]], "bold": [3],
     "notes": ["판매 비중은 향후 1년 계획에 대한 가정입니다.", "유료 가동률은 196,224 ÷ 280,320 = 70%입니다."]},
    {"file": "07_자금팀_가격 검토용 환율 기준.pdf", "dept": "자금팀", "title": "가격 검토용 환율 기준",
     "intro": ["원화로 제출된 H100 원가와 판매가를 달러로 환산하기 위한 사업계획 환율을 제출합니다."],
     "head": ["항목", "제출 기준"], "widths": [0.30, 0.70], "num_cols": [],
     "rows": [["통화쌍", "미국 달러 / 대한민국 원"], ["계획 환율", "1 USD = 1,350 KRW"], ["적용 방식", "원화 금액 ÷ 1,350 = 달러 금액"]], "bold": [],
     "notes": ["본 환율은 2026.11.27 작성한 가상 사업계획의 공통 가정입니다.",
               "환율(원/달러) 입력란에 1,350을 적용하고, 실제 견적 발행 시 적용 환율을 다시 확인합니다."]},
    {"file": "08_사업기획팀_목표 마진 및 판매가 검토안.pdf", "dept": "사업기획팀", "title": "목표 마진 및 판매가 검토안", "to": "프라이싱 담당",
     "intro": ["프라이싱 담당자에게 H100 서비스의 목표 이익률과 부서 제출값을 반영한 시간당 판매가 검산 결과를",
               "전달합니다."],
     "head": ["항목", "계산 근거", "결과"], "widths": [0.26, 0.46, 0.28], "num_cols": [2],
     "rows": [["연간 원가", "감가상각 201.8 + 시설 96 + 사내비 72 (백만원)", "369,800,000원"],
              ["유료 판매시간", "GPU 32장 × 8,760시간 × 70%", "196,224 GPU·시간"], ["시간당 원가", "연간 원가 ÷ 유료 판매시간", "약 1,885원"],
              ["목표 마진", "매출 기준 이익률", "20%"], ["시간당 목표 판매가", "반올림 전 시간당 원가 ÷ 0.8", "약 2,356원"],
              ["달러 표시 판매가", "반올림 전 판매가 ÷ 1,350", "약 1.74 USD"]], "bold": [4],
     "notes": ["부가세 별도, GPU 1장·1시간 기준입니다.", "마진 방식은 매출 기준 이익률, 목표 마진은 20%를 적용합니다."]},
]

# 시연용 충돌 변형: 재무회계팀 취득비가 구매팀 견적과 다름
VARIANTS: list[dict] = [
    {**DOCS[3], "file": "04_재무회계팀_장비 취득비 및 감가상각 명세(수정본).pdf", "title": "장비 취득비 및 감가상각 명세 (수정본)",
     "rows": [["취득비", "장비·레일·설치·예비부품 공급가액", "1,100,000,000원"], ["상각 방식", "잔존가치 0원 가정", "정액법"],
              ["내용연수", "사업 검토 가정", "5년"], ["연간 감가상각비", "취득비 ÷ 5년", "220,000,000원"]],
     "notes": ["예비부품을 포함한 재무회계 기준 취득비입니다. 구매팀 견적과 다릅니다.",
               "화면에 취득비와 상각 연수를 입력하면 연간 감가상각비가 원가에 반영됩니다."]},
]


def draw_doc(d: dict, out: Path) -> None:
    c = canvas.Canvas(str(out), pagesize=A4)
    c.setTitle(d["title"])
    c.setAuthor(f"{d['dept']} (가상 시나리오)")
    y = H - 46
    # 머리글: 로고 · 사업장 | 검토명·날짜
    logo = ImageReader(str(LOGO))
    lw, lh = 64, 64 * 252 / 640
    c.drawImage(logo, M, y - lh + 6, width=lw, height=lh, mask="auto")
    c.setFont("Pretendard", 8.5)
    c.setFillColor(MUTED)
    c.drawString(M + lw + 12, y - 8, SITE)
    c.drawRightString(W - M, y - 8, f"{REVIEW} · {DATE}")
    y -= 22
    c.setStrokeColor(RED)
    c.setLineWidth(1.6)
    c.line(M, y, W - M, y)
    # 제목
    y -= 40
    c.setFillColor(INK)
    c.setFont("Pretendard-Bold", 20)
    c.drawString(M, y, d["title"])
    # 제출 · 수신 | 작성일
    y -= 24
    c.setFont("Pretendard", 9.5)
    c.setFillColor(MUTED)
    c.drawString(M, y, f"제출 {d['dept']}")
    c.drawString(M + 150, y, f"수신 {d.get('to', '사업기획팀 · 프라이싱 담당')}")
    c.drawRightString(W - M, y, f"작성 {DATE}")
    # 소개 문장
    y -= 30
    c.setFillColor(INK)
    c.setFont("Pretendard", 10)
    for t in d["intro"]:
        c.drawString(M, y, t)
        y -= 16
    # 표
    y -= 14
    tw = W - 2 * M
    xs = [M]
    for r in d["widths"][:-1]:
        xs.append(xs[-1] + r * tw)
    row_h = 24
    def cell_row(cells, bold=False, header=False):
        nonlocal y
        if header:
            c.setFillColor(SOFT)
            c.rect(M, y - row_h + 7, tw, row_h, stroke=0, fill=1)
        c.setStrokeColor(LINE)
        c.setLineWidth(0.6)
        c.line(M, y - row_h + 7, W - M, y - row_h + 7)
        c.setFillColor(INK if not header else MUTED)
        c.setFont("Pretendard-Bold" if bold or header else "Pretendard", 9.5 if not header else 9)
        for i, t in enumerate(cells):
            if not t:
                continue
            right = xs[i + 1] - 10 if i + 1 < len(xs) else W - M - 10
            if i in d["num_cols"] or (not header and i == len(cells) - 1):
                c.drawRightString(right, y - 9, t)
            else:
                c.drawString(xs[i] + 10, y - 9, t)
        y -= row_h
    c.setStrokeColor(INK)
    c.setLineWidth(1)
    c.line(M, y + 7, W - M, y + 7)
    cell_row(d["head"], header=True)
    for i, r in enumerate(d["rows"]):
        cell_row(r, bold=i in d["bold"])
    # 메모 (문장)
    y -= 22
    box_h = 16 * len(d["notes"]) + 18
    c.setFillColor(SOFT)
    c.roundRect(M, y - box_h + 12, tw, box_h, 6, stroke=0, fill=1)
    c.setFillColor(INK)
    c.setFont("Pretendard", 9.5)
    y -= 4
    for t in d["notes"]:
        c.drawString(M + 14, y, t)
        y -= 16
    # 바닥글
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.line(M, 52, W - M, 52)
    c.setFont("Pretendard", 8)
    c.setFillColor(MUTED)
    c.drawString(M, 40, d["dept"])
    c.drawRightString(W - M, 40, f"{NOTE} · 1 / 1")
    c.showPage()
    c.save()


def main() -> None:
    register_fonts()
    out = ROOT / "samples-depts"
    (out / "variants").mkdir(parents=True, exist_ok=True)
    for d in DOCS:
        draw_doc(d, out / d["file"])
        print("wrote", d["file"])
    for d in VARIANTS:
        draw_doc(d, out / "variants" / d["file"])
        print("wrote variants/", d["file"])


if __name__ == "__main__":
    main()
