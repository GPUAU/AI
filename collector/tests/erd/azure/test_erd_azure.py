"""Azure 수집기(매일 API·GitHub 스냅샷 백필) → ERD 초안 스키마 적재 테스트. 네트워크를 쓰지 않는다."""
import csv
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg

from gpu_collector import fetch
from gpu_collector.erd.azure import collect as azure, history as azure_history
from erd.support import DDL, SEED, connect, create_database, drop_database

T0 = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)
H100, GB200 = "Standard_ND96isr_H100_v5", "Standard_ND128isr_NDR_GB200_v6"


def item(sku, region, kind, price, start="2023-12-01", term=None, product="Virtual Machines NDsr H100 v5 Series",
         sku_name="ND96isrH100v5", **extra):
    """Retail Prices API 항목 하나. kind: Consumption / Reservation / DevTestConsumption."""
    return {"currencyCode": "USD", "retailPrice": price, "unitPrice": price, "armRegionName": region,
            "location": region, "effectiveStartDate": f"{start}T00:00:00Z", "meterId": f"m-{sku}-{sku_name}",
            "meterName": sku_name, "productId": "DZH318Z0KN53", "skuId": "DZH318Z0KN53/0001", "productName": product,
            "skuName": sku_name, "serviceName": "Virtual Machines", "serviceId": "DZH313Z7MMC8",
            "serviceFamily": "Compute", "unitOfMeasure": "1 Hour", "type": kind, "isPrimaryMeterRegion": True,
            "armSkuName": sku, "reservationTerm": term, **extra}


def api_items(spot="18.169536", od="98.32"):
    return [
        item(H100, "eastus", "Consumption", od),
        item(H100, "eastus", "Consumption", spot, start="2026-01-01", sku_name="ND96isrH100v5 Spot"),
        item(H100, "eastus", "Consumption", "41.094", sku_name="ND96isrH100v5 Low Priority"),
        item(H100, "eastus", "DevTestConsumption", "98.32"),
        item(H100, "eastus", "Consumption", "102.736", product="Virtual Machines NDsr H100 v5 Series Windows"),
        item(H100, "eastus", "Reservation", "551221.0", term="1 Year"),
        item(H100, "eastus", "Reservation", "1134310.0", term="3 Years"),
        item(H100, "eastus", "Reservation", "105120.0", term="10 Years"),   # 이상값(알 수 없는 기간)
        item(H100, "koreacentral", "Consumption", "132.732", start="2025-01-01"),
        item(GB200, "eastus", "Consumption", "108.16", start="2025-04-01", product="Virtual Machines NDsrGB200NDRv6",
             sku_name="ND128isrNDRGB200v6",
             savingsPlan=[{"term": "1 Year", "retailPrice": 80.0, "unitPrice": 80.0}]),
        item(GB200, "eastus", "Reservation", "606388.0", start="2025-04-01", term="1 Year",
             product="Virtual Machines NDsrGB200NDRv6", sku_name="ND128isrNDRGB200v6"),
        item(GB200, "eastus", "Reservation", "1.0", start="2025-04-01", term="3 Years",        # 총액이 아님
             product="Virtual Machines NDsrGB200NDRv6", sku_name="ND128isrNDRGB200v6"),
    ]


