from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from gpu_collector.erd import power

from .support import create_database, connect, drop_database


SOURCE = ("https://www.eia.gov/electricity/wholesale/xls/ice_electric-2026.xlsx",
          "ice_electric-2026.xlsx", "a" * 64, 2)


def observation():
    return power.parse_row(
        ("Indiana Hub RT Peak", datetime(2026, 3, 19), "03/20/2026", "2026-03-20",
         None, "-12.25", -10.75, 0, "   1,600", 2, 3), SOURCE
    )


def test_parse_ice_mixed_cell_types():
    row = observation()
    assert row.trade_date == date(2026, 3, 19)
    assert row.delivery_start_date == row.delivery_end_date == date(2026, 3, 20)
    assert row.high_usd_mwh is None
    assert row.weighted_avg_usd_mwh == Decimal("-10.75")
    assert row.change_usd_mwh == Decimal("0")
    assert row.daily_volume_mwh == Decimal("1600")
    assert row[11:] == SOURCE


def test_rejects_conflicting_keys_in_same_import(monkeypatch):
    row = observation()
    correction = row._replace(weighted_avg_usd_mwh=Decimal("-9.75"))
    monkeypatch.setattr(power, "read_workbook", lambda _: [row, correction])
    with pytest.raises(ValueError, match="서로 다른 가격"):
        power.collect_rows([Path("ice_electric-2026.xlsx")])


def test_import_is_idempotent_and_can_update_changed_value():
    name = create_database()
    try:
        with connect(name) as conn:
            row = observation()
            assert power.write_observations(conn, [row]) == 1
            assert power.write_observations(conn, [row._replace(source_sha256="b" * 64)]) == 0
            saved = conn.execute("""
                SELECT weighted_avg_usd_mwh, high_usd_mwh, source_sha256, source_row
                FROM public.us_wholesale_power_price
            """).fetchall()
            assert saved == [(Decimal("-10.75"), None, "a" * 64, 2)]

            correction = row._replace(weighted_avg_usd_mwh=Decimal("-9.25"), source_sha256="b" * 64)
            assert power.write_observations(conn, [correction]) == 1
            updated = conn.execute("""
                SELECT weighted_avg_usd_mwh, source_sha256 FROM public.us_wholesale_power_price
            """).fetchall()
            assert updated == [(Decimal("-9.25"), "b" * 64)]
    finally:
        drop_database(name)
