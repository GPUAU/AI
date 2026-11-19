# collector

사업자별 GPU 공개 가격을 수집해 ERD 스키마(PostgreSQL 17)에 적재한다. 스키마(DDL·seed)는 이 컴포넌트가 소유한다.

## 구성

| 경로 | 내용 |
| --- | --- |
| `src/gpu_collector/sql/` | `ddl.sql`(스키마), `seed.sql`(사업자·모델·환산 규칙·출처·수집 대상). 패키지에 함께 설치된다 |
| `src/gpu_collector/erd/` | 수집 명령(`__main__.py`), DB 적재 공통(`db.py`), 수기 상태 기록(`status.py`) |
| `src/gpu_collector/erd/{aws,azure,verda,nebius}/` | 사업자별 매일 수집(`collect.py`)·과거 이력 백필(`history.py`) |
| `src/gpu_collector/` | 공통 모듈: HTTP 조회(`fetch`), 파일 캐시(`download`), 비밀값(`env`), AWS 가격 파일 파서(`aws_offer`, `aws_savings`) |
| `tests/erd/` | 사업자별·스키마 테스트. `support.py`가 임시 DB를 만들고 지운다 |
| `scripts/migrate_data.sh` | 예전 스키마 DB의 데이터를 현재 스키마 DB로 옮기기 |
| `.env.example` | 환경 변수 이름 예시 |

## 수집 대상

| 사업자 | 매일 수집 원천 | 과거 이력 |
| --- | --- | --- |
| AWS | Price List Query API, Savings Plans 요율 API, 스팟 가격 API | 가격 파일 과거 버전, 스팟 90일 |
| Azure | Retail Prices API | GitHub 일별 스냅샷 |
| Verda | 공개 API(인스턴스 가격, 약정 할인율) | Wayback 저장본 |
| Nebius | 문서 가격표, 공개 스팟 가격 API | 스팟 30일, 문서 Wayback 저장본 |

## 로컬 실행

루트에서 로컬 DB를 먼저 띄운다(`docker compose up -d`, 루트 README 참고).

```bash
cd collector
uv venv                       # .python-version에 따라 Python 3.12로 만든다
source .venv/bin/activate
uv pip install -e '.[dev]'
cp .env.example .env          # 필요한 값 채우기
set -a; source .env; set +a   # .env 값을 환경 변수로 읽기
```

uv 없이 할 때: `python3.12 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'`

## 명령

```bash
python -m gpu_collector.erd init                  # 빈 DB면 DDL 적용, 이어서 seed 적용
python -m gpu_collector.erd collect aws           # 매일 수집: aws | azure | verda | nebius
python -m gpu_collector.erd backfill aws --since 2023-07      # 과거 이력 (사업자별 옵션은 --help)
python -m gpu_collector.erd status record         # 랙스케일·출시 예정 모델 수기 상태 기록
python -m gpu_collector.erd report --model B300   # 최신 가격과 대상별 마지막 점검
```

- 종료 코드: 성공 0, 실패 1. 요약 JSON을 표준 출력에 쓴다.
- AWS 과거 가격 파일은 `CACHE_DIR`(기본 `./var/cache`)에 받는다. 처리한 과거 파일은 지운다(`--keep-files`면 남김).

## 공용 덤프로 로컬 DB 채우기

기존 적재 데이터는 팀 내부에서 덤프 파일로 공유한다. 덤프 파일은 저장소에 커밋하지 않는다(Azure 원시 데이터 재배포 금지).

| 덤프 | 내용 | sha256 |
| --- | --- | --- |
| `gpu_erd_20261119.sql.gz` | 현재 스키마(상품·오퍼 UNIQUE 포함). AWS 2023-07~, Azure 2025-10~, Verda 2026-08~, Nebius 2026-05~ 과거 이력과 2026-11-19까지 매일 수집분(실행 21건) | `9849c2b054da749083148842b967d20302c5ed3b0e14d2ffcdb2aa43a0fb89ba` |

```bash
shasum -a 256 gpu_erd_20261119.sql.gz                        # 위 값과 같은지 확인
psql "postgresql://postgres:postgres@127.0.0.1:5435/postgres" -c "CREATE DATABASE gpu_erd"   # 비어 있는 DB
gunzip -c gpu_erd_20261119.sql.gz | psql -v ON_ERROR_STOP=1 "$DATABASE_URL"
```

- 2026-11-19 이전에 공유한 덤프는 제약 보강 전 스키마라 쓰지 않는다.
- 이미 `gpu_erd`가 있으면 `DROP DATABASE gpu_erd WITH (FORCE)` 후 다시 만든다.

### 예전 스키마 DB를 현재 스키마로 옮기기

예전 스키마로 쌓인 DB가 있으면 데이터만 현재 스키마 DB로 옮긴다. 대상은 비어 있는 DB여야 한다.

```bash
scripts/migrate_data.sh <원본 DB URL> <대상 DB URL>
```

DDL만 적용한 뒤 기준 정보까지 원본 ID 그대로 옮기고(seed는 적용하지 않음), 테이블 건수와 identity 시퀀스를 원본과 비교한다.

## 테스트

```bash
pytest
```

- `TEST_DATABASE_URL`이 없으면 DB를 쓰는 테스트는 건너뛴다(skip).
- 테스트는 네트워크와 AWS 키를 쓰지 않는다. 원천 응답은 합성 문서와 가짜 함수로 대신한다.

## 환경 변수

| 이름 | 용도 |
| --- | --- |
| `DATABASE_URL` | 실제 수집 데이터를 적재하는 DB(`gpu_erd`). collect·backfill 명령이 쓴다. 필수 |
| `TEST_DATABASE_URL` | 테스트가 접속하는 관리용 DB(`postgres`). 여기서 임시 DB를 만들어 시험하고 지운다. 실제 데이터 DB를 건드리지 않도록 따로 둔다 |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | 로컬에서 AWS 가격 API 호출. EKS에서는 IRSA·Pod Identity 사용 |
| `CACHE_DIR` | 원천 파일 캐시 위치. 선택, 기본 `./var/cache` |
