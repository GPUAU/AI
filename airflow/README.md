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

이미지 레이어는 잘 안 바뀌는 순서로 쌓는다: 의존성(`pyproject.toml`만 보고 설치, 약 160MB) → collector 소스(`--no-deps`) → DAG·스크립트 → Git SHA(`COLLECTOR_BUILD`). collector·DAG만 바뀌면 의존성 레이어는 캐시를 쓰고 레지스트리에도 다시 올라가지 않는다(약 3MB만 새로 push).

## DAG

| DAG | 주기(KST) | collect task |
| --- | --- | --- |
| `collect_aws` | 매일 12:10 | `pricing` → `savings_plans` (`--only` 출처별) |
| `collect_aws_spot` | 매시 40분 | `collect aws --only aws_ec2_spot_api --hourly` |
| `collect_verda` | 매시 10분 | `collect verda --hourly` |
| `collect_nebius` | 매일 12:10 | `collect nebius --only nebius_pricing_docs` |
| `collect_nebius_spot` | 매시 40분 | `collect nebius --only nebius_spot_price_api --hourly` |
| `collect_azure`·`collect_oracle`·`collect_scaleway`·`collect_hyperstack` | 매일 12:10 | `collect <사업자>` |
| `gpu_fx_krw` | 매일 14:00 | `collect_fx` → `convert_krw` → `notify_summary` |

- 수집 DAG는 모두 collect → `notify_changes`(가격 변동 알림) → `check_failures`. 주기·출처는 `gpu_collect.py`의 `COLLECT`에서 바꾼다.
- 1시간 주기는 가격이 실제로 움직이는 출처만: Verda(이력 API 없음, 하루 중 변동), AWS·Nebius 스팟(이력 API에서 새 기록만 이어 적재). 나머지는 최근 10일 변동이 없어 하루 1번. `--hourly`의 동작은 collector README.
- 같은 사업자는 DB 잠금 때문에 동시에 못 돈다. 출처별 task는 순서대로 잇고(앞이 실패해도 다음은 돈다), 하루 1번 DAG(12:10)와 1시간 DAG(매시 40분)는 시각을 엇갈린다. 출처를 새로 켜면 `COLLECT`에도 넣는다.
- `check_failures`는 앞 task가 하나라도 실패하면 실행을 실패로 표시한다(알림 task가 `all_done`이라 실행이 성공으로 보이는 것을 막음, 알림은 안 보냄).
- 모두 `catchup=False`, `max_active_runs=1`. 사업자 수집은 pool `collector`(3자리)로 동시에 3개까지. 1시간 DAG는 재시도 1회(5분 뒤), 제한 20분.
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
- 마지막 재시도까지 실패한 task는 바로 알린다. `notify_summary`는 하루 요약 1건을 보낸다(합계 + 사업자별 관측 수 + 가격 변동, 문제가 있으면 실패·알림을 펼침).
- 가격 변동(`notify_changes`, 이번 실행이 적재한 것만, 없으면 안 보냄)

  | 변동 | 바로 알림 | 하루 요약 |
  | --- | --- | --- |
  | 정가(온디맨드·약정) | 💲 금액이 바뀌면 | 건수 |
  | 급변 ±30% 초과(collector `price_spike`, OR-06) | 🚨 | 알림 목록 |
  | 스팟 일반 변동 | 안 보냄(너무 잦음) | 사업자별 건수 + 변동 폭 큰 3건 |

  - 정가 비교는 원래 통화의 가격 구성 요소로 한다. 환율만 바뀐 경우(Scaleway EUR)는 변동이 아니다. 추정 관측(Verda 약정 가정)은 제외.
  - Nebius 스팟은 collector가 급변 알림에서 뺀다(최근 10일 ±30% 넘는 시간 변동 186건, 하루 약 19건). 하루 요약으로만 본다.
  - 기준 근거: 정가는 드물고 사업자 정책 변화라 즉시, 스팟은 시장 가격이라 추세만. 운영하며 알림 양을 보고 조정한다.
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
docker run --rm --entrypoint bash -v "$PWD/collector/tests:/tests:ro" -e COLLECTOR_BUILD= \
  -e TEST_DATABASE_URL="postgresql://postgres:postgres@host.docker.internal:5435/postgres" \
  gpu-collector-airflow:dev -c 'pip install -q --user pytest && cd / && python -m pytest -q /tests'
```

`COLLECTOR_BUILD`(이미지에 들어간 Git SHA)를 비운다. 있으면 실행 기록 버전 뒤에 `@<sha>`가 붙어 버전 문자열을 비교하는 테스트가 실패한다.

## EKS 배포 (u1-3)

`main`에 `airflow/`·`collector/`·`k8s/airflow/`가 바뀌어 들어가면 `.github/workflows/airflow-deploy.yml`이 배포한다(수동 실행 `workflow_dispatch`도 됨).

```
self-hosted runner(u1-3, 캠퍼스 Mac) → OIDC(gha-u1-3) → 이미지 빌드(linux/amd64)
  → ECR teams/u1-3/app:airflow-<sha> → kubectl apply k8s/airflow → rollout status
