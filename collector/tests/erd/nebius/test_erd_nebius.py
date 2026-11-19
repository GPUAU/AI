"""Nebius 수집기(문서 가격표·스팟 API, 스팟 30일 이력·문서 Wayback) → ERD 초안 스키마 적재 테스트. 네트워크를 쓰지 않는다."""
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg

from gpu_collector.erd.nebius import collect as nebius, history as nebius_history
from erd.support import DDL, SEED, connect, create_database, drop_database

T0 = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)

CURRENT = r"""# Compute pricing in Nebius AI Cloud

#### NVIDIA® B300 NVLink, gpu-b300-sxm

The platform is only available in the `uk-south1`, `eu-west2` and `us-north1` [regions](https://docs.nebius.com/overview/regions.md).

> [tab] USD
>
> | **Item** | **Price** | **Per** |
> | - | - | - |
> | NVIDIA® B300 NVLink | \$9.50 | 1 GPU hour |
> | Preemptible NVIDIA® B300 NVLink | [From \$0.99](https://docs.nebius.com/signup-billing/pricing-policy.md) | 1 GPU hour |

> [tab] ILS
>
> | NVIDIA® B300 NVLink | ₪29.45 | 1 GPU hour |

#### NVIDIA® H100 NVLink

The platform is only available in the `eu-north1` [region](https://docs.nebius.com/overview/regions.md).

> [tab] USD
>
> | **Item** | **Price** | **Per** |
> | - | - | - |
> | NVIDIA® H100 NVLink | \$4.50 | 1 GPU hour |
> | Preemptible NVIDIA® H100 NVLink | [From \$0.79](https://docs.nebius.com/signup-billing/pricing-policy.md) | 1 GPU hour |

#### NVIDIA® RTX PRO™ 6000, gpu-rtx6000

The platform is only available in the `us-central1` [region](https://docs.nebius.com/overview/regions.md).
"""

# 2026-09-30 저장본 형식: 10/1부터 바뀌는 가격이 함께 있다
CAPTURE_SEP = r"""#### NVIDIA® B300 NVLink, gpu-b300-sxm

The platform is only available in the `uk-south1`, `eu-west2` and `us-north1` [regions](/overview/regions).

<Tabs>
  <Tab title="USD">
    | **Item — from October 1, 2026** | **Price** | **Per** |
    | - | - | - |
    | NVIDIA® B300 NVLink | \$9.50 | 1 GPU hour |
    | Preemptible NVIDIA® B300 NVLink | \$4.30 | 1 GPU hour |
    | **Item — before October 1, 2026** | **Price** | **Per** |
    | NVIDIA® B300 NVLink | \$7.85 | 1 GPU hour |
    | Preemptible NVIDIA® B300 NVLink | \$4.30 | 1 GPU hour |
  </Tab>
  <Tab title="ILS">
    | NVIDIA® B300 NVLink | ₪24.335 | 1 GPU hour |
  </Tab>
</Tabs>

#### NVIDIA® H100 NVLink

The platform is only available in the `eu-north1` [region](/overview/regions).

<Tabs>
  <Tab title="USD">
    | **Item — [before October 1, 2025](#unified)** | **Price** | **Per** |
    | NVIDIA® H100 NVLink. GPU | \$2.118 | 1 GPU hour |
    | NVIDIA® H100 NVLink. CPU | \$0.012 | 1 CPU hour |
    | **Item — [from October 1, 2025](#unified)** | **Price** | **Per** |
    | NVIDIA® H100 NVLink | \$3.85 | 1 GPU hour |
    | Preemptible NVIDIA® H100 NVLink | \$1.25 | 1 GPU hour |
  </Tab>
</Tabs>
"""


def spot_item(platform, region, value, sku):
    return {"skuId": sku, "productTitle": f"Preemptible {platform}", "region": region,
            "computeInstance": {"platform": platform}, "price": {"value": value, "currency": "usd", "unitTitle": "GPU hour"},
            "effectiveTime": "2026-11-18T07:36:00Z"}


