"""Hyperstack 수집기(pricebook·flavors API, Wayback 가격 페이지) 테스트. 네트워크를 쓰지 않는다."""
import json
import os
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from gpu_collector import fetch
from gpu_collector.erd.hyperstack import collect as hyperstack, history
from erd.support import DDL, SEED, connect, create_database, drop_database

T0 = datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc)      # 12:00 KST


def price(id_, name, value, discount=False, original=None):
    return {"id": id_, "name": name, "value": f"{value:.9f}", "original_value": f"{original or value:.9f}",
            "discount_applied": discount, "start_time": None, "end_time": None}


def pricebook():
    return [price(8, "vCPU", 0), price(301, "B300-SXM", 7.4), price(56, "B200-SXM", 6.0),
            price(40, "H200-141G-SXM5", 3.99), price(34, "H100-80G-SXM5", 3.2), price(55, "H100-80G-SXM5-spot", 2.56),
            price(27, "H100-80G-PCIe", 2.5), price(50, "H100-80G-PCIe-spot", 2.0), price(9, "A100-80G-PCIe", 1.35)]


def flavor(id_, name, gpu, count, region="CANADA-1", cpu=28, ram=180.0):
    return {"id": id_, "name": name, "region_name": region, "gpu": gpu, "gpu_count": count, "cpu": cpu, "ram": ram,
            "disk": 100, "ephemeral": 750, "stock_available": False, "features": {"network_optimised": True}}


def flavors():
    items = [flavor(1, "n3-B300-SXM6x8", "B300-SXM", 8, cpu=224, ram=1920.0),
             flavor(2, "n3-B200-SXM6x8", "B200-SXM", 8, cpu=252, ram=2048.0),
             flavor(3, "n3-H200-SXM5x8", "H200-141G-SXM5", 8, cpu=176, ram=1800.0),
             flavor(4, "n3-H100-SXM5x8", "H100-80G-SXM5", 8, cpu=192, ram=1800.0),
             flavor(5, "n3-H100-SXM5x8", "H100-80G-SXM5", 8, region="US-1", cpu=192, ram=960.0),
             flavor(6, "n3-H100-SXM5x8-spot", "H100-80G-SXM5-spot", 8, region="US-1", cpu=192, ram=960.0),
             flavor(7, "n3-H100x1", "H100-80G-PCIe", 1), flavor(8, "n3-H100x8", "H100-80G-PCIe", 8, cpu=252, ram=1440.0),
             flavor(9, "n3-H100x1-bigroot", "H100-80G-PCIe", 1), flavor(10, "n3-H100x1-spot", "H100-80G-PCIe-spot", 1),
             flavor(11, "n3-A100x1", "A100-80G-PCIe", 1)]
    return {"status": True, "message": "ok", "data": [{"gpu": "", "region_name": "CANADA-1", "flavors": items}]}


def regions():
    return {"status": True, "regions": [{"id": 2, "name": "CANADA-1", "country": "CA"}, {"id": 3, "name": "US-1", "country": "US"}]}


