# 원가 에이전트 1순위 — 청구서 해석 신뢰 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** IDC 청구서의 금액 단위·금액 열·일할·청구 기간·비원가 줄·요약 시트를 코드가 해석하고, 업계 기준선으로 이상값을 짚고, 담당자가 카드 한 장으로 확인하게 해서 `samples-detailed` 5종의 연간 원가를 정답 ±1%로 만든다.

**Architecture:** 매퍼(규칙·LLM·템플릿) 뒤에 결정적 해석 층(`billmeta` → `columns` → `interpret`)을 두고, `BillService._build`가 그 결과로 금액 배수·금액 열·연간화를 적용한다. 해석은 기본 적용, 담당자가 해제하면 `set_interpretations`로 다시 만든다. 에이전트는 ① 청구서 분석 단계에 `ask_user(interpret)` 카드 하나를 더하고, 사내 인건비·판관비는 ③ 가정값 카드의 입력칸 하나로 받는다.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, openpyxl, LangGraph, pytest (`cd cost-agent && .venv/bin/python -m pytest -q`)

**Spec:** `docs/superpowers/specs/2026-11-26-cost-agent-price-approval-design.md` §0, §1-3, §4A (1순위만. 2순위 §4 ②~⑧은 이 계획 밖)

## Global Constraints

- 숫자는 코드가 읽고 계산한다. LLM(분류·판단자)에 금액·기준선 값·해석 문구를 보내지 않는다. 판단자 관찰 문장(`_analysis_text`)에는 해석 **코드와 개수만** 넣는다.
- `LineRole`, `Period`, `ItemType` enum 값을 늘리지 않는다 (BE DTO·LLM 스키마·FE 호환). 청구 개월 수는 `BillDraft.billing_months: float`로 둔다.
- 해석·기준선 문구는 코드 템플릿(아래 Task 5, 6의 문구 그대로)이다.
- FE `STEP_ORDER`를 바꾸지 않는다. 새 카드는 기존 범용 질문 렌더러(select 입력칸)로 그려져야 한다.
- 기존 59개 테스트와 `samples/` a~i: 질문 순서·BE 요청 본문 그대로. a~i에서는 `interpretations == []`, `bench == []`, 사내 비용 칸은 빈칸.
- 기준값(출처 포함): 상면·임대 26.3만~35만원/kW/월 (현대차 표 16 ③, 표 18 F) · 전력 월 173,667원/계약kW (730h × PUE 1.3 × 183원, 표 18) · GPU당 2.0kW (표 18 A) · 원가 $2.51/GPU·h (표 18 비용 127.7억 ÷ 350.4만 GPU·h ÷ 1,450) · 기타 운영비 18억원/MW/년 (표 18 J) · 리포트 환율 1,450원/$.
- 작업 브랜치: `develop`에서 `feat/bill-interpretation`을 만든다.

## Review Focus

1. **단위가 '원'으로 명시된 청구서** ("금액 단위 | 원 (VAT 별도)") → 배수 1, 해석 없음. (Task 2 `test_explicit_won_unit_is_not_scaled`)
2. **요약 형식이 유일한 시트**(시트 이름이 '요약'인데 원가 줄이 그 시트에만 있음) → 원가에서 빼면 안 된다. (Task 5 `test_summary_sheet_kept_when_it_is_the_only_cost_sheet`)
3. **수량 1인 줄이 많아 단가 열 = 금액 열**인 표 → 1.0배 관계를 GROSS로 오판하면 안 된다. (Task 3 `test_equal_columns_are_not_gross`)
4. **이전 달에 개시한 정상 월 청구서**("서비스 개시일 2025-03-01", "2026년 10월분") → 일할로 보면 안 된다. (Task 2 `test_old_start_date_is_not_prorated`)
5. **담당자가 해석을 해제한 뒤 다시 적용**하거나 통화를 나중에 정함 → 금액·영향 배수가 매번 처음부터 다시 계산되어야 한다 (상태가 누적되면 안 됨). (Task 5 `test_toggle_interpretation_is_idempotent`)

---

### Task 1: 인수 테스트 (상세 샘플 5종 채점)

**Files:**
- Modify: `cost-agent/scripts/score_detailed_samples.py`
- Modify: `cost-agent/tests/conftest.py` (공용 도우미·fixture)
- Create: `cost-agent/tests/test_detailed_samples.py`

**Interfaces:**
- Produces: `score(path: Path, key: dict, service: BillService, off_all: bool = False) -> dict` — 키: `lines`(str "맞음/전체"), `errors`(list[str]), `currency`, `currency_exp`, `run_ratio`(float|None, 운영원가 얻음/정답), `acq`(tuple[float, float]), `draft`(BillDraft). `off_all=True`면 `service.set_interpretations(draft.id, {i.code for i in draft.interpretations})` 결과로 잰다 (Task 5 이후 동작; 그 전에는 `interpretations`가 없어 같은 결과).
- `main()`은 `DIR / name`으로 `score`를 부르고, 표를 "해석 적용"과 "해석 모두 해제" 두 번 출력한다.
- Produces (`tests/conftest.py`, 이후 모든 Task가 씀):
  - `doc_from_rows(rows: list[list], sheet: str = "s") -> ExtractedDocument` — `parsing._make_row`로 행을 만들고 `detect_currency`로 통화 힌트를 채운다.
  - fixture `detailed` (session): `make_detailed_samples.BUILDERS`를 임시 폴더에 만들고 `{name: (path, key)}`.
  - fixture `detailed_doc`: `name -> ExtractedDocument`.
  - fixture `samples_all`: `samples/` a~i 파일을 읽은 `list[ExtractedDocument]`.

- [ ] **Step 1: `score`를 위 시그니처로 바꾼다** (경로·정답을 인자로 받게; 기존 `find`, 1회성 판정 그대로)

- [ ] **Step 2: 도우미·fixture를 `conftest.py`에 넣고 인수 테스트를 쓴다** — `make_detailed_samples`는 기존 `make_samples`와 같은 방식(`importlib`)으로 불러온다.

