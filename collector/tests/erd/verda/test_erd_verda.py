"""Verda 수집기(매일 API·Wayback 백필) → ERD 초안 스키마 적재 테스트. 네트워크를 쓰지 않는다."""
import gzip
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg

from gpu_collector import fetch
from gpu_collector.erd.verda import collect as verda, history as verda_history
from erd.support import DDL, SEED, connect, create_database, drop_database

T0 = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)


def row(instance, model, gpus, vram_total, top, locations, currency="usd", name=None):
    """instance-types 항목. top: (온디맨드, 스팟) 최상위 값. locations: {지역: (온디맨드, 스팟)} 또는 None(옛 응답)."""
    item = {"id": f"id-{instance}", "instance_type": instance, "model": model, "name": name or f"{model} SXM6",
            "description": "Dedicated Hardware Instance", "gpu": {"description": f"{gpus}x {model}", "number_of_gpus": gpus},
            "gpu_memory": {"size_in_gigabytes": vram_total}, "cpu": {"number_of_cores": 30 * gpus},
            "memory": {"size_in_gigabytes": 255 * gpus}, "storage": {"description": "dynamic"},
            "p2p": "1.8 TB/s", "price_per_hour": top[0], "spot_price": top[1], "dynamic_price": None,
            "currency": currency}
    if locations is not None:
        item["location_prices"] = [{"location_code": code, "price_per_hour": od, "spot_price": spot}
                                   for code, (od, spot) in locations.items()]
    return item


def instances(b300_fin01="12.66"):
    return [
        row("1B300.30V", "B300", 1, 268, (b300_fin01, "6.330"),
            {"FIN-01": (b300_fin01, "6.330"), "FIN-02": ("12.41", "6.205")}),
        row("8B300.240V", "B300", 8, 2144, ("101.3", "50.64"), {"FIN-01": ("101.3", "50.64")}),
        row("4B300.120V.CC.AMD", "B300 CC", 4, 1072, ("51.65", "25.83"), {"FIN-01": ("51.65", "25.83")}),
        # 최상위 가격이 지역 가격과 다른 경우: 지역 가격만 쓴다
        row("8H100.80S.176V", "H100", 8, 640, ("42.77", "21.38"), {"FIN-01": ("41.93", "20.96")}),
        row("1A100.22V", "A100 80GB", 1, 80, ("2.410", "1.2"), {"FIN-01": ("2.386", "1.193")}),   # 대상 밖
    ]


PERIODS = [
    {"code": "1_MONTH", "name": "1 month", "is_enabled": True, "unit_name": "month", "unit_value": 1,
     "discount_percentage": 2},
    {"code": "2_YEARS", "name": "2 years", "is_enabled": True, "unit_name": "year", "unit_value": 2,
     "discount_percentage": 25},
    {"code": "3_YEARS", "name": "3 years", "is_enabled": False, "unit_name": "year", "unit_value": 3,
     "discount_percentage": 40},
]


