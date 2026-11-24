"""DAG 구조와 공통 함수 테스트. 이미지 안에서 실행한다(README 참고)."""
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pendulum
import pytest
from airflow.sdk.exceptions import AirflowException, AirflowFailException

DAGS = Path(__file__).resolve().parents[1] / "dags"
sys.path.insert(0, str(DAGS))

import common  # noqa: E402

PROVIDERS = ["aws", "azure", "hyperstack", "nebius", "oracle", "scaleway", "verda"]


@pytest.fixture(scope="module")
def dagbag():
    from airflow.dag_processing.dagbag import DagBag
    return DagBag(str(DAGS))


def test_dags_import(dagbag):
    assert dagbag.import_errors == {}
    assert sorted(dagbag.dags) == sorted([f"collect_{p}" for p in PROVIDERS] + ["gpu_fx_krw"])


def test_provider_dags(dagbag):
    for provider in PROVIDERS:
        dag = dagbag.dags[f"collect_{provider}"]
        assert dag.catchup is False and dag.max_active_runs == 1
        if provider == "aws":
            continue
        (op,) = dag.tasks
        assert op.command == ["collect", provider] and op.pool == "collector"
        assert op.retries == 3


def test_aws_split_by_source(dagbag):
    dag = dagbag.dags["collect_aws"]
    pricing, savings, spot = (dag.get_task(t) for t in ("pricing", "savings_plans", "spot"))
    assert pricing.command == ["collect", "aws", "--only", "aws_ec2_pricing_api"]
    assert savings.command == ["collect", "aws", "--only", "aws_savings_plans_api"]
    assert spot.command == ["collect", "aws", "--only", "aws_ec2_spot_api"]
    # 같은 사업자 잠금 때문에 순서대로, 앞이 실패해도 다음은 돈다
    assert pricing.downstream_task_ids == {"savings_plans", "check_failures"}
    assert savings.downstream_task_ids == {"spot", "check_failures"}
    assert pricing.trigger_rule == "all_success" and savings.trigger_rule == spot.trigger_rule == "all_done"
    assert dag.get_task("check_failures").upstream_task_ids == {"pricing", "savings_plans", "spot"}
    for op in (pricing, savings, spot):
        assert op.pool == "collector" and op.retries == 3
        assert op.env_map == {"AWS_ACCESS_KEY_ID": "COLLECTOR_AWS_ACCESS_KEY_ID",
                              "AWS_SECRET_ACCESS_KEY": "COLLECTOR_AWS_SECRET_ACCESS_KEY"}


def test_fx_krw_dag(dagbag):
    dag = dagbag.dags["gpu_fx_krw"]
    assert dag.timetable.summary == "0 14 * * *"
    convert, notify = dag.get_task("convert_krw"), dag.get_task("notify_summary")
    assert dag.get_task("collect_fx").downstream_task_ids == {"convert_krw", "check_failures"}
    assert convert.downstream_task_ids == {"notify_summary", "check_failures"}
    assert convert.trigger_rule == "all_done" and notify.trigger_rule == "all_done"
    check = dag.get_task("check_failures")
    assert check.upstream_task_ids == {"collect_fx", "convert_krw", "notify_summary"}
    assert check.trigger_rule == "one_failed" and check.retries == 0 and not check.on_failure_callback


@pytest.mark.parametrize("code, error", [(1, AirflowException), (2, AirflowException), (75, AirflowException),
                                         (3, AirflowFailException)])
def test_check_exit(code, error):
    with pytest.raises(error) as raised:
        common.check_exit(code)
    # 3만 재시도 없이 실패(AirflowFailException), 나머지는 재시도되는 일반 예외
    assert (type(raised.value) is AirflowFailException) == (code == 3)


def test_check_exit_ok():
    assert common.check_exit(0) is None


def test_collector_env_renames_only_present_keys():
    environ = {"PATH": "/bin", "COLLECTOR_AWS_ACCESS_KEY_ID": "id", "COLLECTOR_AWS_SECRET_ACCESS_KEY": ""}
    env = common.collector_env({"AWS_ACCESS_KEY_ID": "COLLECTOR_AWS_ACCESS_KEY_ID",
                                "AWS_SECRET_ACCESS_KEY": "COLLECTOR_AWS_SECRET_ACCESS_KEY"}, environ)
    assert env["AWS_ACCESS_KEY_ID"] == "id" and "AWS_SECRET_ACCESS_KEY" not in env and env["PATH"] == "/bin"


def context(run_type="scheduled", try_number=1, logical_date=None):
    run_after = pendulum.datetime(2026, 11, 24, 3, 10, tz="UTC")   # 12:10 KST
    return {"ti": SimpleNamespace(try_number=try_number),
            "dag_run": SimpleNamespace(run_type=run_type, run_after=run_after),
            "logical_date": logical_date}


def test_argv():
    op = common.CollectorOperator(task_id="t", command=["collect", "aws"])
    assert op.argv(context())[-6:] == ["collect", "aws", "--trigger", "scheduled", "--date", "2026-11-24"]
    assert "retry" in op.argv(context(try_number=2))
    assert "manual" in op.argv(context(run_type="manual"))
    # 15:30 UTC = 다음 날 00:30 KST
    late = pendulum.datetime(2026, 11, 24, 15, 30, tz="UTC")
    assert op.argv(context(logical_date=late))[-1] == "2026-11-25"
    convert = common.CollectorOperator(task_id="c", command=["convert", "krw"])
    assert "--date" not in convert.argv(context())


def summary(**overrides):
    base = {"day": date(2026, 11, 24), "observations": {"AWS": 175, "Verda": 630}, "failed_checks": {},
            "alerts": [], "fx_date": date(2026, 11, 24), "krw": 805}
    return {**base, **overrides}


def test_summary_ok_shows_providers():
    text = common.summary_text(summary())
    assert text == "✅ 11-24 수집 · 관측 805건(2/2곳) · 원화 환산 805건 · 환율 기준일 11-24\nAWS 175 · Verda 630"


def test_summary_problems_expand():
    alerts = [("collection_failed", "AWS", "원천 응답 오류")] * 7
    text = common.summary_text(summary(observations={"AWS": 0, "Verda": 630}, failed_checks={"AWS": 2},
                                       alerts=alerts, fx_date=None))
    lines = text.splitlines()
    assert lines[0].startswith("⚠️") and "환율 기준일 없음" in lines[0]
    assert "관측 없음: AWS" in lines and "실패 점검: AWS 2" in lines
    assert "알림 7건: collection_failed 7" in lines and lines[-1] == " - 외 2건"
