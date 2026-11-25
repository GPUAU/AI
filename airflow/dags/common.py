"""수집 DAG 공통: collector CLI 실행, 종료 코드 처리, Slack 알림, 하루 요약."""
import logging
import os
import sys
from datetime import datetime, timedelta

import pendulum
import psycopg
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


def provider_tasks(spec, operator=CollectorOperator):
    """수집 DAG의 task: collect(출처별이면 여러 개) → notify_changes → check_failures.

    parts가 있으면 출처별 task를 순서대로 잇는다. 같은 사업자는 DB 잠금 때문에 동시에 못 돈다.
    앞 task가 실패해도 다음 출처 수집과 변동 알림은 하고(all_done), 하나라도 실패하면 check_failures가
    실행을 실패로 표시한다. 반환: collect task 목록.
    """
    provider, extra = spec["provider"], (["--hourly"] if spec.get("hourly") else [])
    if "parts" in spec:
        parts = spec["parts"]
    else:
        parts = [("collect", spec.get("only"))]
    tasks = [operator(task_id=task_id, command=["collect", provider, *(["--only", source] if source else []), *extra],
                      env_map=spec.get("env"), pool=POOL, trigger_rule="all_success" if i == 0 else "all_done")
             for i, (task_id, source) in enumerate(parts)]
    for upstream, downstream in zip(tasks, tasks[1:]):
        upstream >> downstream
    sources = [source for _, source in parts if source] or None
    notify = notify_changes(spec["name"], sources)
    tasks[-1] >> notify
    [*tasks, notify] >> check_failures()
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
        "changes": conn.execute(CHANGE_SQL["daily_changes"], window).fetchall(),
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
    lines += daily_changes_lines(s.get("changes") or [])
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


# 가격 변동 ---------------------------------------------------------------------------------
# 정가(온디맨드·약정) 변동은 바로 알린다. 원래 통화의 가격 구성 요소로 비교해 환율만 바뀐 경우(EUR 등)는 변동이 아니다.
# 스팟은 시장 가격이라 자주 바뀌어 하루 요약으로만 보고, ±30% 넘는 급변(collector의 price_spike, OR-06)은 바로 알린다.
# 추정 관측(Verda 약정 할인율 가정 등)은 온디맨드가 바뀌면 따라 바뀌므로 비교하지 않는다.

# 관측의 가격 구성 요소(원래 통화) 서명과 실효 USD/GPU·시간. obs에는 바깥 행을 별칭으로 한정해 넣는다(pair.…)
# — 한정하지 않으면 안쪽 테이블의 같은 이름 컬럼으로 묶인다.
_SIGNATURE = """(SELECT string_agg(pc.component_type || ':' || coalesce(pc.amount::text, '') || ':' ||
                                    coalesce(pc.discount_rate::text, ''), ',' ORDER BY pc.component_type, pc.amount)
                  FROM price_component pc WHERE pc.observation_id = {obs})"""
_EFFECTIVE = """(SELECT n.effective_usd_gpu_hour FROM price_normalization n
                  WHERE n.observation_id = {obs} ORDER BY n.rule_id DESC LIMIT 1)"""
_LABEL = """pr.provider_name, o.contract_type, o.commitment_months, p.original_product_name, p.region_code"""

CHANGE_SQL = {
    # 이번 DAG 실행이 적재한 관측 중 직전 관측과 가격이 다른 정가 오퍼
    "list_changes": f"""
        WITH cur AS (
            SELECT ob.observation_id, ob.offer_id, ob.collected_at, {_LABEL}
            FROM price_observation ob
            JOIN collection_check c ON c.check_id = ob.check_id
            JOIN collection_run r ON r.run_id = c.run_id
            JOIN collection_target t ON t.target_id = c.target_id
            JOIN data_source ds ON ds.source_id = t.source_id
            JOIN provider pr ON pr.provider_id = t.provider_id
            JOIN offer o ON o.offer_id = ob.offer_id
            JOIN product p ON p.product_id = o.product_id
            WHERE r.started_at >= %(since)s AND pr.provider_name = %(provider)s
              AND (%(sources)s::text[] IS NULL OR ds.source_name = ANY(%(sources)s))
              AND o.contract_type IN ('on_demand', 'commitment') AND ob.price_certainty <> 'estimate'
        ), pair AS (
            SELECT cur.*, (SELECT o2.observation_id FROM price_observation o2
                           WHERE o2.offer_id = cur.offer_id AND o2.price_certainty <> 'estimate'
                             AND (o2.collected_at, o2.observation_id) < (cur.collected_at, cur.observation_id)
                           ORDER BY o2.collected_at DESC, o2.observation_id DESC LIMIT 1) AS prev_id
            FROM cur
        )
        SELECT provider_name, contract_type, commitment_months, original_product_name, region_code,
               {_EFFECTIVE.format(obs="pair.prev_id")}, {_EFFECTIVE.format(obs="pair.observation_id")}
        FROM pair
        WHERE pair.prev_id IS NOT NULL
          AND {_SIGNATURE.format(obs="pair.prev_id")} IS DISTINCT FROM {_SIGNATURE.format(obs="pair.observation_id")}
        ORDER BY original_product_name, region_code, contract_type, commitment_months""",
    # 이번 DAG 실행에서 collector가 남긴 급변 알림(스팟 포함)
    "spikes": f"""
        SELECT {_LABEL}, a.reason
        FROM collection_alert a
        JOIN collection_run r ON r.run_id = a.run_id
        JOIN collection_target t ON t.target_id = a.target_id
        JOIN data_source ds ON ds.source_id = t.source_id
        JOIN provider pr ON pr.provider_id = t.provider_id
        JOIN price_observation ob ON ob.observation_id = a.current_observation_id
        JOIN offer o ON o.offer_id = ob.offer_id
        JOIN product p ON p.product_id = o.product_id
        WHERE a.alert_type = 'price_spike' AND r.started_at >= %(since)s AND pr.provider_name = %(provider)s
          AND (%(sources)s::text[] IS NULL OR ds.source_name = ANY(%(sources)s))
        ORDER BY a.detected_at""",
    # 하루 요약: 그날 마지막 관측 vs 그 전날까지의 마지막 관측(오퍼별)
    "daily_changes": f"""
        WITH today AS (
            SELECT DISTINCT ON (ob.offer_id) ob.offer_id, ob.observation_id
            FROM price_observation ob
            WHERE ob.collected_at >= %(start)s AND ob.collected_at < %(end)s AND ob.price_certainty <> 'estimate'
            ORDER BY ob.offer_id, ob.collected_at DESC, ob.observation_id DESC
        ), pair AS (
            SELECT today.*, (SELECT o2.observation_id FROM price_observation o2
                             WHERE o2.offer_id = today.offer_id AND o2.price_certainty <> 'estimate'
                               AND o2.collected_at < %(start)s
                             ORDER BY o2.collected_at DESC, o2.observation_id DESC LIMIT 1) AS prev_id
            FROM today
        )
        SELECT {_LABEL}, {_EFFECTIVE.format(obs="pair.prev_id")}, {_EFFECTIVE.format(obs="pair.observation_id")}
        FROM pair
        JOIN offer o ON o.offer_id = pair.offer_id
        JOIN product p ON p.product_id = o.product_id
        JOIN provider pr ON pr.provider_id = p.provider_id
        WHERE pair.prev_id IS NOT NULL
          AND {_SIGNATURE.format(obs="pair.prev_id")} IS DISTINCT FROM {_SIGNATURE.format(obs="pair.observation_id")}
        ORDER BY pr.provider_name, o.contract_type""",
}

