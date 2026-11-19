"""AWS 수집기 → ERD 초안 스키마 적재 테스트.

TEST_DATABASE_URL 서버에 임시 DB를 만들어 패키지의 sql/ddl.sql과 sql/seed.sql을 적용하고, 끝나면 지운다.
가격 파일은 erd/aws/documents.py의 합성 문서를 쓰고 스팟 API는 가짜 함수로 바꾼다. 네트워크와 AWS 키를 쓰지 않는다.
"""
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg

from gpu_collector import fetch
from gpu_collector.erd.aws import collect as aws, spot as aws_spot
from erd.support import DDL, SEED, connect, create_database, drop_database
from erd.aws.documents import offer_document, savings_document

T0 = datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc)
SPOT_TIME = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)


def spot_rows(prices):
    """prices: {가용영역 ID: 가격 문자열}. B300만 돌려준다."""
    return [{"instance_type": "p6-b300.48xlarge", "product_description": "Linux/UNIX",
             "availability_zone": f"us-east-1{zone[-1]}", "availability_zone_id": zone,
             "price": Decimal(price), "timestamp": SPOT_TIME} for zone, price in prices.items()]


class ErdDb(unittest.TestCase):
    FILE_SOURCES = True     # True: 가격 파일 출처로 수집, False: 가격 조회 API 출처로 수집

    def setUp(self):
        self.name = create_database()
        self.conn = connect(self.name)
        self.conn.execute(DDL.read_text(encoding="utf8"))
        self.conn.execute(SEED.read_text(encoding="utf8"))
        # 테스트 문서는 us-east-1만 담는다. API 출처와 가격 파일 출처 중 한쪽만 켠다(FILE_SOURCES)
        self.conn.execute(
            """UPDATE collection_target t SET enabled = (coalesce(t.region_filter = 'us-east-1', false)
                   AND (s.source_name NOT IN ('aws_ec2_price_list', 'aws_savings_plans_price_list',
                                              'aws_ec2_pricing_api', 'aws_savings_plans_api')
                        OR (s.source_name IN ('aws_ec2_price_list', 'aws_savings_plans_price_list')) = %s))
               FROM data_source s WHERE s.source_id = t.source_id""", (self.FILE_SOURCES,))
        self.conn.commit()
        self.temp = tempfile.TemporaryDirectory()
        self.clock = [T0]

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()
        drop_database(self.name)

    def now(self):
        self.clock[0] += timedelta(seconds=1)
        return self.clock[0]

    def collect(self, ec2=None, savings=None, spot=None, version="20261116000000", only=None, spot_error=None):
        ec2 = offer_document() if ec2 is None else ec2
        savings = savings_document() if savings is None else savings
        spot = {"use1-az2": "69.1523", "use1-az6": "73.6831"} if spot is None else spot

        def get_json(url, config):
            if "AmazonEC2" in url:
                return {"regions": {"us-east-1": {
                    "currentVersionUrl": f"/offers/v1.0/aws/AmazonEC2/{version}/us-east-1/index.json"}}}
            return {"regions": [{"regionCode": "us-east-1",
                                 "versionUrl": f"/savingsPlan/v1.0/aws/AWSComputeSavingsPlan/{version}/us-east-1/index.json"}]}

        def downloader(url, destination, config):
            body = json.dumps(ec2 if "AmazonEC2" in url else savings).encode()
            Path(destination).write_bytes(body)
            return fetch.FetchResult(url=url, path=Path(destination), sha256="a" * 64, size=len(body))

        def spot_prices(region, instances, now=None):
            if spot_error:
                raise aws_spot.SpotApiError(spot_error)
            return spot_rows(spot)

        self.clock[0] += timedelta(days=1)
        return aws.collect(self.conn, only=only, cache_dir=self.temp.name, now=self.now, get_json=get_json,
                           downloader=downloader, spot_prices=spot_prices)

    def q(self, sql, *params):
        return self.conn.execute(sql, params).fetchall()

    def target_check(self, run_id, model, contract):
        return self.q("""SELECT c.disclosure_status, c.processing_status, c.scope_completed
                         FROM collection_check c JOIN collection_target t ON t.target_id = c.target_id
                         JOIN gpu_model m ON m.gpu_model_id = t.gpu_model_id
                         WHERE c.run_id = %s AND c.check_scope = 'target' AND m.model_name = %s
                           AND t.contract_filter = %s""", run_id, model, contract)

    def normalized(self, model, contract_product_type, payment=None, months=None, region="us-east-1"):
        return self.q("""SELECT n.nominal_usd_gpu_hour, n.effective_usd_gpu_hour, n.result_status
                         FROM price_normalization n JOIN price_observation ob ON ob.observation_id = n.observation_id
                         JOIN offer o ON o.offer_id = ob.offer_id JOIN product p ON p.product_id = o.product_id
                         JOIN gpu_model m ON m.gpu_model_id = p.gpu_model_id
                         WHERE m.model_name = %s AND o.contract_product_type = %s AND p.region_code = %s
                           AND o.payment_option IS NOT DISTINCT FROM %s AND o.commitment_months IS NOT DISTINCT FROM %s
                         ORDER BY ob.observation_id""", model, contract_product_type, region, payment, months)


