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


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    bill_a(OUT / "bill_a_domestic_krw.xlsx")
    bill_b(OUT / "bill_b_colo_usd.xlsx")
    bill_c(OUT / "bill_c_multisheet_krw.xlsx")
    print("samples:", sorted(p.name for p in OUT.glob("*.xlsx")))
