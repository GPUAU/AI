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


# 과거 이력(Wayback 저장본) -------------------------------------------------------------

import gzip

from gpu_collector.erd.oracle import history as oracle_history

CDX = [["timestamp", "original", "digest"],
       ["20260511153955", "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/?utm_source=x", "D1"],
       ["20260812191351", "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/?currencyCode=USD", "D2"],
       ["20260921200000", "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/?partNumber=B110519", "D3"],
       ["20261026220915", "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/?currencyCode=BRL", "D4"]]


class History(OracleDb):
    def setUp(self):
        super().setUp()
        self.captures = {"20260511153955": body({"B300": 14}), "20260812191351": body()}
        self.fetched = []

    def get_json(self, url, config):
        if url.startswith("https://web.archive.org/cdx/"):
            return CDX
        return super().get_json(url, config)

    def get_bytes(self, url, config):
        stamp = url.split("/web/")[1].split("id_/")[0]
        self.fetched.append(stamp)
        captured = self.captures[stamp]
        if isinstance(captured, Exception):
            raise captured
        return gzip.compress(json.dumps(captured).encode())

    def backfill(self, **kwargs):
        return oracle_history.backfill(self.conn, since="2026-05", until="2026-11", now=self.now,
                                       get_json=self.get_json, get_bytes=self.get_bytes, pause=lambda _s: None,
                                       log=lambda _m: None, **kwargs)

    def test_loads_full_usd_captures_only(self):
        summary = self.backfill()
        self.assertEqual(summary["status"], "success")
        self.assertEqual(self.fetched, ["20260511153955", "20260812191351"])
        self.assertEqual({c["reason"] for c in summary["ignored_captures"]}, {"한 품목만 조회", "USD 아님(BRL)"})
        rows = self.conn.execute(
            """SELECT ob.collected_at::date, n.effective_usd_gpu_hour FROM price_observation ob
               JOIN price_normalization n USING (observation_id) JOIN offer o USING (offer_id)
               JOIN product p USING (product_id) JOIN gpu_model m USING (gpu_model_id)
               WHERE m.model_name = 'B300' ORDER BY 1""").fetchall()
        self.assertEqual([(str(d), v) for d, v in rows], [("2026-05-11", Decimal(14)), ("2026-08-12", Decimal(15))])

    def test_primary_verification_and_aggregator_role(self):
        self.backfill()
        rows = dict(self.conn.execute(
            """SELECT ob.collected_at::date::text, bool_and(e.primary_verified) FROM source_evidence e
               JOIN price_observation ob USING (check_id)
               WHERE e.evidence_role = 'aggregator' GROUP BY 1""").fetchall())
        self.assertEqual(rows, {"2026-05-11": False, "2026-08-12": True})     # 05-11은 B300 가격이 지금과 다름
        self.assertEqual(count(self.conn, "SELECT count(*) FROM collection_alert"), 0)

    def test_rerun_skips_loaded_captures_and_shares_products_with_daily(self):
        self.backfill()
        self.fetched.clear()
        self.backfill()
        self.assertEqual(self.fetched, [])
        self.collect()
        self.assertEqual(count(self.conn, "SELECT count(*) FROM product"), 6)
        self.assertEqual(count(self.conn, "SELECT count(*) FROM price_observation"), 18)

    def test_failed_capture_is_recorded_and_retried(self):
        self.captures["20260812191351"] = fetch.FetchError("http_503")
        self.assertEqual(self.backfill()["status"], "partial_failure")
        self.captures["20260812191351"] = body()
        self.fetched.clear()
        self.assertEqual(self.backfill()["status"], "success")
        self.assertEqual(self.fetched, ["20260812191351"])


# 과거 이력(SkyPilot 카탈로그) ------------------------------------------------------------

from gpu_collector.erd.oracle import skypilot as oracle_skypilot

CSV_HEADER = "InstanceType,AcceleratorName,AcceleratorCount,vCPUs,MemoryGiB,GpuInfo,Price,SpotPrice,Region,AvailabilityZone\n"


