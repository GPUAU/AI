"""청구서 해석 적용과 해제 (설계 §4A-2~4A-4)."""
import pytest

from app.models import Currency, ItemType
from tests.conftest import doc_from_rows


def codes(d):
    return {i.code for i in d.interpretations}


def interp(d, code):
    return next(i for i in d.interpretations if i.code == code)


def test_r2_unit_summary_and_one_time(service, detailed_doc):
    d = service.ingest(detailed_doc("bill_r2_multisheet_thousand_krw.xlsx"))
    assert {"UNIT_SCALE", "SUMMARY_SHEET"} <= codes(d)
    assert interp(d, "UNIT_SCALE").impact == pytest.approx(0.001, rel=0.01)
    assert d.unit_multiplier == 1000
    assert d.reconciliation.matched is True


def test_r1_gross_column_and_non_cost(service, detailed_doc):
    d = service.ingest(detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    gross = interp(d, "GROSS_COLUMN")
    assert "'합계금액" in gross.message and "'공급가액" in gross.message
    assert gross.impact == pytest.approx(1.1, rel=0.01)
    assert "NON_COST_ROWS" in codes(d) and d.reconciliation.matched is True


def test_r3_nrc_to_acquisition(service, detailed_doc):
    d = service.ingest(detailed_doc("bill_r3_colo_mrc_nrc_usd.xlsx"))
    acq = next(c for c in d.cost_items if c.item_type == ItemType.ACQUISITION)
    assert acq.amount == pytest.approx(73_000)
    assert "NRC_ONE_TIME" in codes(d) and d.reconciliation.matched is True


def test_r4_prorated_annualization(service, detailed_doc):
    d = service.ingest(detailed_doc("bill_r4_prorated_first_month_krw.xlsx"))
    rack = next(c for c in d.cost_items if c.item_type == ItemType.RACK_FACILITY)
    assert rack.amount == 48_000_000 * 12
    assert "PRORATED" in codes(d) and d.reconciliation.matched is True


def test_r5_quarter(service, detailed_doc):
    d = service.ingest(detailed_doc("bill_r5_quarterly_no_currency.xlsx"))
    assert d.billing_months == 3
    assert interp(d, "BILLING_MONTHS").impact == pytest.approx(3.0, rel=0.01)


def test_toggle_interpretation_is_idempotent(service, detailed_doc):  # Review Focus 5
    d0 = service.ingest(detailed_doc("bill_r2_multisheet_thousand_krw.xlsx"))
    off = service.set_interpretations(d0.id, {"UNIT_SCALE"})
    assert off.unit_multiplier == 1 and not interp(off, "UNIT_SCALE").applied
    d2 = service.set_interpretations(d0.id, set())
    assert [c.model_dump() for c in d2.cost_items] == [c.model_dump() for c in d0.cost_items]
    d3 = service.set_currency(d0.id, Currency.KRW)
    assert [i.impact for i in d3.interpretations] == [i.impact for i in d0.interpretations]


def test_summary_sheet_kept_when_it_is_the_only_cost_sheet(service):  # Review Focus 2
    d = service.ingest(doc_from_rows([["청구 요약", "금액(원)"], ["전력 사용료", 100], ["상면 사용료", 200], ["합계", 300]],
                                     sheet="요약"))
    assert "SUMMARY_SHEET" not in codes(d) and d.reconciliation.matched is True
