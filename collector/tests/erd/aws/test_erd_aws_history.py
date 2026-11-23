"""AWS 공식 과거 자료 적재 테스트. 임시 DB와 합성 가격 파일을 쓰고 네트워크·AWS 키를 쓰지 않는다."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from gpu_collector import fetch
from gpu_collector.erd.aws import history as aws_history
from erd.aws.documents import offer_document, savings_document
from erd.aws.test_erd_aws import ErdDb

HOST = "https://pricing.us-east-1.amazonaws.com"
# 적용 월 → EC2 버전. 4월 파일에는 B300이 없다.
EC2_VERSIONS = {"20260429000000": "2026-04", "20260529000000": "2026-05"}
SP_VERSIONS = ["20260410000000", "20260420000000", "20260520000000"]   # 4월은 20일 버전만 쓴다


def ec2_file(version):
    document = offer_document(b300_price="150", include_b300=version != "20260429000000")
    document.update(version=version, publicationDate=f"{version[:4]}-{version[4:6]}-{version[6:8]}T00:00:00Z")
    return document


def sp_file(version):
    document = savings_document()
    document.update(version=version, publicationDate=f"{version[:4]}-{version[4:6]}-{version[6:8]}T00:00:00Z")
    return document


class HistoryDb(ErdDb):
    def setUp(self):
        super().setUp()
        self.downloads = []
        self.spot_calls = []

    def backfill(self, since="2026-04", until="2026-05", only=None, ec2=ec2_file, spot_rows=None):
        def get_json(url, config):
            if url == aws_history.EC2_VERSIONS:
                return {"versions": {v: {"versionEffectiveBeginDate": f"{m}-01T00:00:00Z"}
                                     for v, m in EC2_VERSIONS.items()}}
            if url == aws_history.SP_VERSIONS:
                return {"versions": [{"publicationDate": f"{v[:4]}-{v[4:6]}-{v[6:8]}T00:00:00Z",
                                      "offerVersionUrl": f"/savingsPlan/v1.0/aws/AWSComputeSavingsPlan/{v}/region_index.json"}
                                     for v in SP_VERSIONS]}
            version = url.split("/")[-2]
            return {"regions": [{"regionCode": "us-east-1",
                                 "versionUrl": f"/savingsPlan/v1.0/aws/AWSComputeSavingsPlan/{version}/us-east-1/index.json"}]}

        def downloader(url, destination, config):
            version = url.split("/")[-3]
            self.downloads.append(url)
            body = json.dumps(ec2(version) if "AmazonEC2" in url else sp_file(version)).encode()
            Path(destination).write_bytes(body)
            return fetch.FetchResult(url=url, path=Path(destination), sha256="a" * 64, size=len(body))

        rows = spot_rows if spot_rows is not None else [
            ("use1-az2", "60", datetime(2026, 9, 1, tzinfo=timezone.utc)),
            ("use1-az2", "66", datetime(2026, 10, 1, tzinfo=timezone.utc))]

        def spot_history(region, instances, start, end, max_pages=None):
            self.spot_calls.append((start, end))
            return [{"instance_type": "p6-b300.48xlarge", "product_description": "Linux/UNIX",
                     "availability_zone": f"us-east-1{zone[-1]}", "availability_zone_id": zone,
                     "price": Decimal(price), "timestamp": at} for zone, price, at in rows]

        self.clock[0] += timedelta(days=1)
        return aws_history.backfill(self.conn, since=since, until=until, only=only, cache_dir=self.temp.name,
                                    now=self.now, get_json=get_json, downloader=downloader,
                                    spot_history=spot_history, workers=2, log=lambda message: None)

    def history(self, contract_product_type, region="us-east-1", model="B300"):
        return self.q("""SELECT ob.collected_at, n.effective_usd_gpu_hour, s.source_name
                         FROM price_observation ob JOIN price_normalization n ON n.observation_id = ob.observation_id
                         JOIN offer o ON o.offer_id = ob.offer_id JOIN product p ON p.product_id = o.product_id
                         JOIN gpu_model m ON m.gpu_model_id = p.gpu_model_id
                         JOIN collection_check c ON c.check_id = ob.check_id
                         JOIN collection_target t ON t.target_id = c.target_id
                         JOIN data_source s ON s.source_id = t.source_id
                         WHERE m.model_name = %s AND o.contract_product_type = %s AND p.region_code = %s
                           AND o.payment_option IS NULL ORDER BY ob.collected_at""", model, contract_product_type, region)


class BackfillTests(HistoryDb):
    def test_monthly_versions_use_source_time(self):
        summary = self.backfill(only={aws_history.SOURCE_EC2, aws_history.SOURCE_SPOT})
        self.assertEqual(summary["status"], "success", summary)
        # 4월 버전에는 B300이 없어 5월 버전부터 관측이 있다. 관측 시각은 버전 공개 시각이다
        self.assertEqual(self.history("On-Demand"),
                         [(datetime(2026, 5, 29, tzinfo=timezone.utc), Decimal(150) / 8, "aws_ec2_price_list_history")])
        absent = self.q("""SELECT c.processing_status, c.checked_at, c.reason FROM collection_check c
                           JOIN collection_target t ON t.target_id = c.target_id
                           JOIN data_source s ON s.source_id = t.source_id
                           JOIN gpu_model m ON m.gpu_model_id = t.gpu_model_id
                           WHERE c.check_scope = 'target' AND s.source_name = 'aws_ec2_price_list_history'
                             AND m.model_name = 'B300' AND t.contract_filter = 'on_demand'
                           ORDER BY c.checked_at""")
        self.assertEqual([row[0] for row in absent], ["not_found", "success"])
        self.assertEqual(absent[0][1], datetime(2026, 4, 29, tzinfo=timezone.utc))
        # 실제로 받은 시각은 근거에 남는다
        captured = self.q("SELECT min(captured_at) FROM source_evidence")[0][0]
        self.assertGreater(captured, datetime(2026, 11, 1, tzinfo=timezone.utc))
        # 받은 과거 파일은 지운다
        self.assertEqual(list(Path(self.temp.name).rglob("index.json")), [])

    def test_savings_plans_take_last_version_of_month(self):
        summary = self.backfill()
        self.assertEqual(summary["status"], "partial_failure")
        sp_files = sorted(url.split("/")[-3] for url in self.downloads if "savingsPlan" in url)
        self.assertEqual(sp_files, ["20260420000000", "20260520000000"])
        months = self.q("""SELECT DISTINCT e.source_version FROM source_evidence e
                           JOIN collection_check c ON c.check_id = e.check_id
                           JOIN collection_target t ON t.target_id = c.target_id
                           JOIN data_source s ON s.source_id = t.source_id
                           WHERE s.source_name = 'aws_savings_plans_price_list_history' ORDER BY 1""")
        self.assertEqual(months, [("20260520000000",)])
        # 4월 Savings Plans 파일에는 B300 요율이 있지만 EC2 상품이 아직 없어 사양을 이어받을 수 없다 → 검증 실패
        failed = self.q("""SELECT DISTINCT c.processing_status FROM collection_check c
                           JOIN collection_target t ON t.target_id = c.target_id
                           JOIN data_source s ON s.source_id = t.source_id
                           WHERE s.source_name = 'aws_savings_plans_price_list_history'
                             AND c.deduplication_key LIKE 'history-failed:%%'""")
        self.assertEqual(failed, [("validation_failed",)])
        loaded = self.q("""SELECT count(*) FROM price_observation ob JOIN offer o ON o.offer_id = ob.offer_id
                           WHERE o.contract_product_type = 'EC2 Instance Savings Plans'""")
        self.assertEqual(loaded, [(2,)])

    def test_rerun_skips_loaded_versions_and_spot_records(self):
        self.backfill(only={aws_history.SOURCE_EC2, aws_history.SOURCE_SPOT})
        observations = self.q("SELECT count(*) FROM price_observation")[0][0]
        downloads = len(self.downloads)
        second = self.backfill(only={aws_history.SOURCE_EC2, aws_history.SOURCE_SPOT})
        self.assertEqual(second["status"], "success")
        self.assertEqual(len(self.downloads), downloads, "적재한 버전은 다시 받지 않는다")
        self.assertEqual(self.q("SELECT count(*) FROM price_observation")[0][0], observations)
        self.assertEqual(self.q("SELECT count(*) FROM collection_alert"), [(0,)])

    def test_spot_records_keep_their_timestamps(self):
        self.backfill(only={aws_history.SOURCE_EC2, aws_history.SOURCE_SPOT})
        self.assertEqual(self.history("Spot", region="use1-az2"),
                         [(datetime(2026, 9, 1, tzinfo=timezone.utc), Decimal(60) / 8, "aws_ec2_spot_price_history"),
                          (datetime(2026, 10, 1, tzinfo=timezone.utc), Decimal(66) / 8, "aws_ec2_spot_price_history")])
        start, end = self.spot_calls[0]
        self.assertEqual(end - start, timedelta(days=90))
        # 스팟 상품은 그 시점 EC2 상품의 사양을 이어받는다
        self.assertEqual(self.q("SELECT gpu_count FROM product WHERE region_code = 'use1-az2'"), [(8,)])

    def test_regular_collection_ignores_history_targets_and_keeps_latest(self):
        self.backfill(only={aws_history.SOURCE_EC2})
        summary = self.collect()
        self.assertEqual(summary["status"], "success", summary)
        sources = {g["source"] for g in summary["groups"]}
        self.assertFalse(sources & set(aws_history.SOURCES))
        # 과거 자료 다음에 새 수집이 이어진다: 같은 오퍼, 같은 상품 구성
        self.assertEqual([row[1] for row in self.history("On-Demand")], [Decimal(150) / 8, Decimal("17.802")])
        self.assertEqual(self.q("""SELECT count(*) FROM product WHERE original_product_name = 'p6-b300.48xlarge'
                                   AND region_code = 'us-east-1'"""), [(1,)])

    def test_older_config_after_newer_data_is_rejected(self):
        self.collect()        # 현재 구성 먼저 적재

        def older(version):
            document = ec2_file(version)
            if "B300SKU" in document["products"]:
                document["products"]["B300SKU"]["attributes"]["gpuMemory"] = "2000 GB HBM3e"
            return document

        summary = self.backfill(only={aws_history.SOURCE_EC2}, ec2=older)
        self.assertEqual(summary["status"], "partial_failure")
        may = next(g for g in summary["groups"] if g["month"] == "2026-05")
        self.assertEqual(may["status"], "validation_failed")
        self.assertIn("시간 순서", may["reason"])
        self.assertEqual(self.q("""SELECT count(*) FROM product WHERE original_product_name = 'p6-b300.48xlarge'
                                   AND region_code = 'us-east-1'"""), [(1,)])