```python
@pytest.mark.xfail(reason="1순위 구현 중 — Task 9에서 표시를 지운다", strict=False)
@pytest.mark.parametrize("name", list(BUILDERS))
def test_detailed_sample_annual_cost_within_1pct(name, detailed, service):
    path, key = detailed[name]
    r = score(path, key, service)
    assert r["errors"] == []
    assert r["run_ratio"] == pytest.approx(1.0, abs=0.01)
    assert r["acq"][0] == pytest.approx(r["acq"][1], rel=0.01, abs=1)

@pytest.mark.xfail(reason="1순위 구현 중", strict=False)
def test_turning_off_interpretations_reproduces_todays_error(detailed, service):
    ratios = {n: score(*detailed[n], service, off_all=True)["run_ratio"] for n in detailed}
    assert ratios["bill_r5_quarterly_no_currency.xlsx"] == pytest.approx(3.0, rel=0.01)
    assert ratios["bill_r4_prorated_first_month_krw.xlsx"] == pytest.approx(0.553, rel=0.02)

def test_original_samples_have_no_interpretations(service):
    for p in sorted((ROOT / "samples").iterdir()):
        d = service.ingest(extract(p.name, p.read_bytes()))
        assert getattr(d, "interpretations", []) == [] and getattr(d, "bench", []) == []
```

- [ ] **Step 3: 실행** — `.venv/bin/python -m pytest tests/test_detailed_samples.py -q` → 6 xfailed, 1 passed. 전체 `-q` → 59 passed + 새 결과.

- [ ] **Step 4: Commit** — `git add scripts/score_detailed_samples.py tests/test_detailed_samples.py samples-detailed scripts/make_detailed_samples.py` / `test: 상세 청구서 5종 인수 테스트와 채점 함수`

---

### Task 2: 문서 정보 읽기 `billmeta`

**Files:**
- Create: `cost-agent/app/billmeta.py`
- Modify: `cost-agent/app/models.py` (아래 `ContractInfo` 추가)
- Test: `cost-agent/tests/test_billmeta.py`

**Interfaces:**
- Produces (`app/models.py`):
  ```python
  class ContractInfo(BaseModel):
      power_kw: float | None = None
      racks: int | None = None
      gpus: int | None = None
  ```
- Produces (`app/billmeta.py`):
  ```python
  class BillMeta(BaseModel):
      unit_multiplier: float = 1.0
      unit_label: str = ""            # "천원" | "백만원" | ""
      unit_ref: str | None = None
      billing_months: float | None = None   # None = 판단 못함 → 기존 detect_period로
      months_text: str = ""           # 근거 원문 조각
      months_ref: str | None = None
      prorate_days: int | None = None
      month_days: int | None = None
      prorate_ref: str | None = None
      contract: ContractInfo = ContractInfo()
  def read_meta(doc: ExtractedDocument) -> BillMeta
  ```
- 읽기 규칙 (원문 글자칸, 시트별 앞 40행, 행의 글자칸을 공백으로 이어 붙인 문자열 기준):
  - 단위: `단위\s*[:：]?\s*(천|백만)\s*원`, `\((천|백만)\s*원\)`, `in thousands` → 1,000 / 1,000,000.
  - 개월 수: 날짜 범위 `YYYY-MM-DD ~ YYYY-MM-DD`(구분자 `-./`, 물결 `~–-`)가 달 1일~말일로 딱 떨어지면 달 수. `n분기`·`quarter`·`Q[1-4]` → 3, `반기` → 6, 기존 `YEAR_WORDS` → 12, `n월분`/월 표현 → 1.
  - 일할: `개시일|개통일|start date` 뒤 날짜가 청구월(`YYYY년 M월분` 또는 날짜 범위의 달)과 같은 달이고 1일이 아니면 `prorate_days = 말일 − 개시일 + 1`, `month_days = 그 달 일수`. 날짜 범위가 한 달보다 짧을 때도 같게.
  - 계약: `계약전력\s*([\d,.]+)\s*kW`, `committed power\s*([\d,.]+)\s*kW` (대소문자 무시) → `power_kw`. `(\d+)\s*랙`, `cabinets?\s*(\d+)` → `racks`. `서버\s*(\d+)\s*대\s*\(서버당\s*GPU\s*(\d+)\s*장\)` → 곱, 또는 `GPU\s*([\d,]+)\s*장` → `gpus`.

- [ ] **Step 1: 실패하는 테스트** — `tests/test_billmeta.py` (도우미·fixture는 Task 1의 `conftest.py`)

```python
def test_r1_full_month_and_contract(detailed_doc):
    m = read_meta(detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    assert m.unit_multiplier == 1 and m.billing_months == 1 and m.prorate_days is None
    assert m.contract == ContractInfo(power_kw=1000, racks=25, gpus=504)
def test_r2_thousand_won(detailed_doc):
    m = read_meta(detailed_doc("bill_r2_multisheet_thousand_krw.xlsx"))
    assert (m.unit_multiplier, m.unit_label) == (1000, "천원") and m.unit_ref.startswith("상세내역!")
    assert m.contract.power_kw == 512
def test_r4_prorated(detailed_doc):
    m = read_meta(detailed_doc("bill_r4_prorated_first_month_krw.xlsx"))
    assert (m.prorate_days, m.month_days) == (17, 31)
def test_r5_quarter(detailed_doc):
    m = read_meta(detailed_doc("bill_r5_quarterly_no_currency.xlsx"))
    assert m.billing_months == 3 and "3분기" in m.months_text and m.contract.power_kw == 400
def test_r3_committed_power(detailed_doc):
    assert read_meta(detailed_doc("bill_r3_colo_mrc_nrc_usd.xlsx")).contract.power_kw == 800
def test_explicit_won_unit_is_not_scaled():      # Review Focus 1
    assert read_meta(doc_from_rows([["금액 단위", "원 (VAT 별도)"], ["전력 사용료", 100]])).unit_multiplier == 1
def test_old_start_date_is_not_prorated():       # Review Focus 4
    m = read_meta(doc_from_rows([["서비스 개시일", "2025-03-01"], ["청구월", "2026년 10월분"], ["전력", 100]]))
    assert m.prorate_days is None and m.billing_months == 1
def test_original_samples_have_no_meta(samples_all):   # a~i
    for d in samples_all: m = read_meta(d); assert m.unit_multiplier == 1 and m.prorate_days is None and m.contract.power_kw is None
```
- [ ] **Step 2: 실행** — `pytest tests/test_billmeta.py -q` → ImportError로 FAIL
- [ ] **Step 3: `read_meta` 구현** (위 규칙. 날짜는 `datetime.date`, 말일은 `calendar.monthrange`)
- [ ] **Step 4: 실행** — PASS, 전체 스위트 PASS
- [ ] **Step 5: Commit** — `feat: 청구서 머리글에서 금액 단위·청구 기간·일할·계약전력 읽기`

