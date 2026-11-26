"""청구서 머리글·안내문에서 금액 단위, 청구 기간, 일할, 계약 정보를 읽는다 (설계 §4A-1)."""
from app.billmeta import read_meta
from app.models import ContractInfo
from tests.conftest import doc_from_rows


def test_r1_full_month_and_contract(detailed_doc):
    m = read_meta(detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    assert m.unit_multiplier == 1 and m.billing_months == 1 and m.prorate_days is None
    assert m.contract == ContractInfo(power_kw=1000, racks=25, gpus=504)


def test_r2_thousand_won(detailed_doc):
    m = read_meta(detailed_doc("bill_r2_multisheet_thousand_krw.xlsx"))
    assert (m.unit_multiplier, m.unit_label) == (1000, "천원") and m.unit_ref is not None
    assert m.contract.power_kw == 512


def test_r4_prorated(detailed_doc):
    m = read_meta(detailed_doc("bill_r4_prorated_first_month_krw.xlsx"))
    assert (m.prorate_days, m.month_days) == (17, 31)


def test_r5_quarter(detailed_doc):
    m = read_meta(detailed_doc("bill_r5_quarterly_no_currency.xlsx"))
    assert m.billing_months == 3 and "3분기" in m.months_text and m.contract.power_kw == 400


def test_r3_committed_power(detailed_doc):
    assert read_meta(detailed_doc("bill_r3_colo_mrc_nrc_usd.xlsx")).contract.power_kw == 800


def test_explicit_won_unit_is_not_scaled():  # Review Focus 1
    assert read_meta(doc_from_rows([["금액 단위", "원 (VAT 별도)"], ["전력 사용료", 100]])).unit_multiplier == 1


def test_old_start_date_is_not_prorated():  # Review Focus 4
    m = read_meta(doc_from_rows([["서비스 개시일", "2025-03-01"], ["청구월", "2026년 10월분"], ["전력", 100]]))
    assert m.prorate_days is None and m.billing_months == 1


def test_original_samples_have_no_meta(samples_all):
    for d in samples_all:
        m = read_meta(d)
        assert m.unit_multiplier == 1 and m.prorate_days is None and m.contract.power_kw is None, d.file_name