class FullRunTests(ErdDb):
    def test_loads_all_contracts_and_records_missing_ones(self):
        summary = self.collect()
        self.assertEqual(summary["status"], "success", summary)
        run_id = summary["run_id"]

        # 온디맨드: 142.416 / 8
        self.assertEqual(self.normalized("B300", "On-Demand"), [(Decimal("17.802"), Decimal("17.802"), "success")])
        # Savings Plans: 무선납은 명목=실효, 부분 선납은 선납 반영 요율이라 명목 없음
        self.assertEqual(self.normalized("B300", "EC2 Instance Savings Plans", "no_upfront", 36),
                         [(Decimal("61.5237") / 8, Decimal("61.5237") / 8, "success")])
        self.assertEqual(self.normalized("B300", "EC2 Instance Savings Plans", "partial_upfront", 36),
                         [(None, Decimal("56.9664") / 8, "success")])
        # 스팟: 가용영역마다 상품이 따로 있다
        self.assertEqual(self.normalized("B300", "Spot", region="use1-az2"),
                         [(Decimal("69.1523") / 8, Decimal("69.1523") / 8, "success")])
        self.assertEqual(len(self.normalized("B300", "Spot", region="use1-az6")), 1)
        # H100 Reserved: 부분 선납 3년, 전액 선납 1년
        partial = self.normalized("H100", "Reserved Instances (standard)", "partial_upfront", 36)[0]
        hours = Decimal(36 * 730)
        self.assertEqual(partial[0], Decimal("11.008") / 8)
        self.assertEqual(partial[1], (Decimal("289290.24") / hours + Decimal("11.008")) / 8)
        all_upfront = self.normalized("H100", "Reserved Instances (convertible)", "all_upfront", 12)[0]
        self.assertEqual(all_upfront, (None, Decimal(350400) / Decimal(12 * 730) / 8, "success"))

        # B300 Reserved Instances는 원천에 없다 → 범위 확인 완료 + 미발견
        self.assertEqual(self.target_check(run_id, "B300", "reserved_instances"), [("unknown", "not_found", True)])
        self.assertEqual(self.target_check(run_id, "B300", "on_demand"), [("public", "success", True)])
        self.assertEqual(self.target_check(run_id, "B200", "spot"), [("unknown", "not_found", True)])
        self.assertEqual(self.target_check(run_id, "GB300", "on_demand"), [("unknown", "not_found", True)])

    def test_product_specs_from_price_file(self):
        self.collect()
        rows = self.q("""SELECT sku, gpu_count, vram_gb_per_gpu, vcpu_count, ram_gb, country_code, interconnect_status,
                                configuration_version
                         FROM product WHERE original_product_name = 'p6-b300.48xlarge' AND region_code = 'us-east-1'""")
        self.assertEqual(rows, [("B300SKU", 8, Decimal(268), Decimal(192), Decimal(4096) * Decimal("1.073741824"),
                                 "US", "unconfirmed", 1)])
        spot = self.q("""SELECT sku, gpu_count, region_code FROM product
                         WHERE original_product_name = 'p6-b300.48xlarge' AND region_code LIKE 'use1-%%'
                         ORDER BY region_code""")
        self.assertEqual(spot, [("B300SKU", 8, "use1-az2"), ("B300SKU", 8, "use1-az6")])

    def test_evidence_and_observation_links(self):
        self.collect()
        # 오퍼 점검마다 근거·관측이 하나씩 있다
        orphan = self.q("""SELECT c.check_id FROM collection_check c
                           LEFT JOIN price_observation o ON o.check_id = c.check_id
                           LEFT JOIN source_evidence e ON e.check_id = c.check_id
                           WHERE c.check_scope = 'offer' AND c.processing_status = 'success'
                             AND (o.observation_id IS NULL OR e.evidence_id IS NULL)""")
        self.assertEqual(orphan, [])
        # 원본 버전은 URL이 아니라 파일 안의 version 값이다
        versions = {r[0] for r in self.q("SELECT source_version FROM source_evidence WHERE source_version IS NOT NULL")}
        self.assertEqual(versions, {"20261107002852", "20261107012235", aws_spot.API_VERSION})