CONTRACT_KO = {"on_demand": "온디맨드", "spot": "스팟", "commitment": "약정"}


def offer_label(contract, months, product, region):
    term = CONTRACT_KO.get(contract, contract) + (f" {months}개월" if months else "")
    return f"{product} {region or '리전 미상'} {term}"


def money(value):
    return "-" if value is None else (f"${value:,.2f}" if value >= 1 else f"${value:.4f}")


def change_line(prev, cur):
    if prev is None or cur is None or not prev:
        return f"{money(prev)} → {money(cur)}"
    return f"{money(prev)} → {money(cur)} ({(cur - prev) / prev * 100:+.1f}%)"


def load_changes(conn, provider, sources, since):
    params = {"provider": provider, "sources": sources, "since": since}
    return {"list": conn.execute(CHANGE_SQL["list_changes"], params).fetchall(),
            "spikes": conn.execute(CHANGE_SQL["spikes"], params).fetchall()}


def changes_text(provider, changes, max_lines=10):
    """바뀐 게 없으면 None(보내지 않음)."""
    lines = []
    spikes = changes["spikes"]
    if spikes:
        lines.append(f"🚨 {provider} 급변 {len(spikes)}건 (직전 대비 ±30% 초과)")
        lines += [f" - {offer_label(c, m, p, r)}: {reason}" for _, c, m, p, r, reason in spikes[:max_lines]]
        if len(spikes) > max_lines:
            lines.append(f" - 외 {len(spikes) - max_lines}건")
    listed = changes["list"]
    if listed:
        lines.append(f"💲 {provider} 정가 변동 {len(listed)}건 (USD/GPU·시간)")
        lines += [f" - {offer_label(c, m, p, r)}: {change_line(prev, cur)}"
                  for _, c, m, p, r, prev, cur in listed[:max_lines]]
        if len(listed) > max_lines:
            lines.append(f" - 외 {len(listed) - max_lines}건")
    return "\n".join(lines) or None


def run_started(context):
    dag_run = context["dag_run"]
    return getattr(dag_run, "start_date", None) or dag_run.run_after


@task(trigger_rule="all_done", retries=1)
def notify_changes(provider, sources, **context):
    """이번 실행이 적재한 정가 변동·급변을 Slack으로. 없으면 보내지 않는다."""
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=10) as conn:
        text = changes_text(provider, load_changes(conn, provider, sources, run_started(context)))
    print(text or "가격 변동 없음")
    if text:
        send_slack(text)


def daily_changes_lines(rows, top=3):
    """하루 요약의 가격 변동 줄: 사업자·계약별 건수 + 스팟 변동 폭 큰 순."""
    if not rows:
        return []
    counts = {}
    for name, contract, *_ in rows:
        key = f"{name} {CONTRACT_KO.get(contract, contract)}"
        counts[key] = counts.get(key, 0) + 1
    lines = ["가격 변동: " + " · ".join(f"{k} {v}" for k, v in counts.items())]
    spot = [row for row in rows if row[1] == "spot" and row[5] and row[6] is not None]
    spot.sort(key=lambda row: -abs((row[6] - row[5]) / row[5]))
    for name, contract, months, product, region, prev, cur in spot[:top]:
        lines.append(f" - [{name}] {offer_label(contract, months, product, region)}: {change_line(prev, cur)}")
    return lines