---

### Task 3: 금액 열 역할 `columns`

**Files:**
- Create: `cost-agent/app/columns.py`
- Test: `cost-agent/tests/test_columns.py`

**Interfaces:**
- Produces:
  ```python
  class ColRole(str, Enum):
      SUPPLY = "SUPPLY"; VAT = "VAT"; GROSS = "GROSS"; MRC = "MRC"; NRC = "NRC"
      BASE = "BASE"; BILLED = "BILLED"; QTY = "QTY"; RATE = "RATE"
  def column_roles(doc: ExtractedDocument) -> dict[str, dict[int, ColRole]]
  def header_text(doc: ExtractedDocument) -> dict[str, dict[int, str]]   # 시트 → 열 → 머리글 원문 (해석 문구용)
  def preferred_col(roles: dict[int, ColRole], row: SheetRow) -> int | None   # BILLED > SUPPLY > MRC 순, 그 칸에 숫자가 있을 때만
  ```
- 머리글 행: 숫자 없는 행 중 아래 키워드가 2개 이상 맞는 첫 행. 바로 다음 행도 숫자 없고 키워드가 있으면 두 행을 열별로 이어 붙인다 (병합 머리글). 괄호 안 단위(`(원)`, `(USD)`)는 지우고 비교.
- 키워드 (위에서 먼저 맞는 것): `NRC`: nrc, one-time, setup, 1회 / `MRC`: mrc, monthly recurring / `BASE`: 월 정액, 월정액 / `BILLED`: 당월 청구, 일할 청구 / `VAT`: 부가세, 세액, vat, tax / `GROSS`: 합계금액, 합계, total / `SUPPLY`: 공급가액, 금액, 청구액, amount / `RATE`: 단가, unit rate, rate / `QTY`: 수량, qty, 일수, 사용량.
- 숫자 관계: 같은 시트에서 숫자가 있는 행이 3개 이상이고 그 80% 이상에서 열 b = 열 a × 1.1(±0.5%)이면 b를 GROSS로, 열 c = 열 a × 0.1(±0.5%)이면 c를 VAT로 (머리글 역할이 없을 때만 채움). 관계가 1.0배면 아무것도 안 함.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_r1_supply_vat_gross(detailed_doc):
    r = column_roles(detailed_doc("bill_r1_kr_metered_krw.xlsx"))["청구서"]
    assert (r[7], r[8], r[9], r[4], r[6]) == (ColRole.SUPPLY, ColRole.VAT, ColRole.GROSS, ColRole.QTY, ColRole.RATE)
def test_r3_mrc_nrc(detailed_doc):
    r = column_roles(detailed_doc("bill_r3_colo_mrc_nrc_usd.xlsx"))["Invoice"]
    assert (r[6], r[7], r[5]) == (ColRole.MRC, ColRole.NRC, ColRole.RATE)
def test_r4_base_billed(detailed_doc):
    r = column_roles(detailed_doc("bill_r4_prorated_first_month_krw.xlsx"))["청구서"]
    assert (r[1], r[4], r[3]) == (ColRole.BASE, ColRole.BILLED, ColRole.QTY)
def test_r5_merged_header(detailed_doc):
    r = column_roles(detailed_doc("bill_r5_quarterly_no_currency.xlsx"))["3분기"]
    assert (r[2], r[3], r[4]) == (ColRole.SUPPLY, ColRole.VAT, ColRole.GROSS)
def test_gross_found_by_ratio_without_header():
    d = doc_from_rows([["항목", "", ""], *[[f"x{i}", 100*i, 110*i] for i in range(1, 5)]])
    assert column_roles(d)["s"][2] == ColRole.GROSS
def test_equal_columns_are_not_gross():          # Review Focus 3
    d = doc_from_rows([["항목", "단가", "금액"], *[[f"x{i}", 100*i, 100*i] for i in range(1, 5)]])
    assert ColRole.GROSS not in column_roles(d)["s"].values()
def test_preferred_col_skips_empty_cell(detailed_doc):
    d = detailed_doc("bill_r1_kr_metered_krw.xlsx"); roles = column_roles(d)["청구서"]
    due = next(r for r in d.rows if r.label.startswith("총 납부하실 금액"))   # 숫자가 합계 열에만 있음
    first = next(r for r in d.rows if "고밀도 랙 상면료" in r.label)
    assert preferred_col(roles, due) is None and preferred_col(roles, first) == 7
