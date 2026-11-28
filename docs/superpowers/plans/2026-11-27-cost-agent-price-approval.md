# 원가 에이전트 2순위 — 가격 승인 검토와 보고서 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 원가 계산 뒤에 승인 검토 단계(원가 분해·손익분기·민감도·업계 비교·규칙 판정)를 더하고, 자동 검증을 통과한 결과만 C-level용 한 장 보고서(HTML)와 엑셀 근거서로 내보낸다. 누락 확인은 카드 한 장으로 묶고, 가정값에 취득비 예시값·금리·출처 등급을 더한다.

**Architecture:** 계산은 새 순수 모듈 `app/agent/approval.py`(분해·손익분기·민감도·판정)와 `app/agent/verify.py`(보고서 검증)에 둔다. 에이전트는 두 엔진(workflow·graph) 모두 `Step.REVIEW`(TERMS와 REPORT 사이)에서 `review_price`를 실행하고, 발견 사항이 있으면 `ask_user(review)` 카드로 고치기/사유 진행을 받는다. 보고서는 `app/agent/report_html.py`의 코드 템플릿으로 만든다. LLM은 지금처럼 다음 도구만 고른다.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, openpyxl, LangGraph, pytest (`cd cost-agent && .venv/bin/python -m pytest -q`) · FE: vanilla JS, `node --test`

**Spec:** `docs/superpowers/specs/2026-11-26-cost-agent-price-approval-design.md` §4 ②~⑧ + §4B (충돌하면 §4B 우선), §6, §7

## Global Constraints