SPOT = [spot_item("gpu-b300-sxm", "uk-south1", "6.95", "sku-b300-uk"),
        spot_item("gpu-b300-sxm", "us-north1", "3.10", "sku-b300-us"),
        spot_item("gpu-h100-sxm", "eu-north1", "1.70", "sku-h100"),
        spot_item("gpu-rtx6000", "us-central1", "0.80", "sku-rtx")]


def series(points):
    return {"items": [{"skuId": "sku-b300-uk", "productTitle": "Preemptible NVIDIA® B300 NVLink", "region": "uk-south1",
                       "computeInstance": {"platform": "gpu-b300-sxm"}, "currency": "usd", "unitTitle": "GPU hour",
                       "points": [{"timestamp": ts, "value": v} for ts, v in points]}]}


class NebiusDb(unittest.TestCase):
    def setUp(self):
        self.name = create_database()
        self.conn = connect(self.name)
        self.conn.execute(DDL.read_text(encoding="utf8"))
        self.conn.execute(SEED.read_text(encoding="utf8"))
        self.conn.commit()
        self.clock = [T0]
        self.doc = CURRENT
        self.spot = SPOT
        self.series = series([("2026-11-17T10:00:00Z", "5.0"), ("2026-11-17T11:00:00Z", "5.2")])
        self.captures = {"20260930084721": CAPTURE_SEP}

    def tearDown(self):
        self.conn.close()
        drop_database(self.name)

    def now(self):
        self.clock[0] += timedelta(seconds=1)
        return self.clock[0]

    def get_text(self, url, config):
        if url == nebius.PRICING_URL:
            return self.doc
        return self.captures[url.split("/web/")[1][:14]]

    def get_json(self, url, config):
        if url.startswith(nebius.SPOT_URL + "?"):
            return {"items": self.spot}
        if url.startswith(nebius_history.SERIES_URL):
            return self.series
        if "cdx/search" in url:
            return [["timestamp", "original", "digest"]] + [
                [ts, "https://docs.nebius.com/compute/resources/pricing.md", f"D{ts}"] for ts in sorted(self.captures)]
        raise AssertionError(url)

    def collect(self):
        return nebius.collect(self.conn, now=self.now, get_text=self.get_text, get_json=self.get_json,
                              pause=lambda _s: None)

    def backfill(self, **kwargs):
        return nebius_history.backfill(self.conn, now=self.now, get_text=self.get_text, get_json=self.get_json,
                                       pause=lambda _s: None, log=lambda _m: None, **kwargs)

    def prices(self, product, region, contract):
        return [r[0] for r in self.conn.execute(
            """SELECT n.nominal_usd_gpu_hour FROM price_observation ob JOIN price_normalization n USING (observation_id)
               JOIN offer o USING (offer_id) JOIN product p USING (product_id)
               WHERE p.original_product_name = %s AND p.region_code = %s AND o.contract_type = %s
               ORDER BY ob.collected_at, ob.observation_id""", (product, region, contract)).fetchall()]


