# 원가 에이전트 — 부서별 문서로 원가 기준표 채우기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 한 세션에 부서별 문서 여러 개(PDF·엑셀)를 받아 원가 기준표를 칸마다 출처와 함께 채우고, 빈칸·충돌·저신뢰 칸을 담당자가 확인하게 한 뒤, 기준표로 스프링 원가 계산 → 판매가·시장 위치·검증까지 잇는다.

**Architecture:** 문서마다 종류를 판별해(`docs.py`) 견적서·부서 명세는 칸 추출(규칙 → 가린 LLM 위치 매핑, 값은 `values.py`가 원본에서 읽음), 청구서는 기존 파이프라인으로 원가 항목을 만든다. `sheetmerge.py`가 문서별 결과를 `CostSheet`(`costsheet.py`)로 합치며 교차 확인·우선순위·충돌을 정하고, 에이전트는 기준표 확인 카드(기존 MISSING 단계)와 가정값 카드(기준표 값으로 미리 채움)를 거쳐 기준표에서 BE 요청을 만든다.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, pdfplumber, RapidOCR, LangGraph, pytest · FE vanilla JS

**Spec:** `docs/superpowers/specs/2026-11-28-cost-agent-multidoc-costsheet-design.md`

## Global Constraints

- LLM은 숫자를 만들지도 옮기지도 않는다. LLM 응답은 칸마다 `(줄 id, 열 번호)` 또는 없음. 값은 코드가 원본 칸에서 읽는다.
- LLM 전송 내용에 금액·회사명(`솔빛컴퓨팅` 등 `…컴퓨팅`/`(주)`/`(가상 업체)` 앞 단어/`Inc.`/`Ltd.`)·견적번호(`[A-Z]{2,}-\d{4,}(-\d+)?`)·사업장명(설정 `SITE_NAMES`, 기본 `울산`)이 없어야 한다.
- 부서 간 값이 다르면 `conflict` — 자동으로 고르거나 더하지 않는다. 같다 = 상대 차이 ≤ 0.5%.
- 우선순위: 담당자 입력 > 사내 문서(표) > 공개값. 문장 속 값은 교차 확인 전용.
- 원가 칸은 **스프링 원가 항목만** (유지보수·라이선스·보험 등 스프링에 없는 요소는 다루지 않음). `ItemType`·`LineRole`·`Period` enum은 늘리지 않는다. OPERATIONS는 구성 요소(청구서 운영비, 사내 인건비·판관비)를 나눠 보여 주고 합산한다.
- `Step` 값은 그대로(FE `STEP_ORDER` 유지). MISSING 단계의 제목만 `기준표 확인`.
- API는 `files`(여러 개)와 기존 `file`(하나)을 모두 받는다. 파일 하나면 지금과 같은 결과.
- 브랜치: `feat/pdf-input` 위에 `feat/cost-sheet`.

## Review Focus

1. **같은 비용이 청구서와 견적서 모두에 있음** (예: 설치비가 시설 청구서와 구매 견적 둘 다) → 두 번 더하지 않고 `conflict`로. (Task 6 `test_same_cost_in_two_docs_is_not_added_twice`)
2. **값은 같은데 단위만 다름** (`1,009,000,000원` vs `10.09억원`, `5년` vs `60개월`) → 같은 값으로 교차 확인. (Task 1 `test_units_normalize_for_comparison`)
3. **부서 문서가 하나도 없는 칸만 남은 경우** (청구서 한 장만 업로드) → 지금 흐름과 같은 질문 순서·결과. (Task 8 `test_single_bill_upload_behaves_as_before`)
4. **문서 종류를 못 정하는 문서** (제목이 없거나 이미지뿐) → 청구서로 처리하고 "문서 종류를 확인해 주세요" 표시, 조용히 버리지 않음. (Task 3 `test_unknown_doc_falls_back_to_bill_with_notice`)
5. **충돌 칸에서 담당자가 직접 다른 값 입력** → 출처 '담당자 입력', 원본 대조에서 두 문서 값과 다르다고 표시. (Task 8 `test_manual_value_on_conflict_is_flagged_against_sources`)

---

### Task 1: 목데이터 고정 · `\x01` 정리 · 값 읽기 (`values.py`)

**Files:**
- Create: `cost-agent/samples-depts/` (팀 목데이터 8개 PDF 그대로 + `answer_key.json`), `cost-agent/app/values.py`
- Modify: `cost-agent/app/pdfread.py` (`text_page_words`: `\x01` → 공백), `cost-agent/tests/conftest.py` (fixture `dept_docs`)
- Test: `cost-agent/tests/test_values.py`, `cost-agent/tests/test_pdfread.py`