```

- [ ] **Step 2: 실행 → FAIL** · **Step 3: 구현** · **Step 4: 실행 → PASS (전체 포함)**
- [ ] **Step 5: Commit** — `feat: 시트별 금액 열 역할(공급가액·부가세·합계·MRC/NRC·월정액/일할) 판별`

---

### Task 4: 규칙 보정과 총액 대조 후보

**Files:**
- Modify: `cost-agent/app/mapping/rules.py` (`ROLE_KEYWORDS`, `_find_item`, `map_row`)
- Modify: `cost-agent/app/validation.py` (`reconcile`)
- Modify: `cost-agent/app/mapping/llm.py` (`SYSTEM_PROMPT` 규칙 1에 한 줄)
- Test: `cost-agent/tests/test_rules_pipeline.py`

**Interfaces:**
- `ROLE_KEYWORDS[TOTAL]`에 `납부`, `balance due` 추가 (단, `미납`은 Task 5의 비원가 규칙이 먼저 처리).
- `_find_item(text)`: `유지보수`·`maintenance`가 있으면 OPERATIONS. 상면·랙·rack·cabinet·space·suite·cage·colocation 중 하나가 있고 `kwh`가 없으면 ELECTRICITY보다 RACK_FACILITY → RENT를 먼저 본다.
- `map_row`: 항목명이 날짜(`^\d{4}-\d{2}-\d{2}$`)뿐이면 `IGNORE` ("날짜 줄 — 사용량 표").
- `reconcile`: 대상 = 모든 TOTAL 줄(큰 값부터) + 모든 SUBTOTAL 줄. 처음 맞는 조합을 쓴다. 불일치 메시지는 첫 대상 기준 그대로.
- LLM 프롬프트 규칙 1 끝에: `전월 미납·이월·입금 줄은 IGNORE, '총 납부할 금액'은 TOTAL.`

- [ ] **Step 1: 실패하는 테스트** — 파일 안 도우미: `row(label, amount) -> SheetRow` (`cells=[label, str(amount)]`, `numbers={1: amount}`), `cost(a)`·`total(a, label)`·`tax(a)` → 해당 역할의 `MappedLine`.

```python
def test_capacity_fee_with_power_word_is_rack():
    assert map_row(row("전력용량 기반 상면 이용료 계약전력 1,000kW", 245e6), None).item_type == ItemType.RACK_FACILITY
def test_space_and_power_capacity_is_rent():
    assert map_row(row("Private Suite – Space & Power Capacity (per committed kW)", 108000), None).item_type == ItemType.RENT
def test_facility_maintenance_is_operations():
    assert map_row(row("시설 유지보수 (UPS·항온항습)", 6800), None).item_type == ItemType.OPERATIONS
def test_amount_due_is_total_and_date_row_ignored():
    assert map_row(row("총 납부하실 금액", 1), None).role == LineRole.TOTAL
    assert map_row(row("Balance Due", 1), None).role == LineRole.TOTAL
    assert map_row(row("2026-10-01", 1234), None).role == LineRole.IGNORE
def test_reconcile_tries_every_total():
    lines = [cost(100), total(110, "당월 청구금액"), total(150, "총 납부하실 금액"), tax(10)]
    assert reconcile(lines, Currency.KRW)[0].matched is True
