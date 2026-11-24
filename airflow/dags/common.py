"""수집 DAG 공통: collector CLI 실행, 종료 코드 처리, Slack 알림, 하루 요약."""
import logging
import os
import sys
from datetime import datetime, timedelta

import pendulum
from airflow.sdk.exceptions import AirflowException, AirflowFailException
from airflow.providers.standard.hooks.subprocess import SubprocessHook
from airflow.sdk import BaseOperator, task

log = logging.getLogger(__name__)

KST = pendulum.timezone("Asia/Seoul")
SLACK_CONN_ID = "slack_default"
POOL = "collector"

# collector 종료 코드(collector/src/gpu_collector/erd/__main__.py)
EXIT_OK, EXIT_PARTIAL, EXIT_FAILED, EXIT_CONFIG, EXIT_LOCKED = 0, 1, 2, 3, 75


def check_exit(code):
    """0 성공. 3(설정 오류)은 재시도해도 같으니 바로 실패. 나머지(1·2·75)는 재시도."""
    if code == EXIT_OK:
        return
    if code == EXIT_CONFIG:
        raise AirflowFailException("collector 설정 오류(종료 코드 3). 재시도하지 않음")
    if code == EXIT_LOCKED:
        raise AirflowException("같은 사업자 실행이 진행 중(종료 코드 75)")
    raise AirflowException(f"collector 종료 코드 {code}")


def collector_env(env_map, environ=None):
    """현재 환경 + env_map(collector 변수명: pod 변수명). 값이 있는 것만 덮는다.

    AWS 키는 pod에 COLLECTOR_AWS_*로 두고 collect aws에만 AWS_*로 넘긴다.
    pod 전체에 AWS_ACCESS_KEY_ID가 있으면 S3(IRSA)보다 그 키가 먼저 쓰이기 때문이다.
    """
    environ = os.environ if environ is None else environ
    env = dict(environ)
    for name, source in env_map.items():
        if environ.get(source):
            env[name] = environ[source]
    return env


def trigger_type(context):
    if context["ti"].try_number > 1:
        return "retry"
    return "manual" if context["dag_run"].run_type == "manual" else "scheduled"


def collection_date(context):
    """수집 기준일(KST). 재시도도 같은 날짜 키를 쓰도록 실행 시각에서 정한다."""
    when = context.get("logical_date") or context["dag_run"].run_after
    return pendulum.instance(when).in_timezone(KST).date().isoformat()


class CollectorOperator(BaseOperator):
    """`python -m gpu_collector.erd <command>`를 실행하고 종료 코드로 성공·재시도·실패를 정한다."""

    ui_color = "#e8f0fe"

    def __init__(self, *, command, env_map=None, **kwargs):
        super().__init__(**kwargs)
        self.command = list(command)
        self.env_map = dict(env_map or {})
        self._hook = None

    def argv(self, context):
        args = [*self.command, "--trigger", trigger_type(context)]
        if self.command[0] == "collect":
            args += ["--date", collection_date(context)]
        return [sys.executable, "-m", "gpu_collector.erd", *args]

    def execute(self, context):
        self._hook = SubprocessHook()
        result = self._hook.run_command(self.argv(context), env=collector_env(self.env_map))
        check_exit(result.exit_code)

    def on_kill(self):
        # collector는 SIGTERM을 받으면 지금 묶음을 되돌리고 실행을 실패로 기록한다
        if self._hook:
            self._hook.send_sigterm()


def provider_tasks(provider, conf, operator=CollectorOperator):
    """사업자 DAG의 task. parts가 있으면 출처별 task를 순서대로 잇는다.

    같은 사업자는 DB 잠금 때문에 동시에 못 돈다. 앞 출처가 실패해도 다음 출처는 수집하고(all_done),
    하나라도 실패하면 check_failures가 실행을 실패로 표시한다.
    """
    if "parts" not in conf:
        return [operator(task_id="collect", command=["collect", provider], env_map=conf.get("env"), pool=POOL)]
    tasks = [operator(task_id=task_id, command=["collect", provider, "--only", source], env_map=conf.get("env"),
                      pool=POOL, trigger_rule="all_success" if i == 0 else "all_done")
             for i, (task_id, source) in enumerate(conf["parts"])]
    for upstream, downstream in zip(tasks, tasks[1:]):
        upstream >> downstream
    tasks >> check_failures()
    return tasks


# Slack ------------------------------------------------------------------------------------

def send_slack(text):
    """Slack이 실패해도 task 결과는 바꾸지 않는다. 웹훅 주소가 남지 않게 예외 종류만 기록한다."""
    try:
        from airflow.providers.slack.hooks.slack_webhook import SlackWebhookHook
        SlackWebhookHook(slack_webhook_conn_id=SLACK_CONN_ID).send(text=text)
    except Exception as error:
        log.warning("Slack 전송 실패(%s). 보내려던 내용:\n%s", type(error).__name__, text)


def failure_text(context):
    ti = context["ti"]
    lines = [f"❌ {ti.dag_id} · {ti.task_id} 실패 (시도 {ti.try_number}회)"]
    if context.get("exception"):
        lines.append(str(context["exception"])[:300])
    if getattr(ti, "log_url", None):
        lines.append(ti.log_url)
    return "\n".join(lines)


