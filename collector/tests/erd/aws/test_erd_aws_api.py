"""가격 조회 API 출처(GetProducts·Savings Plans 요율 API)로 매일 수집 테스트. API는 가짜 객체로 바꾼다."""
import copy
import unittest
from datetime import timedelta
from decimal import Decimal

from gpu_collector.erd.aws import collect as aws, api as aws_api
from erd.aws.documents import offer_document
from erd.aws.test_erd_aws import ErdDb

PUBLISHED = "2026-11-14T01:29:26Z"


class FakePricingApi:
    """offer_document()를 GetProducts 응답처럼, 아래 요율을 Savings Plans 요율 API 응답처럼 돌려준다."""

    def __init__(self, document=None, rates=None, error=None):
        self.document = document or offer_document()
        self.rates = rates if rates is not None else [
            ("EC2Instance", 3, "No Upfront", "BoxUsage:p6-b300.48xlarge", "61.52371"),
            ("EC2Instance", 3, "No Upfront", "UnusedBox:p6-b300.48xlarge", "61.52371"),
            ("EC2Instance", 3, "Partial Upfront", "BoxUsage:p6-b300.48xlarge", "56.9664"),
            ("Compute", 1, "No Upfront", "BoxUsage:p6-b300.48xlarge", "112.1526"),
        ]
        self.error = error
        self.calls = []

    def get_products(self, filters):
        self.calls.append(filters)
        if self.error:
            raise aws_api.AwsApiError(self.error)
        documents = []
        for sku, product in sorted(self.document["products"].items()):
            attributes = product["attributes"]
            if all((attributes.get(f["Field"]) == f["Value"]) if f["Type"] == "TERM_MATCH"
                   else f["Value"] in attributes.get(f["Field"], "") for f in filters):
                terms = {section: self.document["terms"][section][sku]
                         for section in ("OnDemand", "Reserved") if sku in self.document["terms"][section]}
                documents.append({"product": {**copy.deepcopy(product), "sku": sku}, "terms": terms,
                                  "version": "20261114012926", "publicationDate": PUBLISHED})
        return documents

    def savings_rates(self, region, instances):
        if self.error:
            raise aws_api.AwsApiError(self.error)
        return [{"operation": "RunInstances", "productType": "EC2", "rate": rate, "unit": "Hrs", "usageType": usage,
                 "savingsPlanOffering": {"currency": "USD", "durationSeconds": years * 31536000, "paymentOption": payment,
                                         "offeringId": f"{plan}-{years}-{payment}".replace(" ", ""), "planType": plan}}
                for plan, years, payment, usage, rate in self.rates if usage.split(":")[1] in instances]


class ApiCollectionTests(ErdDb):
    FILE_SOURCES = False

    def collect_api(self, api=None, spot=None):
        self.api = api or FakePricingApi()
        self.clock[0] += timedelta(days=1)
        rows = spot if spot is not None else {"use1-az2": "69.1523"}
        from erd.aws.test_erd_aws import spot_rows
        return aws.collect(self.conn, cache_dir=self.temp.name, now=self.now, pricing_api=self.api,
                           get_json=self.fail, downloader=self.fail,
                           spot_prices=lambda region, instances, now=None: spot_rows(rows))

    @staticmethod
    def fail(*args, **kwargs):
        raise AssertionError("API 수집에서 가격 파일을 받으면 안 된다")

    def test_daily_collection_uses_api_only(self):
        summary = self.collect_api()
        self.assertEqual(summary["status"], "success", summary)
        self.assertEqual({g["source"] for g in summary["groups"]},
                         {aws.SOURCE_EC2_API, aws.SOURCE_SP_API, aws.SOURCE_SPOT})
        self.assertEqual(self.normalized("B300", "On-Demand"), [(Decimal("17.802"), Decimal("17.802"), "success")])
        self.assertEqual(self.normalized("B300", "EC2 Instance Savings Plans", "no_upfront", 36),
                         [(Decimal("61.52371") / 8, Decimal("61.52371") / 8, "success")])
        self.assertEqual(self.normalized("B300", "EC2 Instance Savings Plans", "partial_upfront", 36),
                         [(None, Decimal("56.9664") / 8, "success")])
        self.assertEqual(len(self.normalized("B300", "Compute Savings Plans", "no_upfront", 12)), 1)
        partial = self.normalized("H100", "Reserved Instances (standard)", "partial_upfront", 36)[0]
        self.assertEqual(partial[0], Decimal("11.008") / 8)
        # 사용하지 않은 예약(UnusedBox) 요율은 넣지 않는다
        self.assertEqual(self.q("""SELECT count(*) FROM offer WHERE contract_product_type LIKE '%%Savings Plans'"""),
                         [(3,)])

    def test_evidence_versions(self):
        self.collect_api()
        rows = self.q("""SELECT s.source_name, e.source_version FROM source_evidence e
                         JOIN collection_check c ON c.check_id = e.check_id
                         JOIN collection_target t ON t.target_id = c.target_id
                         JOIN data_source s ON s.source_id = t.source_id
                         WHERE c.check_scope = 'offer' GROUP BY 1, 2 ORDER BY 1""")
        # GetProducts는 가격표 버전을 준다. Savings Plans 요율 API는 버전·적용일이 없어 미확인(NULL)
        self.assertEqual(rows, [("aws_ec2_pricing_api", "20261114012926"), ("aws_ec2_spot_api", "2016-11-15"),
                                ("aws_savings_plans_api", None)])
        effective = self.q("""SELECT DISTINCT ob.effective_from FROM price_observation ob JOIN offer o USING (offer_id)
                              WHERE o.contract_product_type LIKE '%%Savings Plans'""")
        self.assertEqual(effective, [(None,)])

    def test_gb300_detection_uses_contains_filter(self):
        self.collect_api()
        contains = [f for call in self.api.calls for f in call if f["Type"] == "CONTAINS"]
        self.assertEqual(contains, [{"Type": "CONTAINS", "Field": "instanceType", "Value": "gb300"}])
        self.assertEqual(self.target_check(self.q("SELECT max(run_id) FROM collection_run")[0][0], "GB300", "on_demand"),
                         [("unknown", "not_found", True)])

    def test_api_permission_error_is_collection_failure(self):
        summary = self.collect_api(api=FakePricingApi(error="http_400: AccessDeniedException"))
        self.assertEqual(summary["status"], "partial_failure")     # 스팟은 성공
        failed = self.q("""SELECT DISTINCT c.processing_status, c.disclosure_status FROM collection_check c
                           JOIN collection_target t ON t.target_id = c.target_id
                           JOIN data_source s ON s.source_id = t.source_id
                           WHERE s.source_name = 'aws_ec2_pricing_api'""")
        self.assertEqual(failed, [("collection_failed", "unknown")])


if __name__ == "__main__":
    unittest.main()
