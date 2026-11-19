# collector

사업자별 GPU 공개 가격을 수집해 ERD 스키마(PostgreSQL 17)에 적재한다. 스키마(DDL·seed)는 이 컴포넌트의 `sql/`이 소유한다.

## 구성

| 경로 | 내용 |
| --- | --- |
| `src/gpu_collector/` | 수집기 패키지 |
| `sql/` | DDL·seed |
| `tests/` | 테스트 |
| `.env.example` | 환경 변수 이름 예시 |

## 로컬 실행

루트에서 로컬 DB를 먼저 띄운다(`docker compose up -d`, 루트 README 참고).

```bash
cd collector
uv venv                       # .python-version에 따라 Python 3.12로 만든다
source .venv/bin/activate
uv pip install -e '.[dev]'
cp .env.example .env          # 필요한 값 채우기
```

uv 없이 할 때: `python3.12 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'`

## 테스트

```bash
set -a; source .env; set +a   # .env 값을 환경 변수로 읽기
pytest
```

- `TEST_DATABASE_URL`이 없으면 DB 접속 테스트는 건너뛴다(skip).
- 테스트 DB 접속은 환경 변수로만 받는다. 코드에 접속 정보를 쓰지 않는다.

## 환경 변수

| 이름 | 용도 |
| --- | --- |
| `DATABASE_URL` | 실제 수집 데이터를 적재하는 DB(`gpu_erd`). collect·backfill 명령이 쓴다 |
| `TEST_DATABASE_URL` | 테스트가 접속하는 관리용 DB(`postgres`). 여기서 임시 DB를 만들어 시험하고 지운다. 실제 데이터 DB를 건드리지 않도록 따로 둔다 |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | 로컬에서 AWS 가격 API 호출. EKS에서는 IRSA·Pod Identity 사용 |