**Interfaces:**
- Produces: `@dataclass Value(number: float, unit: str, currency: str | None, text: str)`; `parse_value(text: str) -> Value | None`; `same_value(a: Value, b: Value, tol: float = 0.005) -> bool` (통화·단위 정규화 후 비교: `억원`×1e8, `만원`×1e4, `개월`÷12→`년`, `%`는 0~1).
- 단위 정규화: `장`·`대`·`세트`·`년`·`%`·`원`·`KRW`·`USD`·`GPU·시간`·`kWh`. `1USD=1,350KRW` → `Value(1350, "KRW/USD", "KRW")`. `H100 80GB` → `Value(nan, "model", None, "H100 80GB")`(모델명은 `text`로). `약` 접두어 허용.
- `answer_key.json`: 설계 §5 첫 줄의 칸별 정답 + 08 검산(시간당 원가 1,885원, 판매가 2,356원, 1.74USD).

- [ ] **Step 1: 실패하는 테스트**

```python
@pytest.mark.parametrize("text,num,unit,cur", [
    ("32장", 32, "장", None), ("5년", 5, "년", None), ("70%", 0.7, "%", None),
    ("1,009,000,000원", 1_009_000_000, "원", "KRW"), ("1USD=1,350KRW", 1350, "KRW/USD", "KRW"),
    ("약 1,885원", 1885, "원", "KRW"), ("약1.74USD", 1.74, "USD", "USD"), ("280,320 GPU·시간", 280320, "GPU·시간", None)])
def test_parse_value(text, num, unit, cur): ...
def test_model_name(): assert parse_value("H100 80GB").text == "H100 80GB"
def test_units_normalize_for_comparison():   # Review Focus 2
    assert same_value(parse_value("1,009,000,000원"), parse_value("10.09억원"))
    assert same_value(parse_value("5년"), parse_value("60개월"))
def test_dept_pdf_spaces_are_not_control_chars(dept_docs):
    assert all("\x01" not in c for r in dept_docs["01"].rows for c in r.cells)
```
- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: PASS (전체)**
- [ ] **Step 5: Commit** — `feat: 부서별 목데이터, PDF 띄어쓰기 정리, 단위 있는 값 읽기`

---

### Task 2: 기준표 모델과 BE 요청 변환 (`costsheet.py`)

**Files:**
- Create: `cost-agent/app/costsheet.py`
- Test: `cost-agent/tests/test_costsheet.py`

**Interfaces:**
- Produces:
  ```python
  class Source(BaseModel): doc: str; page: int | None; row: int; col: int; text: str; conf: float = 1.0
  class Cell(BaseModel):
      key: str; value: float | str | None = None; unit: str = ""; currency: str | None = None
      period: str | None = None   # YEAR | ONE_TIME | MONTH(원문)
      status: str = "empty"       # filled | included | excluded | empty | conflict
      sources: list[Source] = []; candidates: list[dict] = []; grade: str = ""; owner: str = ""; note: str = ""
  class CostSheet(BaseModel): cells: dict[str, Cell]
  FIELDS: list[FieldSpecDef]   # key, label, kind("cost"|"assumption"), be_item, period, owner
  def empty_sheet() -> CostSheet
  def to_cost_items(sheet: CostSheet) -> list[CostItemDraft]      # 칸 = BE 항목. OPERATIONS는 구성 요소 합
  def assumption_values(sheet: CostSheet) -> dict[str, Any]       # 가정값 카드 미리 채우기용 (model_name, gpu_count, depreciation_years, utilization 0~1, pricing_method, target_rate 0~1, fx_krw_per_usd, …)
  ```
