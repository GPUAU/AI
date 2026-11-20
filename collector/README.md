# collector

사업자별 GPU 공개 가격을 수집해 ERD 스키마(PostgreSQL 17)에 적재한다. 스키마(DDL·seed)는 이 컴포넌트가 소유한다.

## 구성

| 경로 | 내용 |
| --- | --- |
| `src/gpu_collector/sql/` | `ddl.sql`(스키마), `seed.sql`(사업자·모델·환산 규칙·출처·수집 대상). 패키지에 함께 설치된다 |
| `src/gpu_collector/erd/` | 수집 명령(`__main__.py`), DB 적재 공통(`db.py`), 수기 상태 기록(`status.py`) |
| `src/gpu_collector/erd/{aws,azure,verda,nebius}/` | 사업자별 매일 수집(`collect.py`)·과거 이력 백필(`history.py`) |
| `src/gpu_collector/erd/oracle/` | Oracle 매일 수집(`collect.py`) |
| `src/gpu_collector/erd/fx/` | 환율 매일 수집·백필(한국수출입은행, `collect.py`) |
| `src/gpu_collector/` | 공통 모듈: HTTP 조회(`fetch`), 파일 캐시(`download`), 비밀값(`env`), AWS 가격 파일 파서(`aws_offer`, `aws_savings`) |
| `tests/erd/` | 사업자별·스키마 테스트. `support.py`가 임시 DB를 만들고 지운다 |
| `scripts/migrate_data.sh` | 예전 스키마 DB의 데이터를 현재 스키마 DB로 옮기기 |
| `.env.example` | 환경 변수 이름 예시 |

## 수집 대상

| 사업자 | 매일 수집 원천 | 과거 이력 |
| --- | --- | --- |
| AWS | Price List Query API, Savings Plans 요율 API, 스팟 가격 변경 기록 API | 가격 파일 과거 버전, 스팟 90일 |
| Azure | Retail Prices API | GitHub 일별 스냅샷 |
| Verda | 공개 API(인스턴스 가격, 약정 할인율) | Wayback 저장본 |
| Nebius | 문서 가격표, 공개 스팟 가격 시계열 API(1시간 간격) | 스팟 30일, 문서 Wayback 저장본 |
| Oracle | 공개 정가 API(GPU 1장·시간 온디맨드, 리전 단일가) | 없음(예정: Wayback 가격 페이지) |
| 환율(`fx`) | 한국수출입은행 현재환율 API(매매기준율) | 같은 API의 과거 영업일 조회 |

**Oracle:** OCI 문서가 안내하는 공개 정가 API에서 `OCI - Compute - GPU - {모델}` 품목(B300·GB300·B200·GB200·H200·H100)의 USD Pay As You Go 단가를 적재한다. GPU 1장·시간 가격이라 GPU 수로 나누지 않고, 리전 구분이 없어 `region_code`는 비운다. 상품은 모델별 베어메탈 구성(`BM.GPU.B300.8` 등)이고 GPU 수·메모리는 Compute Shapes 문서를 사양 근거로 연결한다(상호연결 문구가 없어 미확인). NVIDIA AI Enterprise(라이선스)·H100T 품목은 제외한다. 스팟·약정은 API에 없다.

**환율:** 응답의 통화 전부(22개, KRW 제외)의 매매기준율을 `{통화}→KRW`로 `exchange_rate`에 적재한다. `JPY(100)`·`IDR(100)`은 1단위로 나눠 저장하고, 교차환율(EUR→USD 등)은 저장하지 않고 환산할 때 계산한다. INR은 제공되지 않는다. 영업일 11시 전후에 그날 환율이 나오고 비영업일·갱신 전은 빈 응답이라, 매일 수집은 수집 기준일부터 거슬러 올라가 가장 가까운 영업일 환율을 적재한다(주말은 호출하지 않음). 기준일이 4일 넘게 지나면 `fx_stale`, 실패하면 `fx_failed` 알림을 남긴다. 하루 1,000회 제한이라 백필은 `--max-calls`(기본 900)에서 멈추고, 다시 실행하면 적재하지 않은 날짜부터 이어서 적재한다.

