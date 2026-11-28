# 원가 에이전트 — PDF 입력 (텍스트 우선, 스캔은 로컬 OCR) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PDF 청구서를 올리면 페이지마다 텍스트가 있으면 pdfplumber로, 없으면 로컬 RapidOCR로 읽어 지금과 같은 `ExtractedDocument`(행 형식)를 만들고, 기존 파이프라인이 그대로 원가·제안가·근거서를 낸다.

**Architecture:** 새 모듈 `app/pdfread.py`(페이지 판별·단어→줄·칸)와 `app/ocr.py`(OCR 엔진 인터페이스, RapidOCR 구현)를 `app/parsing.py`의 `.pdf` 분기에서 쓴다. 두 경로는 같은 "단어 목록 → 줄·칸" 함수를 공유한다. OCR 저신뢰 숫자는 `BillDraft.bench`의 `OCR_LOW_CONFIDENCE`로 올려 기존 해석 확인 카드·승인 검토·리스크로 흘려보낸다.

**Tech Stack:** Python 3.11, pdfplumber(MIT), pypdfium2(Apache/BSD), RapidOCR(Apache-2.0)+onnxruntime, reportlab(개발용 샘플 생성), pytest · FE vanilla JS

**Spec:** `docs/superpowers/specs/2026-11-27-cost-agent-pdf-input-design.md`

## Global Constraints

- 문서는 외부로 보내지 않는다 (외부 OCR API·LLM 비전 없음). OCR은 서버 안에서만.
- PyMuPDF(AGPL), Surya(모델 상업 제한)는 쓰지 않는다.
- 페이지 판별: 글자 수 ≥ 20 **그리고** 깨진 글자(`�`, `(cid:` 패턴) 비율 < 5% → 텍스트 페이지, 아니면 OCR 페이지.
- 시트 이름은 `"PDF"` 하나, 행 번호는 페이지를 넘어 이어진다. 쪽 번호는 `page_of`에.
- `OCR_ENGINE` 기본 `rapidocr`, `off` 허용. `OCR_MIN_CONF` 기본 `0.8`.
- OCR이 필요한데 엔진이 없으면 문구 그대로: `스캔된 페이지가 있어 OCR이 필요해요. 텍스트가 있는 PDF나 엑셀로 올려 주세요`. 암호 PDF: `암호가 걸린 PDF는 열 수 없어요. 암호를 풀고 올려 주세요`.
- 의존성: 런타임 `pdfplumber>=0.11`; extra `ocr = ["rapidocr>=3", "onnxruntime>=1.18"]`; dev에 `reportlab>=4`. Dockerfile은 `.[ocr]`로 설치.
- 기존 엑셀·CSV 경로와 테스트는 그대로.
- 브랜치: `feat/price-approval`(아직 push 전) 위에 `feat/pdf-input`을 쌓는다.

## Review Focus

1. **페이지 번호·머리말 줄** (`- 1 -`, `1 / 2`) → 금액으로 읽혀 원가 줄이 되면 안 된다. (Task 2 `test_page_footer_is_not_a_cost_line`)
2. **두 줄로 접힌 항목명** (PDF 표에서 긴 항목명이 다음 줄로 넘어감) → 금액이 있는 줄의 항목명이 앞부분만 남더라도 분류·총액 대조가 깨지지 않아야 한다. (Task 2 `test_wrapped_label_keeps_amount_row`)
3. **OCR이 쉼표를 마침표로 읽음** (`1.234.000`) → 숫자로 읽지 못해 그 줄 금액이 빠지면 총액 불일치로 드러나야 한다 (조용히 빠지면 안 됨). (Task 5 `test_misread_number_surfaces_as_mismatch`)
4. **괄호 음수·원 표시** (`(500,000)`, `1,200,000원`) → 텍스트 PDF에서도 지금 `parse_number`와 같게. (Task 2 `test_cells_keep_number_formats`)
5. **가로 페이지·여백이 큰 표** → 칸 나누기가 머리글과 금액 열을 같은 열 번호로 맞춰야 금액 열 역할(columns)이 동작한다. (Task 6 r1·r3 PDF가 1.00배로 통과하는 것으로 확인)

---

### Task 1: 의존성과 샘플 PDF 생성기

**Files:**
- Modify: `cost-agent/pyproject.toml`, `cost-agent/Dockerfile`
- Create: `cost-agent/scripts/make_pdf_samples.py`
- Modify: `cost-agent/tests/conftest.py` (fixture)
- Test: `cost-agent/tests/test_pdf_samples.py`

