"""원화 환산(krw_conversion) 테스트. Oracle 매일 수집으로 USD 환산 행을 만들고 환율을 직접 넣는다."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from gpu_collector.erd import __main__ as cli
from gpu_collector.erd.fx import collect as fx, convert as krw
from erd.oracle.test_erd_oracle import OracleDb, count

KST = timezone(timedelta(hours=9))


class KrwConversion(OracleDb):
    def observe(self, when):
        """when(KST)에 Oracle 매일 수집 1회 → 6개 모델 환산 행."""
        self.clock[0] = when.astimezone(timezone.utc) - timedelta(seconds=1)
        self.collect()

    def rate(self, day, value):
        source = fx.source_id(self.conn)
        fx.write_rates(self.conn, source, day, {"USD": Decimal(value), "EUR": Decimal("1500")}, self.now())
        self.conn.commit()

    def convert(self, **kwargs):
        return krw.convert(self.conn, now=self.now, **kwargs)

    def converted(self):
        return self.conn.execute(
            """SELECT (ob.collected_at AT TIME ZONE 'Asia/Seoul')::date::text, m.model_name, e.reference_date::text,
                      k.nominal_krw_gpu_hour, k.effective_krw_gpu_hour
               FROM krw_conversion k JOIN price_normalization n USING (normalization_id)
               JOIN exchange_rate e USING (exchange_rate_id)
               JOIN price_observation ob ON ob.observation_id = n.observation_id
               JOIN offer o ON o.offer_id = ob.offer_id JOIN product p ON p.product_id = o.product_id
               JOIN gpu_model m ON m.gpu_model_id = p.gpu_model_id ORDER BY 1, 2""").fetchall()

    def test_business_day_uses_same_day_usd_rate(self):
        self.observe(datetime(2026, 11, 20, 12, 0, tzinfo=KST))        # 금
        self.rate(date(2026, 11, 20), "1330.8")
        summary = self.convert()
        self.assertEqual((summary["status"], summary["converted"], summary["pending"]), ("success", 6, 0))
        b300 = [r for r in self.converted() if r[1] == "B300"][0]
        self.assertEqual(b300, ("2026-11-20", "B300", "2026-11-20", Decimal("19962.0"), Decimal("19962.0")))

    def test_observation_before_rate_is_published_waits_then_uses_that_day(self):
        self.rate(date(2026, 11, 19), "1331.1")
        self.observe(datetime(2026, 11, 20, 3, 0, tzinfo=KST))         # 고시 전 관측
        self.assertEqual(self.convert()["pending"], 6)                  # 11-20 환율 미확정 → 보류(어제 환율로 바꾸지 않음)
        self.assertEqual(self.converted(), [])
        self.rate(date(2026, 11, 20), "1330.8")
        self.assertEqual(self.convert()["converted"], 6)
        self.assertEqual({r[2] for r in self.converted()}, {"2026-11-20"})

    def test_weekend_waits_for_next_business_day_then_uses_friday(self):
        self.rate(date(2026, 11, 20), "1330.8")                          # 금
        self.observe(datetime(2026, 11, 21, 12, 0, tzinfo=KST))         # 토
        self.assertEqual(self.convert()["pending"], 6)
        self.rate(date(2026, 11, 23), "1340")                            # 월 적재 → 토요일 확정
        self.assertEqual(self.convert()["converted"], 6)
        self.assertEqual({r[2] for r in self.converted()}, {"2026-11-20"})

    def test_rerun_adds_nothing(self):
        self.observe(datetime(2026, 11, 20, 12, 0, tzinfo=KST))
        self.rate(date(2026, 11, 20), "1330.8")
        self.convert()
        self.assertEqual(self.convert()["converted"], 0)
        self.assertEqual(count(self.conn, "SELECT count(*) FROM krw_conversion"), 6)

    def test_observation_before_first_rate_fails_and_alerts(self):
        self.observe(datetime(2026, 11, 18, 12, 0, tzinfo=KST))
        self.rate(date(2026, 11, 20), "1330.8")
        summary = self.convert()
        self.assertEqual((summary["status"], summary["no_rate"]), ("partial_failure", 6))
        self.assertEqual(count(self.conn, "SELECT count(*) FROM collection_alert WHERE alert_type = 'fx_failed'"), 1)

    def test_long_holiday_alerts_stale(self):
        self.rate(date(2026, 11, 13), "1320")
        self.rate(date(2026, 11, 20), "1330.8")
        self.observe(datetime(2026, 11, 19, 12, 0, tzinfo=KST))         # 11-16~19 환율 없음 가정 → 11-13(6일 전)
        summary = self.convert()
        self.assertEqual((summary["converted"], summary["stale_alerts"]), (6, 1))

    def test_since_limits_observation_dates(self):
        self.rate(date(2026, 11, 19), "1331.1")
        self.rate(date(2026, 11, 20), "1330.8")
        self.observe(datetime(2026, 11, 19, 12, 0, tzinfo=KST))
        self.observe(datetime(2026, 11, 20, 12, 0, tzinfo=KST))
        self.assertEqual(self.convert(since="2026-11-20")["converted"], 6)
        self.assertEqual({r[0] for r in self.converted()}, {"2026-11-20"})

    def test_cli_parse_and_lock(self):
        args = cli.parse(["convert", "krw", "--since", "2023-07-01"])
        self.assertEqual((args.target, args.since, cli.lock_name(args)), ("krw", "2023-07-01", "krw"))

    def test_long_usd_values_are_multiplied_exactly(self):
        self.observe(datetime(2026, 11, 20, 12, 0, tzinfo=KST))
        self.rate(date(2026, 11, 20), "1293")
        long_value = Decimal("4.995024866818873668188736682")          # 월 요금 ÷ 730 같은 28자리 값
        self.conn.execute("ALTER TABLE price_normalization DISABLE TRIGGER USER")
        self.conn.execute("UPDATE price_normalization SET nominal_usd_gpu_hour = %s, effective_usd_gpu_hour = %s",
                          (long_value, long_value))
        self.conn.execute("ALTER TABLE price_normalization ENABLE TRIGGER USER")
        self.conn.commit()
        self.convert()
        stored = self.conn.execute("SELECT DISTINCT effective_krw_gpu_hour FROM krw_conversion").fetchall()
        self.assertEqual(stored, [(Decimal("6458.567152796803652968036529826"),)])
