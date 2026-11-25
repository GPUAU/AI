"""양식이 서로 다른 가상 IDC 청구서 3종을 만든다. (실제 사업자 양식·금액이 아님)

    python scripts/make_samples.py   → samples/*.xlsx
"""

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

OUT = Path(__file__).resolve().parent.parent / "samples"


def bill_a(path: Path) -> None:
    """A형: 국내 IDC, 세로 표, 항목·수량·단가·금액, 소계·부가세·합계. 원화 월 청구."""
    wb = Workbook()
    ws = wb.active
    ws.title = "청구서"
    ws.append(["가상IDC A 서비스 이용 청구서"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(["청구월", "2026-10", "", "고객", "GPU AU"])
    ws.append([])
    ws.append(["구분", "항목", "수량", "단위", "단가(원)", "금액(원)"])
    rows = [
        ("상면", "상면 사용료 (42U 랙)", 4, "랙", 1_200_000),
        ("전력", "전력 사용료", 28_800, "kWh", 180),
        ("회선", "인터넷 회선 10Gbps", 1, "회선", 2_500_000),
        ("운영", "운영관리비", 1, "식", 1_800_000),
    ]
    for kind, name, qty, unit, price in rows:
        ws.append([kind, name, qty, unit, price, qty * price])
    cost = sum(q * p for _, _, q, _, p in rows)
    ws.append(["", "소계", "", "", "", cost])
    ws.append(["", "부가세(10%)", "", "", "", round(cost * 0.1)])
    ws.append(["", "합계", "", "", "", cost + round(cost * 0.1)])
    wb.save(path)


def bill_b(path: Path) -> None:
    """B형: 해외 코로케이션, 영어, 전력이 코로케이션 요금에 묶여 있음. 달러 월 청구."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice"
    ws.append(["Example Colo Inc. — Monthly Invoice"])
    ws.append(["Invoice period", "Oct 2026", "Currency", "USD"])
    ws.append([])
    ws.append(["Description", "Qty", "Unit Price (USD)", "Amount (USD)"])
    rows = [
        ("Private Cage Colocation - Power 40kW included", 1, 18_500.00),
        ("Cross Connect (Single-mode fiber)", 4, 350.00),
        ("IP Transit 10Gbps commit", 1, 2_200.00),
        ("Remote Hands (hours)", 6, 150.00),
    ]
    for name, qty, price in rows:
        ws.append([name, qty, price, qty * price])
    total = sum(q * p for _, q, p in rows)
    ws.append(["Total Amount Due", "", "", total])
    wb.save(path)


def bill_c(path: Path) -> None:
    """C형: 요약·상세 2개 시트, 낯선 항목명(동력비·공간사용료·IX 접속료), 할인 줄, 공급가액·세액·청구금액.
    운영·유지보수 항목이 없다 (누락 경고 확인용)."""
    wb = Workbook()
    s = wb.active
    s.title = "요약"
    d = wb.create_sheet("상세내역")
    d.append(["No", "청구 항목", "산정 기준", "청구액"])
    rows = [
        (1, "공간사용료", "전용 구역 30㎡", 9_000_000),
        (2, "동력비", "계약전력 40kW × 720h × 175원", 5_040_000),
        (3, "IX 접속료", "10G × 2포트", 3_000_000),
        (4, "스토리지 백업 서비스", "100TB", 1_500_000),
        (5, "출입 카드 발급 수수료 (1회)", "10매", 200_000),
    ]
    for r in rows:
        d.append(list(r))
    cost = sum(r[3] for r in rows)
    discount = 500_000
    d.append(["", "장기계약 할인", "", -discount])
    supply = cost - discount
    tax = round(supply * 0.1)
    s.append(["가상데이터센터 C 이용요금 청구 요약"])
    s.append(["청구 기간", "2026년 10월분"])
    s.append([])
    s.append(["구분", "금액"])
    s.append(["공급가액", supply])
    s.append(["세액", tax])
    s.append(["청구금액", supply + tax])
    wb.save(path)


def bill_d(path: Path) -> None:
    """D형: 금액 칸이 맨 오른쪽이 아님 (항목 | 청구금액 | 수량 | 단가 | 비고).
    '가장 오른쪽 숫자'를 금액으로 보는 규칙 매퍼는 틀리고, AI는 머리글로 맞혀야 한다. 비고에도 숫자가 있음."""
    wb = Workbook()
    ws = wb.active
    ws.title = "청구내역"
    ws.append(["가상IDC D 월 이용료 청구서 (2026년 10월분)"])
    ws.append([])
    ws.append(["항목", "청구금액", "수량", "단가", "비고"])
    rows = [
        ("랙 상면료", 3_600_000, 3, 1_200_000, "계약번호 2024118"),
        ("전력 사용료", 4_320_000, 24_000, 180, "검침 24000"),
        ("인터넷 회선 (10G)", 2_000_000, 1, 2_000_000, "회선 1"),
        ("기술지원 서비스", 1_500_000, 1, 1_500_000, "월 정액"),
    ]
    for r in rows:
        ws.append(list(r))
    total = sum(r[1] for r in rows)
    ws.append(["공급가액", total, "", "", ""])
    ws.append(["부가세", round(total * 0.1), "", "", ""])
    ws.append(["총 청구금액", total + round(total * 0.1), "", "", ""])
    wb.save(path)


def bill_e(path: Path) -> None:
    """E형: 청구 총액에 표에 없는 금액(45만 원)이 포함됨 → 재분류로도 맞출 수 없음 → 담당자에게 질문."""
    wb = Workbook()
    ws = wb.active
    ws.title = "청구서"
    ws.append(["가상IDC E 이용요금 (2026-10)"])
    ws.append(["항목", "금액(원)"])
    rows = [("상면 사용료", 4_000_000), ("전력 사용료", 3_000_000), ("회선료", 1_500_000), ("운영관리비", 1_000_000)]
    for r in rows:
        ws.append(list(r))
    ws.append(["합계", sum(v for _, v in rows) + 450_000])  # 표에 없는 45만 원
    wb.save(path)


def bill_f(path: Path) -> None:
    """F형: 영어·달러·연간 계약 청구 (Annual Service Agreement). 월 ×12를 하면 안 됨. 전력은 kWh로 별도."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Annual Invoice"
    ws.append(["Example DataHall Ltd. — Annual Service Agreement Invoice"])
    ws.append(["Service term", "Jan 2026 - Dec 2026 (annual)", "Currency", "USD"])
    ws.append([])
    ws.append(["Item", "Basis", "Annual Amount (USD)"])
    rows = [
        ("Dedicated Suite Space", "120 sqm", 216_000),
        ("Metered Power", "350,000 kWh", 42_000),
        ("Bandwidth 20Gbps", "committed", 30_000),
        ("Managed Operations & Monitoring", "24x7", 24_000),
    ]
    for r in rows:
        ws.append(list(r))
    ws.append(["Total (annual)", "", sum(r[2] for r in rows)])
    wb.save(path)


def bill_g(path: Path) -> None:
    """G형: CSV · GPU 임차·메모리 항목 · 할인 · 부가세 · 네트워크 비용 없음 (누락 질문 확인용)."""
    rows = [
        ["가상클라우드 G GPU 인프라 청구 (2026년 10월분)", ""],
        ["항목", "금액(원)"],
        ["GPU 서버 임차료 (H200 8장)", "28,000,000"],
        ["추가 메모리 증설", "1,200,000"],
        ["코로케이션 공간 사용료", "6,000,000"],
        ["전력 요금", "4,500,000"],
        ["원격 관제·운영 지원", "1,300,000"],
        ["신규 계약 할인", "-1,000,000"],
        ["공급가액", "40,000,000"],
        ["부가가치세", "4,000,000"],
        ["청구 합계", "44,000,000"],
    ]
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)


def bill_h(path: Path) -> None:
    """H형: 통합 패키지 한 줄에 상면·전력·회선·관제가 모두 포함 → 묶음(INCLUDED_IN_OTHER) 처리, 누락 없음."""
    wb = Workbook()
    ws = wb.active
    ws.title = "청구서"
    ws.append(["가상IDC H 통합 호스팅 청구서", "2026년 10월분"])
    ws.append(["서비스", "내역", "금액(원)"])
    ws.append(["통합 호스팅 패키지", "랙 4개 (상면·전력 40kW·10G 회선·24시간 관제 포함)", 12_000_000])
    ws.append(["추가 IP 블록", "/28", 150_000])
    ws.append(["소계", "", 12_150_000])
    ws.append(["부가세", "", 1_215_000])
    ws.append(["합계", "", 13_365_000])
    wb.save(path)


def bill_i(path: Path) -> None:
    """I형: 지저분한 실무형 양식 + 민감정보. 고객사명·담당자 이메일·전화가 머리글에 있고, 빈 줄·메모 줄이 섞여 있음.
    SENSITIVE_TERMS="주식회사 한빛데이터,한빛데이터"로 실행하면 AI 송출 내용에서 고객사명이 [가림]으로 바뀌어야 한다."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["[가상IDC I] 데이터센터 서비스 요금 청구서"])
    ws.append(["고객사", "주식회사 한빛데이터", "", "담당", "billing@hanbit-data.example / 02-1234-5678"])
    ws.append(["청구월", "2026년 10월분", "", "계약번호", "HB-2026-0042"])
    ws.append([])
    ws.append(["※ 아래 금액은 부가세 별도입니다."])
    ws.append(["구분", "세부 항목", "", "", "금액"])
    ws.append(["공간", "케이지 임대 (한빛데이터 전용)", "", "", 7_000_000])
    ws.append(["", "UPS·항온항습 유지보수", "", "", 900_000])
    ws.append([])
    ws.append(["전력", "사용 전력 (kWh 검침)", "", "", 5_600_000])
    ws.append(["네트워크", "IX 10G × 2", "", "", 2_400_000])
    ws.append(["보안", "보안관제 서비스", "", "", 800_000])
    ws.append(["", "", "", "공급가액", 16_700_000])
    ws.append(["", "", "", "부가세", 1_670_000])
    ws.append(["", "", "", "청구금액", 18_370_000])
    ws.append(["문의: 정산팀 02-1234-5678 (평일 9~18시)"])
    wb.save(path)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    bill_a(OUT / "bill_a_domestic_krw.xlsx")
    bill_b(OUT / "bill_b_colo_usd.xlsx")
    bill_c(OUT / "bill_c_multisheet_krw.xlsx")
    bill_d(OUT / "bill_d_amount_col_left_krw.xlsx")
    bill_e(OUT / "bill_e_total_mismatch_krw.xlsx")
    bill_f(OUT / "bill_f_annual_usd.xlsx")
    bill_g(OUT / "bill_g_gpu_lease_krw.csv")
    bill_h(OUT / "bill_h_bundle_krw.xlsx")
    bill_i(OUT / "bill_i_messy_sensitive_krw.xlsx")
    print("samples:", sorted(p.name for p in OUT.iterdir() if p.suffix in (".xlsx", ".csv")))