**Interfaces:**
- Produces: `make_pdf_samples.to_pdf(xlsx_path: Path, pdf_path: Path) -> None` (엑셀 각 시트를 표 모양 PDF로; 시트마다 새 쪽, 칸 사이 가로 간격 일정, 한글 CID 글꼴 `HYSMyeongJo-Medium`), `to_scanned(pdf_path: Path, out_path: Path, dpi: int = 150) -> None` (pypdfium2로 이미지 → reportlab로 이미지만 든 PDF), `to_mixed(text_pdf, scanned_pdf, out_path)` (1쪽 텍스트 + 2쪽 스캔).
- Produces (conftest): fixture `pdf_samples` (session) → `{name: {"text": Path, "scan": Path}}` (상세 샘플 r1~r5), `mixed_pdf` → Path.

- [ ] **Step 1: 의존성 설치** — `.venv/bin/pip install "pdfplumber>=0.11" "rapidocr>=3" "onnxruntime>=1.18" "reportlab>=4"`, pyproject·Dockerfile 반영.
- [ ] **Step 2: 실패하는 테스트**

```python
def test_text_pdf_has_korean_text(pdf_samples):
    import pdfplumber
    with pdfplumber.open(pdf_samples["bill_r1_kr_metered_krw.xlsx"]["text"]) as p:
        t = p.pages[0].extract_text()
    assert "고밀도 랙 상면료" in t and "537,141,920" in t
def test_scanned_pdf_has_no_text(pdf_samples):
    import pdfplumber
    with pdfplumber.open(pdf_samples["bill_r1_kr_metered_krw.xlsx"]["scan"]) as p:
        assert (p.pages[0].extract_text() or "").strip() == ""
```
- [ ] **Step 3: FAIL 확인 → 구현 → PASS.** CID 글꼴에서 pdfplumber가 한글을 못 뽑으면 ledger에 Ruling을 남기고 `/System/Library/Fonts/Supplemental/AppleGothic.ttf` 또는 `fonts-nanum`의 `NanumGothic.ttf`를 찾아 쓰는 방식으로 바꾼다 (없으면 테스트 skip 사유 명시).
- [ ] **Step 4: Commit** — `build: PDF 읽기 의존성과 샘플 PDF 생성기`

---

### Task 2: 텍스트 페이지 → 줄·칸 (`pdfread.py`)

**Files:**
- Create: `cost-agent/app/pdfread.py`
- Test: `cost-agent/tests/test_pdfread.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass
  class Word:
      text: str; x0: float; x1: float; top: float; bottom: float; conf: float = 1.0
  def is_text_page(text: str) -> bool                       # Global Constraints 규칙
  def words_to_rows(words: list[Word]) -> list[list[Word]]  # 줄마다 칸(단어 묶음)
  def row_cells(row: list[list[Word]]) -> list[str]         # 칸 글자
  def text_page_words(page) -> list[Word]                    # pdfplumber page.extract_words(keep_blank_chars=False, use_text_flow=False)
  ```
- 줄 묶기: 단어 세로 중심이 현재 줄 중심과 `0.5 × 중앙 글자 높이` 이내면 같은 줄. 칸 나누기: 같은 줄에서 앞 단어 끝과 다음 단어 시작 간격이 `2 × 중앙 글자 폭`(글자 폭 = 단어 폭 ÷ 글자 수)보다 크면 새 칸.
- **열 맞춤**: 문서에서 가장 칸이 많은 머리글 줄(숫자 없는 줄 중 칸 수 최대)의 칸 시작 x 좌표를 기준 열로 삼고, 다른 줄의 칸을 가장 가까운 기준 열 번호에 놓는다 (빈 열은 `""`). 그래야 `columns.column_roles`가 머리글 열 번호와 금액 열 번호를 맞춘다.

- [ ] **Step 1: 실패하는 테스트** (`Word`로 직접 만든 합성 입력)

