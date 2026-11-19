"""AWS 사양 근거(모델 원문·노드 안 상호연결) 테스트."""
from erd.aws.test_erd_aws import ErdDb
from gpu_collector.erd.aws import collect as aws


class SpecTests(ErdDb):
    def setUp(self):
        super().setUp()
        self.set_docs(True)          # ErdDb는 리전 필터가 없는 대상을 끈다

    def set_docs(self, enabled):
        self.conn.execute("""UPDATE collection_target t SET enabled = %s FROM data_source s
                             WHERE s.source_id = t.source_id AND s.source_name = 'aws_ec2_instance_docs'""", (enabled,))
        self.conn.commit()

    def product(self, region="us-east-1"):
        return self.conn.execute(
            """SELECT original_model_name, interconnect_status, intra_node_interconnect, configuration_version
               FROM product WHERE original_product_name = 'p6-b300.48xlarge' AND region_code = %s
               ORDER BY configuration_version DESC LIMIT 1""", (region,)).fetchone()

    def test_collect_fills_model_name_and_confirms_interconnect(self):
        self.collect()
        model, status, interconnect, version = self.product()
        self.assertEqual((model, status, version), ("NVIDIA B300 GPU", "confirmed", 1))
        self.assertIn("1800 GB/s", interconnect)
        # 스팟(가용영역) 상품도 같은 근거를 갖는다
        spot = self.conn.execute(
            """SELECT original_model_name, interconnect_status FROM product
               WHERE original_product_name = 'p6-b300.48xlarge' AND region_code = 'use1-az2'""").fetchone()
        self.assertEqual(spot, ("NVIDIA B300 GPU", "confirmed"))
        attributes = {r[0] for r in self.conn.execute(
            """SELECT pe.attribute_name FROM product_evidence pe JOIN product p USING (product_id)
               WHERE p.original_product_name = 'p6-b300.48xlarge' AND p.region_code = 'us-east-1'""")}
        self.assertEqual(attributes, set(aws.SPEC_ATTRIBUTES) | {"intra_node_interconnect"})

    def test_attach_specs_fills_existing_products_without_new_version(self):
        self.set_docs(False)
        self.collect()                                   # 근거 대상이 꺼진 상태: 예전처럼 미확인
        self.conn.execute("UPDATE product SET original_model_name = NULL")
        self.assertEqual(self.product()[1], "unconfirmed")
        self.set_docs(True)
        summary = aws.attach_specs(self.conn, now=self.now)
        self.assertGreater(summary["model_name_filled"], 0)
        self.assertEqual(self.product()[:2], ("NVIDIA B300 GPU", "confirmed"))
        products = self.conn.execute("SELECT count(*) FROM product").fetchone()[0]
        self.collect()                                   # 다음 수집은 같은 구성으로 본다(새 버전 없음)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM product").fetchone()[0], products)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM collection_alert WHERE alert_type = 'config_changed'").fetchone()[0], 0)
        self.assertEqual(aws.attach_specs(self.conn, now=self.now)["products_linked"], 0)
