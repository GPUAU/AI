"""Load EIA workbooks of ICE daily wholesale electricity prices."""
import argparse
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from importlib import resources
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
from typing import NamedTuple
from zipfile import BadZipFile

import psycopg


BASE_URL = "https://www.eia.gov/electricity/wholesale/xls/"
FILENAME = re.compile(r"ice_electric-(?P<year>[0-9]{4})(?P<final>final)?\.xlsx")
HEADERS = (
    "Price hub", "Trade date", "Delivery start date", "Delivery end date",
    "High price $/MWh", "Low price $/MWh", "Wtd avg price $/MWh", "Change",
    "Daily volume MWh", "Number of trades", "Number of counterparties",
)
SCHEMA = resources.files("gpu_collector") / "sql" / "us_wholesale_power_price.sql"
UPSERT = """
INSERT INTO public.us_wholesale_power_price AS existing (
    hub, trade_date, delivery_start_date, delivery_end_date,
    high_usd_mwh, low_usd_mwh, weighted_avg_usd_mwh, change_usd_mwh,
    daily_volume_mwh, trade_count, counterparty_count,
    source_url, source_file, source_sha256, source_row
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (hub, trade_date, delivery_start_date, delivery_end_date) DO UPDATE SET
    high_usd_mwh = excluded.high_usd_mwh,
    low_usd_mwh = excluded.low_usd_mwh,
    weighted_avg_usd_mwh = excluded.weighted_avg_usd_mwh,
    change_usd_mwh = excluded.change_usd_mwh,
    daily_volume_mwh = excluded.daily_volume_mwh,
    trade_count = excluded.trade_count,
    counterparty_count = excluded.counterparty_count,
    source_url = excluded.source_url,
    source_file = excluded.source_file,
    source_sha256 = excluded.source_sha256,
    source_row = excluded.source_row,
    loaded_at = now()
WHERE (existing.high_usd_mwh, existing.low_usd_mwh, existing.weighted_avg_usd_mwh,
       existing.change_usd_mwh, existing.daily_volume_mwh, existing.trade_count,
       existing.counterparty_count)
    IS DISTINCT FROM
      (excluded.high_usd_mwh, excluded.low_usd_mwh, excluded.weighted_avg_usd_mwh,
       excluded.change_usd_mwh, excluded.daily_volume_mwh, excluded.trade_count,
       excluded.counterparty_count)
"""


class Observation(NamedTuple):
    hub: str
    trade_date: date
    delivery_start_date: date
    delivery_end_date: date
    high_usd_mwh: Decimal | None
    low_usd_mwh: Decimal | None
    weighted_avg_usd_mwh: Decimal
    change_usd_mwh: Decimal | None
    daily_volume_mwh: Decimal | None
    trade_count: int | None
    counterparty_count: int | None
    source_url: str
    source_file: str
    source_sha256: str
    source_row: int


def date_value(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return datetime.strptime(value.strip(), "%m/%d/%Y").date()
    raise ValueError("거래일과 인도 시작·종료일을 확인해 주세요.")


def number_value(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation as error:
        raise ValueError("숫자 값이 올바르지 않습니다.") from error
    if not number.is_finite():
        raise ValueError("유한한 숫자만 적재할 수 있습니다.")
    return number


def count_value(value):
    number = number_value(value)
    if number is None:
        return None
    if number < 0 or number != number.to_integral_value():
        raise ValueError("거래·상대방 수는 0 이상의 정수여야 합니다.")
    return int(number)


def parse_row(cells, source):
    hub, trade, start, end, high, low, average, change, volume, trades, counterparties = cells
    if not isinstance(hub, str) or not hub.strip():
        raise ValueError("전력 허브가 비어 있습니다.")
    trade, start, end = map(date_value, (trade, start, end))
    if end < start:
        raise ValueError("인도 종료일이 시작일보다 이릅니다.")
    high, low, average, change, volume = map(number_value, (high, low, average, change, volume))
    if average is None:
        raise ValueError("가중평균 단가가 비어 있습니다.")
    if volume is not None and volume < 0:
        raise ValueError("거래량은 0 이상이어야 합니다.")
    return Observation(hub.strip(), trade, start, end, high, low, average, change, volume,
                       count_value(trades), count_value(counterparties), *source)


def read_workbook(path):
    from openpyxl import load_workbook

    match = FILENAME.fullmatch(path.name)
    if not match:
        raise ValueError(f"공식 EIA 전력 파일명을 확인해 주세요: {path.name}")
    data = path.read_bytes()
    url = BASE_URL + ("archive/" if match["final"] else "") + path.name
    source = (url, path.name, sha256(data).hexdigest())
    workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    try:
        if match["year"] not in workbook.sheetnames:
            raise ValueError(f"{path.name}: 연도 시트가 없습니다.")
        sheet = workbook[match["year"]]
        rows = sheet.iter_rows(max_col=len(HEADERS), values_only=True)
        headers = tuple(" ".join(str(cell or "").split()) for cell in next(rows, ()))
        if headers != HEADERS:
            raise ValueError(f"{path.name}: EIA 전력 파일 헤더를 확인해 주세요.")
        observations = []
        for row_number, cells in enumerate(rows, 2):
            if all(cell is None or cell == "" for cell in cells):
                continue
            try:
                observations.append(parse_row(cells, (*source, row_number)))
            except ValueError as error:
                raise ValueError(f"{path.name} {row_number}행: {error}") from error
        if not observations:
            raise ValueError(f"{path.name}: 가격 관측이 없습니다.")
        return observations
    finally:
        workbook.close()


def collect_rows(paths):
    rows = {}
    files = []
    for path in paths:
        observations = read_workbook(path)
        files.append({"file": path.name, "rows": len(observations)})
        for observation in observations:
            key = observation[:4]
            previous = rows.setdefault(key, observation)
            if previous[:11] != observation[:11]:
                raise ValueError(f"같은 허브·거래일·인도 기간에 서로 다른 가격이 있습니다: {key}")
    return rows, files


def write_observations(conn, observations):
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('eia_ice_us_wholesale_power_price'))")
        exists = conn.execute("SELECT to_regclass('public.us_wholesale_power_price')").fetchone()[0]
        if exists is None:
            conn.execute(SCHEMA.read_text(encoding="utf8"))
        with conn.cursor() as cursor:
            cursor.executemany(UPSERT, observations)
            return cursor.rowcount


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbooks", nargs="+", type=Path)
    parser.add_argument("--check", action="store_true", help="파일을 검증하고 DB에는 쓰지 않습니다.")
    args = parser.parse_args(argv)
    try:
        rows, files = collect_rows(args.workbooks)
    except (OSError, ValueError, BadZipFile, ImportError) as error:
        parser.error(str(error))

    dates = [row.trade_date for row in rows.values()]
    summary = {"files": files, "unique_rows": len(rows), "hubs": len({row.hub for row in rows.values()}),
               "first_trade_date": str(min(dates)), "last_trade_date": str(max(dates))}
    if not args.check:
        url = os.environ.get("DATABASE_URL")
        if not url:
            parser.error("DATABASE_URL 환경 변수가 필요합니다.")
        try:
            with psycopg.connect(url, connect_timeout=10) as conn:
                written = write_observations(conn, rows.values())
        except psycopg.Error as error:
            print(f"전력가격 적재 실패: {type(error).__name__}", file=sys.stderr)
            return 1
        summary.update(inserted_or_updated=written, unchanged=len(rows) - written)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
