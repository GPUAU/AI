"""Scaleway 수집기(공개 카탈로그 API) → ERD 초안 스키마 적재 테스트. 네트워크를 쓰지 않는다."""
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from gpu_collector import fetch
from gpu_collector.erd.fx import collect as fx
from gpu_collector.erd.scaleway import collect as scaleway
from erd.support import DDL, SEED, connect, create_database, drop_database

T0 = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)      # 12:00 KST


def instance(name, zone, units, nanos, gpus, gpu_type, vcpus=224, ram_gib=3840, status="general_availability"):
    return {"sku": f"/instance/server/{name.lower().replace('-', '_')}/{zone}", "service_category": "Compute",
            "product_category": "Instance", "product": name, "variant": f"{name} - {zone}",
            "description": f"{name} - {zone}", "locality": {"zone": zone},
            "price": {"retail_price": {"currency_code": "EUR", "units": units, "nanos": nanos}},
            "properties": {"hardware": {"cpu": {"virtual": {"count": vcpus}, "threads": vcpus},
                                        "ram": {"description": f"{ram_gib} GiB", "size": ram_gib * 2 ** 30},
                                        "storage": {"description": "Block, Scratch", "total": 0},
                                        "network": {"description": "Internal: 20 Gb/s, Public: 20 Gb/s"},
                                        "gpu": {"description": f"{gpus} x {gpu_type}", "count": gpus, "type": gpu_type}},
                           "instance": {"range": "GPU", "offer_id": name}},
            "unit_of_measure": {"unit": "minute", "size": 1}, "status": status}


def metal(unit, units, nanos):
    return {"sku": f"/baremetal/em-t620e-h100/{unit}ly/run_par2", "product_category": "Elastic Metal",
            "product": "EM-T620E-H100", "description": "EM", "locality": {"zone": "fr-par-2"},
            "price": {"retail_price": {"currency_code": "EUR", "units": units, "nanos": nanos}},
            "properties": {"hardware": {"cpu": {"threads": 64}, "ram": {"size": 512 * 2 ** 30},
                                        "storage": {"description": "Fixed local: 2 x 7.68 TB NVMe"},
                                        "gpu": {"count": 2, "type": "H100"}}},
            "unit_of_measure": {"unit": unit, "size": 1}, "status": "general_availability"}


def catalog():
    return [instance("B300-SXM-8-288G", "fr-par-2", 1, 0, 8, "B300-SXM"),
            instance("H100-SXM-8-80G", "fr-par-2", 0, 422180000, 8, "H100-SXM", 128, 960),
            instance("H100-1-80G", "fr-par-2", 0, 47775000, 1, "H100-PCIe", 24, 240),
            instance("H100-1-80G", "pl-waw-2", 0, 47775000, 1, "H100-PCIe", 24, 240),
            instance("H100-2-80G", "fr-par-2", 0, 95550000, 2, "H100-PCIe", 48, 480),
            instance("H100-1-M", "fr-par-2", 0, 70000000, 1, "H100", status="retired"),
            metal("hour", 9, 589000000), metal("month", 3499, 990000000), metal("setup", 3499, 990000000),
            {"sku": "/ai/inference_dedicated/h100-1/fr-par", "product_category": "Inference Dedicated", "product": "H100",
             "locality": {"region": "fr-par"}, "price": {"retail_price": {"currency_code": "EUR", "units": 0, "nanos": 1}},
             "unit_of_measure": {"unit": "node", "size": 1}, "status": "general_availability"},
            {"sku": "/dns/x", "product_category": "Domains and DNS", "product": "x", "status": "general_availability"}]