**스팟:** 매일 수집은 마지막으로 적재한 스팟 기록 이후에 바뀐 가격을 모두 받는다(기록 하나 = 관측 하나, 관측 시각 = 기록 시각). 처음 실행이면 AWS는 지금 적용 중인 가격, Nebius는 최근 1일을 받고, 그 전은 backfill로 채운다. 같은 오퍼·같은 시각의 기록은 매일 수집과 backfill을 통틀어 한 번만 적재한다. Nebius의 끝나지 않은 시간 구간 값은 다음 실행에서 확정값으로 적재한다. 며칠 실행하지 못해도 원천 보관 기간(AWS 90일, Nebius 30일) 안이면 다음 실행이 채운다.

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
python -m gpu_collector.erd collect aws           # 매일 수집: aws | azure | verda | nebius | oracle | fx(환율)
python -m gpu_collector.erd collect aws --date 2026-11-20     # 수집 기준일 지정(KST, 생략하면 오늘)
python -m gpu_collector.erd backfill aws --since 2023-07      # 과거 이력 (사업자별 옵션은 --help)
python -m gpu_collector.erd backfill fx --since 2023-07-01    # 과거 환율(날짜는 YYYY-MM-DD, 하루 1,000회 제한)
python -m gpu_collector.erd status record         # 랙스케일·출시 예정 모델 수기 상태 기록
python -m gpu_collector.erd report --model B300   # 최신 가격과 대상별 마지막 점검
```

- 요약 JSON을 표준 출력에 쓴다. AWS 과거 가격 파일은 `CACHE_DIR`(기본 `./var/cache`)에 받고, 처리한 과거 파일은 지운다(`--keep-files`면 남김).

### 재실행·동시 실행

- **같은 날 다시 실행해도 된다.** 매일 수집은 수집 기준일(`--date`, KST) 단위로 중복을 막는다. 그날 이미 적재한 묶음(사업자의 출처·리전 단위)은 원천을 호출하지 않고 건너뛰고(`skipped`), 실패한 묶음만 다시 적재한다.
- **같은 사업자는 동시에 하나만 실행된다.** 같은 사업자의 collect·backfill·specs가 이미 실행 중이면 아무것도 하지 않고 종료 코드 75로 끝난다(DB advisory lock).
- **중단된 실행은 다음 실행이 정리한다.** 강제 종료로 `running`에 남은 실행은 같은 사업자의 다음 실행이 실패(`abandoned`)로 닫는다. SIGTERM을 받으면 진행 중인 묶음을 되돌리고 실행을 실패로 기록한다.
- **일시적 원천 오류는 짧게 다시 시도한다.** 접속 실패·시간 초과와 429·5xx는 최대 3번(2초, 4초 간격, `Retry-After`가 있으면 그만큼) 시도한다. 403 등 거부는 다시 시도하지 않는다.
- **DB 시간 제한:** 접속 10초, 문 하나 10분, 잠금 대기 30초.

| 종료 코드 | 뜻 | 다시 시도 |
| --- | --- | --- |
| 0 | 성공(건너뛴 묶음 포함) | — |
| 1 | 일부 묶음 실패, 예기치 않은 오류, SIGTERM 중단 | 예 |
| 2 | 모든 묶음 실패(원천 장애 등), DB 접속 실패 | 예 |
| 3 | 설정 오류(DATABASE_URL 없음, 잘못된 인자, 수집 대상·기준 정보 없음) | 아니요 |
| 75 | 같은 사업자 실행 중 | 잠시 후 |

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
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | AWS 가격 API 호출(IAM 사용자 `gpu-price-collector`, 가격 조회 권한만). EKS에서는 Kubernetes Secret으로 주입하고 이미지·저장소에 넣지 않는다 |
| `KOREAEXIM_API_KEY` | 한국수출입은행 현재환율 API 인증키(`collect fx`·`backfill fx`). 발급 2년 뒤 파기되므로(등록 이메일로 재동의하면 연장) 종료 코드 3과 `result_3`이 나오면 키 만료를 먼저 확인한다. EKS에서는 Kubernetes Secret으로 주입한다 |
| `CACHE_DIR` | 원천 파일 캐시 위치. 선택, 기본 `./var/cache` |
