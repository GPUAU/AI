# cost-agent — IDC 청구서 원가 매핑 에이전트

담당자가 IDC 청구서 엑셀을 보고 원가 항목을 손으로 옮겨 적던 일을 대신한다.
양식이 사업자마다 달라도 **AI가 각 줄을 원가 항목에 매핑**하고, **코드가 총액·누락을 검증**한 뒤,
**GPUAU/BE 원가 계산 API**로 제안가까지 계산한다.

> 1차 범위(MVP): 청구서 매핑 → 검증 → 담당자 확인 → 템플릿 저장 → BE 원가 계산.
> 견적서 내보내기, 가격 변동 알림은 다음 단계.

## 1. 왜 AI가 필요한가

양식이 하나뿐이면 규칙 파서로 충분하다. AI가 필요한 이유는 **양식을 미리 알 수 없기 때문**이다.

| 문제 | 예 |
| --- | --- |
| 같은 항목인데 이름이 다름 | 전력 사용료 · 동력비 · Power · kWh 요금 |
| 표 구조가 다름 | 시트 여러 개, 요약/상세 분리, 항목이 행 또는 열 |
| 한 줄에 여러 항목이 묶임 | `Colocation - Power 40kW included` |
| 새 양식이 계속 생김 | 사업자 추가, 양식 개편 |

그래서 **처음 보는 양식만 AI**가 매핑하고, 담당자가 확인한 결과는 **템플릿**으로 저장해 다음부터는 AI 없이 처리한다.

## 2. 처리 흐름

```
엑셀 업로드 (POST /v1/bills)
  │
  ├─ 양식 지문(fingerprint)으로 저장된 템플릿 조회
  │     있음 → 템플릿 적용 (AI 호출 없음)
  │     없음 → LLM 매퍼 (키 없거나 실패 → 키워드 규칙 매퍼)
  │
  ├─ 금액 읽기: LLM은 "몇 번째 칸이 금액인지"만 답하고, 숫자는 코드가 그 칸에서 읽는다
  │
  ├─ 코드 검증
  │     · 총액 대조: 원가 합계 (− 할인) (+ 부가세) = 청구 총액/공급가액
  │     · 큰 항목 누락: 전력 · 공간·상면 · 네트워크 · 운영·유지보수
  │     · 확신도 낮은 줄, '기타' 비중 과다, 금액 없는 줄
  │
  ├─ BE 형식으로 합치기
  │     · 같은 항목 합산 (BE는 항목 중복 불가)
  │     · 월 청구 ×12 → 연간 (BE 기본 프로필은 MONTH 불가)
  │     · 1회성 비용 → 취득비(ACQUISITION)
  │     · 묶인 항목 → INCLUDED_IN_OTHER
  │
담당자 확인·수정 (PUT /v1/bills/{id}/lines) → 오류가 없으면 템플릿 저장
  │
원가 계산 (POST /v1/bills/{id}/simulate) → BE /api/v1/cost-simulations
      청구서에 없는 값(GPU 수, 취득비, 감가상각, 가동률, 마진)은 담당자 입력
```

**민감정보 보호**: LLM에는 항목명만 보낸다. 금액 칸은 `#NUM`, 글자 속 숫자는 `#`, 이메일·URL·`SENSITIVE_TERMS`는 가리고 파일명은 보내지 않는다.
청구 기간(월/연)은 가리기 전 원문에서 코드가 판단한다. 실제로 보낸 내용은 응답의 `llm.sent`로 담당자에게 그대로 보여 준다.
외부 전송 자체가 안 되면 `OPENAI_BASE_URL`로 사내 sLLM을 쓰거나, 확정 템플릿만으로 처리한다.

**원칙**: 숫자는 코드가 읽고 BE가 계산한다. LLM은 분류와 설명만 맡는다. 같은 입력이면 같은 결과가 나와야 한다.

## 3. 원가 에이전트 (대화형, `/v1/agent`)

목표 "청구서 한 장 → 근거 있는 제안가와 견적 근거서"를 향해 단계를 진행하고, 사람만 정할 수 있는 것은 질문 카드로 묻는다.
다음 단계는 코드(상태기계)가 정하고, LLM은 분류·재분류에만 쓴다. 숫자는 BE만 계산한다.

| 단계 | 에이전트가 하는 일 | 묻는 것 |
| --- | --- | --- |
| 청구서 분석 | 분류(숫자 가림) → 총액 대조 → 안 맞으면 원인을 주고 재분류(최대 2회) | 그래도 안 맞으면 진행/중단 |
| 누락 확인 | 빠진 큰 비용 탐지 | 별도 금액 / 다른 요금에 포함 / 제외 |
| 가정값 | 근거 붙은 제안값 (보고서·BE 환율) | 확인·수정 |
| 원가 계산 | BE `/cost-simulations` | 실패 시 입력 유지하고 재질문 |
| 시장 비교 | BE `/market/offers` → 사업자별 대표값으로 최저·중앙·최고 | 범위 밖이면 중앙값 맞춤(마진 재계산) 또는 가정값 재확인 |
| 기간별 가격 | 3·5년 계약가 | 할인율 |
| 근거서 | 엑셀 (요약·원가 항목·가정값과 근거·시장 비교·진행 기록) | — |

`POST /v1/agent/sessions`(file, model_id, gpu_count, terms) → `POST /v1/agent/sessions/{id}/answer`(question_id, choice, values) 반복
→ `GET /v1/agent/sessions/{id}/report.xlsx`. 세션은 메모리에만 둔다.

