"""환율 수집기(한국수출입은행 현재환율 API) → exchange_rate 적재 테스트. 네트워크를 쓰지 않는다."""
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

import requests

from gpu_collector import fetch
from gpu_collector.erd import __main__ as cli
from gpu_collector.erd.fx import collect as fx
from erd.support import DDL, SEED, connect, create_database, drop_database

# 2026-11-20(금) 12:00 KST
T0 = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)
SECRET = "test-authkey-must-not-leak"


def item(unit, rate, result=1):
    return {"result": result, "cur_unit": unit, "ttb": None, "tts": None, "deal_bas_r": rate, "bkpr": None,
            "yy_efee_r": "0", "ten_dd_efee_r": "0", "kftc_bkpr": None, "kftc_deal_bas_r": rate, "cur_nm": unit}


def body(usd="1,331.1"):
    return [item("EUR", "1,460.68"), item("JPY(100)", "835.23"), item("KRW", "1"), item("USD", usd),
            item("IDR(100)", "7.3")]


def error(code):
    return [{**item(None, None, code), "cur_unit": None}]


def rates(conn, day=None):
    sql = "SELECT base_currency, quote_currency, rate, reference_date, source_url FROM exchange_rate"
    if day:
        return conn.execute(sql + " WHERE reference_date = %s ORDER BY base_currency", (day,)).fetchall()
    return conn.execute(sql + " ORDER BY reference_date, base_currency").fetchall()