class RepeatRunTests(ErdDb):
    def test_same_prices_append_without_new_products(self):
        self.collect()
        products = self.q("SELECT count(*) FROM product")[0][0]
        observations = self.q("SELECT count(*) FROM price_observation")[0][0]
        second = self.collect(version="20261117000000")
        self.assertEqual(second["status"], "success")
        self.assertEqual(self.q("SELECT count(*) FROM product")[0][0], products)
        self.assertEqual(self.q("SELECT count(*) FROM price_observation")[0][0], observations * 2)
        self.assertEqual(self.q("SELECT count(*) FROM collection_alert"), [(0,)])

    def test_missing_offer_and_price_spike(self):
        self.collect()
        second = self.collect(spot={"use1-az2": "103.7285"}, only={aws.SOURCE_SPOT})  # +50%, az6 사라짐
        self.assertEqual(second["status"], "success")
        missing = self.q("""SELECT p.region_code, c.processing_status, c.disclosure_status FROM collection_check c
                            JOIN product p ON p.product_id = c.product_id
                            WHERE c.run_id = %s AND c.check_scope = 'offer' AND c.processing_status = 'not_found'""",
                         second["run_id"])
        self.assertEqual(missing, [("use1-az6", "not_found", "unknown")])
        alerts = self.q("SELECT alert_type, reason FROM collection_alert")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][0], "price_spike")
        self.assertIn("+50.0%", alerts[0][1])

    def test_config_change_makes_new_version(self):
        self.collect()
        changed = offer_document()
        changed["products"]["B300SKU"]["attributes"]["gpuMemory"] = "2304 GB HBM3e"
        second = self.collect(ec2=changed, version="20261117000000")
        self.assertEqual(second["status"], "success")
        versions = self.q("""SELECT configuration_version, vram_gb_per_gpu FROM product
                             WHERE original_product_name = 'p6-b300.48xlarge' AND region_code = 'us-east-1'
                             ORDER BY configuration_version""")
        self.assertEqual(versions, [(1, Decimal(268)), (2, Decimal(288))])
        # EC2 상품과, 그 사양을 이어받는 스팟 가용영역 상품 2개가 함께 새 구성이 된다
        alerts = self.q("""SELECT a.alert_type, p.region_code FROM collection_alert a
                           JOIN collection_check c ON c.check_id = a.check_id
                           JOIN product p ON p.product_id = c.product_id""")
        # DB 정렬 규칙(collation)에 따라 ORDER BY 순서가 달라지므로 파이썬에서 정렬해 비교한다
        self.assertEqual(sorted(alerts), [("config_changed", "us-east-1"), ("config_changed", "use1-az2"),
                                          ("config_changed", "use1-az6")])
        # 옛 구성의 오퍼는 미발견으로 남기지 않는다 (새 구성으로 이어짐)
        missing = self.q("""SELECT count(*) FROM collection_check WHERE run_id = %s AND check_scope = 'offer'
                            AND processing_status = 'not_found'""", second["run_id"])
        self.assertEqual(missing, [(0,)])
        # Savings Plans는 새 구성 상품에 붙는다
        sp_versions = self.q("""SELECT DISTINCT p.configuration_version FROM offer o
                                JOIN product p ON p.product_id = o.product_id
                                JOIN price_observation ob ON ob.offer_id = o.offer_id
                                JOIN collection_check c ON c.check_id = ob.check_id
                                WHERE o.contract_product_type LIKE '%%Savings Plans' AND c.run_id = %s""",
                             second["run_id"])
        self.assertEqual(sp_versions, [(2,)])


class FailureTests(ErdDb):
    def test_spot_failure_keeps_other_sources(self):
        summary = self.collect(spot_error="http_401: AuthFailure")
        self.assertEqual(summary["status"], "partial_failure")
        failed = self.q("""SELECT DISTINCT c.disclosure_status, c.processing_status, c.scope_completed
                           FROM collection_check c JOIN collection_target t ON t.target_id = c.target_id
                           WHERE c.run_id = %s AND t.contract_filter = 'spot'""", summary["run_id"])
        self.assertEqual(failed, [("unknown", "collection_failed", False)])
        self.assertEqual(self.target_check(summary["run_id"], "B300", "on_demand"), [("public", "success", True)])
        self.assertEqual([r[0] for r in self.q("SELECT alert_type FROM collection_alert")], ["collection_failed"])
        run = self.q("SELECT run_status, failure_reason FROM collection_run WHERE run_id = %s", summary["run_id"])[0]
        self.assertEqual(run[0], "partial_failure")
        self.assertIn("AuthFailure", run[1])

    def test_savings_without_ec2_product_is_validation_failure(self):
        summary = self.collect(only={aws.SOURCE_SP})
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(self.target_check(summary["run_id"], "B300", "savings_plans"),
                         [("unknown", "validation_failed", False)])


class NormalizeTests(unittest.TestCase):
    def test_unknown_gpu_count_is_not_computable(self):
        result = aws.normalize("spot", {}, [{"component_type": "hourly_rate", "amount": Decimal(10)}], [1], None,
                               Decimal(730))
        self.assertEqual((result["status"], result["reason"]), ("not_computable", "GPU 수 미확인"))


if __name__ == "__main__":
    unittest.main()