## 4. API (단건)

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET | `/health` | 상태, 사용 중인 매퍼 |
| POST | `/v1/bills` | `file`(xlsx/csv) 업로드 → 매핑 초안(`BillDraft`) |
| GET | `/v1/bills/{id}` | 초안 조회 |
| PUT | `/v1/bills/{id}/lines` | `{"corrections":[{"ref":"상세내역!6","item_type":"OPERATIONS"}],"save_template":true}` |
| POST | `/v1/bills/{id}/simulate` | `{"model_id":"7","gpu_count":64,"acquisition_amount":2000000,"fx_krw_per_usd":1400}` |
| GET | `/v1/templates` | 저장된 양식 템플릿 |

`BillDraft` 주요 필드: `lines`(줄별 분류·금액·확신도·근거), `reconciliation`(총액 대조), `cost_items`(BE로 보낼 항목), `issues`(ERROR/WARNING/INFO), `needs_review`.

## 5. 실행

```bash
cd cost-agent
uv venv && uv pip install -e ".[dev]"
cp .env.example .env            # OPENAI_API_KEY는 .env에만 (Git 제외). 앱이 자동으로 읽고, 이미 있는 환경변수는 덮어쓰지 않음
python scripts/make_samples.py  # 가상 청구서 3종 → samples/
python -m app.cli samples/bill_b_colo_usd.xlsx            # 터미널에서 매핑 결과 보기
uvicorn app.main:create_app --factory --reload --port 8000  # API (http://localhost:8000/docs)
pytest
```

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `OPENAI_API_KEY` | (없음) | 있으면 LLM 매퍼 사용 |
| `OPENAI_MODEL` | `gpt-4o-mini` | Structured Outputs(json_schema) 지원 모델 |
| `MAPPER` | `auto` | `auto` / `openai` / `rules` |
| `SPRING_URL` | `http://backend` | BE 서비스 주소 (클러스터 안) |
| `TEMPLATE_DIR` | `./data/templates` | 확정 템플릿 저장 위치 |
| `MAX_UPLOAD_MB` | `5` | 업로드 크기 제한 |
| `LLM_MASK` | `true` | LLM에 보내기 전 금액·숫자·식별자 가림. 끄면 원문 전송 |
| `SENSITIVE_TERMS` | (없음) | 추가로 가릴 고객·사업자·프로젝트명 (쉼표 구분) |
| `OPENAI_BASE_URL` | (없음) | 사내 sLLM(vLLM·Ollama 등 OpenAI 호환) 주소. 설정하면 외부 OpenAI로 보내지 않음 |

## 6. 샘플 (가상 데이터)

| 파일 | 확인하는 것 |
| --- | --- |
| `bill_a_domestic_krw.xlsx` | 국내 IDC 기본 양식, 소계·부가세·합계 대조 |
| `bill_b_colo_usd.xlsx` | 영어 양식, 코로케이션에 전력 포함(묶음) |
| `bill_c_multisheet_krw.xlsx` | 요약/상세 2개 시트, 낯선 항목명, 할인, 1회성 비용, 운영비 누락 경고 |

실제 사업자 양식·금액이 아니다.

## 7. 한계와 다음 단계

- 초안은 메모리에 저장한다 (Pod 재시작 시 사라짐). 템플릿은 `TEMPLATE_DIR` 파일 → 운영 시 PVC 또는 DB로 옮긴다.
- 할인 줄은 원가에서 빼지 않는다 (보수적). 계약 할인 반영은 담당자 판단.
- 상면(RACK_FACILITY)과 공간 임대(RENT) 중 한쪽만 청구되면 다른 쪽을 `INCLUDED_IN_OTHER`로 보낸다 (BE 누락 판정 방지).
- 금융비용(FINANCING)은 청구서에 없으므로 BE 결과가 `INCOMPLETE`·`FINANCING_NOT_INCLUDED`로 나온다. 금리 입력은 다음 단계.
- 월 청구서 1장을 ×12로 연간 추정한다. 여러 달 청구서 평균은 다음 단계.
- 수식 셀은 엑셀에 저장된 결과값으로 읽는다. 결과값이 저장되지 않은 파일(일부 생성 도구)은 금액이 비어 보일 수 있다.
- 다음 단계: 견적서·보고서 엑셀 내보내기, 경쟁사 가격 변동 → 제안가 영향 알림, BE `agent-messages`와 연결.

## 8. 배포 (수동 · Harbor, 2026-11-24 테스트 배포)

CI(deploy workflow)는 아직 없다. AI 저장소용 self-hosted runner가 없어서, 캠퍼스 PC에서 Harbor 로봇 계정으로 push하고 kubectl로 적용했다.

```bash
docker login harbor.skala-us.com -u 'robot$u1-3+ci'          # 비밀번호는 팀 가이드 페이지
docker buildx build --platform linux/amd64 -t harbor.skala-us.com/u1-3/cost-agent:<sha> --push cost-agent
# Secret(최초 1회): cost-agent-openai / 키 OPENAI_API_KEY  — 값은 Git·채팅에 남기지 않는다
sed "s|IMAGE_PLACEHOLDER|harbor.skala-us.com/u1-3/cost-agent:<sha>|" cost-agent/k8s/cost-agent.yaml | kubectl apply -f -
kubectl -n u1-3 rollout status deployment/cost-agent
```

FE nginx의 `/agent/`가 `http://cost-agent.u1-3.svc.cluster.local:8000`으로 넘긴다 (GPUAU/FE PR #18).