def count(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


class ScalewayDb(unittest.TestCase):
    def setUp(self):
        self.name = create_database()
        self.conn = connect(self.name)
        self.conn.execute(DDL.read_text(encoding="utf8"))
        self.conn.execute(SEED.read_text(encoding="utf8"))
        self.conn.commit()
        self.clock = [T0]
        self.products = catalog()
        self.page_size = 4
        self.calls = 0
        source = fx.source_id(self.conn)
        fx.write_rates(self.conn, source, date(2026, 11, 20), {"USD": Decimal("1330.8"), "EUR": Decimal("1459.62")}, T0)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        drop_database(self.name)

    def now(self):
        self.clock[0] += timedelta(seconds=1)
        return self.clock[0]

    def get_json(self, url, config):
        self.calls += 1
        if isinstance(self.products, Exception):
            raise self.products
        page = int(url.rsplit("page=", 1)[1])
        chunk = self.products[(page - 1) * self.page_size: page * self.page_size]
        return json.loads(json.dumps({"products": chunk, "total_count": len(self.products)}))

    def collect(self, date=None):
        return scaleway.collect(self.conn, now=self.now, date=date, get_json=self.get_json)

    def rows(self):
        return self.conn.execute(
            """SELECT p.original_product_name, p.region_code, p.country_code, m.model_name, p.gpu_count,
                      p.interconnect_status, p.intra_node_interconnect, n.effective_usd_gpu_hour, n.result_status,
                      n.calculation_inputs
               FROM price_observation ob JOIN offer o USING (offer_id) JOIN product p USING (product_id)
               JOIN gpu_model m USING (gpu_model_id) JOIN price_normalization n USING (observation_id)
               ORDER BY 1, 2""").fetchall()


class Collect(ScalewayDb):
    def test_loads_target_gpu_products_only(self):
        summary = self.collect()
        self.assertEqual(summary["status"], "success")
        names = [(r[0], r[1]) for r in self.rows()]
        self.assertEqual(names, [("B300-SXM-8-288G", "fr-par-2"), ("EM-T620E-H100", "fr-par-2"),
                                 ("H100-1-80G", "fr-par-2"), ("H100-1-80G", "pl-waw-2"), ("H100-2-80G", "fr-par-2"),
                                 ("H100-SXM-8-80G", "fr-par-2")])
        self.assertGreater(self.calls, 1)                    # 모든 페이지

    def test_eur_minute_price_to_usd_gpu_hour_with_cross_rate(self):
        self.collect()
        b300 = [r for r in self.rows() if r[0] == "B300-SXM-8-288G"][0]
        expected = Decimal(60) * (Decimal("1459.62") / Decimal("1330.8")) / 8
        self.assertEqual(b300[8], "success")
        self.assertEqual(round(b300[7], 18), round(expected, 18))
        inputs = b300[9]
        self.assertEqual((inputs["currency"], inputs["time_unit"], inputs["reference_date"]), ("EUR", "minute", "2026-11-20"))
        metal = [r for r in self.rows() if r[0] == "EM-T620E-H100"][0]   # 시간 단가는 × 60 하지 않음
        self.assertEqual(round(metal[7], 18), round(Decimal("9.589") * Decimal("1459.62") / Decimal("1330.8") / 2, 18))

    def test_krw_conversion_matches_eur_times_eur_krw(self):
        from gpu_collector.erd.fx import convert
        self.collect()
        convert.convert(self.conn, now=self.now)
        krw = self.conn.execute(
            """SELECT k.effective_krw_gpu_hour FROM krw_conversion k JOIN price_normalization n USING (normalization_id)
               JOIN price_observation ob USING (observation_id) JOIN offer o USING (offer_id)
               JOIN product p USING (product_id) WHERE p.original_product_name = 'B300-SXM-8-288G'""").fetchone()[0]
        self.assertAlmostEqual(float(krw), 60 * 1459.62 / 8, places=6)

    def test_specs_country_and_interconnect(self):
        self.collect()
        rows = {(r[0], r[1]): r for r in self.rows()}
        self.assertEqual(rows[("H100-1-80G", "pl-waw-2")][2], "PL")
        self.assertEqual(rows[("B300-SXM-8-288G", "fr-par-2")][5:7], ("confirmed", "NVIDIA NVLink (SXM)"))
        self.assertEqual(rows[("H100-2-80G", "fr-par-2")][5:7], ("confirmed", "PCIe (NVLink 없음)"))
        self.assertEqual(rows[("H100-1-80G", "fr-par-2")][5], "not_applicable")
        self.assertEqual(rows[("EM-T620E-H100", "fr-par-2")][5], "unconfirmed")
        vram = self.conn.execute("""SELECT vram_gb_per_gpu, vcpu_count, ram_gb, inter_node_network FROM product
                                    WHERE original_product_name = 'B300-SXM-8-288G'""").fetchone()
        self.assertEqual(vram[0], Decimal(288))                                  # 표기값 그대로
        self.assertEqual(vram[2], Decimal(3840 * 2 ** 30) / Decimal(10 ** 9))    # RAM은 GB(10^9)
        self.assertEqual(vram[1], 224)
        self.assertIsNone(vram[3])

    def test_missing_rate_leaves_normalization_not_computable(self):
        self.conn.execute("DELETE FROM exchange_rate")
        self.conn.commit()
        summary = self.collect()
        self.assertEqual(summary["groups"][0]["not_computable"], 6)
        self.assertEqual({r[8] for r in self.rows()}, {"not_computable"})

    def test_same_day_rerun_skips(self):
        self.collect()
        calls = self.calls
        self.assertEqual(self.collect()["groups"][0]["status"], "skipped")
        self.assertEqual(self.calls, calls)

    def test_incomplete_catalog_fails_validation(self):
        original = self.get_json

        def short(url, config):
            body = original(url, config)
            body["total_count"] += 5
            return body
        self.get_json = short
        summary = self.collect()
        self.assertEqual(summary["groups"][0]["status"], "validation_failed")
        self.assertEqual(count(self.conn, "SELECT count(*) FROM price_observation"), 0)

    def test_fetch_error_is_recorded_and_retried(self):
        self.products = fetch.FetchError("http_503")
        self.assertEqual(self.collect()["groups"][0]["status"], "collection_failed")
        self.products = catalog()
        self.assertEqual(self.collect()["status"], "success")


# 과거 이력(Wayback 가격 페이지) ----------------------------------------------------------

import gzip

from gpu_collector.erd.scaleway import history as scaleway_history


def next_data_page(products, key="name"):
    data = {"props": {"pageProps": {"externalData": {"ranges": [{"name": "NVIDIA H100", "products": [
        {key: name, "hardware": {"gpu": {"count": gpus}, "cpu": {"virtual": {"count": vcpu}}},
         "zones": [{"zone": zone, "price": {"perMinute": {"sku": f"/compute/{name.lower()}/run_{zone}",
                                                         "value": {"currencyCode": "EUR", "units": units, "nanos": nanos}},
                                            "hourly": {"value": {"currencyCode": "EUR", "units": 99, "nanos": 0}}}}
                   for zone in zones]}
        for name, gpus, vcpu, units, nanos, zones in products]}]}}}}
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)      # 실제 페이지처럼 공백 없이
    return f'<html><script id="__NEXT_DATA__" type="application/json">{body}</script></html>'.encode()