class AzureDb(unittest.TestCase):
    def setUp(self):
        self.name = create_database()
        self.conn = connect(self.name)
        self.conn.execute(DDL.read_text(encoding="utf8"))
        self.conn.execute(SEED.read_text(encoding="utf8"))
        self.conn.commit()
        self.temp = tempfile.TemporaryDirectory()
        self.clock = [T0]
        self.api = api_items()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()
        drop_database(self.name)

    def now(self):
        self.clock[0] += timedelta(seconds=1)
        return self.clock[0]

    def get_json(self, url, config):
        if "contains" in url:
            return {"Items": [], "NextPageLink": None}
        return {"Items": self.api, "NextPageLink": None}

    def collect(self):
        return azure.collect(self.conn, now=self.now, get_json=self.get_json, pause=lambda _s: None)

    def latest(self, sku, region, contract, months=None, product_type=None):
        return self.conn.execute(
            """SELECT n.nominal_usd_gpu_hour, n.effective_usd_gpu_hour, ob.effective_from, ob.collected_at
               FROM price_observation ob JOIN price_normalization n USING (observation_id)
               JOIN offer o USING (offer_id) JOIN product p USING (product_id)
               WHERE p.original_product_name = %s AND p.region_code = %s AND o.contract_type = %s
                 AND o.commitment_months IS NOT DISTINCT FROM %s
                 AND (%s::text IS NULL OR o.contract_product_type = %s)
               ORDER BY ob.collected_at DESC, ob.observation_id DESC LIMIT 1""",
            (sku, region, contract, months, product_type, product_type)).fetchone()

    def target_check(self, model, contract, source="azure_retail_prices_api"):
        return self.conn.execute(
            """SELECT c.disclosure_status, c.processing_status, c.reason FROM collection_check c
               JOIN collection_target t USING (target_id) JOIN gpu_model m USING (gpu_model_id)
               JOIN data_source s ON s.source_id = t.source_id
               WHERE m.model_name = %s AND t.contract_filter IS NOT DISTINCT FROM %s AND s.source_name = %s
                 AND c.check_scope = 'target' ORDER BY c.check_id DESC LIMIT 1""", (model, contract, source)).fetchone()