```

| 파일 | 내용 |
| --- | --- |
| `k8s/airflow/configmap.yaml` | 비밀값 아닌 설정(LocalExecutor, `airflow` 스키마, `/airflow` 하위 경로, S3 로그, 로그인 사용자) |
| `k8s/airflow/scheduler.yaml` | scheduler + task 실행. initContainer가 스키마 준비 → `db migrate` → pool 생성 |
| `k8s/airflow/dag-processor.yaml` | DAG 파일 등록 |
| `k8s/airflow/api-server.yaml` | UI·API·Execution API + Service |
| `k8s/airflow/ingress.yaml` | `https://u1-3.skala-us.com/airflow`, 캠퍼스 IP만 |
| `k8s/airflow/create-secrets.sh` | Secret 2개 만들기·갱신(개인 kubeconfig) |

- 자원(limits): scheduler 600m/2.5Gi, dag-processor 150m/512Mi, api-server 300m/1Gi. 팀 쿼터 4코어에서 FE·BE·cost-agent(1.75)와 BE 롤링(+1)을 빼고 남는 몫이라 모두 `Recreate`(롤링 여유분 없음).
- 이미지는 BE·FE와 같은 ECR 리포지토리 `teams/u1-3/app`에 `airflow-<sha>` 태그로 둔다. 태그는 덮어쓸 수 없어(IMMUTABLE) 같은 커밋을 다시 배포하면 push를 건너뛴다. 노드 IAM으로 pull하므로 imagePullSecrets는 쓰지 않는다.
- 모든 pod는 SA `s3-access`(S3 IRSA)를 쓴다.
- 메타 DB 접속 문자열은 `rds-credentials`의 `DATABASE_URL`(`?sslmode=require` 포함) 뒤에 `&options=-csearch_path%3Dairflow`를 붙인다.
- api-server: Simple auth가 비밀번호 파일을 쓰기 모드로 열어서, Secret(읽기 전용)을 시작할 때 emptyDir로 복사해 쓴다.

### 처음 한 번

1. runner: AI 레포 Settings → Actions → Runners에 캠퍼스 Mac 등록(라벨 `u1-3`, 레포 밖 폴더, `./svc.sh install && ./svc.sh start`).
   - `svc.sh install`이 만드는 서비스 설정(`~/Library/LaunchAgents/actions.runner.*.plist`)의 `SessionCreate`를 지운다. 있으면 키체인을 못 열어 ECR 로그인이 `-25308`로 실패한다. 다시 install하면 또 들어간다.
     ```bash
     cd ~/actions-runner-ai && ./svc.sh stop
     /usr/libexec/PlistBuddy -c "Delete :SessionCreate" ~/Library/LaunchAgents/actions.runner.GPUAU-AI.u1-3-mac-AI.plist
     ./svc.sh start
     ```
2. 클러스터 Secret(배포 workflow 역할은 Secret을 못 만든다): `export KUBECONFIG=~/.kube/u1-3.yaml && bash k8s/airflow/create-secrets.sh`
   - `airflow-secrets`: 수출입은행·Hyperstack·AWS(가격 조회 전용) 키, Slack 웹훅, JWT·Fernet(자동 생성)
   - `airflow-auth`: UI 비밀번호(admin·team)
   - 키를 바꿀 때도 다시 실행한 뒤 `rollout restart`(스크립트가 명령을 출력한다).

### 배포 뒤 확인

```bash
kubectl -n u1-3 get pod -l app=airflow
kubectl -n u1-3 logs deploy/airflow-scheduler -c migrate      # 스키마·마이그레이션
open https://u1-3.skala-us.com/airflow                         # 캠퍼스에서, admin 로그인
```

DAG는 일시정지 상태로 올라온다. 로컬 적재를 멈추고 덤프를 RDS에 다시 적재한 뒤 켠다.

## DAG 추가

사업자는 `COLLECT`에 한 줄 추가한다. 종료 코드 처리·재시도·실패 알림·가격 변동 알림이 같이 따라온다.

```python
"collect_vultr": {"provider": "vultr", "name": "Vultr", "schedule": DAILY},
```

- `CollectorOperator`는 `python -m gpu_collector.erd <command> --trigger …`(collect면 `--date`도)를 실행한다. 다른 모듈은 `argv()`를 바꿔 쓴다.
- 새 키는 Secret과 `env_map`에 같이 추가한다.

## 원칙

- Executor는 LocalExecutor. task는 scheduler pod 안에서 collector CLI를 subprocess로 실행한다(Role을 만들 수 없어 KubernetesPodOperator는 안 씀).
- DAG와 collector 패키지는 한 이미지로 함께 배포한다(git-sync 안 씀).
- 과거 데이터 백필은 DAG가 아니라 같은 이미지로 수동 Job을 띄운다(필요해지면 매니페스트를 만든다).