TABLE_2024 = ("<table><tr><td>H100-1-80G</td><td></td><td>24 cores</td><td>1 GPU</td><td>240 GB</td>"
              "<td>Block Storage</td><td>10 Gbps</td><td>€2.52</td><td>/hour</td><td>€1839.6</td><td>/month</td></tr>"
              "<tr><td>H100-2-80G</td><td>48 cores</td><td>2 GPU</td><td>480 GB</td><td>Block Storage</td>"
              "<td>20 Gbps</td><td>€5.04</td><td>/hour</td></tr></table>").encode()

CDX = [["timestamp", "original", "digest"],
       ["20240524045214", "https://www.scaleway.com/en/pricing/gpu/", "A"],
       ["20250322230659", "https://www.scaleway.com/en/pricing/gpu/", "B"],
       ["20260307002714", "https://www.scaleway.com/en/pricing/gpu/", "C"]]


class Parsers(unittest.TestCase):
    def test_json_name_and_apiid_and_table(self):
        rows = scaleway_history.read_page(next_data_page([("B300-SXM-8-288G", 8, 224, 1, 0, ["fr-par-2"])]))
        self.assertEqual([(r["name"], r["zone"], r["unit"], r["amount"]) for r in rows],
                         [("B300-SXM-8-288G", "fr-par-2", "minute", Decimal(1))])
        rows = scaleway_history.read_page(next_data_page([("H100-1-80G", 1, 24, 0, 45500000, ["fr-par-2", "pl-waw-2"])],
                                                         key="apiId"))
        self.assertEqual({(r["zone"], r["amount"]) for r in rows}, {("fr-par-2", Decimal("0.0455")), ("pl-waw-2", Decimal("0.0455"))})
        rows = scaleway_history.read_page(gzip.compress(TABLE_2024))
        self.assertEqual([(r["name"], r["zone"], r["unit"], r["amount"], r["vcpu"]) for r in rows],
                         [("H100-1-80G", None, "hour", Decimal("2.52"), 24), ("H100-2-80G", None, "hour", Decimal("5.04"), 48)])

    def test_next_data_with_other_attribute_order(self):
        page = next_data_page([("B300-SXM-8-288G", 8, 224, 1, 0, ["fr-par-2"])]).replace(
            b'<script id="__NEXT_DATA__" type="application/json">', b'<script type="application/json" id="__NEXT_DATA__">')
        self.assertEqual(len(scaleway_history.read_page(page)), 1)

    def test_truncated_next_data_reads_complete_product_objects(self):
        page = next_data_page([("H100-1-80G", 1, 24, 0, 47775000, ["fr-par-2"]),
                               ("B300-SXM-8-288G", 8, 224, 1, 0, ["fr-par-2"])], "apiId").decode()
        page = page.replace('"hardware"', '"note":"brace } and quote \\" inside","hardware"', 1)
        cut = page.rfind('"B300-SXM-8-288G"') + 30                     # B300 객체 중간에서 잘림
        rows = scaleway_history.read_page(page[:cut].encode())
        self.assertEqual([(r["name"], r["amount"]) for r in rows], [("H100-1-80G", Decimal("0.047775"))])

    def test_unknown_page_fails(self):
        with self.assertRaises(scaleway.SourceInvalid):
            scaleway_history.read_page(b"<html>nothing</html>")