```python
def test_words_group_into_lines_and_cells(): ...          # 두 줄, 줄마다 3칸 → [["항목","수량","금액"],["전력 사용료","1","5,000"]]
def test_columns_align_to_header(): ...                    # 금액만 있는 합계 줄도 머리글의 '금액' 열 번호로
def test_page_footer_is_not_a_cost_line(): ...             # Review Focus 1: "- 1 -" 줄 → 행은 되지만 _make_row 후 label 없음 → RulesMapper가 IGNORE
def test_wrapped_label_keeps_amount_row(): ...             # Review Focus 2: "고밀도 랙" / "상면료 4,200,000" → 금액 줄에 금액 칸 유지
def test_cells_keep_number_formats(): ...                  # Review Focus 4: "(500,000)" → -500000, "1,200,000원" → 1200000
def test_is_text_page_rule():
    assert is_text_page("가" * 20) and not is_text_page("가" * 19)
    assert not is_text_page("(cid:12)" * 10 + "가" * 30)
```
- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat: PDF 단어를 줄·칸으로 (텍스트 페이지)`

---

### Task 3: OCR 엔진 (`ocr.py`)

**Files:**
- Create: `cost-agent/app/ocr.py`
- Modify: `cost-agent/app/config.py` (`ocr_engine: str`, `ocr_min_conf: float`)
- Test: `cost-agent/tests/test_ocr.py`

**Interfaces:**
- Produces:
  ```python
  class OcrEngine(Protocol):
      name: str
      def words(self, image: "PIL.Image.Image") -> list[Word]: ...
  class RapidOcrEngine:      # rapidocr.RapidOCR(); 결과 상자(4점)·글자·점수 → Word(bbox의 min/max, conf=점수)
      name = "rapidocr"
  def engine_from_settings(engine: str) -> OcrEngine | None   # "off" 또는 import 실패 → None
  def page_image(pdf_bytes: bytes, page_index: int, dpi: int = 200) -> "PIL.Image.Image"   # pypdfium2
  ```
- RapidOCR는 한국어 인식 모델을 쓰도록 설정한다 (`rapidocr` 3.x의 언어 파라미터로 korean; 정확한 인자는 설치 버전 문서를 따르고 ledger에 남긴다).

- [ ] **Step 1: 실패하는 테스트**

```python
def test_engine_off_returns_none(): assert engine_from_settings("off") is None
def test_rapidocr_reads_scanned_sample(pdf_samples):
    eng = engine_from_settings("rapidocr")
    if eng is None: pytest.skip("rapidocr 미설치")
    img = page_image(pdf_samples["bill_r1_kr_metered_krw.xlsx"]["scan"].read_bytes(), 0)
    text = " ".join(w.text for w in eng.words(img))
    assert "상면료" in text and all(0 <= w.conf <= 1 for w in eng.words(img))
```
- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat: 로컬 RapidOCR 엔진 (한국어)`

---

### Task 4: `extract`의 PDF 분기

**Files:**
- Modify: `cost-agent/app/parsing.py` (`extract`), `cost-agent/app/models.py` (`ExtractedDocument`)
- Modify: `cost-agent/app/main.py` (서비스 생성 시 OCR 엔진 주입), FE는 Task 7
- Test: `cost-agent/tests/test_parsing.py`

**Interfaces:**
- `ExtractedDocument`에 추가: `source_kind: str = "xlsx"` (`xlsx`·`csv`·`pdf-text`·`pdf-ocr`·`pdf-mixed`), `ocr_low_confidence: list[str] = []` (신뢰도 < `ocr_min_conf`인 숫자 칸이 있는 행 ref), `page_of: dict[str, int] = {}`.
- `extract(file_name: str, data: bytes, ocr: OcrEngine | None = None, ocr_min_conf: float = 0.8) -> ExtractedDocument` — `.pdf`: pdfplumber로 열고(암호 → `UnsupportedFile` 위 문구), 페이지마다 `is_text_page(page.extract_text() or "")`로 경로 결정 → `Word` 목록 → `words_to_rows` → 전체 문서 기준 열 맞춤 → `_make_row("PDF", n, cells)`. OCR 페이지에서 숫자로 읽힌 칸의 단어 `conf < ocr_min_conf`면 그 행 ref를 `ocr_low_confidence`에. `MAX_ROWS` 넘으면 `truncated`.
- API(`/v1/bills`, `/v1/agent/sessions`)는 `Settings`의 OCR 엔진을 넘긴다.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_text_pdf_extracts_like_xlsx(pdf_samples, detailed_doc):
    d = extract("r1.pdf", pdf_samples["bill_r1_kr_metered_krw.xlsx"]["text"].read_bytes())
    assert d.source_kind == "pdf-text" and d.sheets == ["PDF"]
    labels = {r.label for r in d.rows}
    assert any("고밀도 랙 상면료" in l for l in labels)
def test_scanned_pdf_without_engine_is_refused(pdf_samples):
    with pytest.raises(UnsupportedFile, match="OCR이 필요해요"):
        extract("s.pdf", pdf_samples["bill_r1_kr_metered_krw.xlsx"]["scan"].read_bytes(), ocr=None)
