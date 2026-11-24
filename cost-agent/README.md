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

**원칙**: 숫자는 코드가 읽고 BE가 계산한다. LLM은 분류와 설명만 맡는다. 같은 입력이면 같은 결과가 나와야 한다.

## 3. API

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET | `/health` | 상태, 사용 중인 매퍼 |
| POST | `/v1/bills` | `file`(xlsx/csv) 업로드 → 매핑 초안(`BillDraft`) |
| GET | `/v1/bills/{id}` | 초안 조회 |
| PUT | `/v1/bills/{id}/lines` | `{"corrections":[{"ref":"상세내역!6","item_type":"OPERATIONS"}],"save_template":true}` |
| POST | `/v1/bills/{id}/simulate` | `{"model_id":"7","gpu_count":64,"acquisition_amount":2000000,"fx_krw_per_usd":1400}` |
| GET | `/v1/templates` | 저장된 양식 템플릿 |

`BillDraft` 주요 필드: `lines`(줄별 분류·금액·확신도·근거), `reconciliation`(총액 대조), `cost_items`(BE로 보낼 항목), `issues`(ERROR/WARNING/INFO), `needs_review`.

## 4. 실행

```bash
cd cost-agent
uv venv && uv pip install -e ".[dev]"
cp .env.example .env            # OPENAI_API_KEY는 .env에만. 없으면 규칙 매퍼로 동작
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

## 5. 샘플 (가상 데이터)

| 파일 | 확인하는 것 |
| --- | --- |
| `bill_a_domestic_krw.xlsx` | 국내 IDC 기본 양식, 소계·부가세·합계 대조 |
| `bill_b_colo_usd.xlsx` | 영어 양식, 코로케이션에 전력 포함(묶음) |
| `bill_c_multisheet_krw.xlsx` | 요약/상세 2개 시트, 낯선 항목명, 할인, 1회성 비용, 운영비 누락 경고 |

실제 사업자 양식·금액이 아니다.

## 6. 한계와 다음 단계

- 초안은 메모리에 저장한다 (Pod 재시작 시 사라짐). 템플릿은 `TEMPLATE_DIR` 파일 → 운영 시 PVC 또는 DB로 옮긴다.
- 할인 줄은 원가에서 빼지 않는다 (보수적). 계약 할인 반영은 담당자 판단.
- 월 청구서 1장을 ×12로 연간 추정한다. 여러 달 청구서 평균은 다음 단계.
- 수식 셀은 엑셀에 저장된 결과값으로 읽는다. 결과값이 저장되지 않은 파일(일부 생성 도구)은 금액이 비어 보일 수 있다.
- 다음 단계: 견적서·보고서 엑셀 내보내기, 경쟁사 가격 변동 → 제안가 영향 알림, BE `agent-messages`와 연결.