class VerdaDb(unittest.TestCase):
    def setUp(self):
        self.name = create_database()
        self.conn = connect(self.name)
        self.conn.execute(DDL.read_text(encoding="utf8"))
        self.conn.execute(SEED.read_text(encoding="utf8"))
        self.conn.commit()
        self.clock = [T0]
        self.instances = instances()
        self.periods = PERIODS
        self.long_term_error = None
        self.captures = {}
        self.clusters = [{"cluster_type": "72GB300", "model": "GB300 Cluster", "name": "GB300 SXM6 288GB"}]

    def tearDown(self):
        self.conn.close()
        drop_database(self.name)

    def now(self):
        self.clock[0] += timedelta(seconds=1)
        return self.clock[0]

    def get_json(self, url, config):
        if url == verda.INSTANCE_URL:
            return self.instances
        if url == verda.CLUSTER_URL:
            return self.clusters
        if url == verda.LONG_TERM_URL:
            if self.long_term_error:
                raise fetch.FetchError(self.long_term_error)
            return self.periods
        if "cdx/search" in url:
            if "api.datacrunch.io" not in url:
                return []
            return [["timestamp", "original", "digest"]] + [
                [stamp, "https://api.datacrunch.io/v1/instance-types", f"D{stamp}"] for stamp in sorted(self.captures)]
        raise AssertionError(url)

    def get_bytes(self, url, config):
        stamp = url.split("/web/")[1][:14]
        return self.captures[stamp]

    def collect(self):
        return verda.collect(self.conn, now=self.now, get_json=self.get_json, pause=lambda _s: None)

    def backfill(self, since="2026-08", verify=True):
        return verda_history.backfill(self.conn, since=since, until="2026-11", now=self.now, get_json=self.get_json,
                                      get_bytes=self.get_bytes, pause=lambda _s: None, verify=verify,
                                      log=lambda _m: None)

    def latest(self, instance, region, contract, months=None):
        return self.conn.execute(
            """SELECT n.nominal_usd_gpu_hour, n.effective_usd_gpu_hour, n.result_status, ob.price_certainty,
                      ob.collected_at, n.calculation_inputs, o.payment_option, ob.observation_id
               FROM price_observation ob JOIN price_normalization n USING (observation_id)
               JOIN offer o USING (offer_id) JOIN product p USING (product_id)
               WHERE p.original_product_name = %s AND p.region_code IS NOT DISTINCT FROM %s AND o.contract_type = %s
                 AND o.commitment_months IS NOT DISTINCT FROM %s
               ORDER BY ob.collected_at DESC, ob.observation_id DESC LIMIT 1""",
            (instance, region, contract, months)).fetchone()

    def product(self, instance, region):
        return self.conn.execute(
            """SELECT service_option, country_code, gpu_count, vram_gb_per_gpu, interconnect_status,
                      intra_node_interconnect, storage_description
               FROM product WHERE original_product_name = %s AND region_code IS NOT DISTINCT FROM %s
               ORDER BY configuration_version DESC LIMIT 1""", (instance, region)).fetchone()