def catalog(rows, regions=("us-ashburn-1", "ap-chuncheon-1")):
    lines = [CSV_HEADER]
    for shape, gpus, price in rows:
        for index, region in enumerate(regions):
            value = price[index] if isinstance(price, tuple) else price
            lines.append(f'{shape},X,{gpus},224,2048,"{{}}",{value},,{region},{region}-AD-1\n')
    lines.append('VM.Standard.E5.Flex,,,1,8,,0.03,,us-ashburn-1,AD-1\n')
    return "".join(lines)


def commit(sha, date):
    return {"sha": sha, "commit": {"committer": {"date": date}}}


class SkyPilot(OracleDb):
    def setUp(self):
        super().setUp()
        self.commits = {"v6": [commit("a" * 40, "2025-01-12T10:00:00Z"), commit("0" * 40, "2024-12-13T10:00:00Z")],
                        "v7": [commit("b" * 40, "2025-04-29T10:00:00Z")],
                        "v8": [commit("d" * 40, "2026-11-18T10:00:00Z"), commit("c" * 40, "2025-09-11T10:00:00Z")]}
        hopper = [("BM.GPU.H100.8", 8, 80), ("BM.GPU.H200.8", 8, 80)]
        self.files = {"0" * 40: catalog([("BM.GPU4.8", 8, 24.4)]), "a" * 40: catalog(hopper), "b" * 40: catalog(hopper),
                      "c" * 40: catalog(hopper),
                      "d" * 40: catalog(hopper + [("BM.GPU.B300.8", 8, 120), ("BM.GPU.GB300.4", 4, 72),
                                                  ("BM.GPU.B200.8", 8, 112), ("BM.GPU.GB200.4", 4, 64)])}
        self.fetched = []

    def get_json(self, url, config):
        if url.startswith("https://api.github.com/"):
            version = url.split("catalogs/")[1].split("/")[0]
            return self.commits.get(version, [])
        return super().get_json(url, config)

    def get_text(self, url, config):
        sha = url.split("skypilot-catalog/")[1].split("/")[0]
        self.fetched.append(sha[:1])
        return self.files[sha]

    def backfill(self):
        return oracle_skypilot.backfill(self.conn, since="2024-01", until="2026-11", now=self.now, get_json=self.get_json,
                                        get_text=self.get_text, pause=lambda _s: None, log=lambda _m: None)

    def b300_and_h100(self):
        return self.conn.execute(
            """SELECT m.model_name, ob.collected_at::date::text, n.effective_usd_gpu_hour FROM price_observation ob
               JOIN price_normalization n USING (observation_id) JOIN offer o USING (offer_id)
               JOIN product p USING (product_id) JOIN gpu_model m USING (gpu_model_id)
               WHERE m.model_name IN ('B300', 'H100') ORDER BY 1, 2""").fetchall()

    def test_loads_node_prices_per_gpu_on_daily_products(self):
        self.collect()
        summary = self.backfill()
        self.assertEqual(summary["status"], "success")
        self.assertEqual(self.fetched, ["0", "a", "b", "c", "d"])
        self.assertEqual(self.b300_and_h100(), [
            ("B300", "2026-11-18", Decimal(15)), ("B300", "2026-11-20", Decimal(15)),
            ("H100", "2025-01-12", Decimal(10)), ("H100", "2025-04-29", Decimal(10)), ("H100", "2025-09-11", Decimal(10)),
            ("H100", "2026-11-18", Decimal(10)), ("H100", "2026-11-20", Decimal(10))])
        self.assertEqual(count(self.conn, "SELECT count(*) FROM product"), 6)       # 매일 수집과 같은 상품
        verified = self.conn.execute("""SELECT bool_and(primary_verified) FROM source_evidence
                                        WHERE evidence_role = 'aggregator'""").fetchone()[0]
        self.assertTrue(verified)

    def test_rerun_skips_commits(self):
        self.collect()
        self.backfill()
        self.fetched.clear()
        self.backfill()
        self.assertEqual(self.fetched, [])

    def test_region_price_mismatch_fails_validation(self):
        self.collect()
        self.files["a" * 40] = catalog([("BM.GPU.H100.8", 8, (80, 81))])
        summary = self.backfill()
        self.assertEqual(summary["status"], "partial_failure")
        self.assertIn("region_prices_differ", next(g for g in summary["groups"] if g["sha"].startswith("a"))["reason"])