```

- [ ] **Step 2: FAIL 확인** · **Step 3: 구현** · **Step 4: 전체 스위트 PASS** (기존 a~i 결과 그대로)
- [ ] **Step 5: Commit** — `fix: 용량형 상면료·시설 유지보수 분류, 납부 총액·날짜 줄, 모든 총액 후보로 대조`

---

### Task 5: 해석 적용 `interpret`과 서비스 연결

**Files:**
- Create: `cost-agent/app/interpret.py`
- Modify: `cost-agent/app/models.py` (`Interpretation`, `BillDraft` 필드)
- Modify: `cost-agent/app/validation.py` (`aggregate` 시그니처)
- Modify: `cost-agent/app/service.py` (`ingest`, `_build`, `set_interpretations`, `remap`, `set_currency`, `correct`)
- Test: `cost-agent/tests/test_interpret.py`

**Interfaces:**
- Consumes: `read_meta`, `BillMeta` (Task 2) · `column_roles`, `header_text`, `preferred_col`, `ColRole` (Task 3)
- Produces (`app/models.py`):
  ```python
  class Interpretation(BaseModel):
      code: str                     # InterpCode 값
      message: str
      applied: bool = True
      impact: float | None = None   # 이 해석만 끈 연간 운영원가 ÷ 지금 연간 운영원가
      refs: list[str] = Field(default_factory=list)
  # BillDraft에 추가
      unit_multiplier: float = 1.0
      billing_months: float = 1.0
      contract: ContractInfo = Field(default_factory=ContractInfo)
      interpretations: list[Interpretation] = Field(default_factory=list)
      bench: list["BenchFinding"] = Field(default_factory=list)   # Task 6이 채움
  ```
- Produces (`app/interpret.py`):
  ```python
  class InterpCode(str, Enum):
      UNIT_SCALE = "UNIT_SCALE"; GROSS_COLUMN = "GROSS_COLUMN"; NRC_ONE_TIME = "NRC_ONE_TIME"
      BILLING_MONTHS = "BILLING_MONTHS"; PRORATED = "PRORATED"; NON_COST_ROWS = "NON_COST_ROWS"; SUMMARY_SHEET = "SUMMARY_SHEET"
  @dataclass
  class Interpreted:
      mapping: DocumentMapping
      multiplier: float
      months: float | None
      base: dict[str, float]             # ref → 월 정액 (배수 적용 전 원문 값)
      prorate: tuple[int, int] | None    # (사용 일수, 그 달 일수)
      found: list[Interpretation]        # 적용 여부와 관계없이 찾은 것 전부, applied = code not in off
  NON_COST_WORDS = ["미납", "이월", "입금", "납부 완료", "previous balance", "payment received", "balance forward"]
  def interpret(doc: ExtractedDocument, mapping: DocumentMapping, meta: BillMeta,
                roles: dict[str, dict[int, ColRole]], off: frozenset[str] = frozenset()) -> Interpreted
  ```
- 해석별 동작 (켜져 있을 때만 적용, 찾기는 항상):
  - `NON_COST_ROWS`: 항목명에 `NON_COST_WORDS`가 있는 줄 → `IGNORE` (사유 "원가 아님 — 미납·이월·입금"). 다른 해석보다 먼저.
  - `SUMMARY_SHEET`: 시트 이름에 `요약`·`summary`·`총괄`이 있고 **다른 시트에 COST 줄이 있을 때만**, 그 시트의 COST·DISCOUNT 줄 → `IGNORE`.
  - `GROSS_COLUMN`: COST·DISCOUNT 줄의 `amount_col` 역할이 VAT·GROSS·QTY·RATE·BASE이고 `preferred_col`이 있으면 그 열로 바꾼다.
  - `NRC_ONE_TIME`: `amount_col`(바꾼 뒤)의 역할이 NRC인 COST 줄 → `period = ONE_TIME`.
  - `UNIT_SCALE` / `BILLING_MONTHS`(months ≠ 1) / `PRORATED`: `meta` 값을 `multiplier`/`months`/`prorate`로 넘긴다. 끄면 1 / None / None.
  - `base`: PRORATED가 켜져 있을 때, BASE 열에 숫자가 있는 COST 줄의 그 값.
- 해석 문구 (그대로):
  - UNIT_SCALE: `금액 단위가 {unit_label}이에요 → 모든 금액에 {multiplier:,.0f}배를 적용했어요`
  - GROSS_COLUMN: `'{gross_header}' 열은 부가세·수량·단가 열이라 '{used_header}' 열을 원가로 썼어요`
  - NRC_ONE_TIME: `1회성(NRC) 열의 {n}줄은 초기 투자(취득비)로 옮겼어요`
  - BILLING_MONTHS: `{months:g}개월 청구서라 ×{12/months:g}로 연간화했어요 ({months_text})`
  - PRORATED: `{days}일 사용분(일할)이에요 → 월 정액이 있는 줄은 월 정액 × 12, 나머지는 ÷ {days}일 × 365로 연간화했어요`
  - NON_COST_ROWS: `원가가 아닌 줄 {n}개(전월 미납·이월·입금)는 원가에서 뺐어요`
  - SUMMARY_SHEET: `'{sheet}' 시트는 상세 시트를 요약한 것이라 원가에서 뺐어요`
- `aggregate(lines, currency, default_period, months: float | None = None, base: dict[str, float] | None = None, prorate: tuple[int, int] | None = None)`. 1회성이 아닌 줄의 연간 금액: `base`에 ref가 있으면 `base × 12` → `prorate`가 있으면 `amount ÷ days × 365` → 줄 기간이 YEAR면 `amount` → 아니면 `amount × 12 ÷ m` (`m = months`, 없으면 `default_period`가 YEAR일 때 12, 아니면 1). `MONTH_TO_YEAR` 안내는 `m == 1`일 때 기존 문구, 아니면 `청구 기간 {m:g}개월 금액을 연간으로 바꿨습니다 (×{12/m:g}).`
- `BillService`: `ingest`가 `read_meta`·`column_roles`를 한 번 구해 `self._meta[id]`·`self._roles[id]`·`self._off[id] = frozenset()`에 둔다. `_build`는 매번 `interpret(doc, mapping, meta, roles, off)`를 새로 부르고, 줄 금액 = 칸 숫자 × `multiplier`, `base` 값도 × `multiplier`. `billing_months = months or (12 if default_period == YEAR else 1)`.
  - 영향 배수: 켜진 해석마다 그 코드를 `off`에 더해 cost_items만 다시 계산하고 `운영원가(off) ÷ 운영원가(지금)` (운영원가 = USER_INPUT이고 ACQUISITION이 아닌 항목 금액 합). 지금이 0이면 None.
  - `def set_interpretations(self, draft_id: str, off: set[str]) -> BillDraft` — `self._off` 교체 후 `_build`.
  - 템플릿 저장은 해석 전 매핑(`self._mappings`) 그대로. `remap`·`set_currency`·`correct`는 저장된 `off`로 `_build`.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_r2_unit_summary_and_one_time(service, detailed_doc):
    d = service.ingest(detailed_doc("bill_r2_multisheet_thousand_krw.xlsx"))
    codes = {i.code for i in d.interpretations}
    assert {"UNIT_SCALE", "SUMMARY_SHEET"} <= codes
    assert next(i for i in d.interpretations if i.code == "UNIT_SCALE").impact == pytest.approx(0.001, rel=0.01)
    assert d.reconciliation.matched is True
def test_r1_gross_column_and_non_cost(service, detailed_doc):
    d = service.ingest(detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    gross = next(i for i in d.interpretations if i.code == "GROSS_COLUMN")
    assert "'합계금액" in gross.message and "'공급가액" in gross.message and gross.impact == pytest.approx(1.1, rel=0.01)
    assert any(i.code == "NON_COST_ROWS" for i in d.interpretations) and d.reconciliation.matched is True
def test_r3_nrc_to_acquisition(service, detailed_doc):
    d = service.ingest(detailed_doc("bill_r3_colo_mrc_nrc_usd.xlsx"))
    acq = next(c for c in d.cost_items if c.item_type == ItemType.ACQUISITION)
    assert acq.amount == pytest.approx(73_000) and d.reconciliation.matched is True
def test_r4_prorated_annualization(service, detailed_doc):
    d = service.ingest(detailed_doc("bill_r4_prorated_first_month_krw.xlsx"))
    rack = next(c for c in d.cost_items if c.item_type == ItemType.RACK_FACILITY)
    assert rack.amount == 48_000_000 * 12
def test_r5_quarter(service, detailed_doc):
    d = service.ingest(detailed_doc("bill_r5_quarterly_no_currency.xlsx"))
    assert d.billing_months == 3 and next(i for i in d.interpretations if i.code == "BILLING_MONTHS").impact == pytest.approx(3.0, rel=0.01)
def test_toggle_interpretation_is_idempotent(service, detailed_doc):      # Review Focus 5
    d0 = service.ingest(detailed_doc("bill_r2_multisheet_thousand_krw.xlsx"))
    service.set_interpretations(d0.id, {"UNIT_SCALE"})
    d2 = service.set_interpretations(d0.id, set())
    assert [c.model_dump() for c in d2.cost_items] == [c.model_dump() for c in d0.cost_items]
    d3 = service.set_currency(d0.id, Currency.KRW)
    assert [i.impact for i in d3.interpretations] == [i.impact for i in d0.interpretations]
def test_summary_sheet_kept_when_it_is_the_only_cost_sheet(service):       # Review Focus 2
    d = service.ingest(doc_from_rows([["청구 요약", "금액(원)"], ["전력 사용료", 100], ["상면 사용료", 200], ["합계", 300]], sheet="요약"))
    assert not any(i.code == "SUMMARY_SHEET" for i in d.interpretations) and d.reconciliation.matched is True
```