class CollectTests(VerdaDb):
    def test_loads_location_prices_per_region(self):
        summary = self.collect()
        self.assertEqual(summary["status"], "success")
        self.assertEqual(self.latest("1B300.30V", "FIN-01", "on_demand")[0], Decimal("12.66"))
        self.assertEqual(self.latest("1B300.30V", "FIN-02", "on_demand")[0], Decimal("12.41"))
        self.assertEqual(self.latest("1B300.30V", "FIN-02", "spot")[1], Decimal("6.205"))
        self.assertEqual(self.latest("8B300.240V", "FIN-01", "on_demand")[0], Decimal("101.3") / 8)
        # 최상위 가격(42.77)이 아니라 지역 가격(41.93)
        self.assertEqual(self.latest("8H100.80S.176V", "FIN-01", "on_demand")[0], Decimal("41.93") / 8)
        self.assertIsNone(self.latest("1A100.22V", "FIN-01", "on_demand"))
        self.assertIsNone(self.latest("1B300.30V", None, "on_demand"))

    def test_product_spec_service_option_and_interconnect(self):
        self.collect()
        one = self.product("1B300.30V", "FIN-01")
        self.assertEqual(one[:4], ("standard", "FI", 1, Decimal(268)))
        self.assertEqual(one[4], "not_applicable")
        self.assertIn("별도 블록 스토리지", one[6])
        eight = self.product("8B300.240V", "FIN-01")
        self.assertEqual(eight[3], Decimal(268))
        self.assertEqual(eight[4:6], ("confirmed", "NVLink, GPU 간 1.8 TB/s"))
        self.assertEqual(self.product("4B300.120V.CC.AMD", "FIN-01")[0], "confidential")
        # 사양 근거: 상품마다 한 번, 모델 페이지 출처
        rows = self.conn.execute(
            """SELECT count(DISTINCT pe.product_id) FROM product_evidence pe
               JOIN source_evidence se USING (evidence_id) JOIN data_source s ON s.source_id = se.source_id
               WHERE s.source_name = 'verda_product_pages'""").fetchone()[0]
        self.assertEqual(rows, 5)   # 1B300 2지역 + 8B300 + B300 CC + 8H100, 전부 근거 연결
        self.assertEqual(self.conn.execute("SELECT count(*) FROM product").fetchone()[0], 5)

    def test_commitment_is_discount_assumption_on_same_run_on_demand(self):
        self.collect()
        on_demand = self.latest("8B300.240V", "FIN-01", "on_demand")
        two_years = self.latest("8B300.240V", "FIN-01", "commitment", 24)
        self.assertIsNone(two_years[0])
        self.assertEqual(two_years[1], Decimal("101.3") * Decimal("0.75") / 8)
        self.assertEqual(two_years[2:4], ("assumption", "estimate"))
        self.assertEqual(two_years[5]["base_observation_id"], on_demand[7])
        self.assertEqual(two_years[6], "all_upfront")
        self.assertEqual(self.latest("1B300.30V", "FIN-02", "commitment", 1)[1], Decimal("12.41") * Decimal("0.98"))
        self.assertIsNone(self.latest("1B300.30V", "FIN-01", "commitment", 36))     # 사용 불가 기간
        # 구성 요소는 할인율 하나(같은 관측 소속), 기준 온디맨드는 입력 기록에만 둔다
        component = self.conn.execute(
            """SELECT c.component_type, c.discount_rate, c.amount FROM price_component c
               WHERE c.observation_id = (SELECT ob.observation_id FROM price_observation ob JOIN offer o USING (offer_id)
                                         JOIN product p USING (product_id)
                                         WHERE p.original_product_name = '8B300.240V' AND o.commitment_months = 24)""").fetchall()
        self.assertEqual(component, [("discount_rate", Decimal("0.25"), None)])

    def test_second_run_reuses_products_and_marks_missing_offer(self):
        self.collect()
        before = self.conn.execute("SELECT count(*) FROM product").fetchone()[0]
        self.instances = [r for r in instances("12.79") if r["instance_type"] != "8H100.80S.176V"]
        summary = self.collect()
        self.assertEqual(summary["status"], "success")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM product").fetchone()[0], before)
        self.assertEqual(self.latest("1B300.30V", "FIN-01", "on_demand")[0], Decimal("12.79"))
        missing = self.conn.execute(
            """SELECT count(*) FROM collection_check c JOIN product p USING (product_id)
               WHERE c.run_id = %s AND c.processing_status = 'not_found' AND p.original_product_name = '8H100.80S.176V'""",
            (summary["run_id"],)).fetchone()[0]
        self.assertEqual(missing, 4)   # 온디맨드 1, 스팟 1, 약정 2(1개월·2년)

    def test_long_term_failure_keeps_instance_prices(self):
        self.long_term_error = "http_503"
        summary = self.collect()
        self.assertEqual(summary["status"], "partial_failure")
        self.assertIsNotNone(self.latest("1B300.30V", "FIN-01", "on_demand"))
        self.assertIsNone(self.latest("1B300.30V", "FIN-01", "commitment", 1))
        failed = self.conn.execute(
            """SELECT count(*) FROM collection_check c JOIN collection_target t USING (target_id)
               JOIN data_source s ON s.source_id = t.source_id
               WHERE s.source_name = 'verda_long_term_periods_api' AND c.processing_status = 'collection_failed'
                 AND c.disclosure_status = 'unknown'""").fetchone()[0]
        self.assertEqual(failed, 5)

    def test_non_usd_is_excluded(self):
        self.instances = [row("1B300.30V", "B300", 1, 268, ("11.0", "5.5"), {"FIN-01": ("11.0", "5.5")}, currency="eur")]
        summary = self.collect()
        self.assertIsNone(self.latest("1B300.30V", "FIN-01", "on_demand"))
        reason = self.conn.execute(
            """SELECT c.reason FROM collection_check c JOIN collection_target t USING (target_id)
               JOIN gpu_model m USING (gpu_model_id) JOIN data_source s ON s.source_id = t.source_id
               WHERE c.run_id = %s AND c.check_scope = 'target' AND m.model_name = 'B300'
                 AND s.source_name = 'verda_instance_types_api' AND t.contract_filter = 'on_demand'""",
            (summary["run_id"],)).fetchone()[0]
        self.assertIn("제외 1건", reason)