def count(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


class FxDb(unittest.TestCase):
    def setUp(self):
        self.name = create_database()
        self.conn = connect(self.name)
        self.conn.execute(DDL.read_text(encoding="utf8"))
        self.conn.execute(SEED.read_text(encoding="utf8"))
        self.conn.commit()
        self.clock = [T0]
        self.responses = {}       # date → 응답. 없으면 빈 배열(비영업일·갱신 전)
        self.calls = []

    def tearDown(self):
        self.conn.close()
        drop_database(self.name)

    def now(self):
        self.clock[0] += timedelta(seconds=1)
        return self.clock[0]

    def fetch_day(self, day):
        self.calls.append(day)
        response = self.responses.get(day, [])
        if isinstance(response, Exception):
            raise response
        return response

    def collect(self, date=None):
        return fx.collect(self.conn, now=self.now, date=date, fetch_day=self.fetch_day)

    def backfill(self, since, until, **kwargs):
        return fx.backfill(self.conn, since=since, until=until, now=self.now, fetch_day=self.fetch_day, **kwargs)

    def alerts(self, alert_type):
        return self.conn.execute("SELECT reason FROM collection_alert WHERE alert_type = %s ORDER BY alert_id",
                                 (alert_type,)).fetchall()

    def last_run(self):
        return self.conn.execute("""SELECT run_status, collector_version, failure_reason FROM collection_run
                                    ORDER BY run_id DESC LIMIT 1""").fetchone()


class ReadRates(unittest.TestCase):
    def test_parses_commas_units_and_drops_krw(self):
        parsed = fx.read_rates(body())
        self.assertEqual(parsed, {"EUR": Decimal("1460.68"), "JPY": Decimal("8.3523"), "USD": Decimal("1331.1"),
                                  "IDR": Decimal("0.073")})

    def test_empty_means_no_rate_for_that_day(self):
        self.assertIsNone(fx.read_rates([]))

    def test_result_codes(self):
        with self.assertRaises(fx.ConfigError):
            fx.read_rates(error(2))
        with self.assertRaises(fx.ConfigError):
            fx.read_rates(error(3))
        with self.assertRaises(fx.LimitExceeded):
            fx.read_rates(error(4))

    def test_invalid(self):
        for bad in ({"result": 1}, [item("USD", "abc")], [item("USD", "0")], [item("US", "1")],
                    [item("EUR", "1,460.68")], [item("USD", "1"), item("USD", "2")]):
            with self.subTest(bad=bad), self.assertRaises(fx.SourceInvalid):
                fx.read_rates(bad)


class RequestDay(unittest.TestCase):
    def test_failure_reason_hides_authkey(self):
        def get(url, params, **kwargs):
            raise requests.ConnectionError(f"Max retries exceeded with url: {url}?authkey={params['authkey']}")
        with mock.patch.object(fetch, "_sleep", lambda _s: None):
            with self.assertRaises(fetch.FetchError) as caught:
                fx.request_day(date(2026, 11, 20), SECRET, get=get)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_sends_key_as_param_and_date(self):
        seen = {}

        class Response:
            status_code = 200

            def json(self):
                return body()

        def get(url, params, **kwargs):
            seen.update(url=url, params=params)
            return Response()
        fx.request_day(date(2026, 11, 20), SECRET, get=get)
        self.assertEqual(seen["url"], fx.API_URL)
        self.assertEqual(seen["params"], {"authkey": SECRET, "searchdate": "20261120", "data": "AP01"})
        self.assertNotIn("authkey", fx.source_url(date(2026, 11, 20)))

    def test_missing_key_is_config_error(self):
        with mock.patch.dict("os.environ", {}, clear=True), self.assertRaises(fx.ConfigError):
            fx.api_fetcher()


class DailyCollect(FxDb):
    def test_loads_all_currencies_of_the_day(self):
        self.responses[date(2026, 11, 20)] = body()
        summary = self.collect()
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["groups"][0]["reference_date"], "2026-11-20")
        loaded = rates(self.conn)
        self.assertEqual([(r[0], r[1], r[2]) for r in loaded],
                         [("EUR", "KRW", Decimal("1460.68")), ("IDR", "KRW", Decimal("0.073")),
                          ("JPY", "KRW", Decimal("8.3523")), ("USD", "KRW", Decimal("1331.1"))])
        self.assertTrue(all(r[4] == fx.source_url(date(2026, 11, 20)) for r in loaded))
        self.assertEqual(self.last_run()[:2], ("success", fx.COLLECTOR_VERSION))

    def test_same_day_rerun_does_not_call(self):
        self.responses[date(2026, 11, 20)] = body()
        self.collect()
        second = self.collect()
        self.assertEqual(second["groups"][0]["status"], "skipped")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(rates(self.conn)), 4)

    def test_weekend_uses_friday_without_calling_weekend(self):
        self.responses[date(2026, 11, 20)] = body()
        summary = self.collect(date="2026-11-22")
        self.assertEqual(summary["groups"][0]["reference_date"], "2026-11-20")
        self.assertEqual(self.calls, [date(2026, 11, 20)])
        self.assertEqual(self.alerts("fx_stale"), [])

    def test_before_update_keeps_previous_day(self):
        self.responses[date(2026, 11, 19)] = body("1,330")
        self.collect(date="2026-11-19")
        summary = self.collect()                      # 11-20 아직 갱신 전(빈 배열)
        self.assertEqual(summary["groups"][0]["status"], "skipped")
        self.assertEqual(summary["groups"][0]["reference_date"], "2026-11-19")
        self.responses[date(2026, 11, 20)] = body()   # 갱신 후 다시 실행
        self.assertEqual(self.collect()["groups"][0]["reference_date"], "2026-11-20")
        self.assertEqual(len(rates(self.conn)), 8)

    def test_long_holiday_alerts_stale(self):
        self.responses[date(2026, 11, 20)] = body()
        summary = self.collect(date="2026-11-26")     # 11-23(월)~26(목) 비영업일로 가정
        self.assertEqual(summary["status"], "success")
        self.assertTrue(summary["groups"][0]["stale"])
        self.assertEqual(len(self.alerts("fx_stale")), 1)

    def test_nothing_within_lookback_fails(self):
        summary = self.collect()
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(len(self.alerts("fx_failed")), 1)
        self.assertEqual(rates(self.conn), [])

    def test_limit_exceeded_fails(self):
        self.responses[date(2026, 11, 20)] = error(4)
        summary = self.collect()
        self.assertEqual(summary["status"], "failed")
        self.assertIn("result_4", summary["groups"][0]["reason"])

    def test_invalid_key_is_config_error_and_closes_run(self):
        self.responses[date(2026, 11, 20)] = error(3)
        with self.assertRaises(fx.ConfigError):
            self.collect()
        self.assertEqual(self.last_run()[0], "failed")
        self.assertEqual(len(self.alerts("fx_failed")), 1)

    def test_fetch_error_reason_has_no_key(self):
        self.responses[date(2026, 11, 20)] = fetch.FetchError("request_failed: ConnectionError")
        summary = self.collect()
        self.assertEqual(summary["status"], "failed")
        stored = self.conn.execute("""SELECT string_agg(coalesce(failure_reason, ''), ' ') FROM collection_run""").fetchone()[0]
        self.assertNotIn(SECRET, stored)