def notify_failure(context):
    send_slack(failure_text(context))


@task(trigger_rule="one_failed", retries=0, on_failure_callback=None)
def check_failures():
    """앞 task 중 하나라도 실패하면 이 DAG 실행을 실패로 표시한다.

    요약 task가 all_done이라 마지막 task만 보면 실행이 성공으로 보이기 때문이다.
    실패 알림은 실패한 task가 이미 보냈으니 여기서는 보내지 않는다. 실패가 없으면 skipped.
    """
    raise AirflowFailException("앞 task 중 실패가 있음(실패 알림은 해당 task에서 보냄)")


DEFAULT_ARGS = {
    "owner": "collector",
    "retries": 3,
    "retry_delay": timedelta(minutes=10),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=40),
    "execution_timeout": timedelta(minutes=45),
    "on_failure_callback": notify_failure,
}


# 하루 요약 ---------------------------------------------------------------------------------

SUMMARY_SQL = {
    "observations": """
        SELECT pr.provider_name, count(po.observation_id)
        FROM provider pr
        LEFT JOIN product pd ON pd.provider_id = pr.provider_id
        LEFT JOIN offer o ON o.product_id = pd.product_id
        LEFT JOIN price_observation po ON po.offer_id = o.offer_id
             AND po.collected_at >= %(start)s AND po.collected_at < %(end)s
        GROUP BY pr.provider_name ORDER BY pr.provider_name""",
    "failed_checks": """
        SELECT pr.provider_name, count(*)
        FROM collection_check c
        JOIN collection_target t ON t.target_id = c.target_id
        JOIN provider pr ON pr.provider_id = t.provider_id
        WHERE c.checked_at >= %(start)s AND c.checked_at < %(end)s
          AND c.processing_status IN ('collection_failed', 'validation_failed')
        GROUP BY pr.provider_name ORDER BY pr.provider_name""",
    "alerts": """
        -- 알림에 대상이 없으면(출처 단위 실패) 그 출처를 쓰는 대상의 사업자, 환율이면 '환율'
        SELECT a.alert_type,
               coalesce(pr.provider_name,
                        (SELECT min(p2.provider_name) FROM collection_target t2
                         JOIN provider p2 ON p2.provider_id = t2.provider_id WHERE t2.source_id = a.source_id),
                        CASE WHEN ds.source_type = 'fx_source' THEN '환율' END, ds.source_name, '-'),
               coalesce(a.reason, '')
        FROM collection_alert a
        LEFT JOIN collection_target t ON t.target_id = a.target_id
        LEFT JOIN provider pr ON pr.provider_id = t.provider_id
        LEFT JOIN data_source ds ON ds.source_id = a.source_id
        WHERE a.detected_at >= %(start)s AND a.detected_at < %(end)s
        ORDER BY a.detected_at""",
    "fx_date": "SELECT max(reference_date) FROM exchange_rate WHERE base_currency = 'USD' AND quote_currency = 'KRW'",
    "krw": "SELECT count(*) FROM krw_conversion WHERE calculated_at >= %(start)s AND calculated_at < %(end)s",
}


def load_summary(conn, day):
    """day(KST 날짜)의 사업자별 관측 수, 실패 점검, 알림, 환율 기준일, 원화 환산 수."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=KST)
    window = {"start": start, "end": start + timedelta(days=1)}
    query = lambda name: conn.execute(SUMMARY_SQL[name], window).fetchall()
    return {
        "day": day,
        "observations": dict(query("observations")),
        "failed_checks": dict(query("failed_checks")),
        "alerts": query("alerts"),
        "fx_date": query("fx_date")[0][0],
        "krw": query("krw")[0][0],
    }


def summary_text(s, max_alerts=5):
    """첫 줄은 합계, 둘째 줄은 사업자별 관측 수. 문제가 있으면 그 아래에 펼친다."""
    obs, failed, alerts = s["observations"], s["failed_checks"], s["alerts"]
    missing = sorted(name for name, count in obs.items() if count == 0)
    day = s["day"].strftime("%m-%d")
    fx = s["fx_date"].strftime("%m-%d") if s["fx_date"] else "없음"
    total = sum(obs.values())
    head = (f"{day} 수집 · 관측 {total:,}건({len(obs) - len(missing)}/{len(obs)}곳) · "
            f"원화 환산 {s['krw']:,}건 · 환율 기준일 {fx}")
    mark = "⚠️" if (missing or failed or alerts) else "✅"
    lines = [f"{mark} {head}", " · ".join(f"{name} {count:,}" for name, count in obs.items())]
    if missing:
        lines.append("관측 없음: " + ", ".join(missing))
    if failed:
        lines.append("실패 점검: " + ", ".join(f"{name} {count}" for name, count in failed.items()))
    if alerts:
        counts = {}
        for kind, _, _ in alerts:
            counts[kind] = counts.get(kind, 0) + 1
        lines.append(f"알림 {len(alerts)}건: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        lines += [f" - [{who}] {kind}: {reason[:120]}" for kind, who, reason in alerts[:max_alerts]]
        if len(alerts) > max_alerts:
            lines.append(f" - 외 {len(alerts) - max_alerts}건")
    return "\n".join(lines)
