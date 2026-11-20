# airflow

수집기(collector)를 일정에 맞춰 실행하고, 재시도·실패 알림을 맡는다. 수집 로직은 넣지 않고 collector 명령을 실행만 한다.

## 구성

| 경로 | 내용 | 상태 |
| --- | --- | --- |
| `Dockerfile` | `apache/airflow:3.3.2-python3.12` + collector 패키지 | 있음 |
| `dags/` | 일일 수집 DAG, 수동 복구 DAG 등 | 예정 |
| `tests/` | DAG import·구조 테스트 | 예정 |

## 이미지

빌드 컨텍스트는 저장소 루트다.

```bash
docker build -f airflow/Dockerfile --build-arg GIT_SHA=$(git rev-parse HEAD) -t gpu-collector-airflow:dev .
```

- collector는 Airflow constraints로 설치한다. collector `pyproject.toml`은 호환 범위만 적고, 이미지 안의 실제 버전은 Airflow가 검증한 버전을 따른다. 설치 직후 `pip check`로 충돌이 없는지 확인한다.
- 이미지에 `.env`·키·덤프·테스트를 넣지 않는다(저장소 루트 `.dockerignore`). 비밀값은 실행할 때 환경 변수로 주입한다. EKS에서는 Kubernetes Secret.
- 빌드 인자 `GIT_SHA`는 환경 변수 `COLLECTOR_BUILD`로 남고, 실행 기록(`collection_run.collector_version`)에 `@<sha 12자리>`로 붙는다.
- 실행 사용자는 이미지 기본값 `airflow`(비루트)다.

### 이미지로 collector 실행

로컬 DB는 컨테이너 안에서 `host.docker.internal`로 접근한다.

```bash
docker run --rm --entrypoint python \
  -e DATABASE_URL="postgresql://postgres:postgres@host.docker.internal:5435/gpu_erd" \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
  gpu-collector-airflow:dev -m gpu_collector.erd collect aws
```

### 이미지로 collector 테스트

```bash
docker run --rm --entrypoint bash -v "$PWD/collector/tests:/tests:ro" \
  -e TEST_DATABASE_URL="postgresql://postgres:postgres@host.docker.internal:5435/postgres" \
  gpu-collector-airflow:dev -c 'pip install -q --user pytest && cd / && python -m pytest -q /tests'
```

## 원칙

- Executor는 LocalExecutor. Redis·Celery는 쓰지 않는다. Airflow 메타 DB(PostgreSQL)는 필요하며 수집 데이터 DB와 분리한다.
- 일일 수집은 Airflow pod 안에서 collector CLI를 실행한다. 대용량 백필만 별도 pod(KubernetesPodOperator)로 실행한다.
- DAG와 collector 패키지는 한 이미지로 함께 배포한다(git-sync로 DAG만 따로 바꾸지 않음).
- 매일 수집 DAG는 `catchup=False`. 스팟은 매일 수집이 마지막 적재 기록 이후를 채우므로 별도 공백 메우기 task가 필요 없다.

## 예정 작업

- 일일 수집 DAG와 로컬 Airflow(docker compose) — 별도 이슈
  - 기반 이미지를 `apache/airflow:slim-3.3.2-python3.12`로 바꾸고 필요한 provider만 추가(postgres, cncf.kubernetes 등). 공식 이미지는 쓰지 않는 provider(Celery·Redis 등)가 많아 무겁다
- 인프라 준비 후: Harbor 푸시, CI 이미지 빌드
