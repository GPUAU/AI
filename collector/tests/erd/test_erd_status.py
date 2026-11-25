"""랙스케일·출시 예정 모델 공개 상태 수기 기록(erd/status.py) 테스트."""
import unittest
import uuid
from pathlib import Path

import psycopg

from gpu_collector.erd import status
from erd.support import DDL, SEED, connect, create_database, drop_database



class StatusTests(unittest.TestCase):
    def setUp(self):
        self.name = create_database()
        self.conn = connect(self.name)
        self.conn.execute(DDL.read_text(encoding="utf8"))
        self.conn.execute(SEED.read_text(encoding="utf8"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        drop_database(self.name)

    def test_records_each_status_once_with_evidence(self):
        summary = status.record(self.conn)
        self.assertEqual((summary["added"], summary["skipped"]), (len(status.MANUAL), 0))
        rows = self.conn.execute(
            """SELECT pv.provider_name, m.model_name, c.disclosure_status, c.processing_status, se.evidence_role
               FROM collection_check c JOIN collection_target t USING (target_id)
               JOIN provider pv ON pv.provider_id = t.provider_id JOIN gpu_model m ON m.gpu_model_id = t.gpu_model_id
               JOIN source_evidence se ON se.check_id = c.check_id ORDER BY 1, 2""").fetchall()
        self.assertIn(("Nebius", "GB300", "contact_sales", "success", "official"), rows)
        self.assertIn(("Verda", "VR200", "not_disclosed", "success", "official"), rows)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM price_observation").fetchone()[0], 0)
        again = status.record(self.conn)
        self.assertEqual((again["added"], again["skipped"]), (0, len(status.MANUAL)))


if __name__ == "__main__":
    unittest.main()