def count(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


class HyperstackDb(unittest.TestCase):
    def setUp(self):
        self.name = create_database()
        self.conn = connect(self.name)
        self.conn.execute(DDL.read_text(encoding="utf8"))
        self.conn.execute(SEED.read_text(encoding="utf8"))
        self.conn.commit()
        self.clock = [T0]
        self.responses = {"pricebook": pricebook(), "flavors": flavors(), "regions": regions()}
        self.headers = []

    def tearDown(self):
        self.conn.close()
        drop_database(self.name)

    def now(self):
        self.clock[0] += timedelta(seconds=1)
        return self.clock[0]

    def get_json(self, url, config, headers=None):
        self.headers.append(headers)
        body = self.responses[url.rsplit("/", 1)[1]]
        if isinstance(body, Exception):
            raise body
        return json.loads(json.dumps(body))

    def collect(self, date=None):
        return hyperstack.collect(self.conn, now=self.now, date=date, get_json=self.get_json, key="test-key")

    def rows(self):
        return self.conn.execute(
            """SELECT p.original_product_name, p.region_code, o.contract_type, p.country_code, p.gpu_count,
                      p.vram_gb_per_gpu, p.interconnect_status, p.intra_node_interconnect, n.effective_usd_gpu_hour
               FROM price_observation ob JOIN offer o USING (offer_id) JOIN product p USING (product_id)
               JOIN price_normalization n USING (observation_id) ORDER BY 1, 2, 3""").fetchall()


class Collect(HyperstackDb):
    def test_loads_targets_only_without_bigroot(self):
        summary = self.collect()
        self.assertEqual(summary["status"], "success")
        self.assertEqual([(r[0], r[1], r[2]) for r in self.rows()],
                         [("n3-B200-SXM6x8", "CANADA-1", "on_demand"), ("n3-B300-SXM6x8", "CANADA-1", "on_demand"),
                          ("n3-H100-SXM5x8", "CANADA-1", "on_demand"), ("n3-H100-SXM5x8", "US-1", "on_demand"),
                          ("n3-H100-SXM5x8-spot", "US-1", "spot"), ("n3-H100x1", "CANADA-1", "on_demand"),
                          ("n3-H100x1-spot", "CANADA-1", "spot"), ("n3-H100x8", "CANADA-1", "on_demand"),
                          ("n3-H200-SXM5x8", "CANADA-1", "on_demand")])
        self.assertEqual(self.headers[0], {"api_key": "test-key", "accept": "application/json"})

    def test_gpu_price_is_not_divided(self):
        self.collect()
        rows = {(r[0], r[1]): r for r in self.rows()}
        self.assertEqual(rows[("n3-B300-SXM6x8", "CANADA-1")][8], Decimal("7.4"))
        self.assertEqual(rows[("n3-H100x8", "CANADA-1")][8], Decimal("2.5"))
        self.assertEqual(rows[("n3-H100-SXM5x8-spot", "US-1")][8], Decimal("2.56"))

    def test_specs_country_and_interconnect(self):
        self.collect()
        rows = {(r[0], r[1]): r for r in self.rows()}
        self.assertEqual(rows[("n3-H100-SXM5x8", "US-1")][3], "US")
        self.assertEqual(rows[("n3-B300-SXM6x8", "CANADA-1")][5], Decimal(288))
        self.assertEqual(rows[("n3-B300-SXM6x8", "CANADA-1")][6:8], ("confirmed", hyperstack.SPECS["B300-SXM"]["interconnect"]))
        self.assertEqual(rows[("n3-H100x8", "CANADA-1")][6], "unconfirmed")      # PCIe 일반형은 NVLink 문구 없음
        self.assertEqual(rows[("n3-H100x1", "CANADA-1")][6], "not_applicable")

    def test_account_discount_fails_validation(self):
        for discounted in (price(301, "B300-SXM", 5.0, True, 7.4), price(301, "B300-SXM", 5.0, False, 7.4)):
            self.responses["pricebook"] = [r if r["name"] != "B300-SXM" else discounted for r in pricebook()]
            summary = self.collect(date=f"2026-11-2{3 + discounted['discount_applied']}")
            self.assertEqual(summary["groups"][0]["status"], "validation_failed")
            self.assertIn("account_discount_detected", summary["groups"][0]["reason"])
        self.assertEqual(count(self.conn, "SELECT count(*) FROM price_observation"), 0)
        self.assertEqual(count(self.conn, "SELECT count(*) FROM collection_alert WHERE alert_type = 'collection_failed'"), 2)

    def test_excluded_flavors_are_recorded(self):
        body = flavors()
        body["data"][0]["flavors"].append(flavor(12, "n3-H100-SXM5x8-IB", "H100-80G-SXM5-IB", 8))
        self.responses["flavors"] = body
        self.assertEqual(self.collect()["status"], "success")
        notes = [r[0] for r in self.conn.execute(
            """SELECT e.excerpt FROM source_evidence e JOIN collection_check c USING (check_id)
               WHERE c.check_scope = 'target' AND e.excerpt LIKE '%%excluded%%'""").fetchall()]
        text = " ".join(notes)
        self.assertIn("n3-H100x1-bigroot", text)
        self.assertIn("H100-80G-SXM5-IB", text)
        self.assertIn("unknown_gpu_type", text)
        self.assertEqual(text.count("H100-80G-SXM5-IB"), 1)        # 온디맨드 대상에만(스팟 대상엔 안 붙음)

    def test_forbidden_is_retryable_failure(self):
        self.responses["pricebook"] = fetch.FetchError("http_403")
        self.assertEqual(self.collect()["groups"][0]["status"], "collection_failed")

    def test_rejected_key_is_config_error(self):
        self.responses["pricebook"] = fetch.FetchError("http_401")
        with self.assertRaises(LookupError):
            self.collect()
        self.assertEqual(count(self.conn, "SELECT count(*) FROM collection_run WHERE run_status = 'failed'"), 1)

    def test_same_day_rerun_skips(self):
        self.collect()
        calls = len(self.headers)
        self.assertEqual(self.collect()["groups"][0]["status"], "skipped")
        self.assertEqual(len(self.headers), calls)

    def test_fetch_error_is_recorded_and_retried(self):
        self.responses["flavors"] = fetch.FetchError("http_503")
        self.assertEqual(self.collect()["groups"][0]["status"], "collection_failed")
        self.assertEqual(count(self.conn, "SELECT count(*) FROM price_observation"), 0)
        self.responses["flavors"] = flavors()
        self.assertEqual(self.collect()["status"], "success")

    def test_missing_key_is_config_error(self):
        with mock.patch.dict(os.environ, clear=True), self.assertRaises(LookupError):
            hyperstack.collect(self.conn, now=self.now, get_json=self.get_json)
        self.assertEqual(count(self.conn, "SELECT count(*) FROM collection_run"), 0)


# 과거 이력(Wayback 가격 페이지) ----------------------------------------------------------

PAGE_2023 = ('<div class="pricing__name">H100 PCIe</div><div>80</div><div>32</div><div>186</div>'
             '<div class="pricing__price">$ 4.30 per GPU</div>'
             '<h3>Add-ons</h3><p>Price</p><p>vCPU</p><p>$ 0.01 per GPU/hour</p>').encode()
PAGE_2024 = ('<tr><td>NVIDIA H100 SXM 80GB</td><td>80</td><td>24</td><td>240</td><td>$ 3.75 per Hour</td>'
             '<td>Starts from</td><td>$2.25/hour</td></tr>'
             '<tr><td>NVIDIA H100 PCIe 80GB</td><td>80</td><td>28</td><td>180</td><td>$ 3.44 per Hour</td></tr>'
             '<tr><td>NVIDIA H100 SXM 80GB</td><td>80</td><td>24</td><td>240</td><td>$ 3.75 per Hour</td></tr>').encode()
PAGE_2026 = ('<h3>On-Demand GPU</h3><p>Pricing</p><table><tr><td>NVIDIA H100 SXM</td><td>80</td><td>24</td><td>240</td>'
             '<td>$3.20</td></tr><tr><td>NVIDIA B300</td><td>288</td><td>28</td><td>240</td><td>$7.40</td></tr></table>'
             '<h3>Reservation</h3><p>Pricing</p><table><tr><td>GPU Model</td><td>Starting from</td></tr>'
             '<tr><td>NVIDIA H100 SXM</td><td>$2.72</td><td>Reserve here</td></tr></table>'
             '<h3>Spot VM</h3><p>Pricing</p><table><tr><td>NVIDIA H100 PCIe</td><td>$2.00</td></tr>'
             '<tr><td>NVIDIA A100</td><td>$1.08</td></tr></table><h3>On-Demand CPU</h3>').encode()


class ReadPage(unittest.TestCase):
    def rows(self, body):
        return {(r["gpu"], r["kind"]): r["amount"] for r in history.read_page(body)}

    def test_formats(self):
        self.assertEqual(self.rows(PAGE_2023), {("H100-80G-PCIe", "on_demand"): Decimal("4.30")})
        self.assertEqual(self.rows(PAGE_2024), {("H100-80G-SXM5", "on_demand"): Decimal("3.75"),
                                                ("H100-80G-PCIe", "on_demand"): Decimal("3.44")})
        self.assertEqual(self.rows(PAGE_2026), {("H100-80G-SXM5", "on_demand"): Decimal("3.20"),
                                                ("B300-SXM", "on_demand"): Decimal("7.40"),
                                                ("H100-80G-PCIe", "spot"): Decimal("2.00")})   # 예약가는 읽지 않음

    def test_spot_section_without_targets_and_add_ons_flag(self):
        body = PAGE_2026.replace(b"<tr><td>NVIDIA H100 PCIe</td><td>$2.00</td></tr>", b"")
        self.assertNotIn(("H100-80G-PCIe", "spot"), self.rows(body))
        self.assertEqual({r["add_ons"] for r in history.read_page(PAGE_2023)}, {True})
        self.assertEqual({r["add_ons"] for r in history.read_page(PAGE_2026)}, {False})

    def test_unknown_target_label_fails(self):
        body = b"<tr><td>NVIDIA H100 Ultra</td><td>80</td><td>24</td><td>240</td><td>$3.00</td></tr>"
        with self.assertRaises(hyperstack.SourceInvalid):
            history.read_page(body)

    def test_conflicting_duplicate_fails(self):
        body = (b"<tr><td>NVIDIA H100 SXM</td><td>80</td><td>24</td><td>240</td><td>$3.20</td></tr>"
                b"<tr><td>NVIDIA H100 SXM</td><td>80</td><td>24</td><td>240</td><td>$3.99</td></tr>")
        with self.assertRaises(hyperstack.SourceInvalid):
            history.read_page(body)


class Backfill(HyperstackDb):
    CAPTURES = [["timestamp", "original", "digest"],
                ["20231109232321", "https://www.hyperstack.cloud/gpu-pricing", "A"],
                ["20261116064938", "https://www.hyperstack.cloud/gpu-pricing", "B"]]

    def backfill(self):
        pages = {"20231109232321": PAGE_2023, "20261116064938": PAGE_2026}
        return history.backfill(self.conn, since="2023-11", until="2026-11", now=self.now,
                                get_json=lambda url, config: self.CAPTURES,
                                get_bytes=lambda url, config: pages[url.split("/web/")[1][:14]],
                                pause=lambda s: None, log=lambda m: None)

    def test_loads_region_unknown_gpu_products_and_verifies_against_daily(self):
        self.collect()
        summary = self.backfill()
        self.assertEqual(summary["status"], "success")
        rows = self.conn.execute(
            """SELECT p.original_product_name, o.contract_type, ob.collected_at::date, n.effective_usd_gpu_hour,
                      e.primary_verified
               FROM price_observation ob JOIN offer o USING (offer_id) JOIN product p USING (product_id)
               JOIN price_normalization n USING (observation_id)
               JOIN source_evidence e ON e.check_id = ob.check_id AND e.evidence_role = 'aggregator'
               WHERE p.region_code IS NULL ORDER BY 3, 1, 2""").fetchall()
        self.assertEqual([(r[0], r[1], str(r[2]), r[3], r[4]) for r in rows],
                         [("H100-80G-PCIe", "on_demand", "2023-11-09", Decimal("4.30"), None),   # ±7일 밖: 대조 불가
                          ("B300-SXM", "on_demand", "2026-11-16", Decimal("7.40"), True),
                          ("H100-80G-PCIe", "spot", "2026-11-16", Decimal("2.00"), True),
                          ("H100-80G-SXM5", "on_demand", "2026-11-16", Decimal("3.20"), True)])

    def test_rerun_skips_loaded_captures(self):
        self.backfill()
        before = count(self.conn, "SELECT count(*) FROM price_observation")
        self.assertEqual(self.backfill()["groups"], [])
        self.assertEqual(count(self.conn, "SELECT count(*) FROM price_observation"), before)

    def test_add_ons_era_terms_and_cdx_failure(self):
        self.backfill()
        terms = count(self.conn, """SELECT ob.raw_terms FROM price_observation ob
                                    WHERE ob.collected_at < '2024-01-01'""")
        self.assertIn("Add-ons", terms)

        def broken(url, config):
            raise fetch.FetchError("http_503")
        summary = history.backfill(self.conn, since="2023-11", until="2026-11", now=self.now, get_json=broken,
                                   get_bytes=None, pause=lambda s: None, log=lambda m: None)
        self.assertEqual(summary["status"], "failed")
