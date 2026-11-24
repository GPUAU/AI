# GPU 시장가격 Dashboard Agent

공개 GPU 가격, 최저가·범위, 사업자·계약 비교, 과거 추이, 가격 변경과 예측을 질문으로 조회하는 Dashboard Agent의 소스와 테스트다. 대화 상태는 Spring 서버가 관리하며, 화면은 `#agent`에서 실행한다.

| 위치 | 담당 범위 |
| --- | --- |
| `backend/src/main/java/com/gpumarket/domain/agent/` | API, 질문 해석, 조회 조건, 세션, Spring 도구 연결, 응답 |
| `backend/src/test/java/com/gpumarket/domain/agent/` | API, 세션, 조회 의도와 도구 테스트 |
| `backend/scripts/evaluate_agent.py` | Agent 30문항 기능·정확도 평가 |
| `backend/scripts/agent_v4_official_key.json` | 2026-11-24 KST 기준의 공식 가격·단위·출처와 기대 동작 |
| `frontend/docker_erd_20261117/app/web/static/agent/` | 대화, 응답 표·차트, Agent 스타일 |
| `integration/` | 팀 BE·FE와 연결할 때 필요한 공유 코드 변경과 기준 파일 목록 |
| `evaluation/mentoring-review.html` | 질문, 기대답변, 실제 답변과 메모를 기록하는 브라우저용 검토 화면 |

Spring의 공개가격·카탈로그 서비스를 호출한다. 최저가와 가격 범위는 상품별 마지막 공개 관측을 사용하며, 근거를 확인할 수 있는 사업자 공개 가격표와 계산에 사용한 오퍼 목록을 응답 카드에서 확인할 수 있다. 고객 제안가 후보는 같은 모델의 내부 계산 결과를 화면 세션에서 읽어 표시한다. 전력가격과 임대료는 주 단위, DRAM 수출물가지수와 임대료는 월 단위로 비교한다. 표본이 부족하면 근거 부족과 조회 범위를 안내한다.

[전력·DRAM 데이터 원천과 적재](DATA_SOURCES.md)에 입력 파일의 공식 링크·SHA-256과 ECOS 조회 기간을 기록했다. 전력 및 DRAM 적재 변경은 각각 [전력 적재 PR #44](https://github.com/GPUAU/AI/pull/44)와 [DRAM 적재 PR #46](https://github.com/GPUAU/AI/pull/46)에서 리뷰한다. 과거 덤프를 복원한 환경에서는 Spring 백엔드가 보는 동일한 DB에 두 테이블을 적재한다.

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

최저가·가격 범위 응답의 `result.analysis.comparedOffers`에는 같은 집계에 사용한 오퍼가 단가 오름차순으로 포함된다. 반환된 `result.sessionId`를 후속 요청에 보내면 이전 조건을 이어갈 수 있다. 단순 조회는 조건에 맞는 오퍼별 마지막 공개가격을 `result.results`에 최근 수집순으로 반환한다. 화면은 `sources`의 출처를 각 결과의 `evidenceIds`와 연결한다. 최저가·범위·추이 같은 집계는 `analysis` 또는 `comparison`으로 전달한다.

## 검증

- BE에서 `./gradlew test googleJavaFormatCheck`를 실행한다. PostgreSQL 통합 테스트에는 별도 테스트 DB의 `TEST_POSTGRES_URL`과 `TEST_POSTGRES_USER`를 설정한다.
- FE에서 `node --test tests/*.test.mjs`를 실행한다.
- 실제 수집 데이터가 준비돼 있으면 `python3 agent/backend/scripts/evaluate_agent.py --base-url http://127.0.0.1:18091`로 30문항 기능 평가를 실행한다.

복원 스냅샷에서 30문항의 응답과 원천 가격을 평가하려면 AI 저장소 루트에서 다음 명령을 실행한다.

```sh
python3 agent/backend/scripts/evaluate_agent.py \
  --base-url http://127.0.0.1:18091 \
  --official-key agent/backend/scripts/agent_v4_official_key.json \
  --output /tmp/gpu-agent-v4-official-report.json
```

`officialPassed`는 문항에 지정된 조건, 계산값, 출처 또는 기대 동작을 모두 충족한 수다. 30문항 중 29문항 이상을 통과해야 한다. `independentPrices`는 관측 상세 API의 원천 통화·단가·단위·출처와 독립 환산값을 각각 채점한다. 정답표에 기록한 AWS·Nebius 요율은 공급사 원천에서 확인했다. 11월 18일 Verda의 리전별 요율은 당시 원본이 없어 공식 표본에서 제외했다. 최저가 문항은 복원된 수집 단가 37개에서 독립적으로 순위를 재계산하고 최저가의 출처를 별도로 대조한다.

정답표는 복원 DB의 오퍼·관측 ID와 2026-11-24 KST 기준 상대 날짜를 사용한다. 평가 날짜나 DB가 달라지면 해당 기대값을 원천 자료와 함께 갱신한다. 예측 문항은 오퍼 재질문과 이력 부족 처리를 확인하며, 모델 오차는 별도 평가 대상이다.


멘토링 검토 화면은 파일을 브라우저에서 열고 질문별 기대답변, 실제 응답, 판정과 메모를 기록한다. 브라우저 자동 저장과 JSON 내보내기·가져오기를 지원한다.
