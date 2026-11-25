"""수집 DAG.

- collect_*: 사업자(또는 출처)마다 DAG 하나. 주기·출처는 COLLECT에서 바꾼다.
  collect → notify_changes(정가 변동·급변 Slack) → check_failures(하나라도 실패하면 실행 실패).
  가격이 실제로 움직이는 출처(Verda 인스턴스, AWS·Nebius 스팟)는 1시간마다(--hourly), 나머지는 하루 1번.
- gpu_fx_krw: 환율 수집 → 원화 환산 → Slack 하루 요약. 하루 1번 수집이 재시도까지 끝난 뒤인 14:00에 돈다.
"""
import os
from datetime import timedelta

import pendulum
import psycopg
from airflow.sdk import DAG, task
from airflow.timetables.trigger import CronTriggerTimetable

from common import (DEFAULT_ARGS, KST, CollectorOperator, check_failures, collection_date, load_summary,
                    provider_tasks, send_slack, summary_text)

START = pendulum.datetime(2026, 11, 24, tz=KST)
DAILY = "10 12 * * *"
AWS_KEYS = {"AWS_ACCESS_KEY_ID": "COLLECTOR_AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY": "COLLECTOR_AWS_SECRET_ACCESS_KEY"}
# 1시간 DAG는 다음 실행 전에 끝나도록 재시도를 짧게
HOURLY_ARGS = {"retries": 1, "retry_delay": timedelta(minutes=5), "retry_exponential_backoff": False,
               "execution_timeout": timedelta(minutes=20)}

# dag_id: provider(collector 이름), name(DB 사업자명), schedule(cron, KST), env(이름을 바꿔 넘길 환경 변수),
#   parts: 출처별 task (task_id, --only 출처). 같은 사업자는 DB 잠금 때문에 동시에 못 돌아 순서대로 잇는다.
#   only: 출처 하나만, hourly: --hourly(그날 첫 실행은 매일 수집, 이후는 새 기록·바뀐 가격만).
# 같은 사업자의 하루 1번 DAG(12:10)와 1시간 DAG(매시 40분)는 잠금이 겹치지 않게 시각을 엇갈린다.
# 출처를 새로 켜면 여기에도 넣는다(parts·only에 없는 출처는 수집되지 않음).
COLLECT = {
    "collect_aws": {"provider": "aws", "name": "AWS", "schedule": DAILY, "env": AWS_KEYS,
                    # Savings Plans는 EC2 상품 사양을 쓰므로 pricing이 먼저(새 인스턴스가 처음 나온 날 대비)
                    "parts": [("pricing", "aws_ec2_pricing_api"), ("savings_plans", "aws_savings_plans_api")]},
    "collect_aws_spot": {"provider": "aws", "name": "AWS", "schedule": "40 * * * *", "env": AWS_KEYS,
                         "only": "aws_ec2_spot_api", "hourly": True},
    "collect_azure": {"provider": "azure", "name": "Azure", "schedule": DAILY},
    "collect_verda": {"provider": "verda", "name": "Verda", "schedule": "10 * * * *", "hourly": True},
    "collect_nebius": {"provider": "nebius", "name": "Nebius", "schedule": DAILY, "only": "nebius_pricing_docs"},
    "collect_nebius_spot": {"provider": "nebius", "name": "Nebius", "schedule": "40 * * * *",
                            "only": "nebius_spot_price_api", "hourly": True},
    "collect_oracle": {"provider": "oracle", "name": "Oracle", "schedule": DAILY},
    "collect_scaleway": {"provider": "scaleway", "name": "Scaleway", "schedule": DAILY},
    "collect_hyperstack": {"provider": "hyperstack", "name": "Hyperstack", "schedule": DAILY},
}


def schedule(cron):
    return CronTriggerTimetable(cron, timezone=KST)


for dag_id, spec in COLLECT.items():
    with DAG(
        dag_id=dag_id,
        description=f"{spec['name']} 가격 수집" + (" (1시간마다)" if spec.get("hourly") else ""),
        schedule=schedule(spec["schedule"]),
        start_date=START,
        catchup=False,
        max_active_runs=1,
        default_args={**DEFAULT_ARGS, **(HOURLY_ARGS if spec.get("hourly") else {})},
        tags=["collect", "hourly" if spec.get("hourly") else "daily"],
    ):
        provider_tasks(spec)


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