- [ ] **Step 2: FAIL 확인** · **Step 3: 구현** · **Step 4: 전체 스위트 PASS** — Task 1의 r1~r5 인수 테스트가 이제 XPASS여야 한다 (`pytest -rX tests/test_detailed_samples.py`). XPASS가 아닌 샘플이 있으면 이 Task 안에서 고친다.
- [ ] **Step 5: Commit** — `feat: 청구서 해석(단위·금액 열·NRC·청구 기간·일할·비원가·요약 시트)과 해제 시 영향 배수`

---

### Task 6: 업계 기준선 검사 `benchmarks`

**Files:**
- Create: `cost-agent/app/benchmarks.py`
- Modify: `cost-agent/app/models.py` (`BenchFinding`), `cost-agent/app/service.py` (`_build`에서 `draft.bench` 채움)
- Test: `cost-agent/tests/test_benchmarks.py`

**Interfaces:**
- Produces (`app/models.py`):
  ```python
  class BenchFinding(BaseModel):
      code: str        # RENT_PER_KW | POWER_PER_KW | KW_PER_GPU | COST_PER_GPU_HOUR
      message: str
      value: float
      low: float
      high: float
      source: str
  ```
- Produces (`app/benchmarks.py`):
  ```python
  REPORT_FX = 1450.0
  RENT_PER_KW_MONTH = (263_000.0, 350_000.0)
  POWER_PER_KW_MONTH = 173_667.0
  KW_PER_GPU = 2.0
  COST_PER_GPU_HOUR_USD = 2.51
  OTHER_OPEX_PER_MW_YEAR = 1_800_000_000.0
  def bill_checks(items: list[CostItemDraft], currency: Currency | None, contract: ContractInfo) -> list[BenchFinding]
  def gpu_checks(contract: ContractInfo, gpu_count: int) -> list[BenchFinding]
  def cost_check(hourly_cost_usd: float) -> list[BenchFinding]
  def inhouse_suggestion(items: list[CostItemDraft], currency: Currency | None, contract: ContractInfo) -> float | None
  ```
- 경고 범위와 문구 (값은 원화 환산, 달러 청구서는 × `REPORT_FX`; 범위 안이면 빈 목록; `power_kw`나 통화가 없으면 검사 안 함):
  - RENT_PER_KW = (RACK_FACILITY + RENT 연간) ÷ 12 ÷ kW, 경고 `< 131,500` 또는 `> 700,000`: `상면·임대 단가가 계약 kW당 월 {v:,.0f}원으로 계산돼요. 업계 기준 26.3만~35만원과 크게 달라요. 금액 단위나 금액 열을 확인해 주세요.` · source `현대차증권 2026-09 표 16·18`
  - POWER_PER_KW = ELECTRICITY 연간 ÷ 12 ÷ kW, 경고 `< 52,100` 또는 `> 260,500`: `전기료가 계약 kW당 월 {v:,.0f}원으로 계산돼요. 만실 기준 약 17.4만원(PUE 1.3·183원/kWh)과 크게 달라요.` · 같은 출처
  - KW_PER_GPU = kW ÷ GPU 수, 경고 `< 1.2` 또는 `> 3.5`: `계약전력 ÷ GPU 수가 {v:.1f}kW예요. GPU 한 장에 서버·네트워크 포함 약 2kW가 보통이라, GPU 수나 계약전력을 확인해 주세요.` · `현대차증권 표 18 A, 하나증권 2026-07 도표 3·4`
  - COST_PER_GPU_HOUR, 경고 `< 1.0` 또는 `> 6.28`: `원가가 GPU·시간당 ${v:.2f}예요. 리포트 기준 약 $2.5와 크게 달라요. 빠지거나 두 번 들어간 비용이 없는지 확인해 주세요.` · `현대차증권 표 18`
  - `inhouse_suggestion` = `OTHER_OPEX_PER_MW_YEAR × kW/1000` (달러면 ÷ `REPORT_FX`) − (NETWORK_STORAGE + OPERATIONS 연간), 0 미만이면 0, 원화는 100만원, 달러는 1,000달러 단위 반올림. `power_kw` 없으면 None.

- [ ] **Step 1: 실패하는 테스트**

```python
@pytest.mark.parametrize("name", list(BUILDERS))
def test_correct_detailed_samples_pass_bill_checks(name, service, detailed_doc):
    assert service.ingest(detailed_doc(name)).bench == []
def test_thousand_won_off_trips_rent_check(service, detailed_doc):
    d = service.ingest(detailed_doc("bill_r2_multisheet_thousand_krw.xlsx"))
    d = service.set_interpretations(d.id, {"UNIT_SCALE"})
    assert [b.code for b in d.bench] == ["RENT_PER_KW", "POWER_PER_KW"]
def test_gpu_and_cost_checks_bounds():
    assert gpu_checks(ContractInfo(power_kw=1000), 504) == []
    assert gpu_checks(ContractInfo(power_kw=1000), 64)[0].code == "KW_PER_GPU"
    assert cost_check(2.4) == [] and cost_check(0.9)[0].code == "COST_PER_GPU_HOUR"
def test_inhouse_suggestion_r1(service, detailed_doc):
    d = service.ingest(detailed_doc("bill_r1_kr_metered_krw.xlsx"))
    assert inhouse_suggestion(d.cost_items, d.currency, d.contract) == 1_337_000_000
```

- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: 전체 PASS** (a~i는 `power_kw`가 없어 `bench == []`)
- [ ] **Step 5: Commit** — `feat: 리포트 기준선으로 상면·전력 단가, GPU당 전력, 원가 수준 검사`

---

### Task 7: 에이전트 — 청구서 해석 확인 카드