- `Cell.components: list[dict]`(`{"name","value","sources"}`) — OPERATIONS의 구성 요소(`청구서 운영비`, `사내 인건비·판관비`). 값 = 구성 요소 합.
- 칸 키: `rack`, `rent`, `electricity`, `network`, `operations`, `gpu_lease`, `memory`, `other`, `acquisition`, `gpu_model`, `gpu_count`, `depreciation_years`, `utilization`, `pricing_method`, `target_rate`, `fx_krw_per_usd`, `interest_rate_type`, `annual_interest_rate`, `discount_3`, `discount_5`.
- owner: acquisition → `구매팀·재무회계팀`, annual_interest_rate·fx → `자금팀`, operations(사내 인건비) → `관리회계팀`, utilization → `영업팀`, gpu_model·gpu_count → `인프라운영팀`, target_rate → `사업기획팀`, 시설 칸 → `시설운영팀`.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_operations_components_are_summed():
    s = empty_sheet(); add_component(s, "operations", "청구서 운영비", 12_000_000); add_component(s, "operations", "사내 인건비·판관비", 72_000_000)
    ops = next(i for i in to_cost_items(s) if i.item_type.value == "OPERATIONS")
    assert ops.amount == 84_000_000 and ops.period.value == "YEAR"
def test_included_and_excluded_cells_map_to_status(): ...   # rent included in rack → INCLUDED_IN_OTHER(included_in=RACK_FACILITY); excluded → NOT_INCLUDED
def test_assumption_values_from_cells(): ...                # utilization 70% → 0.7, target_rate 20% → 0.2, gpu_count 32
```
- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat: 원가 기준표 모델과 BE 요청 변환`

---

### Task 3: 문서 종류 판별 (`docs.py`)

**Files:**
- Create: `cost-agent/app/docs.py`
- Test: `cost-agent/tests/test_docs.py`

**Interfaces:**
- Produces: `class DocKind(str, Enum): QUOTE, BILL, MEMO, PLAN`; `classify_doc(doc: ExtractedDocument) -> tuple[DocKind, str]` (종류, 근거 문구). 첫 8줄의 글자로: `견적` → QUOTE · `청구|영수증|수납|시설비|이용료` → BILL · `검토안` + (`판매가` 또는 `시간당`) → PLAN · `명세|계획|기준` → MEMO · 없으면 BILL + 근거 `"문서 종류를 확인해 주세요"`.

- [ ] **Step 1: 실패하는 테스트**

