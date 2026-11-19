"""DDL 제약 테스트: 같은 상품·오퍼는 한 행만 둔다(동시 실행 중복 방지)."""
import unittest

import psycopg

from erd.support import DDL, SEED, connect, create_database, drop_database

PRODUCT = """INSERT INTO product (provider_id, gpu_model_id, original_product_name, region_code, system_type,
                                  service_option, interconnect_status, configuration_version)
             VALUES ((SELECT provider_id FROM provider WHERE provider_name = 'AWS'),
                     (SELECT gpu_model_id FROM gpu_model WHERE model_name = 'B300'),
                     'p6-b300.48xlarge', %s, 'instance', 'standard', 'unconfirmed', %s)
             RETURNING product_id"""
OFFER = """INSERT INTO offer (product_id, contract_type, contract_product_type, commitment_months, payment_option)
           VALUES (%s, %s, %s, %s, %s) RETURNING offer_id"""


class IdentityUniqueTests(unittest.TestCase):
    def setUp(self):
        self.name = create_database()
        self.conn = connect(self.name)
        self.conn.execute(DDL.read_text(encoding="utf8"))
        self.conn.execute(SEED.read_text(encoding="utf8"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        drop_database(self.name)

    def insert(self, sql, params):
        return self.conn.execute(sql, params).fetchone()[0]

    def test_same_product_rejected(self):
        self.insert(PRODUCT, ("us-east-1", 1))
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.insert(PRODUCT, ("us-east-1", 1))

    def test_same_product_with_null_region_rejected(self):
        self.insert(PRODUCT, (None, 1))
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.insert(PRODUCT, (None, 1))

    def test_new_configuration_version_allowed(self):
        self.insert(PRODUCT, ("us-east-1", 1))
        self.insert(PRODUCT, ("us-east-1", 2))

    def test_same_offer_rejected(self):
        product = self.insert(PRODUCT, ("us-east-1", 1))
        self.insert(OFFER, (product, "on_demand", None, None, None))
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.insert(OFFER, (product, "on_demand", None, None, None))

    def test_different_commitment_allowed(self):
        product = self.insert(PRODUCT, ("us-east-1", 1))
        self.insert(OFFER, (product, "commitment", "Reserved Instance", 12, "no_upfront"))
        self.insert(OFFER, (product, "commitment", "Reserved Instance", 36, "no_upfront"))


if __name__ == "__main__":
    unittest.main()
