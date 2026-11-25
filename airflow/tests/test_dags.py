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

AWS_ENV = {"AWS_ACCESS_KEY_ID": "COLLECTOR_AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY": "COLLECTOR_AWS_SECRET_ACCESS_KEY"}
# dag_id: (collect task 명령들, cron)
EXPECTED = {
    "collect_aws": ([["collect", "aws", "--only", "aws_ec2_pricing_api"],
                     ["collect", "aws", "--only", "aws_savings_plans_api"]], "10 12 * * *"),
    "collect_aws_spot": ([["collect", "aws", "--only", "aws_ec2_spot_api", "--hourly"]], "40 * * * *"),
    "collect_azure": ([["collect", "azure"]], "10 12 * * *"),
    "collect_verda": ([["collect", "verda", "--hourly"]], "10 * * * *"),
    "collect_nebius": ([["collect", "nebius", "--only", "nebius_pricing_docs"]], "10 12 * * *"),
    "collect_nebius_spot": ([["collect", "nebius", "--only", "nebius_spot_price_api", "--hourly"]], "40 * * * *"),
    "collect_oracle": ([["collect", "oracle"]], "10 12 * * *"),
    "collect_scaleway": ([["collect", "scaleway"]], "10 12 * * *"),
    "collect_hyperstack": ([["collect", "hyperstack"]], "10 12 * * *"),
}


@pytest.fixture(scope="module")
def dagbag():
    from airflow.dag_processing.dagbag import DagBag
    return DagBag(str(DAGS))


def test_dags_import(dagbag):
    assert dagbag.import_errors == {}
    assert sorted(dagbag.dags) == sorted([*EXPECTED, "gpu_fx_krw"])


@pytest.mark.parametrize("dag_id", sorted(EXPECTED))
def test_collect_dags(dagbag, dag_id):
    commands, cron = EXPECTED[dag_id]
    dag = dagbag.dags[dag_id]
    assert dag.catchup is False and dag.max_active_runs == 1 and dag.timetable.summary == cron
    collects = [t for t in dag.tasks if isinstance(t, common.CollectorOperator)]
    assert [t.command for t in collects] == commands
    hourly = "--hourly" in commands[0]
    for i, op in enumerate(collects):
        assert op.pool == "collector"
        assert op.retries == (1 if hourly else 3)
        assert op.trigger_rule == ("all_success" if i == 0 else "all_done")   # 앞 출처가 실패해도 다음 출처는 수집
        assert op.env_map == (AWS_ENV if dag_id.startswith("collect_aws") else {})
    for upstream, downstream in zip(collects, collects[1:]):                 # 같은 사업자 잠금 때문에 순서대로
        assert downstream.task_id in upstream.downstream_task_ids
    notify, check = dag.get_task("notify_changes"), dag.get_task("check_failures")
    assert collects[-1].downstream_task_ids == {"notify_changes", "check_failures"}
    assert notify.trigger_rule == "all_done"
    assert check.upstream_task_ids == {*(t.task_id for t in collects), "notify_changes"}
    assert check.trigger_rule == "one_failed" and check.retries == 0 and not check.on_failure_callback


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


def test_changes_text_none_when_nothing_changed():
    assert common.changes_text("Verda", {"list": [], "spikes": []}) is None


def test_changes_text_lists_spikes_then_list_changes():
    from decimal import Decimal as D
    spikes = [("AWS", "spot", None, "p5.48xlarge", "use1-az2", "실효 단가 3.1 → 4.5 (+45.2%)")]
    listed = [("AWS", "on_demand", None, "p5.48xlarge", "us-east-1", D("6.88"), D("7.20")),
              ("AWS", "commitment", 12, "p5.48xlarge", "us-east-1", D("4.10"), D("3.90"))]
    lines = common.changes_text("AWS", {"list": listed, "spikes": spikes}).splitlines()
    assert lines[0] == "🚨 AWS 급변 1건 (직전 대비 ±30% 초과)"
    assert lines[1] == " - p5.48xlarge use1-az2 스팟: 실효 단가 3.1 → 4.5 (+45.2%)"
    assert lines[2] == "💲 AWS 정가 변동 2건 (USD/GPU·시간)"
    assert lines[3] == " - p5.48xlarge us-east-1 온디맨드: $6.88 → $7.20 (+4.7%)"
    assert lines[4] == " - p5.48xlarge us-east-1 약정 12개월: $4.10 → $3.90 (-4.9%)"


def test_summary_price_change_lines():
    from decimal import Decimal as D
    changes = [("Nebius", "spot", None, "gpu-h100 8gpu", "eu-north1", D("0.79"), D("2.07")),
               ("Nebius", "spot", None, "gpu-h200 8gpu", "eu-north1", D("2.00"), D("2.10")),
               ("Verda", "on_demand", None, "1B300.30V", "FIN-01", D("13.05"), D("13.18"))]
    lines = common.summary_text(summary(changes=changes)).splitlines()
    assert lines[0].startswith("✅")                       # 가격 변동은 문제가 아니다
    assert lines[2] == "가격 변동: Nebius 스팟 2 · Verda 온디맨드 1"
    assert lines[3] == " - [Nebius] gpu-h100 8gpu eu-north1 스팟: $0.7900 → $2.07 (+162.0%)"   # 변동 폭 큰 순
    assert len(lines) == 5                                  # 스팟만 상위 목록(최대 3)