```python
@pytest.mark.parametrize("code,kind", [("01","QUOTE"),("02","MEMO"),("03","BILL"),("04","MEMO"),("05","MEMO"),("06","MEMO"),("07","MEMO"),("08","PLAN")])
def test_dept_doc_kinds(dept_docs, code, kind): assert classify_doc(dept_docs[code])[0].value == kind
def test_detailed_bills_are_bills(detailed_doc): ...          # r1~r5 → BILL
def test_unknown_doc_falls_back_to_bill_with_notice():         # Review Focus 4
    k, why = classify_doc(doc_from_rows([["", "1,000"]])); assert k.value == "BILL" and "확인" in why
```
- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat: 문서 종류 판별 (견적·청구·부서 명세·검산)`

---

### Task 4: 견적서·부서 명세 칸 추출 (규칙)

**Files:**
- Modify: `cost-agent/app/docs.py`
- Test: `cost-agent/tests/test_docs.py`

**Interfaces:**
- Produces: `@dataclass FieldHit(key: str, value: Value, source: Source, role: str)` (`role`: `table` | `prose` | `result`); `extract_fields(doc: ExtractedDocument, kind: DocKind, doc_name: str) -> list[FieldHit]`.
- MEMO·PLAN 표 줄: 항목명 키워드 → 칸 (스프링 칸이 아닌 항목은 버린다), 값은 **마지막 값 칸**(제출값·결과·연간배부액·공급가액 열)에서 `parse_value`. 계산 근거 칸의 숫자는 쓰지 않는다. 키워드(시작점): `GPU 모델`→gpu_model · `서버 구성`/`GPU 수`→gpu_count · `취득비`→acquisition · `내용연수`/`감가 연수`→depreciation_years · `유료 가동률`/`가동률`→utilization · `계획 환율`/`환율`→fx_krw_per_usd · `목표 마진`→target_rate · `마진 방식`/`매출기준`→pricing_method · `사내 인건비`/`판관비`/`합계`(관리회계 문서 안)→operations 구성 요소 `사내 인건비·판관비` · `연간 원가`·`시간당 원가`·`판매가` (PLAN) → `role="result"` 키 `plan_annual_cost`·`plan_hourly_cost`·`plan_price`.
- QUOTE: 원가 줄은 모두 acquisition 후보, `공급가액 합계` 줄 값이 있으면 그것만 `table`로(부가세 제외), 품목·수량에서 `gpu_model`·`gpu_count`(= 서버 대수 × `×N장`) 힌트.
- 문장 줄의 값(`…원입니다`)은 `role="prose"`.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_dept_fields_match_answer_key(dept_docs, dept_key):
    hits = {(h.key, h.role): h for code, d in dept_docs.items() for h in extract_fields(d, classify_doc(d)[0], code)}
    assert hits[("acquisition", "table")].value.number == 1_009_000_000
    assert hits[("gpu_count", "table")].value.number == 32 and hits[("depreciation_years", "table")].value.number == 5
    assert hits[("utilization", "table")].value.number == 0.7 and hits[("fx_krw_per_usd", "table")].value.number == 1350
    assert hits[("operations", "table")].value.number == 72_000_000 and hits[("target_rate", "table")].value.number == 0.2
    assert hits[("plan_hourly_cost", "result")].value.number == 1885
def test_calculation_basis_numbers_are_not_values(dept_docs): ...   # 02의 '32장×365일×24시간'이 gpu_count가 되지 않음
def test_prose_values_are_marked_prose(dept_docs): ...              # 01 '공급가액은 1,009,000,000원입니다' → role prose
```
- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat: 견적서·부서 명세에서 기준표 칸 추출`

---

### Task 5: 가린 LLM 위치 매핑 + 마스킹 확장

**Files:**
- Create: `cost-agent/app/mapping/fields_llm.py`
- Modify: `cost-agent/app/privacy.py` (회사·견적번호·사업장 패턴), `cost-agent/app/config.py` (`site_names: list[str]`, 기본 `["울산"]`)
- Test: `cost-agent/tests/test_fields_llm.py`, `cost-agent/tests/test_privacy.py`

**Interfaces:**
- Produces: `map_fields_llm(doc: ExtractedDocument, rows: list[SheetRow], keys: list[str], complete: Completion) -> list[tuple[str, str, int]]` — `(칸 키, 줄 ref, 열 번호)`. 프롬프트: 칸 목록(키·뜻) + 가린 줄. 응답 스키마 strict JSON `{"fields":[{"key","ref","col"}]}`. 문서에 없는 ref·숫자 없는 칸은 버린다.
- `extract_fields`(Task 4)는 규칙으로 못 찾은 칸만 이 함수에 넘긴다 (LLM 키가 없으면 건너뜀).
- `mask_text`: 위 Global Constraints 패턴 추가.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_masked_prompt_has_no_vendor_quote_site_or_amounts(dept_docs):
    sent = []
    map_fields_llm(dept_docs["01"], dept_docs["01"].rows, ["acquisition"], lambda s, u, sch: (sent.append(u), '{"fields":[]}')[1])
    for bad in ("솔빛컴퓨팅", "SIM-261126-02", "울산", "1,009,000,000", "250,000,000"):
        assert bad not in sent[0]
def test_llm_answer_is_position_only_and_value_read_by_code(dept_docs): ...  # 가짜 LLM이 (acquisition, 공급가액 합계 줄, 3열) → 값 1,009,000,000은 코드가 읽음
def test_bogus_ref_or_empty_cell_is_dropped(dept_docs): ...
```
- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat: 가린 사본 위치 매핑(LLM)과 회사·견적번호·사업장 가림`

---

### Task 6: 합치기 · 교차 확인 · 우선순위 (`sheetmerge.py`)

**Files:**
- Create: `cost-agent/app/sheetmerge.py`
- Test: `cost-agent/tests/test_sheetmerge.py`

**Interfaces:**
- Produces: `build_sheet(hits: list[FieldHit], bills: list[tuple[str, BillDraft]], public: dict[str, Source] | None = None) -> CostSheet`; `cross_checks(sheet: CostSheet) -> list[str]` (파생 관계 확인 결과 문구).
- 청구서 → 원가 칸: `BillDraft.cost_items`(이미 연간화)를 칸으로(RACK_FACILITY→rack, RENT→rent, ELECTRICITY→electricity, NETWORK_STORAGE→network, OPERATIONS→operations, ACQUISITION→acquisition …), 출처는 그 항목의 `source_refs`.
- 같은 칸 여러 출처: `same_value`면 `filled` + 출처 모두, 아니면 `conflict` + `candidates=[{"doc","value","text"}]`. `prose` 출처는 값 결정에 쓰지 않고 `note`에 "문장 교차 확인 일치/불일치".
- `public`(예: 수출입은행 환율)은 사내 문서 값이 없을 때만 쓰고, 둘 다 있으면 사내 값 + `note`에 공개값과 차이(%).
- 파생 확인: `gpu_count` vs 견적의 서버×장당 GPU, `utilization` vs 영업 문서의 유료 시간 ÷ 공급 시간, `acquisition ÷ depreciation_years` vs 재무회계 '연간 감가상각비'.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_dept_sheet_matches_answer_key(dept_docs, dept_key): ...     # 전기 57,600,000 · 상면 28,800,000 · 회선 9,600,000 · 취득비 1,009,000,000(출처 01·04 두 개) …
def test_conflicting_departments_make_conflict(dept_docs): ...        # 04 취득비를 1,100,000,000으로 바꾼 변형 → status conflict, candidates 2개, value None
def test_same_cost_in_two_docs_is_not_added_twice(): ...              # Review Focus 1: 시설 청구서와 견적서 둘 다 '설치비' → acquisition conflict (합계 아님)
def test_internal_fx_beats_public_fx(dept_docs): ...                  # 1,350 채택, note에 '수출입은행 1,450과 -6.9%'
def test_prose_annual_facility_cross_checks_table(dept_docs): ...     # 03 문장 96,000,000 = (전기+상면+회선) → note '일치', 연간화 한 번
```
- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat: 문서별 결과를 기준표로 합치기 — 교차 확인·충돌·우선순위`

---

### Task 7: 짙은 배경 OCR 전처리

**Files:**
- Modify: `cost-agent/app/ocr.py` (`preprocess(image) -> image`), `cost-agent/scripts/make_pdf_samples.py` (`to_dark_scanned`)
- Test: `cost-agent/tests/test_ocr.py`

**Interfaces:**
- `preprocess`: 회색조 평균 밝기(0~1) < 0.45면 반전, 이어서 `ImageOps.autocontrast`. `RapidOcrEngine.words`가 먼저 적용.
- `to_dark_scanned(pdf, out)`: 배경 `#1F2937`, 글자 흰색으로 렌더링한 스캔판.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_dark_background_is_inverted(): ...     # 평균 밝기 0.2 이미지 → preprocess 후 평균 > 0.5
def test_dark_scan_reads_better_with_preprocess(pdf_samples, tmp_path):
    # r1 짙은 배경판: 전처리 후 인식된 '상면료'·'105,000,000' 포함, 전처리 없이보다 단어 수가 같거나 많음 (수치는 README에 기록)