class ParseTests(unittest.TestCase):
    def test_current_doc(self):
        sections = nebius.parse_pricing(CURRENT, T0)
        self.assertEqual(set(sections), {"gpu-b300-sxm", "gpu-h100-sxm"})
        self.assertEqual(sections["gpu-b300-sxm"]["regions"], ["uk-south1", "eu-west2", "us-north1"])
        self.assertEqual(sections["gpu-b300-sxm"]["on_demand"]["amount"], Decimal("9.50"))
        self.assertIsNone(sections["gpu-b300-sxm"]["spot_fixed"])                 # 'From $0.99'는 시작가
        self.assertEqual(sections["gpu-b300-sxm"]["skipped"][0]["reason"], "starting_from")

    def test_period_tables_follow_capture_time(self):
        before = nebius.parse_pricing(CAPTURE_SEP, datetime(2026, 9, 30, 8, 47, tzinfo=timezone.utc))
        self.assertEqual(before["gpu-b300-sxm"]["on_demand"]["amount"], Decimal("7.85"))
        self.assertEqual(before["gpu-b300-sxm"]["spot_fixed"]["amount"], Decimal("4.30"))
        self.assertEqual(before["gpu-h100-sxm"]["on_demand"]["amount"], Decimal("3.85"))   # 분리 과금 행은 쓰지 않음
        after = nebius.parse_pricing(CAPTURE_SEP, datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.assertEqual(after["gpu-b300-sxm"]["on_demand"]["amount"], Decimal("9.50"))
        self.assertEqual(after["gpu-b300-sxm"]["on_demand"]["effective_from"], datetime(2026, 10, 1, tzinfo=timezone.utc))


CAPTURE_OCT = r"""#### NVIDIA® B300 NVLink, gpu-b300-sxm

The platform is only available in the `uk-south1`, `eu-west2` and `us-north1` [regions](https://docs.nebius.com/overview/regions.md).

**USD**

| **Item — from October 8, 2026** | **Price** | **Per** |
| - | - | - |
| NVIDIA® B300 NVLink | \$9.50 | 1 GPU hour |
| Preemptible NVIDIA® B300 NVLink | [From \$0.99](https://docs.nebius.com/signup-billing/pricing-policy.md) | 1 GPU hour |
| **Item — from October 1 until October 8, 2026** | **Price** | **Per** |
| NVIDIA® B300 NVLink | \$9.50 | 1 GPU hour |
| Preemptible NVIDIA® B300 NVLink | \$4.30 | 1 GPU hour |
| **Item — before October 1, 2026** | **Price** | **Per** |
| NVIDIA® B300 NVLink | \$7.85 | 1 GPU hour |

**ILS**

| NVIDIA® B300 NVLink | ₪29.45 | 1 GPU hour |
"""


class BoldHeadingFormatTests(unittest.TestCase):
    def test_until_period_and_bold_usd_heading(self):
        mid = nebius.parse_pricing(CAPTURE_OCT, datetime(2026, 10, 5, tzinfo=timezone.utc))["gpu-b300-sxm"]
        self.assertEqual((mid["on_demand"]["amount"], mid["spot_fixed"]["amount"]), (Decimal("9.50"), Decimal("4.30")))
        self.assertEqual(mid["on_demand"]["effective_from"], datetime(2026, 10, 1, tzinfo=timezone.utc))
        late = nebius.parse_pricing(CAPTURE_OCT, datetime(2026, 10, 25, tzinfo=timezone.utc))["gpu-b300-sxm"]
        self.assertEqual(late["on_demand"]["amount"], Decimal("9.50"))
        self.assertIsNone(late["spot_fixed"])                                    # 10/8부터 시작가만
        early = nebius.parse_pricing(CAPTURE_OCT, datetime(2026, 9, 1, tzinfo=timezone.utc))["gpu-b300-sxm"]
        self.assertEqual(early["on_demand"]["amount"], Decimal("7.85"))


class CollectTests(NebiusDb):
    def test_on_demand_per_preset_and_region_spot_and_commitment_status(self):
        summary = self.collect()
        self.assertEqual(summary["status"], "success")
        self.assertEqual(self.prices("gpu-b300-sxm 8gpu-192vcpu-2768gb", "eu-west2", "on_demand"), [Decimal("9.50")])
        self.assertEqual(self.prices("gpu-b300-sxm 1gpu-24vcpu-346gb", "us-north1", "on_demand"), [Decimal("9.50")])
        self.assertEqual(self.prices("gpu-b300-sxm 8gpu-192vcpu-2768gb", "uk-south1", "spot"), [Decimal("6.95")])
        self.assertEqual(self.prices("gpu-h100-sxm 8gpu-128vcpu-1600gb", "eu-north1", "spot"), [Decimal("1.70")])
        product = self.conn.execute(
            """SELECT gpu_count, vram_gb_per_gpu, vcpu_count, country_code, interconnect_status, inter_node_network
               FROM product WHERE original_product_name = 'gpu-b300-sxm 8gpu-192vcpu-2768gb' AND region_code = 'uk-south1'""").fetchone()
        self.assertEqual(product[:5], (8, Decimal(270), 192, "GB", "confirmed"))
        self.assertIn("InfiniBand 800 Gbps", product[5])
        single = self.conn.execute(
            """SELECT interconnect_status, inter_node_network FROM product
               WHERE original_product_name = 'gpu-b300-sxm 1gpu-24vcpu-346gb' AND region_code = 'uk-south1'""").fetchone()
        self.assertEqual(single, ("not_applicable", None))
        commitment = self.conn.execute(
            """SELECT c.disclosure_status, c.processing_status FROM collection_check c JOIN collection_target t USING (target_id)
               JOIN gpu_model m USING (gpu_model_id) WHERE t.contract_filter = 'commitment' AND m.model_name = 'B300'
                 AND c.check_scope = 'target'""").fetchone()
        self.assertEqual(commitment, ("contact_sales", "success"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM offer WHERE contract_type = 'commitment'").fetchone()[0], 0)
        # B200·H200은 문서 조각에 없어 대상 미발견
        not_found = self.conn.execute(
            """SELECT count(*) FROM collection_check WHERE run_id = %s AND check_scope = 'target'
                 AND processing_status = 'not_found'""", (summary["run_id"],)).fetchone()[0]
        self.assertEqual(not_found, 5)   # B200·H200 × (온디맨드, 스팟) + VR200 등재 확인
        vr200 = self.conn.execute(
            """SELECT c.reason FROM collection_check c JOIN collection_target t USING (target_id)
               JOIN gpu_model m USING (gpu_model_id) WHERE m.model_name = 'VR200' AND c.run_id = %s""",
            (summary["run_id"],)).fetchone()[0]
        self.assertIn("미공개로 단정하지 않음", vr200)

    def test_doc_failure_keeps_spot(self):
        self.doc = "<html>maintenance</html>"
        summary = self.collect()
        self.assertEqual(summary["status"], "partial_failure")
        self.assertEqual(self.prices("gpu-b300-sxm 8gpu-192vcpu-2768gb", "uk-south1", "spot"), [Decimal("6.95")])


class BackfillTests(NebiusDb):
    def test_spot_series_points_and_rerun_adds_only_new(self):
        summary = self.backfill(only={"spot"})
        self.assertEqual(summary["status"], "success")
        self.assertEqual(self.prices("gpu-b300-sxm 8gpu-192vcpu-2768gb", "uk-south1", "spot"),
                         [Decimal("5.0"), Decimal("5.2")])
        self.series = series([("2026-11-17T10:00:00Z", "5.0"), ("2026-11-17T11:00:00Z", "5.2"),
                              ("2026-11-17T12:00:00Z", "5.6")])
        self.backfill(only={"spot"})
        self.assertEqual(self.prices("gpu-b300-sxm 1gpu-24vcpu-346gb", "uk-south1", "spot"),
                         [Decimal("5.0"), Decimal("5.2"), Decimal("5.6")])

    def test_docs_capture_uses_applicable_period_and_verification(self):
        summary = self.backfill(only={"docs"})
        self.assertEqual(summary["status"], "success")
        self.assertEqual(self.prices("gpu-b300-sxm 8gpu-192vcpu-2768gb", "eu-west2", "on_demand"), [Decimal("7.85")])
        self.assertEqual(self.prices("gpu-b300-sxm 8gpu-192vcpu-2768gb", "eu-west2", "spot"), [Decimal("4.30")])
        verified = self.conn.execute(
            """SELECT DISTINCT se.primary_verified FROM source_evidence se JOIN collection_check c USING (check_id)
               JOIN offer o ON o.offer_id = c.offer_id WHERE se.evidence_role = 'aggregator'""").fetchall()
        self.assertEqual(verified, [(False,)])     # 지금 문서(9.50·시작가)와 다름
        self.assertEqual(self.backfill(only={"docs"})["groups"], [])

    def test_history_then_daily_collect_share_products(self):
        self.backfill()
        products = self.conn.execute("SELECT count(*) FROM product").fetchone()[0]
        self.collect()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM product").fetchone()[0], products)


if __name__ == "__main__":
    unittest.main()
