"""실행 안전장치 테스트: 수집 기준일 키·건너뛰기, 사업자 잠금, 중단된 실행 정리, SIGTERM, HTTP 재시도, 종료 코드."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests
from psycopg.conninfo import make_conninfo

from gpu_collector import fetch
from gpu_collector.erd import __main__ as cli, runs
from gpu_collector.erd.verda import collect as verda
from erd.support import DDL, SEED, admin_url, connect, create_database, drop_database
from erd.verda.test_erd_verda import VerdaDb


def count(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


OBSERVATIONS = "SELECT count(*) FROM price_observation"
COMMITMENTS = """SELECT count(*) FROM price_observation ob JOIN offer o USING (offer_id)
                 WHERE o.contract_type = 'commitment'"""


class SameDayRerunTests(VerdaDb):
    def run_collect(self, date=None, get_json=None):
        return verda.collect(self.conn, now=self.now, date=date, get_json=get_json or self.get_json,
                             pause=lambda _s: None)

    def test_same_day_rerun_skips_loaded_groups(self):
        first = self.run_collect()
        loaded = count(self.conn, OBSERVATIONS)
        second = self.run_collect()
        self.assertEqual(first["date"], "2026-11-18")
        self.assertEqual(second["status"], "success")
        self.assertEqual({g["status"] for g in second["groups"]}, {"skipped"})
        self.assertEqual(count(self.conn, OBSERVATIONS), loaded)

    def test_next_day_collects_again(self):
        self.run_collect()
        loaded = count(self.conn, OBSERVATIONS)
        self.clock[0] += timedelta(days=1)
        self.run_collect()
        self.assertEqual(count(self.conn, OBSERVATIONS), loaded * 2)

    def test_explicit_date_is_the_dedup_unit(self):
        self.run_collect(date="2026-11-20")
        loaded = count(self.conn, OBSERVATIONS)
        self.assertEqual({g["status"] for g in self.run_collect(date="2026-11-20")["groups"]}, {"skipped"})
        self.run_collect(date="2026-11-21")
        self.assertEqual(count(self.conn, OBSERVATIONS), loaded * 2)

    def test_kst_date_boundary(self):
        # 2026-11-18 15:30 UTC = 2026-11-19 00:30 KST
        self.clock[0] = datetime(2026, 11, 18, 15, 30, tzinfo=timezone.utc)
        self.assertEqual(self.run_collect()["date"], "2026-11-19")

    def test_retry_loads_only_failed_group_with_saved_on_demand(self):
        self.long_term_error = "http_503"
        first = self.run_collect()
        self.assertEqual(first["status"], "partial_failure")
        self.assertEqual(count(self.conn, COMMITMENTS), 0)
        instance_observations = count(self.conn, OBSERVATIONS)
        last_first_run = count(self.conn, "SELECT max(observation_id) FROM price_observation")

        self.long_term_error = None
        calls = []

        def tracking(url, config):
            calls.append(url)
            return self.get_json(url, config)

        second = self.run_collect(get_json=tracking)
        self.assertEqual(second["status"], "success")
        self.assertEqual([g["status"] for g in second["groups"]], ["skipped", "success"])
        self.assertNotIn(verda.INSTANCE_URL, calls)              # 인스턴스 묶음은 다시 받지 않음
        commitments = count(self.conn, COMMITMENTS)
        self.assertGreater(commitments, 0)
        self.assertEqual(count(self.conn, OBSERVATIONS), instance_observations + commitments)
        # 약정 계산 기준은 첫 실행에서 적재한 온디맨드 관측
        bases = self.conn.execute(
            """SELECT DISTINCT (n.calculation_inputs ->> 'base_observation_id')::bigint
               FROM price_normalization n JOIN price_observation ob USING (observation_id)
               JOIN offer o USING (offer_id) WHERE o.contract_type = 'commitment'""").fetchall()
        self.assertTrue(all(base <= last_first_run for (base,) in bases))

    def test_same_day_rerun_matches_single_successful_run(self):
        # 실패 후 다시 실행한 결과가 한 번에 성공한 실행과 같은 수의 관측을 남긴다
        self.long_term_error = "http_503"
        self.run_collect()
        self.long_term_error = None
        self.run_collect()
        retried = count(self.conn, OBSERVATIONS)
        self.clock[0] += timedelta(days=1)
        self.run_collect()
        self.assertEqual(count(self.conn, OBSERVATIONS), retried * 2)


class TerminateTests(VerdaDb):
    def test_sigterm_rolls_back_and_marks_run_failed(self):
        def terminated(url, config):
            raise cli.Terminated()

        with self.assertRaises(cli.Terminated):
            verda.collect(self.conn, now=self.now, get_json=terminated, pause=lambda _s: None)
        self.assertEqual(self.conn.execute("SELECT run_status FROM collection_run").fetchall(), [("failed",)])
        self.assertEqual(count(self.conn, OBSERVATIONS), 0)


class LockTests(unittest.TestCase):
    def setUp(self):
        self.name = create_database()
        self.url = make_conninfo(admin_url(), dbname=self.name)
        self.conn = connect(self.name)
        self.conn.execute(DDL.read_text(encoding="utf8"))
        self.conn.execute(SEED.read_text(encoding="utf8"))
        self.conn.commit()
        self.others = []

    def tearDown(self):
        for other in self.others:
            other.close()
        self.conn.close()
        drop_database(self.name)

    def other(self):
        conn = connect(self.name)
        self.others.append(conn)
        return conn

    def test_lock_excludes_same_name_only(self):
        other = self.other()
        self.assertTrue(runs.try_lock(self.conn, "aws"))
        self.assertFalse(runs.try_lock(other, "aws"))
        self.assertTrue(runs.try_lock(other, "azure"))

    def test_lock_released_when_connection_closes(self):
        holder = self.other()
        self.assertTrue(runs.try_lock(holder, "aws"))
        holder.close()
        self.assertTrue(runs.try_lock(self.conn, "aws"))

    def test_cli_exits_75_when_same_provider_running(self):
        self.assertTrue(runs.try_lock(self.other(), "aws"))
        with mock.patch.dict(os.environ, {"DATABASE_URL": self.url}), mock.patch("builtins.print"):
            self.assertEqual(cli.main(["collect", "aws"]), cli.EXIT_LOCKED)
        self.assertEqual(count(self.conn, "SELECT count(*) FROM collection_run"), 0)

    def test_close_abandoned_only_same_provider(self):
        now = datetime(2026, 11, 20, tzinfo=timezone.utc)
        ids = {}
        for label, version, finished in [("aws", "gpu-collector 0.1.0 / erd-aws 1", None),
                                         ("aws_history", "gpu-collector 0.1.0 / erd-aws-history 1", None),
                                         ("aws_done", "gpu-collector 0.1.0 / erd-aws 1", now),
                                         ("azure", "gpu-collector 0.1.0 / erd-azure 1", None)]:
            ids[label] = count(self.conn, """INSERT INTO collection_run (started_at, finished_at, run_status, trigger_type,
                                                                         collector_version)
                                             VALUES (%s, %s, %s, 'manual', %s) RETURNING run_id""",
                               now - timedelta(hours=1), finished, "success" if finished else "running", version)
        self.conn.commit()
        closed = runs.close_abandoned(self.conn, "aws", now)
        self.assertEqual(closed, sorted([ids["aws"], ids["aws_history"]]))
        status = dict(self.conn.execute("SELECT run_id, run_status FROM collection_run").fetchall())
        self.assertEqual(status[ids["azure"]], "running")
        self.assertEqual(status[ids["aws_done"]], "success")
        self.assertEqual(status[ids["aws"]], "failed")


class ExitCodeTests(unittest.TestCase):
    def test_missing_database_url_is_config_error(self):
        env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stderr"):
            self.assertEqual(cli.main(["report"]), cli.EXIT_CONFIG)

    def test_bad_arguments_are_config_errors(self):
        for argv in (["collect", "unknown"], ["collect", "aws", "--date", "2026-13-01"], ["backfill", "aws"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr"), self.assertRaises(SystemExit) as raised:
                cli.parse(argv)
            self.assertEqual(raised.exception.code, cli.EXIT_CONFIG)

    def test_status_mapping(self):
        self.assertEqual(cli.EXIT_BY_STATUS, {"success": 0, "partial_failure": 1, "failed": 2})


class FakeResponse:
    def __init__(self, status, headers=None):
        self.status_code, self.headers, self.closed = status, headers or {}, False

    def close(self):
        self.closed = True


class RetryTests(unittest.TestCase):
    def send(self, outcomes):
        waits, calls = [], []

        def call():
            outcome = outcomes[len(calls)]
            calls.append(outcome)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return fetch.send(call, sleep=waits.append), waits, len(calls)

    def test_retries_5xx_then_succeeds(self):
        response, waits, calls = self.send([FakeResponse(503), FakeResponse(200)])
        self.assertEqual((response.status_code, waits, calls), (200, [2], 2))

    def test_honors_retry_after(self):
        response, waits, _ = self.send([FakeResponse(429, {"Retry-After": "7"}), FakeResponse(200)])
        self.assertEqual((response.status_code, waits), (200, [7]))

    def test_does_not_retry_forbidden(self):
        response, waits, calls = self.send([FakeResponse(403)])
        self.assertEqual((response.status_code, waits, calls), (403, [], 1))

    def test_gives_up_after_attempts(self):
        response, waits, calls = self.send([FakeResponse(503), FakeResponse(502), FakeResponse(500)])
        self.assertEqual((response.status_code, waits, calls), (500, [2, 4], 3))

    def test_retries_connection_error(self):
        response, waits, _ = self.send([requests.ConnectionError(), FakeResponse(200)])
        self.assertEqual((response.status_code, waits), (200, [2]))

    def test_connection_error_exhausted_raises(self):
        with self.assertRaises(requests.ConnectionError):
            self.send([requests.ConnectionError()] * 3)

    def test_get_json_retries_then_reports_status(self):
        responses = [FakeResponse(503), FakeResponse(503), FakeResponse(503)]
        with mock.patch.object(fetch.requests, "get", side_effect=responses), \
                mock.patch.object(fetch, "_sleep", lambda _s: None):
            with self.assertRaises(fetch.FetchError) as raised:
                fetch.get_json("https://example.invalid/x.json", {})
        self.assertEqual(str(raised.exception), "http_503")


if __name__ == "__main__":
    unittest.main()
