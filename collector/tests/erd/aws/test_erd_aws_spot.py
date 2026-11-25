"""AWS 매일 스팟 수집: 마지막으로 적재한 기록(워터마크) 이후 변경 기록을 모두 받고, 같은 기록은 한 번만 적재한다."""
from datetime import timedelta
from decimal import Decimal

from gpu_collector.erd.aws import collect as aws
from erd.aws.test_erd_aws_history import HistoryDb

ONLY_SPOT = {aws.SOURCE_SPOT}


def row(zone, price, at):
    return {"instance_type": "p6-b300.48xlarge", "product_description": "Linux/UNIX",
            "availability_zone": f"us-east-1{zone[-1]}", "availability_zone_id": zone, "price": Decimal(price),
            "timestamp": at}


class SpotSeriesTests(HistoryDb):
    def daily(self, rows_for):
        """rows_for(start, end) → 원천이 돌려줄 기록."""
        self.clock[0] += timedelta(days=1)

        def spot_history(region, instances, start, end, max_pages=None):
            self.spot_calls.append((start, end))
            return rows_for(start, end)

        return aws.collect(self.conn, only=ONLY_SPOT, cache_dir=self.temp.name, now=self.now,
                           spot_history=spot_history)

    def spot_points(self, zone="use1-az2"):
        return self.q("""SELECT ob.collected_at, ob.effective_from, pc.amount
                         FROM price_observation ob JOIN offer o USING (offer_id) JOIN product p USING (product_id)
                         JOIN price_component pc ON pc.observation_id = ob.observation_id
                         WHERE o.contract_type = 'spot' AND p.region_code = %s ORDER BY ob.collected_at""", zone)

    def test_first_run_reads_price_in_effect_now(self):
        self.collect(only=ONLY_SPOT)
        start, end = self.spot_calls[-1]
        self.assertEqual(start, end)

    def test_next_run_starts_at_last_loaded_record(self):
        self.collect(only=ONLY_SPOT)
        first_record = self.spot_calls[-1][1] - timedelta(hours=1)      # 가짜 원천: 수집 1시간 전 기록
        self.collect(only=ONLY_SPOT)
        self.assertEqual(self.spot_calls[-1][0], first_record)

    def test_all_changes_since_watermark_loaded_once(self):
        t1 = {}

        def first(start, end):
            t1["at"] = end - timedelta(hours=3)
            return [row("use1-az2", "60", t1["at"])]

        self.daily(first)

        def second(start, end):
            # AWS는 시작 시각에 적용 중이던 기록(이미 적재함)과 그 뒤 변경 기록을 함께 준다
            self.assertEqual(start, t1["at"])
            return [row("use1-az2", "60", t1["at"]), row("use1-az2", "62", t1["at"] + timedelta(hours=6)),
                    row("use1-az2", "61", t1["at"] + timedelta(hours=12))]

        summary = self.daily(second)
        self.assertEqual(summary["status"], "success")
        points = self.spot_points()
        self.assertEqual([p[2] for p in points], [Decimal(60), Decimal(62), Decimal(61)])
        # 관측 시각은 기록 시각이다
        self.assertTrue(all(collected == effective for collected, effective, _ in points))

    def test_daily_and_backfill_share_records(self):
        loaded = self.clock[0] + timedelta(hours=1)
        self.backfill(only={"aws_ec2_spot_price_history"}, spot_rows=[("use1-az2", "60", loaded)])

        def daily(start, end):
            self.assertEqual(start, loaded)                       # 백필이 적재한 기록이 워터마크
            return [row("use1-az2", "60", loaded), row("use1-az2", "63", end - timedelta(hours=2))]

        self.daily(daily)
        self.assertEqual([p[2] for p in self.spot_points()], [Decimal(60), Decimal(63)])

    def test_same_day_rerun_skips(self):
        self.daily(lambda start, end: [row("use1-az2", "60", end - timedelta(hours=1))])
        calls = len(self.spot_calls)
        self.clock[0] += timedelta(hours=1)
        summary = aws.collect(self.conn, only=ONLY_SPOT, cache_dir=self.temp.name, now=self.now,
                              spot_history=lambda *a, **k: self.fail("같은 날 다시 호출하면 안 됨"))
        self.assertEqual([g["status"] for g in summary["groups"]], ["skipped"])
        self.assertEqual(len(self.spot_calls), calls)

    def test_backfill_after_daily_does_not_duplicate(self):
        at = {}

        def daily(start, end):
            at["t"] = end - timedelta(hours=1)
            return [row("use1-az2", "60", at["t"])]

        self.daily(daily)
        self.backfill(only={"aws_ec2_spot_price_history"},
                      spot_rows=[("use1-az2", "60", at["t"]), ("use1-az2", "58", at["t"] - timedelta(days=2))])
        self.assertEqual([p[2] for p in self.spot_points()], [Decimal(58), Decimal(60)])

    def test_hourly_rerun_adds_new_records_only(self):
        at = {}

        def first(start, end):
            at["t"] = end - timedelta(hours=1)
            return [row("use1-az2", "60", at["t"])]

        self.daily(first)
        self.clock[0] += timedelta(hours=1)

        def hourly(region, instances, start, end, max_pages=None):
            self.spot_calls.append((start, end))
            return [row("use1-az2", "60", at["t"]), row("use1-az2", "64", end - timedelta(minutes=10))]

        summary = aws.collect(self.conn, only=ONLY_SPOT, cache_dir=self.temp.name, now=self.now,
                              spot_history=hourly, hourly=True)
        (group,) = summary["groups"]
        self.assertEqual(group["status"], "success")
        self.assertTrue(group["slot"].startswith("hourly:"))
        self.assertEqual([p[2] for p in self.spot_points()], [Decimal(60), Decimal(64)])
        missing = self.q("SELECT count(*) FROM collection_check WHERE run_id = %s AND processing_status = 'not_found' "
                         "AND check_scope = 'offer'",
                         summary["run_id"])[0][0]
        self.assertEqual(missing, 0)
        again = aws.collect(self.conn, only=ONLY_SPOT, cache_dir=self.temp.name, now=self.now,
                            spot_history=lambda *a, **k: self.fail("같은 시간대에 다시 호출하면 안 됨"), hourly=True)
        self.assertEqual([g["status"] for g in again["groups"]], ["skipped"])