```
- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat: 짙은 배경 스캔본 OCR 전처리`

---

### Task 8: 에이전트 — 여러 문서 세션과 기준표 확인 카드

**Files:**
- Modify: `cost-agent/app/agent/models.py` (`AgentSession.docs: list[dict]`, `sheet: CostSheet | None`, `Goal`은 그대로), `cost-agent/app/agent/orchestrator.py`, `cost-agent/app/agent/graph.py`, `cost-agent/app/main.py`
- Test: `cost-agent/tests/test_costsheet_agent.py`

**Interfaces:**
- `CostAgent.start_many(docs: list[ExtractedDocument], goal: Goal) -> AgentSession` — 문서마다 `classify_doc`; BILL은 기존 `bills.ingest`(+해석 카드), 나머지는 `extract_fields`. `build_sheet`로 `s.sheet`. `start(doc, goal)`은 `start_many([doc], goal)`.
- 해석 확인 카드: 청구서가 여럿이면 문서별 해석을 한 카드에 (입력칸 이름 `interp:{문서}:{code}`).
- **기준표 확인 카드** (step MISSING, 제목 `기준표를 확인해 주세요 — 확인할 칸 {n}개`): 문제 칸만 —
  `empty` → `cell:{key}` number + `choice:{key}`(`amount`/`included`/`excluded`), hint에 담당 부서 · `conflict` → `cell:{key}` select(후보 값과 문서명) + `amount` 직접 입력 · 저신뢰·해석 변경 칸 → `cell:{key}` number(원문 hint). 스프링 원가 칸(청구서 핵심 4개·취득비)이 비면 여기서 묻는다. 기존 `_missing_groups` 카드는 기준표 경로에서는 쓰지 않는다. 문제 칸이 없으면 카드 없이 통과.
- 답변: 고친 칸은 출처 `담당자 입력` + `grade="담당자 입력"`; 원본 대조(출처 원문 값과 다르면 `note` "원본 ○○과 다름").
- 가정값 카드: 제안값 = `assumption_values(sheet)`, hint = 출처 문서명(예: `07 자금팀 문서`), 등급 `사내 문서`.
- 원가 계산: `to_cost_items(sheet)` + 가정값 → 기존 `build_request`.
- API: `POST /v1/agent/sessions`에 `files: list[UploadFile] = File(None)`; `file`만 오면 기존처럼.

