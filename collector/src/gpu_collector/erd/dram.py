"""Load the Bank of Korea's monthly USD-basis DRAM export price index."""
import argparse
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from importlib import resources
import json
import os
import re
import sys
from urllib.parse import quote
from zoneinfo import ZoneInfo

import psycopg
import requests


API = "https://ecos.bok.or.kr/api/StatisticSearch"
TABLE = "402Y016"
ITEM = "30911201AA"
BASIS = "D"
UNIT = "2020=100"
SCHEMA = resources.files("gpu_collector") / "sql" / "dram_export_price_index.sql"
UPSERT = """
INSERT INTO public.dram_export_price_index AS existing (observation_month, index_value, source_url)
VALUES (%s, %s, %s)
ON CONFLICT (observation_month) DO UPDATE
SET index_value = excluded.index_value, source_url = excluded.source_url, collected_at = now()
WHERE existing.index_value IS DISTINCT FROM excluded.index_value
"""


def month_argument(text):
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", text):
            raise ValueError
        return date.fromisoformat(text + "-01")
    except ValueError as error:
        raise argparse.ArgumentTypeError("월을 YYYY-MM 형식으로 입력해 주세요.") from error


def add_months(month, count):
    year, offset = divmod(month.year * 12 + month.month - 1 + count, 12)
    return date(year, offset + 1, 1)


def search_url(key, start, end, limit):
    start_code, end_code = (f"{month.year:04d}{month.month:02d}" for month in (start, end))
    return f"{API}/{quote(key, safe='')}/json/kr/1/{limit}/{TABLE}/M/{start_code}/{end_code}/{ITEM}/{BASIS}/"


def read_window(session, key, start, end, limit):
    try:
        response = session.get(search_url(key, start, end, limit), timeout=20)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise ValueError("ECOS API에서 월별 지수를 가져오지 못했습니다.") from error

    result = payload.get("RESULT", {})
    if result.get("CODE") == "INFO-200":
        return []
    search = payload.get("StatisticSearch", {})
    records = search.get("row", [])
    if not records or search.get("list_total_count") != len(records):
        raise ValueError("ECOS 지수 응답이 없거나 조회 범위가 잘렸습니다.")

    rows = []
    for record in records:
        metadata = tuple(record.get(field) for field in ("STAT_CODE", "ITEM_CODE1", "ITEM_CODE2", "UNIT_NAME"))
        if metadata != (TABLE, ITEM, BASIS, UNIT):
            raise ValueError("ECOS 응답의 항목 코드, 달러 기준 또는 기준연도가 예상과 다릅니다.")
        time = str(record.get("TIME", ""))
        if not re.fullmatch(r"[0-9]{6}", time):
            raise ValueError("ECOS 응답에 올바른 기준월이 없습니다.")
        month = date.fromisoformat(f"{time[:4]}-{time[4:]}-01")
        try:
            value = Decimal(str(record.get("DATA_VALUE")))
        except InvalidOperation as error:
            raise ValueError("ECOS 응답에 올바른 지수 값이 없습니다.") from error
        if not (start <= month <= end) or not value.is_finite() or value <= 0:
            raise ValueError("ECOS 응답에 범위를 벗어난 기준월 또는 지수 값이 있습니다.")
        rows.append((month, value, search_url("sample", month, month, 1)))
    return rows


def fetch_observations(session, key, start, end):
    limit = 10 if key == "sample" else 120
    rows = []
    cursor = start
    while cursor <= end:
        window_end = min(add_months(cursor, limit - 1), end)
        rows.extend(read_window(session, key, cursor, window_end, limit))
        cursor = add_months(window_end, 1)
    rows.sort()
    if not rows:
        raise ValueError("요청 기간에 공개된 DRAM 수출물가지수가 없습니다.")
    for offset, (month, _, _) in enumerate(rows):
        if month != add_months(start, offset):
            raise ValueError("ECOS 응답에 누락되거나 중복된 기준월이 있습니다.")
    return rows


def write_observations(conn, rows):
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('bok_ecos_dram_export_price_index'))")
        exists = conn.execute("SELECT to_regclass('public.dram_export_price_index')").fetchone()[0]
        if exists is None:
            conn.execute(SCHEMA.read_text(encoding="utf8"))
        with conn.cursor() as cursor:
            cursor.executemany(UPSERT, rows)
            return cursor.rowcount


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=month_argument, default=date(2024, 1, 1))
    parser.add_argument("--until", type=month_argument,
                        default=datetime.now(ZoneInfo("Asia/Seoul")).date().replace(day=1))
    parser.add_argument("--check", action="store_true", help="ECOS 자료를 확인하고 DB에는 쓰지 않습니다.")
    args = parser.parse_args(argv)
    if args.since > args.until:
        parser.error("--until은 --since와 같은 월이거나 더 늦어야 합니다.")
    try:
        with requests.Session() as session:
            rows = fetch_observations(session, os.environ.get("ECOS_API_KEY") or "sample", args.since, args.until)
    except ValueError as error:
        parser.error(str(error))
    summary = {
        "rows": len(rows), "first_month": rows[0][0].strftime("%Y-%m"),
        "last_published_month": rows[-1][0].strftime("%Y-%m"), "unit": UNIT,
        "latest_index": str(rows[-1][1]),
    }
    if not args.check:
        url = os.environ.get("DATABASE_URL")
        if not url:
            parser.error("DATABASE_URL 환경 변수가 필요합니다.")
        try:
            with psycopg.connect(url, connect_timeout=10) as conn:
                written = write_observations(conn, rows)
        except psycopg.Error as error:
            print(f"DRAM 지수 적재 실패: {type(error).__name__}", file=sys.stderr)
            return 1
        summary.update(inserted_or_updated=written, unchanged=len(rows) - written)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