class History(ScalewayDb):
    def setUp(self):
        super().setUp()
        source = fx.source_id(self.conn)
        for day, eur, usd in ((date(2024, 5, 24), "1470", "1360"), (date(2025, 3, 21), "1580", "1460"),
                              (date(2026, 3, 6), "1500", "1350")):
            fx.write_rates(self.conn, source, day, {"USD": Decimal(usd), "EUR": Decimal(eur)}, T0)
        self.conn.commit()
        self.pages = {"20240524045214": gzip.compress(TABLE_2024),
                      "20250322230659": next_data_page([("H100-1-80G", 1, 24, 0, 45500000, ["fr-par-2", "pl-waw-2"])], "apiId"),
                      "20260307002714": next_data_page([("B300-SXM-8-288G", 8, 224, 1, 0, ["fr-par-2"]),
                                                        ("H100-1-80G", 1, 24, 0, 47775000, ["fr-par-2", "pl-waw-2"])])}

    def get_json(self, url, config):
        if url.startswith("https://web.archive.org/cdx/"):
            return CDX
        return super().get_json(url, config)

    def backfill(self):
        return scaleway_history.backfill(self.conn, since="2024-01", until="2026-11", now=self.now, get_json=self.get_json,
                                         get_bytes=lambda url, config: self.pages[url.split("/web/")[1].split("id_/")[0]],
                                         pause=lambda _s: None, log=lambda _m: None)

    def test_backfill_attaches_zoned_history_to_daily_products_and_2024_to_unknown_region(self):
        self.collect()
        summary = self.backfill()
        self.assertEqual(summary["status"], "success")
        rows = self.conn.execute(
            """SELECT p.original_product_name, p.region_code, ob.collected_at::date::text, pc.amount, pc.time_unit,
                      n.result_status
               FROM price_observation ob JOIN offer o USING (offer_id) JOIN product p USING (product_id)
               JOIN price_component pc ON pc.observation_id = ob.observation_id
               JOIN price_normalization n ON n.observation_id = ob.observation_id
               WHERE ob.collected_at < '2026-11-01' ORDER BY 1, 2, 3""").fetchall()
        self.assertIn(("H100-1-80G", None, "2024-05-24", Decimal("2.52"), "hour", "success"), rows)
        self.assertIn(("H100-1-80G", "pl-waw-2", "2025-03-22", Decimal("0.0455"), "minute", "success"), rows)
        self.assertIn(("B300-SXM-8-288G", "fr-par-2", "2026-03-07", Decimal("1"), "minute", "success"), rows)
        # 매일 수집 상품(zone 있음)은 새 구성 버전 없이 이어받는다
        self.assertEqual(count(self.conn, """SELECT count(*) FROM product WHERE region_code IS NOT NULL
                                              AND configuration_version > 1"""), 0)
        self.assertEqual(count(self.conn, "SELECT count(*) FROM collection_alert"), 0)

    def test_rerun_skips_loaded_captures(self):
        self.collect()
        self.backfill()
        observations = count(self.conn, "SELECT count(*) FROM price_observation")
        self.backfill()
        self.assertEqual(count(self.conn, "SELECT count(*) FROM price_observation"), observations)
