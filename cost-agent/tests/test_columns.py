"""시트별 금액 열 역할 (설계 §4A-2)."""
from app.columns import ColRole, column_roles, header_text, preferred_col
from tests.conftest import doc_from_rows


def test_r1_supply_vat_gross(detailed_doc):
    r = column_roles(detailed_doc("bill_r1_kr_metered_krw.xlsx"))["청구서"]
    assert (r[7], r[8], r[9], r[4], r[6]) == (ColRole.SUPPLY, ColRole.VAT, ColRole.GROSS, ColRole.QTY, ColRole.RATE)


def test_r3_mrc_nrc(detailed_doc):
    r = column_roles(detailed_doc("bill_r3_colo_mrc_nrc_usd.xlsx"))["Invoice"]
    assert (r[6], r[7], r[5]) == (ColRole.MRC, ColRole.NRC, ColRole.RATE)


def test_r4_base_billed(detailed_doc):
    r = column_roles(detailed_doc("bill_r4_prorated_first_month_krw.xlsx"))["청구서"]
    assert (r[1], r[4], r[3]) == (ColRole.BASE, ColRole.BILLED, ColRole.QTY)


def test_r5_merged_header(detailed_doc):
    d = detailed_doc("bill_r5_quarterly_no_currency.xlsx")
    r = column_roles(d)["3분기"]
    assert (r[2], r[3], r[4]) == (ColRole.SUPPLY, ColRole.VAT, ColRole.GROSS)
    assert "공급가액" in header_text(d)["3분기"][2]


def test_gross_found_by_ratio_without_header():
    d = doc_from_rows([["항목", "", ""], *[[f"x{i}", 100 * i, 110 * i] for i in range(1, 5)]])
    assert column_roles(d)["s"][2] == ColRole.GROSS


def test_equal_columns_are_not_gross():  # Review Focus 3
    d = doc_from_rows([["항목", "단가", "금액"], *[[f"x{i}", 100 * i, 100 * i] for i in range(1, 5)]])
    assert ColRole.GROSS not in column_roles(d)["s"].values()


def test_preferred_col_skips_empty_cell(detailed_doc):
    d = detailed_doc("bill_r1_kr_metered_krw.xlsx")
    roles = column_roles(d)["청구서"]
    due = next(r for r in d.rows if r.label.startswith("총 납부하실 금액"))  # 숫자가 합계 열에만 있음
    first = next(r for r in d.rows if "고밀도 랙 상면료" in r.label)
    assert preferred_col(roles, due) is None and preferred_col(roles, first) == 7


def test_original_samples_keep_simple_roles(samples_all):
    # 기존 a~i에는 VAT·GROSS·NRC·BASE 열이 없다 → 해석 대상이 생기면 안 된다
    for d in samples_all:
        for roles in column_roles(d).values():
            assert not {ColRole.VAT, ColRole.GROSS, ColRole.NRC, ColRole.BASE} & set(roles.values()), d.file_name
