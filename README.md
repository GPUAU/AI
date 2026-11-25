# GPUAU AI

GPU 공개 가격을 수집·적재하고, 그 데이터를 AI Agent로 조회하는 레포다.
컴포넌트마다 의존성과 실행 환경을 따로 가진다. AI Agent(`agent/`)는 이후 추가한다.

| 폴더 | 역할 | 배포 단위 |
| --- | --- | --- |
| [`collector/`](collector/) | 사업자별 공개 가격 수집, ERD 스키마(DDL·seed) 소유, DB 적재 | Airflow 이미지에 패키지로 설치 |
| [`airflow/`](airflow/) | 수집 일정·재시도·알림 (DAG) | Airflow 이미지 |
| [`docs/`](docs/) | ERD 기준 문서, 수집 원천·이용 조건 | — |
| [`cost-agent/`](cost-agent/) | IDC 청구서 엑셀 → 원가 항목 AI 매핑·검증 → BE 원가 계산 API 연동 (FastAPI) | 자체 이미지 |

## 컴포넌트 규칙

- **가상환경·의존성 파일은 컴포넌트마다 따로 둔다.** Airflow는 constraints로 패키지 버전을 고정하므로 다른 컴포넌트 의존성과 섞지 않는다.
- **컴포넌트끼리 코드를 import하지 않는다.** 이후 추가할 Agent도 collector와 DB로만 연결하고, 읽기 전용 계정으로 조회용 View만 읽는다.
- **스키마는 collector가 소유한다(`collector/src/gpu_collector/sql/`).** 설계 문서는 [`docs/erd.md`](docs/erd.md)이고 DDL과 같이 고친다.
- **비밀값은 컴포넌트별 `.env`에 둔다.** 저장소에는 `.env.example`(이름만)만 커밋한다. 배포 환경에서는 Kubernetes Secret 등으로 주입하고 이미지에 넣지 않는다.

## 로컬 개발 환경

필요한 것: Docker(Compose 포함), Python 3.12, [uv](https://docs.astral.sh/uv/)(권장)

### 1. 로컬 DB 실행

```bash
docker compose up -d
docker compose ps        # db 상태가 healthy인지 확인
```

- PostgreSQL 17, `127.0.0.1:5435`, DB `gpu_erd`, 계정 `postgres`/`postgres`(로컬 전용)
- 맥에 설치된 PostgreSQL(5432)과 겹치지 않도록 5435 포트를 쓴다.
- 중지: `docker compose down` (데이터 유지) / 초기화: `docker compose down -v` (데이터 삭제)

### 2. 컴포넌트별 실행

각 폴더의 README를 따른다.

- [collector](collector/README.md)
- [airflow](airflow/README.md)
- [cost-agent](cost-agent/README.md)

## 브랜치·이슈 규칙

- 이슈 제목: `<이모지> [유형] 제목` (유형: Feature, Bug, Refactor, Setting, Deploy, Docs)
- 브랜치: 이슈 화면의 Development → Create a branch로 `develop`에서 만든다. 이름은 `<유형>/<기능-케밥>#<이슈번호>`
- PR: base는 `develop`, 본문에 `Closes #<이슈번호>`와 검증 결과를 적는다.
