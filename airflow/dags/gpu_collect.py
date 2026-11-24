"""매일 수집 DAG.

- collect_<사업자>: 사업자마다 DAG 하나. 주기는 PROVIDERS에서 사업자별로 바꾼다.
  보통 task 하나, AWS는 출처별(pricing → savings_plans → spot) task로 나눈다.
- gpu_fx_krw: 환율 수집 → 원화 환산 → Slack 하루 요약. 사업자 수집이 재시도까지 끝난 뒤인 14:00에 돈다.
  check_failures는 앞 task가 하나라도 실패하면 실행을 실패로 표시한다(아니면 skipped).
"""
import os

import pendulum
import psycopg
from airflow.sdk import DAG, task
from airflow.timetables.trigger import CronTriggerTimetable

from common import (DEFAULT_ARGS, KST, CollectorOperator, check_failures, collection_date, load_summary,
                    provider_tasks, send_slack, summary_text)

START = pendulum.datetime(2026, 11, 24, tz=KST)
AWS_KEYS = {"AWS_ACCESS_KEY_ID": "COLLECTOR_AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY": "COLLECTOR_AWS_SECRET_ACCESS_KEY"}

# 사업자: 실행 주기(cron, KST), collector에 이름을 바꿔 넘길 환경 변수,
# parts: 출처별 task로 나눌 때 (task_id, --only 출처). 같은 사업자는 DB 잠금 때문에 동시에 못 돌아 순서대로 잇는다.
# 출처를 새로 켜면 parts에도 넣는다(여기 없는 출처는 매일 수집되지 않음).
PROVIDERS = {
    "aws": {"schedule": "10 12 * * *", "env": AWS_KEYS,
            # Savings Plans·스팟은 EC2 상품 사양을 쓰므로 pricing이 먼저(새 인스턴스가 처음 나온 날 대비)
            "parts": [("pricing", "aws_ec2_pricing_api"), ("savings_plans", "aws_savings_plans_api"),
                      ("spot", "aws_ec2_spot_api")]},
    "azure": {"schedule": "10 12 * * *"},
    "verda": {"schedule": "10 12 * * *"},
    "nebius": {"schedule": "10 12 * * *"},
    "oracle": {"schedule": "10 12 * * *"},
    "scaleway": {"schedule": "10 12 * * *"},
    "hyperstack": {"schedule": "10 12 * * *"},
}


def schedule(cron):
    return CronTriggerTimetable(cron, timezone=KST)


for provider, conf in PROVIDERS.items():
    with DAG(
        dag_id=f"collect_{provider}",
        description=f"{provider} 가격 수집 (collect {provider})",
        schedule=schedule(conf["schedule"]),
        start_date=START,
        catchup=False,
        max_active_runs=1,
        default_args=DEFAULT_ARGS,
        tags=["collect"],
    ):
        provider_tasks(provider, conf)


with DAG(
    dag_id="gpu_fx_krw",
    description="환율 수집 → 원화 환산 → Slack 하루 요약",
    schedule=schedule("0 14 * * *"),
    start_date=START,
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["fx", "summary"],
):
    collect_fx = CollectorOperator(task_id="collect_fx", command=["collect", "fx"])
    # 주말·공휴일엔 환율이 비어도 환산은 돈다(환율 미확정 관측은 다음 실행으로 넘어감)
    convert_krw = CollectorOperator(task_id="convert_krw", command=["convert", "krw"], trigger_rule="all_done")

    @task(trigger_rule="all_done", retries=1)
    def notify_summary(**context):
        day = pendulum.parse(collection_date(context)).date()
        with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=10) as conn:
            text = summary_text(load_summary(conn, day))
        print(text)
        send_slack(text)

    summary = notify_summary()
    collect_fx >> convert_krw >> summary
    [collect_fx, convert_krw, summary] >> check_failures()
