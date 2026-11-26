"""업계 기준선 검사 (설계 §4A-6 · 현대차증권 표 16·18, 하나증권 도표 3·4)."""
import pytest

from app.benchmarks import cost_check, gpu_checks, inhouse_suggestion
from app.models import ContractInfo
from tests.conftest import BUILDERS


@pytest.mark.parametrize("name", list(BUILDERS))
def test_correct_detailed_samples_pass_bill_checks(name, service, detailed_doc):
    assert service.ingest(detailed_doc(name)).bench == []


def test_thousand_won_off_trips_rent_check(service, detailed_doc):
    d = service.ingest(detailed_doc("bill_r2_multisheet_thousand_krw.xlsx"))
    d = service.set_interpretations(d.id, {"UNIT_SCALE"})
    assert [b.code for b in d.bench] == ["RENT_PER_KW", "POWER_PER_KW"]
    assert "26.3만~35만원" in d.bench[0].message and "현대차증권" in d.bench[0].source


def test_gpu_and_cost_checks_bounds():
    assert gpu_checks(ContractInfo(power_kw=1000), 504) == []
    assert gpu_checks(ContractInfo(power_kw=1000), 64)[0].code == "KW_PER_GPU"
    assert gpu_checks(ContractInfo(), 64) == []
    assert cost_check(2.4) == [] and cost_check(0.9)[0].code == "COST_PER_GPU_HOUR"
    assert cost_check(7.0)[0].code == "COST_PER_GPU_HOUR"


def test_inhouse_suggestion_r1(service, detailed_doc):
    d = service.ingest(detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    assert inhouse_suggestion(d.cost_items, d.currency, d.contract) == 1_337_000_000


def test_inhouse_suggestion_needs_contract(service, doc):
    d = service.ingest(doc("a"))
    assert inhouse_suggestion(d.cost_items, d.currency, d.contract) is None
