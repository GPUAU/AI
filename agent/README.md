# GPU 시장가격 Dashboard Agent

공개 GPU 가격, 최저가·범위, 사업자·계약 비교, 과거 추이, 가격 변경과 예측을 질문으로 조회하는 Dashboard Agent의 소스와 테스트다. 대화 상태는 Spring 서버가 관리하며, 화면은 `#agent`에서 실행한다.

| 위치 | 담당 범위 |
| --- | --- |
| `backend/src/main/java/com/gpumarket/domain/agent/` | API, 질문 해석, 조회 조건, 세션, Spring 도구 연결, 응답 |
| `backend/src/test/java/com/gpumarket/domain/agent/` | API, 세션, 조회 의도와 도구 테스트 |
| `backend/scripts/evaluate_agent.py` | 공개가격 Agent 기능 평가 30문항 |
| `frontend/docker_erd_20261117/app/web/static/agent/` | 대화, 응답 표·차트, Agent 스타일 |
| `integration/` | 팀 BE·FE와 연결할 때 필요한 공유 코드 변경과 기준 파일 목록 |

Spring의 공개가격·카탈로그 서비스를 호출한다. Agent에 DB 접속 정보나 SQL 실행 도구를 제공하지 않는다. 고객 제안가의 후보가는 해당 모델에 대해 화면 세션에서 완료한 내부 계산이 있을 때 표시한다. 전기료·메모리 가격과 임대료의 시차는 비교 기간에 맞는 외부 시계열이 연결될 때까지 근거 부족으로 안내한다.

## BE·FE에 적용하기

기준은 [GPUAU/BE 1af0bbe](https://github.com/GPUAU/BE/commit/1af0bbe40aabda5dcdb53e0c29cda4cd0e478e45), [GPUAU/FE d492006](https://github.com/GPUAU/FE/commit/d492006a1cf01d7580031facfbb104d4ca0d6faf)이다. 각 체크아웃에서 작업 브랜치를 만든 뒤 AI 저장소 루트에서 Python 3.12로 실행한다. 적용 전 `--check`가 파일과 공유 코드 패치의 호환 여부를 확인한다.

```sh
python3 agent/apply.py backend /path/to/BE --check
python3 agent/apply.py frontend /path/to/FE --check
python3 agent/apply.py backend /path/to/BE
python3 agent/apply.py frontend /path/to/FE
```

BE·FE를 각 저장소의 README에 따라 실행하고 FE의 `/#agent`를 연다. 서버와 브라우저가 사용하는 계약은 `POST /api/v1/agent-messages`와 `DELETE /api/v1/agent-sessions/{sessionId}`다. API 계약을 바꾸는 작업은 요청·응답 예시와 테스트를 함께 갱신한다.

```json
{"message":"B200 온디맨드 가격과 출처 알려줘"}
```

반환된 `result.sessionId`를 후속 요청에 보내면 이전 조건을 이어갈 수 있다. 단순 조회는 조건에 맞는 오퍼별 마지막 공개가격을 `result.results`에 최근 수집순으로 반환한다. 화면은 `sources`의 출처를 각 결과의 `evidenceIds`와 연결한다. 최저가·범위·추이 같은 집계는 `analysis` 또는 `comparison`으로 전달한다.

## 검증

- BE에서 `./gradlew test googleJavaFormatCheck`를 실행한다. PostgreSQL 통합 테스트에는 별도 테스트 DB의 `TEST_POSTGRES_URL`과 `TEST_POSTGRES_USER`를 설정한다.
- FE에서 `node --test tests/*.test.mjs`를 실행한다.
- 실제 수집 데이터가 준비돼 있으면 `python3 agent/backend/scripts/evaluate_agent.py --base-url http://127.0.0.1:18091`로 30문항 기능 평가를 실행한다. 공식 출처·환산의 정확도 평가는 공식 정답표로 별도 실행한다.