class DetectTests(VerdaDb):
    def vr200_check(self):
        return self.conn.execute(
            """SELECT c.disclosure_status, c.processing_status, c.reason FROM collection_check c
               JOIN collection_target t USING (target_id) JOIN gpu_model m USING (gpu_model_id)
               JOIN data_source s ON s.source_id = t.source_id
               WHERE m.model_name = 'VR200' AND s.source_name = 'verda_instance_types_api'
               ORDER BY c.check_id DESC LIMIT 1""").fetchone()

    def test_vr200_not_listed_is_not_found(self):
        self.assertEqual(self.collect()["status"], "success")
        disclosure, processing, reason = self.vr200_check()
        self.assertEqual((disclosure, processing), ("unknown", "not_found"))
        self.assertIn("미공개로 단정하지 않음", reason)

    def test_vr200_listed_in_cluster_types_is_public(self):
        self.clusters.append({"cluster_type": "72VR200", "model": "VR200 Cluster", "name": "Vera Rubin NVL72"})
        self.collect()
        disclosure, processing, reason = self.vr200_check()
        self.assertEqual((disclosure, processing), ("public", "success"))
        self.assertIn("72VR200", reason)


class BackfillTests(VerdaDb):
    def setUp(self):
        super().setUp()
        old = [row("1B300.30V", "B300", 1, 268, ("7.50", "3.75"), None),
               row("8B300.240V", "B300", 8, 2144, ("60.00", "30.00"), None)]
        recent = instances("12.41")
        self.captures = {"20260831072519": gzip.compress(json.dumps(old).encode()),
                         "20261115164215": json.dumps(recent).encode()}

    def test_backfill_top_level_to_unknown_region_and_location_prices_to_regions(self):
        summary = self.backfill()
        self.assertEqual(summary["status"], "success")
        old = self.latest("8B300.240V", None, "on_demand")
        self.assertEqual(old[0], Decimal("60.00") / 8)
        self.assertEqual(old[4], datetime(2026, 8, 31, 7, 25, 19, tzinfo=timezone.utc))
        self.assertEqual(self.product("8B300.240V", None)[1], None)          # 지역 미상이면 국가도 모름
        recent = self.latest("1B300.30V", "FIN-02", "on_demand")
        self.assertEqual(recent[0], Decimal("12.41"))
        # 약정 할인율 저장본은 없어 약정은 적재하지 않는다
        self.assertEqual(self.conn.execute("SELECT count(*) FROM offer WHERE contract_type = 'commitment'").fetchone()[0], 0)

    def test_primary_verified_against_current_api(self):
        self.backfill()
        rows = dict(self.conn.execute(
            """SELECT p.region_code || ':' || p.original_product_name || ':' || o.contract_type, se.primary_verified
               FROM source_evidence se JOIN collection_check c USING (check_id) JOIN offer o ON o.offer_id = c.offer_id
               JOIN product p ON p.product_id = o.product_id
               WHERE se.evidence_role = 'aggregator' AND p.region_code IS NOT NULL""").fetchall())
        self.assertTrue(rows["FIN-02:1B300.30V:on_demand"])          # 지금도 12.41
        self.assertFalse(rows["FIN-01:1B300.30V:on_demand"])         # 저장본 12.41, 지금 12.66

    def test_rerun_skips_loaded_captures_and_daily_collect_reuses_products(self):
        self.backfill()
        observations = self.conn.execute("SELECT count(*) FROM price_observation").fetchone()[0]
        self.assertEqual(self.backfill()["groups"], [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM price_observation").fetchone()[0], observations)
        products = self.conn.execute("SELECT count(*) FROM product WHERE region_code IS NOT NULL").fetchone()[0]
        self.collect()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM product WHERE region_code IS NOT NULL").fetchone()[0],
                         products)

    def test_broken_capture_is_recorded_as_validation_failure(self):
        self.captures["20261001000000"] = b"<html>not json</html>"
        summary = self.backfill()
        self.assertEqual(summary["status"], "partial_failure")
        self.assertEqual([g["status"] for g in summary["groups"]], ["success", "validation_failed", "success"])


if __name__ == "__main__":
    unittest.main()
