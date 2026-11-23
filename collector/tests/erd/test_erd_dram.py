from datetime import date
from decimal import Decimal
from unittest.mock import Mock

import pytest

from gpu_collector.erd import dram

from .support import connect, create_database, drop_database


def record(month, value="88.21"):
    return {
        "STAT_CODE": "402Y016",
        "ITEM_CODE1": "30911201AA",
        "ITEM_CODE2": "D",
        "UNIT_NAME": "2020=100",
        "TIME": month,
        "DATA_VALUE": value,
    }


def response(records):
    return Mock(**{"json.return_value": {"StatisticSearch": {"row": records, "list_total_count": len(records)}}})


def test_sample_windows_keep_only_published_months():
    session = Mock()
    session.get.side_effect = [
        response([record(f"2026{month:02d}") for month in range(1, 11)]),
        response([record("202611")]),
    ]

    rows = dram.fetch_observations(session, "sample", date(2026, 1, 1), date(2026, 12, 1))

    assert len(rows) == 11
    assert rows[-1][:2] == (date(2026, 11, 1), Decimal("88.21"))
    assert "/sample/json/kr/1/1/" in rows[-1][2]
    urls = [call.args[0] for call in session.get.call_args_list]
    assert "/1/10/402Y016/M/202601/202610/" in urls[0]
    assert "/1/10/402Y016/M/202611/202612/" in urls[1]


def test_rejects_currency_change_or_missing_intermediate_month():
    session = Mock()
    session.get.return_value = response([{**record("202601"), "ITEM_CODE2": "W"}])
    with pytest.raises(ValueError, match="달러 기준"):
        dram.fetch_observations(session, "sample", date(2026, 1, 1), date(2026, 1, 1))

    session.get.return_value = response([record("202601"), record("202603")])
    with pytest.raises(ValueError, match="누락되거나 중복"):
        dram.fetch_observations(session, "sample", date(2026, 1, 1), date(2026, 3, 1))


def test_postgres_load_preserves_unchanged_rows_and_updates_revision():
    name = create_database()
    try:
        with connect(name) as conn:
            month = date(2026, 1, 1)
            url = dram.search_url("sample", month, month, 1)
            row = (month, Decimal("147.44"), url)
            assert dram.write_observations(conn, [row]) == 1
            assert dram.write_observations(conn, [row]) == 0

            revision = (month, Decimal("147.43"), url)
            assert dram.write_observations(conn, [revision]) == 1
            stored = conn.execute("""
                SELECT observation_month, index_value, source_url FROM public.dram_export_price_index
            """).fetchall()
            assert stored == [revision]
    finally:
        drop_database(name)