def test_mixed_pdf_kind(mixed_pdf): ...       # rapidocr 있으면 source_kind == "pdf-mixed", 두 쪽 행 모두 있음, page_of 값 {1, 2}
def test_encrypted_pdf_is_refused(tmp_path): ...  # reportlab encrypt로 만든 PDF → "암호가 걸린 PDF"
```
- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: 전체 PASS**
- [ ] **Step 5: Commit** — `feat: PDF 업로드 — 텍스트 페이지는 pdfplumber, 스캔 페이지는 로컬 OCR`

---

### Task 5: OCR 저신뢰 숫자 경고

**Files:**
- Modify: `cost-agent/app/service.py` (`_build`의 `bench`)
- Test: `cost-agent/tests/test_pdf_pipeline.py`

**Interfaces:**
- `_build`: `doc.ocr_low_confidence`가 있으면 `bench`에 `BenchFinding(code="OCR_LOW_CONFIDENCE", value=len(refs), low=0, high=0, source=f"RapidOCR 신뢰도 < {min_conf}", message=f"OCR로 읽은 숫자 {n}개는 신뢰도가 낮아요 — {쪽·줄 목록 앞 5개} 원본과 비교해 주세요")`를 더한다 (쪽은 `page_of`). 기존 해석 확인 카드(`bench:` 입력칸)·승인 검토 `BENCH_WARN`·리스크가 그대로 받는다.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_low_confidence_numbers_become_bench_warning(service): ...   # ocr_low_confidence=["PDF!7"], page_of {"PDF!7": 1} 문서 → bench에 OCR_LOW_CONFIDENCE, 메시지에 "1쪽"
def test_misread_number_surfaces_as_mismatch(service):               # Review Focus 3
    # "1.234.000"처럼 숫자로 못 읽힌 원가 줄이 있는 문서 → reconciliation.matched is False (조용히 맞지 않음)
```
- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat: OCR 저신뢰 숫자를 해석 확인 카드와 리스크로`

---

### Task 6: PDF 인수 채점

**Files:**
- Modify: `cost-agent/scripts/score_detailed_samples.py` (`--pdf text|scan`)
- Create: `cost-agent/tests/test_pdf_acceptance.py`
- Modify: `cost-agent/samples-detailed/README.md` (결과 표)

**Interfaces:**
- `score(path, key, service, off_all=False, ocr=None)` — `path`가 `.pdf`면 `extract(..., ocr=ocr)`.

- [ ] **Step 1: 테스트**

```python
@pytest.mark.parametrize("name", list(BUILDERS))
def test_text_pdf_scores_like_xlsx(name, pdf_samples, detailed, service):
    r = score(pdf_samples[name]["text"], detailed[name][1], service)
    assert r["errors"] == [] and r["run_ratio"] == pytest.approx(1.0, abs=0.01)

@pytest.mark.parametrize("name", list(BUILDERS))
def test_scanned_pdf_never_silently_wrong(name, pdf_samples, detailed, service):
    eng = engine_from_settings("rapidocr")
    if eng is None: pytest.skip("rapidocr 미설치")
    r = score(pdf_samples[name]["scan"], detailed[name][1], service, ocr=eng)
    d = r["draft"]
    flagged = d.reconciliation.matched is False or bool(d.bench)   # 총액 불일치 또는 OCR 저신뢰·기준선 경고
    assert r["run_ratio"] == pytest.approx(1.0, abs=0.01) or flagged
```
- [ ] **Step 2: 실행** — 텍스트 PDF가 1.00배가 아니면 Task 2의 줄·칸·열 맞춤을 고친다 (테스트를 느슨하게 하지 않는다). 스캔판 정확도(줄 분류, 배수)를 README 표에 기록.
- [ ] **Step 3: Commit** — `test: PDF 텍스트판·스캔판 인수 채점`

---

### Task 7: FE 업로드와 마무리

**Files (GPUAU-FE, `feat/cost-agent-review` 위 또는 새 브랜치):**
- Modify: `docker_erd_20261117/app/web/static/cost-agent-ui.mjs` (`accept=".xlsx,.csv,.pdf"`, 안내 문구 "엑셀·CSV·PDF")
- Test: `tests/cost-agent-ui.test.mjs`
- Modify (AI): `cost-agent/README.md` (PDF·OCR 설정, `OCR_ENGINE`·`OCR_MIN_CONF`)

- [ ] **Step 1: 실패하는 테스트** — `renderStart()` HTML에 `accept=".xlsx,.csv,.pdf"`
- [ ] **Step 2: FAIL** · **Step 3: 구현** · **Step 4: `node --test tests/*.test.mjs` PASS**
- [ ] **Step 5: Commit** (FE) — `✨ [FEAT] 원가 에이전트 PDF 업로드 허용` · (AI) — `docs: PDF·OCR 설정 안내`
