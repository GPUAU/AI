import pytest

from app.costsheet import FIELDS, Cell, add_component, assumption_values, empty_sheet, to_cost_items
from app.models import ItemStatus, ItemType


def _items(s):
    return {i.item_type: i for i in to_cost_items(s)}


def test_empty_sheet_has_every_spring_field():
    s = empty_sheet()
    assert {"rack", "rent", "electricity", "network", "operations", "gpu_lease", "memory", "other", "acquisition",
            "gpu_model", "gpu_count", "depreciation_years", "utilization", "pricing_method", "target_rate",
            "fx_krw_per_usd", "interest_rate_type", "annual_interest_rate", "discount_3", "discount_5"} <= set(s.cells)
    assert all(c.status == "empty" for c in s.cells.values())
    assert {f.key: f.owner for f in FIELDS}["fx_krw_per_usd"] == "자금팀"


def test_operations_components_are_summed():
    s = empty_sheet()
    add_component(s, "operations", "청구서 운영비", 12_000_000)
    add_component(s, "operations", "사내 인건비·판관비", 72_000_000)
    ops = _items(s)[ItemType.OPERATIONS]
    assert ops.amount == 84_000_000 and ops.period.value == "YEAR" and ops.status == ItemStatus.USER_INPUT
    assert s.cells["operations"].status == "filled" and len(s.cells["operations"].components) == 2


def test_included_and_excluded_cells_map_to_status():
    s = empty_sheet()
    s.cells["rack"] = Cell(key="rack", value=28_800_000, unit="원", currency="KRW", period="YEAR", status="filled")
    s.cells["rent"].status = "included"
    s.cells["memory"].status = "excluded"
    it = _items(s)
    assert it[ItemType.RENT].status == ItemStatus.INCLUDED_IN_OTHER and it[ItemType.RENT].included_in == ItemType.RACK_FACILITY
    assert it[ItemType.MEMORY].status == ItemStatus.NOT_INCLUDED
    assert it[ItemType.RACK_FACILITY].amount == 28_800_000 and it[ItemType.RACK_FACILITY].currency.value == "KRW"


def test_acquisition_is_one_time():
    s = empty_sheet()
    s.cells["acquisition"] = Cell(key="acquisition", value=1_009_000_000, unit="원", currency="KRW", status="filled")
    assert _items(s)[ItemType.ACQUISITION].period.value == "ONE_TIME"


def test_conflict_cell_cannot_be_sent():
    s = empty_sheet()
    s.cells["acquisition"].status = "conflict"
    with pytest.raises(ValueError):
        to_cost_items(s)


def test_assumption_values_from_cells():
    s = empty_sheet()
    for k, v, u in [("utilization", 0.7, "%"), ("target_rate", 0.2, "%"), ("gpu_count", 32, "장"),
                    ("fx_krw_per_usd", 1350, "KRW/USD"), ("depreciation_years", 5, "년"),
                    ("gpu_model", "H100 80GB", "model"), ("pricing_method", "REVENUE_MARGIN", "")]:
        s.cells[k] = Cell(key=k, value=v, unit=u, status="filled")
    a = assumption_values(s)
    assert a == {"utilization": 0.7, "target_rate": 0.2, "gpu_count": 32, "fx_krw_per_usd": 1350,
                 "depreciation_years": 5, "model_name": "H100 80GB", "pricing_method": "REVENUE_MARGIN"}