class CollectTests(AzureDb):
    def test_loads_target_rows_and_normalizes(self):
        summary = self.collect()
        self.assertEqual(summary["status"], "success")
        od = self.latest(H100, "eastus", "on_demand")
        self.assertEqual(od[0], Decimal("98.32") / 8)
        self.assertEqual(od[2], datetime(2023, 12, 1, tzinfo=timezone.utc))
        self.assertEqual(self.latest(H100, "eastus", "spot")[1], Decimal("18.169536") / 8)
        reserved = self.latest(H100, "eastus", "commitment", 12, "Reserved VM Instances")
        self.assertIsNone(reserved[0])
        self.assertEqual(reserved[1], Decimal("551221.0") / (12 * 730) / 8)
        self.assertEqual(self.latest(GB200, "eastus", "commitment", 12, "Reserved VM Instances")[1], Decimal("606388.0") / 8760 / 4)
        # preview 응답의 Savings Plan 요율은 별도 오퍼(시간당)로 적재된다
        sp = self.conn.execute(
            """SELECT o.contract_product_type, n.effective_usd_gpu_hour FROM offer o JOIN product p USING (product_id)
               JOIN price_observation ob USING (offer_id) JOIN price_normalization n USING (observation_id)
               WHERE p.original_product_name = %s AND o.contract_product_type = 'Savings Plan for Compute'""",
            (GB200,)).fetchone()
        self.assertEqual(sp, ("Savings Plan for Compute", Decimal("80.0") / 4))
        self.assertEqual(self.latest(H100, "koreacentral", "on_demand")[0], Decimal("132.732") / 8)

    def test_excludes_windows_devtest_low_priority_and_implausible_reservations(self):
        self.collect()
        offers = self.conn.execute(
            """SELECT p.original_product_name, p.region_code, o.contract_type, o.commitment_months FROM offer o
               JOIN product p USING (product_id) ORDER BY 1, 2, 3, 4""").fetchall()
        self.assertEqual(offers, [
            (GB200, "eastus", "commitment", 12), (GB200, "eastus", "commitment", 12), (GB200, "eastus", "on_demand", None),
            (H100, "eastus", "commitment", 12), (H100, "eastus", "commitment", 36), (H100, "eastus", "on_demand", None),
            (H100, "eastus", "spot", None), (H100, "koreacentral", "on_demand", None)])
        # 3년 예약(총액 1.0)과 10년 예약은 근거에 제외 사유로 남는다
        excerpt = self.conn.execute(
            """SELECT e.excerpt FROM source_evidence e JOIN collection_check c USING (check_id)
               JOIN collection_target t USING (target_id) JOIN gpu_model m USING (gpu_model_id)
               WHERE c.check_scope = 'target' AND m.model_name = 'GB200' AND t.contract_filter = 'reservations'""").fetchone()[0]
        self.assertIn("reservation_total_not_plausible", excerpt)
        self.assertIn("unknown_term: 10 Years", self.conn.execute(
            """SELECT e.excerpt FROM source_evidence e JOIN collection_check c USING (check_id)
               JOIN collection_target t USING (target_id) JOIN gpu_model m USING (gpu_model_id)
               WHERE c.check_scope = 'target' AND m.model_name = 'H100' AND t.contract_filter = 'reservations'""").fetchone()[0])

    def test_spec_evidence_confirms_interconnect_and_fills_specs(self):
        self.collect()
        rows = self.conn.execute(
            """SELECT p.original_product_name, p.gpu_count, p.vram_gb_per_gpu, p.original_model_name,
                      p.intra_node_interconnect, p.interconnect_status, p.country_code,
                      (SELECT count(*) FROM product_evidence pe WHERE pe.product_id = p.product_id)
               FROM product p WHERE p.region_code = 'eastus' ORDER BY 1""").fetchall()
        self.assertEqual([r[:3] for r in rows], [(GB200, 4, Decimal(192)), (H100, 8, Decimal(80))])
        for row in rows:
            self.assertEqual(row[5], "confirmed")
            self.assertTrue(row[4])
            self.assertEqual(row[6], "US")
            self.assertEqual(row[7], len(azure.SPEC_ATTRIBUTES) + 1)
        # 두 번째 수집은 사양 근거를 다시 만들지 않는다
        before = self.conn.execute("SELECT count(*) FROM source_evidence WHERE evidence_role = 'spec'").fetchone()[0]
        self.collect()
        self.assertEqual(before, self.conn.execute(
            "SELECT count(*) FROM source_evidence WHERE evidence_role = 'spec'").fetchone()[0])

    def test_status_for_missing_and_detection_targets(self):
        self.collect()
        self.assertEqual(self.target_check("GB300", "on_demand")[:2], ("unknown", "not_found"))
        self.assertEqual(self.target_check("B300", None)[:2], ("unknown", "not_found"))
        self.assertEqual(self.target_check("H100", "on_demand")[:2], ("public", "success"))

    def test_price_change_alert_and_missing_offer(self):
        self.collect()
        self.api = [r for r in api_items(spot="30.0") if r["armRegionName"] != "koreacentral"]
        self.collect()
        alerts = self.conn.execute("SELECT alert_type FROM collection_alert ORDER BY alert_id").fetchall()
        self.assertEqual(alerts, [("price_spike",)])
        missing = self.conn.execute(
            """SELECT count(*) FROM collection_check WHERE check_scope = 'offer' AND processing_status = 'not_found'"""
        ).fetchone()[0]
        self.assertEqual(missing, 1)

    def test_failure_is_recorded_without_disclosure_change(self):
        def broken(url, config):
            raise fetch.FetchError("http_503")
        summary = azure.collect(self.conn, now=self.now, get_json=broken, pause=lambda _s: None)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(self.target_check("H100", "on_demand")[:2], ("unknown", "collection_failed"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM price_observation").fetchone()[0], 0)


class SnapshotTests(AzureDb):
    RELEASES = [("2026-01-01", "2026-01-01T06:30:00Z"), ("2026-01-15", "2026-01-15T06:30:00Z"),
                ("2026-02-01", "2026-02-01T06:31:00Z")]

    def setUp(self):
        super().setUp()
        self.snapshots = {"2026-01-01": api_items(spot="19.02492"), "2026-01-15": api_items(spot="19.02492"),
                          "2026-02-01": api_items()}
        for row in self.snapshots["2026-01-01"] + self.snapshots["2026-01-15"]:
            if row["skuName"].endswith("Spot"):
                row["effectiveStartDate"] = "2025-12-01T00:00:00Z"
        self.downloads = []

    def get_json(self, url, config):
        if "api.github.com" in url:
            if not url.endswith("page=1"):
                return []
            return [{"tag_name": f"prices-{day}", "published_at": published,
                     "assets": [{"name": "azure-retail-prices-usd.csv", "size": 0,
                                 "browser_download_url": f"https://example.test/prices-{day}.csv"}]}
                    for day, published in reversed(self.RELEASES)] + [{"tag_name": "latest", "assets": []}]
        return super().get_json(url, config)

    def downloader(self, url, destination, config):
        day = url.split("prices-")[1][:10]
        self.downloads.append(day)
        fields = list(azure.EXCERPT_FIELDS) + ["serviceName", "serviceId", "serviceFamily", "tierMinimumUnits"]
        with open(destination, "w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in azure.expand_savings_plans(self.snapshots[day]):
                writer.writerow({k: ("" if v is None else v) for k, v in row.items() if k != "savingsPlan"})
        return fetch.FetchResult(url=url, path=Path(destination), sha256="b" * 64, size=Path(destination).stat().st_size)

    def backfill(self, every="month"):
        return azure_history.backfill(self.conn, since="2026-01", until="2026-02", every=every, cache_dir=self.temp.name,
                                      now=self.now, get_json=self.get_json, downloader=self.downloader)

    def test_monthly_snapshots_load_with_source_time_and_verification(self):
        summary = self.backfill()
        self.assertEqual(summary["status"], "success")
        self.assertEqual(self.downloads, ["2026-01-01", "2026-02-01"])
        spot = self.conn.execute(
            """SELECT n.effective_usd_gpu_hour, ob.collected_at, ob.effective_from, e.primary_verified, e.evidence_role
               FROM price_observation ob JOIN price_normalization n USING (observation_id)
               JOIN offer o USING (offer_id) JOIN product p USING (product_id)
               JOIN source_evidence e ON e.check_id = ob.check_id
               WHERE p.original_product_name = %s AND p.region_code = 'eastus' AND o.contract_type = 'spot'
               ORDER BY ob.collected_at""", (H100,)).fetchall()
        self.assertEqual(spot[0], (Decimal("19.02492") / 8, datetime(2026, 1, 1, 6, 30, tzinfo=timezone.utc),
                                   datetime(2025, 12, 1, tzinfo=timezone.utc), False, "aggregator"))
        self.assertEqual(spot[1][0], Decimal("18.169536") / 8)
        self.assertTrue(spot[1][3])        # 지금 API 값과 같다
        self.assertEqual(self.conn.execute("SELECT count(*) FROM collection_alert").fetchone()[0], 0)
        self.assertFalse(any(Path(self.temp.name, "azure", "snapshot").iterdir()))

    def test_rerun_skips_loaded_snapshots_and_daily_collect_continues(self):
        self.backfill()
        count = self.conn.execute("SELECT count(*) FROM price_observation").fetchone()[0]
        self.downloads.clear()
        self.backfill()
        self.assertEqual(self.downloads, [])
        self.assertEqual(count, self.conn.execute("SELECT count(*) FROM price_observation").fetchone()[0])
        self.assertEqual(self.collect()["status"], "success")
        products = self.conn.execute("SELECT count(*), max(configuration_version) FROM product").fetchone()
        self.assertEqual(products, (3, 1))

    def test_weekly_picks_more_snapshots(self):
        self.backfill(every="week")
        self.assertEqual(self.downloads, ["2026-01-01", "2026-01-15", "2026-02-01"])


if __name__ == "__main__":
    unittest.main()