class Backfill(FxDb):
    def week(self):
        # 11-16(월)~11-22(일). 11-18은 공휴일로 가정(빈 배열)
        for d in (16, 17, 19, 20):
            self.responses[date(2026, 11, d)] = body()

    def test_loads_business_days_and_skips_weekend(self):
        self.week()
        summary = self.backfill("2026-11-16", "2026-11-22")
        self.assertEqual(summary["status"], "success")
        self.assertEqual((summary["calls"], summary["loaded_days"], summary["empty_days"]), (5, 4, 1))
        self.assertNotIn(date(2026, 11, 21), self.calls)
        self.assertEqual(count(self.conn, "SELECT count(DISTINCT reference_date) FROM exchange_rate"), 4)
        self.assertEqual(self.last_run()[:2], ("success", fx.HISTORY_VERSION))

    def test_rerun_calls_only_missing_days(self):
        self.week()
        self.backfill("2026-11-16", "2026-11-22")
        self.calls.clear()
        summary = self.backfill("2026-11-16", "2026-11-22")
        self.assertEqual(self.calls, [date(2026, 11, 18)])     # 빈 날만 다시 확인
        self.assertEqual(summary["already_loaded_days"], 4)
        self.assertEqual(len(rates(self.conn)), 16)

    def test_max_calls_stops_and_resumes(self):
        self.week()
        first = self.backfill("2026-11-16", "2026-11-22", max_calls=2)
        self.assertEqual(first["status"], "partial_failure")
        self.assertEqual(first["groups"][-1]["status"], "incomplete")
        second = self.backfill("2026-11-16", "2026-11-22")
        self.assertEqual(second["status"], "success")
        self.assertEqual(count(self.conn, "SELECT count(DISTINCT reference_date) FROM exchange_rate"), 4)

    def test_limit_exceeded_stops(self):
        self.week()
        self.responses[date(2026, 11, 17)] = error(4)
        summary = self.backfill("2026-11-16", "2026-11-22")
        self.assertEqual(summary["status"], "partial_failure")
        self.assertEqual(self.calls, [date(2026, 11, 16), date(2026, 11, 17)])
        self.assertEqual(len(self.alerts("fx_failed")), 1)

    def test_bad_range_is_config_error(self):
        with self.assertRaises(fx.ConfigError):
            self.backfill("2026-11-22", "2026-11-16")
        with self.assertRaises(fx.ConfigError):
            self.backfill("2026-11", "2026-11-16")


class Cli(unittest.TestCase):
    def test_parse_and_lock(self):
        args = cli.parse(["backfill", "fx", "--since", "2023-07-01", "--max-calls", "100"])
        self.assertEqual((args.provider, args.since, args.max_calls), ("fx", "2023-07-01", 100))
        self.assertEqual(cli.lock_name(args), "fx")
        self.assertEqual(cli.lock_name(cli.parse(["collect", "fx"])), "fx")


class GapFill(FxDb):
    def test_daily_collect_fills_missed_business_days(self):
        self.responses[date(2026, 11, 13)] = body("1,320")
        self.collect(date="2026-11-13")                                  # 금 적재
        for d in (16, 17, 18):
            self.responses[date(2026, 11, d)] = body()
        self.calls.clear()
        summary = self.collect(date="2026-11-18")                        # 월·화 실패 후 수요일 성공
        self.assertEqual(self.calls, [date(2026, 11, 18), date(2026, 11, 17), date(2026, 11, 16)])
        self.assertEqual(summary["groups"][0]["loaded_dates"], ["2026-11-16", "2026-11-17", "2026-11-18"])
        self.assertEqual(count(self.conn, "SELECT count(DISTINCT reference_date) FROM exchange_rate"), 4)
