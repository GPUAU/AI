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