- 작업 브랜치: `feat/bill-interpretation`(PR #73)에서 `feat/price-approval`을 만든다. #73이 먼저 merge되면 `develop`으로 rebase.
- 숫자는 코드와 BE가 계산한다. 판단자 LLM에는 발견 사항·판정의 **코드와 개수만** 보낸다 (`_analysis_text`·`_answer_text`·`_status`). 보고서 문장은 코드 템플릿, LLM 서술 없음.
- enum `LineRole`·`Period`·`ItemType` 값은 늘리지 않는다. `Step`에는 `REVIEW`를 추가한다 (FE `STEP_ORDER`도 함께, Task 10).
- 판정 임계값: 분해 대조 0.5%, 제안가 공식 0.1%, 손익분기 여유 10%p, 업계 구성비 0.5~2배, 시장 표본 사업자 3곳.
- 업계 구성비 기준(현대차 표 18): 감가 36% · 공간 34% · 전력 16% · 운영 14%. 묶음: 공간=RACK_FACILITY+RENT, 전력=ELECTRICITY, 운영=OPERATIONS+NETWORK_STORAGE+MEMORY+OTHER+GPU, 감가=ACQUISITION 감가. 금융비용은 비교에서 뺀다.
- 취득비 예시값 `GPU 수 × $38,000` (표 18 B). 금리 종류 선택지: `NOT_APPLIED`(기본)·`ASSUMED`·`ACTUAL_LOAN`. `PUBLIC_BASE`는 이번 범위 밖.
- 기존 136+12개 테스트와 a~i·r1~r5 동작: 질문 **순서**는 누락 카드가 한 장으로 합쳐지는 것 말고 바뀌지 않는다. 바뀌는 기존 테스트는 그 이유를 ledger에 Ruling으로 남긴다.

## Review Focus

1. **금리를 보냈는데 배포 BE가 반영하지 않음** (`interestWasProvided: false`) → 금융비용 미반영으로 처리, 분해에서 금융 묶음 0, 리스크에 표시. (Task 3 `test_interest_not_applied_by_be_is_reported`)
2. **원가 가산율(COST_MARKUP) 방식** → 제안가 공식 검증과 손익분기가 같은 식으로 맞아야 한다. (Task 7 `test_cost_markup_formula_verified`)
3. **시장 표본이 없음** (공개가 0개) → 시장 위치·시장 이탈 판정은 건너뛰고 "시장 비교 불가"를 조건부 사유로. 검증의 시장 통계 검사는 생략(통과로 셈). (Task 5 `test_no_market_samples_is_conditional`)
4. **담당자가 발견 사항에 '고치기'를 고른 뒤 값을 바꿈** → 원가·시장·검토를 다시 하고, 새 검토 결과로 카드를 다시 띄운다 (이전 사유는 같은 코드면 유지). (Task 6 `test_fix_reruns_review`)
5. **장기계약가가 원가보다 낮음** (할인율 과대) → 재검토, 보고서에 손익분기 > 100% 표시. (Task 5 `test_term_below_cost_is_review`)

---

### Task 1: 누락 확인 카드 한 장 + 비고 읽기 + 제안 금액

**Files:**
- Modify: `cost-agent/app/agent/orchestrator.py` (`_step_missing`, `_q_missing`, `_answer_missing`, `_missing_text`, `_finish_missing`)
- Modify: `cost-agent/app/agent/graph.py` (`_answer_text`의 MISSING, `_tool_ask_user(missing)`)
- Modify: `cost-agent/app/benchmarks.py` (`missing_suggestion`)
- Modify: `cost-agent/scripts/eval_agent.py` (`auto_answer`)
- Test: `cost-agent/tests/test_agent.py`

**Interfaces:**
- Produces: `benchmarks.missing_suggestion(group: str, currency: Currency | None, contract: ContractInfo) -> float | None` — 월 금액 예시값. 전력 `173_667 × kW`, 공간·상면 `263_000 × kW`, 네트워크·운영 `1_800_000_000 / 4 / 12 × kW/1000`. 달러면 ÷1450, 원화 만원 단위·달러 10달러 단위 반올림. kW 없으면 None.
- Produces: `CostAgent._note_hint(s, group) -> str | None` — `s.draft`의 원문 행(서비스가 들고 있는 `doc`)에서 `직접 계약|별도 계약|별도 청구|고객사 부담`과 그룹 단어(전력: 전력·전기·power / 공간·상면: 상면·랙·공간 / 네트워크: 회선·네트워크·인터넷·ix·cross connect / 운영: 운영·관제·유지보수)가 같은 행에 있으면 그 행의 `ref`와 앞 40자 반환.
- 카드 `_q_missing(s) -> Question` (그룹 목록 전체): title `청구서에 없는 큰 비용이 {n}개 있어요`, fields — 그룹마다
  `choice:{그룹}` select(`amount` 별도 계약 금액 / `included` 다른 요금에 포함 / `exclude` 이번엔 제외; 기본 `amount`면 비고 hint, 아니면 `included`가 가능할 때 `included`, 아니면 `amount`),
  `amount:{그룹}` number(value = `missing_suggestion`, `show_for=["amount"]`), `currency:{그룹}`, `period:{그룹}`(기본 MONTH), `parent:{그룹}`(`show_for=["included"]`).
  `show_for`는 같은 그룹의 choice 칸을 가리키도록 FE가 이름 접두사로 묶는다 (Task 10).
- `_answer_missing`: 그룹마다 지금과 같은 `rec`을 만들어 `s.missing_answers[그룹]`에 넣는다. 검증 오류(금액 숫자 아님·음수·포함 요금 없음)는 `AgentError`로 같은 카드 유지.
- graph `_answer_text(MISSING)`: `'전력'·'네트워크'는 별도 계약, '운영'은 포함`처럼 그룹명과 선택만.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_missing_groups_in_one_card(Agent, tmp_path, detailed_doc):  # r4: 네트워크만 빠짐 + 비고 '직접 계약'
    agent, s = start(Agent, tmp_path, detailed_doc("bill_r4_prorated_first_month_krw.xlsx"))
    s = run_to(agent, s, Step.MISSING)
    f = {x.name: x for x in s.question.fields}
    assert s.question.title == "청구서에 없는 큰 비용이 1개 있어요"
    assert f["choice:네트워크"].value == "amount" and "직접 계약" in f["choice:네트워크"].hint
    assert f["amount:네트워크"].value == 12_000_000  # 18억/4/12 × 0.32MW, 만원 반올림
def test_missing_card_answers_all_groups(Agent, tmp_path, doc): ...  # 기존 c 흐름: choice:운영·유지보수=amount, amount=1,000,000 → BE 요청 OPERATIONS 12,000,000 (기존 단언 유지)
def test_missing_card_rejects_bad_amount(Agent, tmp_path, doc): ...  # amount:운영·유지보수="abc" → AgentError, 같은 카드
```
기존 `test_full_flow_with_questions_market_loop_and_report`의 누락 단언(제목 `'운영·유지보수'`, options 3개)은 새 카드 형식으로 바꾼다.

- [ ] **Step 2: FAIL 확인** · **Step 3: 구현** (eval `auto_answer`의 MISSING은 모든 `choice:*`를 `amount`, 금액 3,000,000 KRW 월로) · **Step 4: 전체 PASS**
- [ ] **Step 5: Commit** — `feat: 누락 확인을 카드 한 장으로, 비고 읽기와 제안 금액`

---

### Task 2: 가정값 — 취득비 예시값, 금리, 출처 등급

**Files:**
- Modify: `cost-agent/app/models.py` (`SimulationInput.interest_rate_type: str = "NOT_APPLIED"`, `annual_interest_rate: float | None = None`)
- Modify: `cost-agent/app/be_client.py` (`build_request`)
- Modify: `cost-agent/app/agent/orchestrator.py` (`_q_assumptions`, `_answer_assumptions`, `_do_simulate`)
- Create: `cost-agent/app/agent/sources.py`
- Test: `cost-agent/tests/test_backend_and_api.py`, `cost-agent/tests/test_agent.py`

**Interfaces:**
- `build_request`: `interest_rate_type != "NOT_APPLIED"`이면 `items`에 `{"itemType":"FINANCING","status":"USER_INPUT"}` 추가, `assumptions.interestRateType`, `assumptions.annualInterestRate`(문자열 아닌 float). 아니면 지금과 같음 (기존 요청 본문 그대로).
- 가정값 카드: `acquisition_amount.value = gpu_count × 38_000`(USD), hint `예시값 · 현대차증권 표 18 GPU 단가 $38,000 · 견적서 값으로 바꾸세요`. `interest_rate_type` select(`NOT_APPLIED` 적용 안 함 / `ASSUMED` 가정 / `ACTUAL_LOAN` 실제 대출, 기본 NOT_APPLIED), `annual_interest_rate`(%, `required=False`, `show_for=["ASSUMED","ACTUAL_LOAN"]`). 적용 시 금리 빈칸·음수 → `AgentError("연 금리를 0 이상의 숫자로 입력하세요.")`.
- `sources.py`: `Grade = Literal["공개자료","리포트 가정","예시값","담당자 입력"]`, `grade_of(name: str, source_text: str) -> Grade` — 출처 문구가 `"담당자 입력"`이면 담당자 입력, `수출입은행`이면 공개자료, `예시값`이 있으면 예시값, `보고서|리포트|표 18|미래에셋|현대차`면 리포트 가정, 그 밖은 예시값. `_answer_assumptions`가 `a["grades"] = {name: grade_of(...)}` 저장.
- `_do_simulate`: `SimulationInput`에 금리 두 값 전달.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_financing_item_sent_when_interest_assumed():
    req = build_request([], SimulationInput(model_id="29", gpu_count=8, acquisition_amount=1e5,
                                            interest_rate_type="ASSUMED", annual_interest_rate=0.05))
    assert {"itemType": "FINANCING", "status": "USER_INPUT"} in req["items"]
    assert req["assumptions"]["interestRateType"] == "ASSUMED" and req["assumptions"]["annualInterestRate"] == 0.05
def test_not_applied_request_unchanged(): ...   # 기본값 → FINANCING 없음, NOT_APPLIED
def test_acquisition_example_and_grades(Agent, tmp_path, detailed_doc):
    # r1 gpu 504 → acquisition_amount 제안값 19,152,000; 그대로 답하면 grades["acquisition_amount"] == "예시값", fx는 "공개자료"
def test_interest_requires_rate(Agent, tmp_path, doc): ...  # ASSUMED + 빈 금리 → AgentError
```

- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: 전체 PASS**
- [ ] **Step 5: Commit** — `feat: 가정값에 취득비 예시값·금리·출처 등급`

---

### Task 3: 원가 분해와 합계 대조 (`approval.py`)

**Files:**
- Create: `cost-agent/app/agent/approval.py`
- Modify: `cost-agent/app/agent/models.py` (아래 모델)
- Test: `cost-agent/tests/test_approval.py`

**Interfaces:**
- Produces (`app/agent/models.py`):
  ```python
  class BreakdownRow(BaseModel):
      group: str            # "공간" | "전력" | "운영" | "감가상각" | "금융비용"
      item_types: list[str]
      annual_usd: float
      hourly_usd: float
      share: float          # 원가 대비 비중 0~1
      refs: list[str] = []
  class Approval(BaseModel):
      breakdown: list[BreakdownRow] = []
      breakdown_ok: bool = False
      interest_applied: bool = False
      breakeven: dict[str, float] = {}        # "candidate" | "3y" | "5y" → 가동률 0~1+
      sensitivity: list[dict] = []            # {key, label, price_delta, grade}
      benchmark: list[dict] = []              # {group, share, ref_share, ratio}
      findings: list["Finding"] = []
      verdict: str = ""                        # "승인 권고" | "조건부" | "재검토"
      reasons: list[str] = []
  class Finding(BaseModel):
      code: str; level: str                     # "재검토" | "조건부"
      message: str; fix_step: str | None = None  # Step 값
      reason: str = ""                          # 담당자 사유
  # AgentSession.approval: Approval | None = None
  ```
- Produces (`approval.py`): `breakdown(request: dict, response: dict, fx: float | None) -> tuple[list[BreakdownRow], bool, bool]` — (행들, breakdown_ok, interest_applied). §4B-3 그대로: YEAR 항목은 묶음별 합(KRW는 `÷ fx`), 감가 = ACQUISITION 금액(USD 환산) ÷ `assumptions.depreciationYears`, 금융 = `annualCost − 운영 − 감가`(금리 반영 시, 0 미만 0), 시간당 = ÷ `billableGpuHoursPerYear`(없으면 `8760 × utilization × gpuCount`). 대조 실패면 `breakdown_ok=False`.

- [ ] **Step 1: 실패하는 테스트** — BE 명세 응답 예시(`annualCost 114000`, `billableGpuHoursPerYear 49056`, 운영 34,000 + 취득 400,000/5년)를 그대로 쓴다.

```python
def test_breakdown_matches_spec_example():
    rows, ok, interest = breakdown(SPEC_REQ, SPEC_RES, fx=None)
    assert ok and not interest
    assert {r.group: round(r.annual_usd) for r in rows} == {"운영": 34000, "감가상각": 80000}
    assert sum(r.hourly_usd for r in rows) == pytest.approx(2.323874755382, rel=1e-6)
def test_breakdown_mismatch_flags_not_ok(): ...      # annualCost 120000 → ok False
def test_interest_applied_goes_to_financing(): ...    # interestWasProvided True, annualCost 125000 → 금융 11000
def test_interest_not_applied_by_be_is_reported():    # Review Focus 1
    rows, ok, interest = breakdown(REQ_WITH_FINANCING, RES_NOT_PROVIDED, fx=None)
    assert ok and not interest and "금융비용" not in {r.group for r in rows}
def test_krw_items_converted_with_fx(): ...
```

- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat: BE annualCost 기준 원가 분해와 합계 대조`

---

### Task 4: 손익분기·업계 비교·민감도

**Files:**
- Modify: `cost-agent/app/agent/approval.py`
- Test: `cost-agent/tests/test_approval.py`

**Interfaces:**
- `breakeven(utilization: float, hourly_cost: float, prices: dict[str, float]) -> dict[str, float]` = `{k: utilization × cost ÷ price}` (price ≤ 0이면 `inf` 대신 9.99).
- `benchmark_shares(rows: list[BreakdownRow]) -> list[dict]` — 금융 뺀 묶음 비중을 합 1로 다시 나눠 기준(감가 .36·공간 .34·전력 .16·운영 .14)과 `ratio = share ÷ ref_share`.
- `sensitivity(base_input: SimulationInput, items: list[CostItemDraft], simulate: Callable[[dict], dict], grades: dict[str, str], has_krw: bool) -> list[dict]` — 하나씩 바꿔 `simulate(build_request(items, 바꾼 입력))`를 부른다: 가동률 −10%p(`max(0.05, u−0.1)`), +10%p(`min(1, u+0.1)`), 취득비 ±10%, 감가 연수 `5↔6`(5면 6, 아니면 5), 금리 +2%p(미적용이면 `ASSUMED` 2%), `has_krw`면 환율 ±5%. 결과 `price_delta = 새 candidatePrice − 기존`. `|price_delta|` 큰 순. 실패한 호출은 건너뛴다. `grade`는 바꾼 가정의 출처 등급.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_breakeven_formula():
    assert breakeven(0.7, 2.0, {"candidate": 2.5, "3y": 2.125})["3y"] == pytest.approx(0.6588, rel=1e-3)
def test_benchmark_ratio_excludes_financing(): ...
def test_sensitivity_sorted_and_isolated():
    calls = []
    res = sensitivity(BASE, ITEMS, fake_sim(calls), {"utilization": "리포트 가정"}, has_krw=False)
    assert res[0]["key"] == "utilization_down" and len(calls) == 6
    assert all(c is not BASE_REQ for c in calls)       # 원래 요청·세션을 바꾸지 않음
```

- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat: 손익분기 가동률·업계 구성비·민감도`

---

### Task 5: 발견 사항과 판정 규칙

**Files:**
- Modify: `cost-agent/app/agent/approval.py`
- Test: `cost-agent/tests/test_approval.py`

**Interfaces:**
- `findings(s: AgentSession, ap: Approval) -> list[Finding]` — 코드와 수준, 문구(코드 템플릿), `fix_step`:

| code | level | 조건 | fix_step |
|---|---|---|---|
| `MISSING_EXCLUDED` | 재검토 | `missing_answers`에 `exclude`가 있음 | MISSING |
| `PRICE_BELOW_COST` | 재검토 | 제안가 또는 3·5년가 < 원가 | TERMS |
| `COST_ABOVE_MARKET` | 재검토 | 시장 표본 있고 원가 ≥ 중앙값 | ASSUMPTIONS |
| `BREAKDOWN_MISMATCH` | 재검토 | `breakdown_ok` False | ASSUMPTIONS |
| `THIN_BREAKEVEN` | 조건부 | 어느 가격이든 `가정 가동률 − 손익분기 < 0.10` | TERMS |
| `MARKET_OUT_KEPT` | 조건부 | 시장 범위 밖 + `margin_adjusted`인데 중앙값 맞추기 아님 | MARKET |
| `NO_FINANCING` | 조건부 | `interest_applied` False | ASSUMPTIONS |
| `EXAMPLE_IN_TOP2` | 조건부 | 민감도 상위 2개 가정 중 등급이 `예시값` | ASSUMPTIONS |
| `THIN_MARKET` | 조건부 | 사업자 < 3곳 (0곳 포함: "시장 비교 불가") | None |
| `ODD_MIX` | 조건부 | 업계 ratio < 0.5 또는 > 2 | ASSUMPTIONS |
| `BENCH_WARN` | 조건부 | `draft.bench` 또는 `s.checks`가 비지 않음 | ANALYZE |

- `verdict(fs: list[Finding]) -> tuple[str, list[str]]` — 재검토가 하나라도 있으면 `재검토`(사유가 있어도), 조건부만 있으면 `조건부`, 없으면 `승인 권고`. reasons는 문구 목록.

- [ ] **Step 1: 실패하는 테스트** — 판정 표의 행마다 한 개 (세션은 테스트 도우미로 최소 구성).

```python
def test_term_below_cost_is_review(): ...           # Review Focus 5 → PRICE_BELOW_COST, verdict 재검토
def test_no_market_samples_is_conditional(): ...    # Review Focus 3 → THIN_MARKET만, COST_ABOVE_MARKET 없음
def test_review_reason_does_not_upgrade_verdict(): ...  # 재검토 finding에 reason 있어도 재검토
def test_clean_session_is_approval(): ...
```

- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat: 승인 검토 발견 사항과 판정 규칙`

---

### Task 6: 에이전트 연결 — REVIEW 단계와 검토 카드

**Files:**
- Modify: `cost-agent/app/agent/models.py` (`Step.REVIEW`, `STEP_TITLES[REVIEW]="승인 검토"`, `AgentSession.approval`, `review_basis: str | None`)
- Modify: `cost-agent/app/agent/orchestrator.py` (`_step_review`, `_do_review`, `_q_review`, `_answer_review`; TERMS 다음 REVIEW로)
- Modify: `cost-agent/app/agent/graph.py` (도구 `review_price`, `ASK_STEP["review"]`, `_policy`, `_unmet`, `_tool_ask_user(review)`, `_analysis_text`/`_answer_text`)
- Test: `cost-agent/tests/test_agent.py`, `cost-agent/tests/test_graph_agent.py`

**Interfaces:**
- Consumes: `breakdown`, `breakeven`, `benchmark_shares`, `sensitivity`, `findings`, `verdict` (Task 3~5)
- `_do_review(s)`: `s.approval`을 새로 만든다(이전 finding의 `reason`은 같은 code면 옮김). `review_basis = sim_basis + market_basis + terms` 지문.
- `_q_review(s) -> Question`: title `승인 전에 확인할 점이 {n}개 있어요`, finding마다 `finding:{code}` select(`fix` 고치기 / `proceed` 사유를 적고 진행, 기본 `proceed`) + `reason:{code}` text(`show_for=["proceed"]`).
- `_answer_review`: `proceed`인데 사유 빈칸 → `AgentError("진행하려면 사유를 적어 주세요.")`. `fix`가 하나라도 있으면 가장 앞 단계(`fix_step`)로 돌아간다: ASSUMPTIONS면 `assumptions["confirmed"]=False`, MISSING이면 해당 그룹 `missing_answers` 삭제, TERMS면 `s.terms=[]`, MARKET이면 `margin_adjusted=False`. 다 `proceed`면 `review_done=True`.
- 그래프: 정책 순서 … terms → **`review_price`(검토가 최신 아님)** → **`ask_user(review)`(사유 없는 finding)** → finish. guard `_unmet`: `승인 검토가 최신 아님 → review_price`, `발견 사항 미처리 → ask_user(review)`. 판단자 관찰: `판정 조건부 · 발견 THIN_BREAKEVEN, NO_FINANCING (2개, 미처리 1)`.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_review_card_after_terms(Agent, tmp_path, detailed_doc):
    # r1 제안대로 끝까지 → TERMS 다음 질문이 '승인 전에 확인할 점이' 카드, finding:NO_FINANCING·EXAMPLE_IN_TOP2 포함
def test_proceed_needs_reason(Agent, tmp_path, detailed_doc): ...
def test_fix_reruns_review(Agent, tmp_path, detailed_doc):   # Review Focus 4
    # finding:EXAMPLE_IN_TOP2=fix → ASSUMPTIONS 카드 → 취득비 바꿔 답 → 원가 재계산 → 다시 검토 카드, EXAMPLE_IN_TOP2 없음
def test_finish_refused_until_review(tmp_path, detailed_doc): ...  # graph: 이른 finish → '승인 검토' 실행 불가
def test_controller_sees_finding_codes_only(tmp_path, detailed_doc): ...  # '손익분기'·'$' 없음, 'THIN_' 류 코드 있음
```

- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: 전체 PASS**
- [ ] **Step 5: Commit** — `feat: 승인 검토 단계와 검토 카드 (두 엔진)`

---

### Task 7: 보고서 자동 검증 관문 (`verify.py`)

**Files:**
- Create: `cost-agent/app/agent/verify.py`
- Modify: `cost-agent/app/agent/orchestrator.py` (`_do_report`), `cost-agent/app/agent/models.py` (`AgentSession.checks_passed: list[str]`, `checks_failed: list[str]`)
- Test: `cost-agent/tests/test_verify.py`

**Interfaces:**
- `risks(s: AgentSession) -> list[str]` — 리스크 문구(코드 템플릿): 취득비 예시값 / 금융비용 미반영 / 기준선 경고 각각 / '이번엔 제외' 그룹 / 해제한 해석 / 총액 불일치를 알고 진행.
- `verify(s: AgentSession) -> tuple[list[str], list[str]]` — (통과, 실패) 검사 이름. §4B-5 7개: `분해 합계`, `제안가 공식`, `기간별 가격`, `손익분기`, `시장 통계`(표본 없으면 통과), `리스크 누락 금지`(`risks(s)`가 근거 조건을 모두 덮는지), `권고 일치`.
- `_do_report`: `verify` 실패가 있으면 `report_ready=False`, board REPORT ERROR(`자동 검증 실패: …`), 진행 기록 warn. 통과면 지금처럼.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_clean_session_passes_all_seven(): ...
def test_tampered_term_price_fails(): ...          # s.terms[0].price 바꿈 → '기간별 가격' 실패
def test_missing_risk_fails(): ...                  # 취득비 예시값인데 risks에서 빠지게 몬키패치 → '리스크 누락 금지' 실패
def test_cost_markup_formula_verified(): ...        # Review Focus 2
def test_report_not_ready_when_verify_fails(Agent, tmp_path, doc): ...
```

- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: 전체 PASS**
- [ ] **Step 5: Commit** — `feat: 보고서 자동 검증 관문`

---

### Task 8: 한 장 보고서 HTML과 엑셀 시트

**Files:**
- Create: `cost-agent/app/agent/report_html.py`
- Modify: `cost-agent/app/agent/report.py` (시트), `cost-agent/app/main.py` (엔드포인트)
- Test: `cost-agent/tests/test_report_html.py`, `cost-agent/tests/test_agent.py`

**Interfaces:**
- `build_report_html(s: AgentSession) -> str` — 완결 HTML(`<!DOCTYPE html>`, 인라인 CSS, A4 `@media print`, 외부 리소스 없음, 모든 글자 `html.escape`). 섹션 순서는 §4B-4. 제목 `GPU 요금 제안 검토 — {모델} {GPU 수}장 · {날짜}`.
- `GET /v1/agent/sessions/{id}/report.html` → `text/html; charset=utf-8`. `report_ready` 아니면 409(`report.xlsx`와 같은 규칙).
- 엑셀: `승인 요약`(첫 시트), `원가 구성`(묶음·연간·시간당·비중·업계 기준·근거 줄), `민감도`, `검토 결과`(코드·수준·문구·담당자 사유·자동 검증 결과). `가정값` 시트에 `출처 등급` 열.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_html_has_sections_and_numbers_from_session(...):
    # 권고 문구, f"${cand:.2f}", 3·5년가, '자동 검증 7/7 통과', 리스크 문구가 모두 HTML에 있다
def test_html_escapes_bill_text(...): ...           # 파일명 '<script>' → '&lt;script&gt;'
def test_html_endpoint_409_before_ready(...): ...
def test_xlsx_sheet_order(...):
    assert wb.sheetnames[:5] == ["승인 요약", "원가 구성", "민감도", "검토 결과", "요약"]
```

- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: 전체 PASS**
- [ ] **Step 5: Commit** — `feat: 한 장 승인 보고서 HTML과 근거서 시트`

---

### Task 9: 샘플 전체 인수 시나리오

**Files:**
- Create: `cost-agent/tests/test_approval_scenarios.py`
- Modify: `cost-agent/scripts/eval_agent.py` (표에 판정·발견 코드 열)

**Interfaces:**
- 시나리오: 각 샘플을 "제안대로 답하기"(Task 1·6 도우미)로 끝까지 진행하고, 검토 카드는 모두 `proceed` + 사유 `테스트`.

- [ ] **Step 1: 테스트**

```python
@pytest.mark.parametrize("name, verdict", [
    ("bill_r1_kr_metered_krw.xlsx", "조건부"),   # 취득비 예시값·금융 미반영
    ("bill_r4_prorated_first_month_krw.xlsx", "조건부"),
    ...])
def test_detailed_sample_verdicts(...): ...         # 판정 + report_ready + verify 7/7
def test_excluded_missing_cost_is_review(...): ...  # c에서 운영 '이번엔 제외' → 재검토
```
기대 판정은 처음 실행 결과를 보고 규칙 표와 맞는지 손으로 확인한 뒤 고정한다 (규칙과 다르면 코드를 고친다).

- [ ] **Step 2: 실행** → 규칙 대조 · **Step 3: 고정** · **Step 4: `scripts/eval_agent.py --no-llm`, `scripts/score_detailed_samples.py` 실행** — 질문 순서는 MISSING이 한 장으로, 끝에 REVIEW가 더해진 것 말고 같다.
- [ ] **Step 5: Commit** — `test: 샘플별 승인 판정 인수 시나리오`

---

### Task 10: FE — 승인 검토 단계·카드·보고서 버튼 (GPUAU-FE, 별도 PR)

**Files (GPUAU-FE, `origin/main`에서 새 worktree — 로컬 main의 커밋 안 된 launcher 변경을 건드리지 않는다):**
- Modify: `docker_erd_20261117/app/web/static/cost-agent-ui.mjs` (`STEP_ORDER`·`STEP_TITLES`에 `REVIEW`, `show_for`를 같은 접두사 그룹 choice 칸에 묶기, 결과 카드 `[승인 보고서]` 버튼 → `/agent/v1/agent/sessions/{id}/report.html` 새 창)
- Test: `tests/cost-agent-ui.test.mjs`

**Interfaces:**
- `fieldInput`의 `data-show-for` 판정: 필드 이름이 `amount:{g}`·`reason:{c}`처럼 접두사를 가지면 같은 `{g}`/`{c}`의 `choice:`/`finding:` 칸 값으로 보인다/숨긴다. 접두사가 없으면 지금처럼 카드의 선택지 버튼 기준.

- [ ] **Step 1: 실패하는 테스트** — `STEP_ORDER`에 REVIEW가 TERMS와 REPORT 사이 / 누락 카드에서 `choice:네트워크=included`면 `amount:네트워크` 숨김·`parent:네트워크` 보임 / 결과 카드에 보고서 링크
- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: `node --test tests/*.test.mjs` PASS**
- [ ] **Step 5: Commit** (GPUAU-FE) — `✨ [FEAT] 원가 에이전트 승인 검토 단계·묶음 카드·보고서 버튼`