- [ ] **Step 1: 실패하는 테스트** (두 엔진 — `Agent` fixture)

```python
def test_dept_docs_fill_sheet_and_ask_only_problems(Agent, tmp_path, dept_docs): ...
    # 8개 문서 → 스프링 원가 칸이 모두 채워짐 → 기준표 확인 카드 없이 가정값 카드로 (취득비·GPU 수는 교차 확인됨, OPERATIONS = 사내 인건비 72,000,000)
def test_assumption_card_prefilled_from_sheet(Agent, tmp_path, dept_docs): ...   # fx 1,350(07 자금팀 문서), utilization 70, target_rate 20, gpu_count 32
def test_single_bill_upload_behaves_as_before(Agent, tmp_path, doc): ...         # Review Focus 3: c 흐름의 질문 순서·BE 요청 동일
def test_manual_value_on_conflict_is_flagged_against_sources(Agent, tmp_path, dept_docs): ...  # Review Focus 5
def test_api_accepts_multiple_files(tmp_path, dept_docs): ...
```
- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: 전체 PASS** (기존 질문 순서 테스트는 파일 하나 경로라 그대로)
- [ ] **Step 5: Commit** — `feat: 여러 문서 세션과 기준표 확인 카드`

---

### Task 9: 검증 추가 — 검산 대비 · 전력량

**Files:**
- Modify: `cost-agent/app/benchmarks.py` (`POWER_KWH_PER_GPU_MONTH`), `cost-agent/app/agent/orchestrator.py` (검산 비교 기록), `cost-agent/app/agent/verify.py` (리스크)
- Test: `cost-agent/tests/test_costsheet_agent.py`

**Interfaces:**
- 검산 비교: 기준표에 `plan_hourly_cost`·`plan_price`가 있으면 원가 계산 뒤 원화로 환산해 비교(±1% 일치) → 진행 기록·요약에 `"사업기획팀 검산과 일치 (산술 검증 — 원가 누락 여부는 보장하지 않음)"` 또는 차이.
- 기준선 `POWER_PER_GPU`: 전기료 칸이 kWh 원문을 가지면 월 kWh ÷ GPU 수가 900 미만 또는 1,500 초과 → `BenchFinding(code="POWER_PER_GPU")`.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_plan_arithmetic_matches(Agent, tmp_path, dept_docs): ...
    # 8개 문서 그대로 → 원가 ≈ 1,885원/h(±1%), 요약에 '사업기획팀 검산과 일치 (산술 검증)'
def test_low_kwh_per_gpu_warns(): ...   # 03의 24,000kWh ÷ 32장 = 750 → POWER_PER_GPU
```
- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat: 검산 대비 산술 검증, GPU당 전력량`

---

### Task 10: 근거서 '원가 기준표' 시트 · FE 여러 파일 · 인수 채점

**Files:**
- Modify: `cost-agent/app/agent/report.py` (첫 시트 `원가 기준표`), `cost-agent/scripts/score_detailed_samples.py` 또는 새 `scripts/score_dept_docs.py`, `cost-agent/samples-depts/README.md`
- Modify (FE, `feat/cost-agent-review` 위): `cost-agent-ui.mjs` (`<input type="file" multiple>`, `startSession`이 `files`로 전송, 결과에 기준표 표)
- Test: `cost-agent/tests/test_dept_acceptance.py`, FE `tests/cost-agent-ui.test.mjs`

**Interfaces:**
- `원가 기준표` 시트 열: `칸 · 값 · 단위 · 상태 · 출처(문서·쪽·행·열) · 교차 확인 수 · 담당자 수정 · 메모`.
- 인수 채점: 목데이터 8종 → 기준표 칸 정확도(설계 §5) 100%, 08 검산 일치, 마스킹 누출 0.

- [ ] **Step 1: 실패하는 테스트** — `test_dept_sheet_accuracy_is_100pct`, `test_report_first_sheet_is_cost_sheet`, FE `test('upload sends multiple files as files[]')`
- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: 전체 PASS (AI·FE)**, 채점 결과를 `samples-depts/README.md`에 기록
- [ ] **Step 5: Commit** — AI `feat: 근거서 원가 기준표 시트와 부서 문서 인수 채점` · FE `✨ [FEAT] 원가 에이전트 여러 문서 업로드와 기준표 표시`
