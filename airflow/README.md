# airflow

수집기(collector)를 일정에 맞춰 실행하고, 재시도·실패 알림을 맡는다. 수집 로직은 넣지 않고 collector 명령을 실행만 한다.

## 예정 구성

| 경로 | 내용 |
| --- | --- |
| `dags/` | 일일 수집 DAG, 수동 복구 DAG 등 |
| `Dockerfile` | `apache/airflow` 이미지 + collector 패키지 설치 |
| `requirements.txt` | Airflow constraints에 맞춘 추가 의존성 |
| `tests/` | DAG import·구조 테스트 |

## 원칙

- 일일 수집은 Airflow pod 안에서 collector CLI를 실행한다. 대용량 백필만 별도 pod(KubernetesPodOperator)로 실행한다.
- DAG와 collector 패키지는 한 이미지로 함께 배포한다(git-sync로 DAG만 따로 바꾸지 않음).
- 매일 수집 DAG는 `catchup=False`. 놓친 날은 수동 복구 DAG로 채운다.

## 예정 작업

- 일일 수집 DAG와 로컬 Airflow(docker compose 프로필) — 별도 이슈
