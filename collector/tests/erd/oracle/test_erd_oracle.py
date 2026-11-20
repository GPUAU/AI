"""Oracle 수집기(OCI 공개 정가 API) → ERD 초안 스키마 적재 테스트. 네트워크를 쓰지 않는다."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from gpu_collector import fetch
from gpu_collector.erd.oracle import collect as oracle
from erd.support import DDL, SEED, connect, create_database, drop_database

T0 = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)
PRICES = {"B300": 15, "GB300": 18, "B200": 14, "GB200": 16, "H200": 10, "H100": 10}


def item(part, name, value, metric="GPU Per Hour", key="currencyCodeLocalizations", **extra):
    return {"partNumber": part, "displayName": name, "metricName": metric, "serviceCategory": "Compute - GPU",
            key: [{"currencyCode": "EUR", "prices": [{"model": "PAY_AS_YOU_GO", "value": value * 0.9}]},
                  {"currencyCode": "USD", "prices": [{"model": "PAY_AS_YOU_GO", "value": value, **extra}]}]}


def body(prices=None, key="currencyCodeLocalizations", extra_items=()):
    prices = {**PRICES, **(prices or {})}
    names = {"B300": "Oracle Cloud Infrastructure - Compute - GPU - B300", "H100": "Oracle Cloud Infrastructure - Compute - GPU - H100"}
    items = [item(f"B{i}", names.get(model, f"OCI - Compute - GPU - {model}"), value, key=key)
             for i, (model, value) in enumerate(prices.items()) if value is not None]
    items += [item("B109480", "OCI - Compute - GPU - H100T", 10.75),
              item("B111829", "OCI - NVIDIA AI Enterprise - B200", 3.5),
              item("B1", "Oracle Exadata Exascale VM Filesystem Storage", 0.03, metric="Gigabyte Per Month"),
              *extra_items]
    return {"lastUpdated": "2026-11-02T14:49:49.342Z", "items": items}


def count(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


class OracleDb(unittest.TestCase):
    def setUp(self):
        self.name = create_database()
        self.conn = connect(self.name)
        self.conn.execute(DDL.read_text(encoding="utf8"))
        self.conn.execute(SEED.read_text(encoding="utf8"))
        self.conn.commit()
        self.clock = [T0]
        self.body = body()
        self.calls = 0

    def tearDown(self):
        self.conn.close()
        drop_database(self.name)

    def now(self):
        self.clock[0] += timedelta(seconds=1)
        return self.clock[0]

    def get_json(self, url, config):
        self.calls += 1
        self.assertEqual(url, oracle.API_URL)
        if isinstance(self.body, Exception):
            raise self.body
        return json.loads(json.dumps(self.body))

    def collect(self, date=None):
        return oracle.collect(self.conn, now=self.now, date=date, get_json=self.get_json)

    def prices(self):
        return dict(self.conn.execute(
            """SELECT m.model_name, n.effective_usd_gpu_hour FROM price_normalization n
               JOIN price_observation ob USING (observation_id) JOIN offer o USING (offer_id)
               JOIN product p USING (product_id) JOIN gpu_model m USING (gpu_model_id)""").fetchall())


class Collect(OracleDb):
    def test_loads_gpu_hour_prices_for_all_models(self):
        summary = self.collect()
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["groups"][0]["offers"], 6)
        self.assertEqual(summary["groups"][0]["source_version"], "2026-11-02T14:49:49.342Z")
        self.assertEqual(self.prices(), {m: Decimal(v) for m, v in PRICES.items()})

    def test_product_is_bare_metal_shape_with_spec_evidence(self):
        self.collect()
        row = self.conn.execute(
            """SELECT p.original_product_name, p.sku, p.system_type, p.gpu_count, p.vram_gb_per_gpu, p.ram_gb,
                      p.region_code, p.interconnect_status, p.product_id
               FROM product p JOIN gpu_model m USING (gpu_model_id) WHERE m.model_name = 'B300'""").fetchone()
        self.assertEqual(row[:8], ("BM.GPU.B300.8", "B0", "node", 8, Decimal("262.5"), Decimal("4096"), None,
                                   "unconfirmed"))
        attributes = {r[0] for r in self.conn.execute(
            "SELECT attribute_name FROM product_evidence WHERE product_id = %s", (row[8],)).fetchall()}
        self.assertEqual(attributes, set(oracle.SPEC_ATTRIBUTES))
        gb300 = self.conn.execute("""SELECT p.gpu_count FROM product p JOIN gpu_model m USING (gpu_model_id)
                                     WHERE m.model_name = 'GB300'""").fetchone()[0]
        self.assertEqual(gb300, 4)

    def test_excluded_items_are_not_loaded_but_recorded(self):
        self.collect()
        self.assertEqual(count(self.conn, "SELECT count(*) FROM product"), 6)
        excerpt = self.conn.execute(
            """SELECT e.excerpt FROM source_evidence e JOIN collection_check c USING (check_id)
               WHERE c.check_scope = 'target' LIMIT 1""").fetchone()[0]
        names = {x["displayName"] for x in json.loads(excerpt)["excluded_gpu_items"]}
        self.assertEqual(names, {"OCI - Compute - GPU - H100T", "OCI - NVIDIA AI Enterprise - B200"})

    def test_same_day_rerun_skips(self):
        self.collect()
        second = self.collect()
        self.assertEqual(second["groups"][0]["status"], "skipped")
        self.assertEqual(self.calls, 1)
        self.assertEqual(count(self.conn, "SELECT count(*) FROM price_observation"), 6)

    def test_next_day_adds_observations_and_alerts_spike(self):
        self.collect()
        self.clock[0] += timedelta(days=1)
        self.body = body({"B300": 20})
        self.collect()
        self.assertEqual(count(self.conn, "SELECT count(*) FROM price_observation"), 12)
        self.assertEqual(count(self.conn, "SELECT count(*) FROM collection_alert WHERE alert_type = 'price_spike'"), 1)

    def test_missing_model_is_not_found(self):
        self.body = body({"GB200": None})
        summary = self.collect()
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["groups"][0]["not_found"], 1)

    def test_documented_prices_key_also_works(self):
        self.body = body(key="prices")
        self.assertEqual(self.collect()["groups"][0]["offers"], 6)

    def test_invalid_responses_fail_validation(self):
        cases = {"duplicate": body(extra_items=[item("B9", "OCI - Compute - GPU - B300", 1)]),
                 "tiered": body(extra_items=[]) | {"items": [item("B9", "OCI - Compute - GPU - B300", 1, rangeMin="0")]},
                 "metric": body(extra_items=[]) | {"items": [item("B9", "OCI - Compute - GPU - B300", 1, metric="OCPU Per Hour")]},
                 "format": {"items": None}}
        for label, bad in cases.items():
            with self.subTest(label):
                self.body = bad
                summary = self.collect(date=f"2026-12-{len(label):02d}")
                self.assertEqual(summary["status"], "failed")
                self.assertEqual(summary["groups"][0]["status"], "validation_failed")
        self.assertEqual(count(self.conn, "SELECT count(*) FROM price_observation"), 0)

    def test_fetch_error_is_collection_failed_and_retried(self):
        self.body = fetch.FetchError("http_503")
        self.assertEqual(self.collect()["groups"][0]["status"], "collection_failed")
        self.body = body()
        self.assertEqual(self.collect()["status"], "success")
        self.assertEqual(count(self.conn, "SELECT count(*) FROM price_observation"), 6)
