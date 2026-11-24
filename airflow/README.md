# airflow

수집기(collector)를 일정에 맞춰 실행하고, 재시도·실패 알림을 맡는다. 수집 로직은 넣지 않고 collector 명령을 실행만 한다.

## 구성

| 경로 | 내용 |
| --- | --- |
| `Dockerfile` | `apache/airflow:slim-3.3.2-python3.12` + slack·amazon·postgres provider + collector + DAG |
| `compose.yaml` | 로컬 실행(api-server · scheduler · dag-processor, LocalExecutor) |
| `dags/gpu_collect.py` | DAG 정의 |
| `dags/common.py` | collector 실행·종료 코드 처리·Slack·하루 요약 |
| `scripts/prepare_db.py` | 메타 DB 스키마 준비(`airflow db migrate` 전) |
| `tests/` | DAG 구조·공통 함수 테스트 |

## DAG

| DAG | 주기(KST) | task |
| --- | --- | --- |
| `collect_<사업자>` ×7 | 매일 12:10 | `collect` (`collect <사업자>`)<br>AWS만 `pricing` → `savings_plans` → `spot`, `check_failures` |
| `gpu_fx_krw` | 매일 14:00 | `collect_fx` → `convert_krw` → `notify_summary`, `check_failures` |

- 사업자마다 DAG 하나라 주기·일시정지·재실행을 사업자별로 한다. 주기는 `gpu_collect.py`의 `PROVIDERS`에서 바꾼다.
- AWS는 출처별(`--only`)로 task를 나눠 재시도·알림을 따로 한다. 같은 사업자는 DB 잠금 때문에 동시에 못 돌아 순서대로 잇고, 앞이 실패해도 다음은 돈다. AWS 출처를 새로 켜면 `parts`에도 넣는다.
- `gpu_fx_krw`는 사업자 수집이 재시도까지 끝난 뒤에 돈다. 환산·요약은 앞 task가 실패해도 실행한다(`all_done`).
  `check_failures`는 앞 task가 하나라도 실패하면 실행을 실패로 표시한다(요약만 보면 성공으로 보이는 것을 막음, 알림은 안 보냄).
- 모두 `catchup=False`, `max_active_runs=1`. 사업자 수집은 pool `collector`(3자리)로 동시에 3개까지.
- task는 `--trigger`(scheduled·manual·retry)와 `--date`(실행 시각의 KST 날짜)를 collector에 넘긴다. 재시도도 같은 날짜 키를 쓴다.

### 종료 코드

| 코드 | 뜻 | 처리 |
| --- | --- | --- |
| 0 | 성공 | 성공 |
| 1·2 | 부분 실패·실패 | 재시도 3회(10분부터 늘려 최대 40분). 이미 적재한 묶음은 collector가 건너뛴다 |
| 3 | 설정 오류(키 없음 등) | 바로 실패, 재시도 안 함 |
| 75 | 같은 사업자 실행 중 | 재시도 |

### Slack

- Connection `slack_default`(Incoming Webhook). 환경 변수 `AIRFLOW_CONN_SLACK_DEFAULT`로 넣는다.
  ```
  AIRFLOW_CONN_SLACK_DEFAULT='{"conn_type": "slackwebhook", "password": "https://hooks.slack.com/services/..."}'
  ```
- 마지막 재시도까지 실패한 task는 바로 알린다. `notify_summary`는 하루 요약 1건을 보낸다(합계 + 사업자별 관측 수, 문제가 있으면 실패·알림을 펼침).
- 웹훅 주소는 저장소·이슈·로그에 남기지 않는다. 연결이 없거나 전송이 실패하면 task 로그에 내용만 남기고 task는 그대로 둔다.

### AWS 키

pod에는 `COLLECTOR_AWS_ACCESS_KEY_ID`·`COLLECTOR_AWS_SECRET_ACCESS_KEY`로 두고, `collect_aws`에만 `AWS_*` 이름으로 넘긴다. pod 전체에 `AWS_ACCESS_KEY_ID`가 있으면 S3 로그(IRSA)보다 그 키가 먼저 쓰인다. 로컬은 `collector/.env`의 `AWS_*`를 그대로 쓴다.

## 로컬 실행

저장소 루트에서 실행한다. 수집 데이터는 루트 `docker-compose.yml`의 로컬 DB(5435) `gpu_erd`, Airflow 메타는 같은 서버의 `airflow` DB · `airflow` 스키마다.

```bash
docker compose up -d db
docker compose -f airflow/compose.yaml up -d --build
open http://127.0.0.1:8088          # 로컬은 로그인 없이 관리자
```

- 키는 `collector/.env`를 읽는다. Slack은 `airflow/.env`에 `AIRFLOW_CONN_SLACK_DEFAULT`(선택, 커밋 금지).
- DAG는 일시정지 상태로 올라온다. 켜면 로컬 `gpu_erd`에 실제로 적재된다.
- 메타 DB 접속 문자열에 `options=-csearch_path%3Dairflow`를 붙인다. 없으면 `alembic_version`이 `public`에 생긴다.

```bash
af() { docker compose -f airflow/compose.yaml exec scheduler airflow "$@"; }
af dags trigger collect_oracle                      # 한 번 실행
af tasks test gpu_fx_krw notify_summary 2026-11-24  # 요약만(DB 읽기만)
docker compose -f airflow/compose.yaml down
```

## 테스트

```bash
docker build -f airflow/Dockerfile -t gpu-collector-airflow:dev .
docker run --rm --entrypoint bash -v "$PWD/airflow:/src:ro" gpu-collector-airflow:dev \
  -c 'pip install -q --user pytest && cd /tmp && python -m pytest -q -p no:cacheprovider /src/tests'
```

collector 테스트는 `collector/README.md`를 따른다. 이미지로 돌릴 때:

```bash
docker run --rm --entrypoint bash -v "$PWD/collector/tests:/tests:ro" \
  -e TEST_DATABASE_URL="postgresql://postgres:postgres@host.docker.internal:5435/postgres" \
  gpu-collector-airflow:dev -c 'pip install -q --user pytest && cd / && python -m pytest -q /tests'
```

## DAG 추가

사업자는 `PROVIDERS`에 한 줄 추가한다. 종료 코드 처리·재시도·실패 알림이 같이 따라온다.

```python
"vultr": {"schedule": "10 12 * * *"},
```

- `CollectorOperator`는 `python -m gpu_collector.erd <command> --trigger …`(collect면 `--date`도)를 실행한다. 다른 모듈은 `argv()`를 바꿔 쓴다.
- 새 키는 Secret과 `env_map`에 같이 추가한다.

## 원칙

- Executor는 LocalExecutor. task는 scheduler pod 안에서 collector CLI를 subprocess로 실행한다(Role을 만들 수 없어 KubernetesPodOperator는 안 씀).
- DAG와 collector 패키지는 한 이미지로 함께 배포한다(git-sync 안 씀).
- 과거 데이터 백필은 DAG가 아니라 같은 이미지로 수동 Job을 띄운다(EKS 배포 이슈에서).