**Files:**
- Modify: `cost-agent/app/agent/models.py` (`AgentSession.interpret_ok: bool = False`)
- Modify: `cost-agent/app/agent/orchestrator.py` (`_step_analyze`, `_answer_analyze`, 새 `_needs_interpret`, `_q_interpret`)
- Modify: `cost-agent/app/agent/graph.py` (`ASK_STEP`, `ASK_KO`, `ask_user` 설명, `_policy`, `_unmet`, `_tool_ask_user`, `_tool_simulate_cost`, `_analysis_text`)
- Test: `cost-agent/tests/test_agent.py`, `cost-agent/tests/test_graph_agent.py`

**Interfaces:**
- Consumes: `BillDraft.interpretations`, `BillDraft.bench`, `BillService.set_interpretations` (Task 5, 6)
- Produces:
  - `CostAgent._needs_interpret(s) -> bool` = `s.draft` 있음, `not s.interpret_ok`, `interpretations` 또는 `bench`가 비어 있지 않음.
  - `CostAgent._q_interpret(s) -> Question`: `step=ANALYZE`, title `청구서를 이렇게 읽었어요. 맞는지 확인해 주세요`, text `회색 값은 에이전트가 제안한 해석이에요. 아니면 '적용 안 함'으로 바꾸세요.`, `submit_label="이대로 계속"`. 입력칸:
    - 해석마다 `FieldSpec(name=f"interp:{code}", label=message, type="select", value="on", options=[Option("on","적용"), Option("off","적용 안 함")], hint=영향 문구)`. 영향 문구: `impact`가 None이거나 0.995~1.005면 `연간 원가에는 영향이 없어요`, 아니면 `놓치면 연간 원가가 {impact:.3g}배로 잡혀요`.
    - 기준선마다 `FieldSpec(name=f"bench:{code}", label=message, type="select", value="ok", options=[Option("ok","확인했음"), Option("stop","청구서 다시 보기")], hint=source)`.
  - `_answer_analyze`: 입력칸 이름이 `interp:`/`bench:`로 시작하면 → `off = {code | 값 == "off"}`, `s.draft = bills.set_interpretations(s.draft.id, off)`, 기준선 중 `stop`이 있으면 board ANALYZE를 ERROR(`담당자가 청구서 다시 보기를 골라 중단`)로 두고 `s.step = DONE`; 아니면 `s.interpret_ok = True`. 진행 기록 `해석 {적용 n}개 적용 · {해제 m}개 해제`.
  - 순서 (워크플로·정책 공통): 통화 → **해석 확인** → 총액 불일치/재분류 → 누락 → 가정값.
  - 그래프: `ASK_STEP["interpret"] = Step.ANALYZE`, `ASK_KO["interpret"] = "청구서 해석"`, `ask_user.kind` 설명에 `interpret=청구서 해석(단위·금액 열·청구 기간) 확인`. guard: `_unmet`에 `청구서 해석 미확인 → ask_user(interpret)`; `_tool_simulate_cost`와 `ask_user(assumptions)`는 해석 확인 전 거절; `ask_user(interpret)`는 필요 없으면 거절.
  - `_analysis_text`(판단자 관찰)에는 `해석 제안: UNIT_SCALE, SUMMARY_SHEET (2개)`, `기준선 경고 n개`처럼 코드와 개수만. 문구·값 금지.

- [ ] **Step 1: 실패하는 테스트** (두 엔진 모두 — 기존 `Agent` 매개변수화 fixture 사용)

```python
# 도우미 start(Agent, tmp_path, doc) -> (agent, session) / board(s, step) -> BoardStep 은 이 파일에 둔다 (fake_backend, Goal(model_id="29", gpu_count=256))
def test_interpret_card_then_flow_continues(Agent, tmp_path, detailed_doc):
    agent = Agent(BillService(RulesMapper(), TemplateStore(tmp_path)), fake_backend()[0])
    s = agent.start(detailed_doc("bill_r2_multisheet_thousand_krw.xlsx"), Goal(model_id="29", gpu_count=256))
    assert s.step == Step.ANALYZE and s.question.title.startswith("청구서를 이렇게 읽었어요")
    f = {x.name: x for x in s.question.fields}
    assert f["interp:UNIT_SCALE"].value == "on" and "0.001배" in f["interp:UNIT_SCALE"].hint
    s = agent.answer(s.id, Answer(question_id=s.question.id, values={}))
    assert s.interpret_ok and s.question.step != Step.ANALYZE
def test_turning_off_interpretation_changes_draft(Agent, tmp_path, detailed_doc):
    agent, s = start(Agent, tmp_path, detailed_doc("bill_r2_multisheet_thousand_krw.xlsx"))
    s = agent.answer(s.id, Answer(question_id=s.question.id, values={"interp:UNIT_SCALE": "off"}))
    assert s.draft.unit_multiplier == 1 and s.interpret_ok
def test_bench_stop_ends_session(Agent, tmp_path):
    bad = doc_from_rows([["청구월", "2026년 10월분", "계약전력", "1,000kW"], ["항목", "금액(원)"],
                         ["상면 사용료", 1000], ["전력 사용료", 150_000_000], ["회선료", 1_000_000],
                         ["운영관리비", 1_000_000], ["합계", 152_001_000]])
    agent, s = start(Agent, tmp_path, bad)                       # 상면 kW당 월 1원 → RENT_PER_KW
    assert "bench:RENT_PER_KW" in {f.name for f in s.question.fields}
    s = agent.answer(s.id, Answer(question_id=s.question.id, values={"bench:RENT_PER_KW": "stop"}))
    assert s.step == Step.DONE and board(s, Step.ANALYZE).status == Status.ERROR
@pytest.mark.parametrize("name", list(BUILDERS))
def test_analyze_cards_at_most_three(Agent, tmp_path, detailed_doc, name):
    # 제안대로 답하며 진행할 때 step == ANALYZE인 질문 카드가 3장 이하 (r5: 통화 + 해석 = 2)
```
`test_graph_agent.py`:
```python
def test_simulate_refused_before_interpret(tmp_path, detailed_doc):
    # 판단자가 analyze_bill 다음 바로 simulate_cost를 부르면 '실행 불가 · 청구서 해석 …'이 기록되고, 질문은 interpret 카드
def test_controller_sees_codes_not_messages(tmp_path, detailed_doc):
    # 판단자에게 간 모든 메시지 내용에 '천원'·'1,000배'·숫자 금액이 없고 'UNIT_SCALE'은 있다
```

- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: 전체 PASS** (기존 질문 순서 테스트 포함)
- [ ] **Step 5: Commit** — `feat: 에이전트 청구서 해석 확인 카드와 guard`

---

### Task 8: 가정값 카드의 사내 인건비·판관비 + 계산 후 기준선

**Files:**
- Modify: `cost-agent/app/agent/models.py` (`AgentSession.checks: list[BenchFinding] = []`)
- Modify: `cost-agent/app/agent/orchestrator.py` (`_q_assumptions`, `_answer_assumptions`, `_final_items`, `_do_simulate`)
- Test: `cost-agent/tests/test_agent.py`

**Interfaces:**
- Consumes: `inhouse_suggestion`, `gpu_checks`, `cost_check` (Task 6)
- 입력칸 (목표 마진 다음, 환율 앞): `FieldSpec(name="inhouse_annual", label="사내 인건비·판관비 (연간)", required=False, value=inhouse_suggestion(...), hint=...)`. hint: 제안값이 있으면 `예시값: 현대차증권 표 18 기타 운영비(18억원/MW/년) − 청구서 네트워크·운영 · 이미 청구서 운영비에 들어 있으면 0`, 없으면 `청구서에 없는 운영 인력·영업·관리 비용 · 참고: 현대차증권 표 18 기타 운영비 18억원/MW/년`.
- `_answer_assumptions`: 빈칸·None → 0.0, 숫자 아님/음수 → `AgentError("사내 인건비·판관비는 0 이상의 숫자로 입력하세요.")`. `a["inhouse_annual"]`에 저장. `gpu_checks(s.draft.contract, a["gpu_count"])` 결과를 `s.checks`에 넣고 `warn` 이벤트와 board ASSUMPTIONS details로 남긴다 (막지 않음).
- `_final_items`: `inhouse_annual > 0`이면 OPERATIONS USER_INPUT 항목에 더한다 (같은 통화). OPERATIONS가 INCLUDED/NOT_INCLUDED이거나 없으면 그 자리를 `USER_INPUT, amount=inhouse, currency=draft.currency, period=YEAR`로 둔다.
- `_do_simulate` 성공 후 `cost_check(float(r["hourlyCost"]))`를 `s.checks`에 더하고 board SIMULATE details·`warn` 이벤트로 남긴다 (막지 않음). 다시 계산하면 이전 COST_PER_GPU_HOUR 항목을 바꾼다.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_inhouse_field_suggested_from_contract(Agent, tmp_path, detailed_doc):
    # r1 흐름에서 가정값 카드 → fields["inhouse_annual"].value == 1_337_000_000, required False
def test_inhouse_amount_added_to_operations(Agent, tmp_path, detailed_doc):
    # inhouse_annual=1e9로 답 → BE 요청 OPERATIONS 금액 == 청구서 운영 연간 + 1e9
def test_blank_inhouse_keeps_request_unchanged(Agent, tmp_path, doc):
    # 기존 c 흐름: 요청의 OPERATIONS가 지금과 같은 12,000,000 (기존 단언 유지)
def test_gpu_check_warns_without_blocking(Agent, tmp_path, detailed_doc):
    # r1, gpu_count=64 → s.checks에 KW_PER_GPU, 흐름은 SIMULATE 이후로 진행
```

- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: 전체 PASS**
- [ ] **Step 5: Commit** — `feat: 가정값 카드에 사내 인건비·판관비, 계산 후 기준선 경고`

---

### Task 9: 근거서 시트, 채점·평가 갱신, 마무리

**Files:**
- Modify: `cost-agent/app/agent/report.py` (새 시트)
- Modify: `cost-agent/tests/test_detailed_samples.py` (xfail 표시 삭제)
- Modify: `cost-agent/samples-detailed/README.md` (채점 결과 표 갱신)
- Test: `cost-agent/tests/test_agent.py` (근거서)

**Interfaces:**
- `build_report`: `가정값` 다음에 `청구서 해석` 시트. 머리글 `구분 | 내용 | 적용 | 해제 시 연간 원가 | 근거 줄`. 해석 행: `해석 | message | 적용/해제 | {impact:.3g}배 | refs 쉼표`. 그 아래 기준선 행(`s.draft.bench` + `s.checks`): `기준선 검사 | message | - | {low:,.0f}~{high:,.0f} | source`. 해석·기준선이 모두 없으면 `해석 변경 없음` 한 행.

- [ ] **Step 1: 실패하는 테스트** — r2 흐름 끝까지 진행한 근거서에 `청구서 해석` 시트가 있고 `UNIT_SCALE` 문구 행의 적용 칸이 `적용`.
- [ ] **Step 2: FAIL** · **Step 3: 구현**
- [ ] **Step 4: `tests/test_detailed_samples.py`의 xfail 두 개를 지우고 전체 실행** — `.venv/bin/python -m pytest -q` → 모두 PASS
- [ ] **Step 5: 채점·평가 실행**
  - `.venv/bin/python scripts/score_detailed_samples.py` → 해석 적용 표: r1~r5 줄 분류 전부 맞음, 운영원가 1.00배(±0.01), 1회성 일치. 해석 해제 표: r4 ≈0.553배, r5 ≈3배.
  - `.venv/bin/python scripts/eval_agent.py --no-llm` → a~i 질문 순서·제안가가 바뀌기 전과 같다 (먼저 `git stash` 상태에서 한 번 돌려 표를 저장해 두고 비교).
  - 두 표를 `samples-detailed/README.md`의 채점 결과에 붙인다.
- [ ] **Step 6: Commit** — `feat: 근거서 '청구서 해석' 시트, 상세 샘플 인수 테스트 통과`
