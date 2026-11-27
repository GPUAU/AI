# 원가 에이전트 — PDF 청구서·견적서 입력 (텍스트 우선, 스캔은 로컬 OCR)

> 작성 2026-11-27 · 상태: 설계 확정 (담당자 승인)
> 대상 코드: `GPUAU-AI/cost-agent` (`app/parsing.py` 뒤의 파이프라인은 그대로), `GPUAU-FE` 업로드 입력

## 왜

담당자는 IDC 청구서·구매 견적서를 PDF로 받는다. 지금 에이전트는 엑셀·CSV만 받아서, PDF는 담당자가 엑셀로 옮겨 적어야 한다.
문서는 **밖으로 보내지 않는다** (담당자 결정: 네이버 CLOVA 등 외부 OCR API 사용 안 함, sLLM도 이번엔 쓰지 않음).

## 목표 · 하지 않을 것 · 실패로 볼 것

- 목표: 텍스트가 있는 PDF는 파이썬 라이브러리로, 글자가 없는(스캔) 페이지는 서버 안의 OCR 모델로 읽어 **지금과 같은 '행' 형식**으로 바꾼다. 그 뒤(분류 → 해석 → 원가 계산 → 승인 검토 → 근거서)는 그대로 쓴다.
- 하지 않을 것: 외부 OCR API, 비전 LLM·sLLM, 견적서에서 취득비 자동 추출(다음 단계), 정답표 업로드, 손글씨.
- 실패로 볼 것: **OCR이 숫자를 잘못 읽었는데 아무 표시 없이 원가에 들어감.** 틀릴 수는 있어도 조용히 틀리면 안 된다.

## 접근

| 안 | 판단 |
|---|---|
| **텍스트 우선 + 페이지별 로컬 OCR (선택)** | 페이지마다 글자를 먼저 뽑고, 없거나 깨졌으면 그 페이지만 OCR. 시스템이 만든 PDF는 OCR 없이 정확·빠르게 처리 |
| 모든 페이지 OCR | 텍스트 PDF에서도 숫자 오류 위험과 시간만 늘어남 |
| 외부 OCR API (CLOVA) | 문서가 밖으로 나감 — 담당자가 제외 |

- PDF 라이브러리: **pdfplumber (MIT)** — 글자와 위치, 표 영역을 함께 준다. PyMuPDF는 AGPL이라 제외.
- 페이지 이미지 변환: **pypdfium2 (Apache-2.0/BSD)** — pdfplumber가 이미 쓰는 의존성.
- OCR: **RapidOCR (Apache-2.0, PaddleOCR 모델의 ONNX판, 한국어 모델)** — CPU로 돌고 무거운 프레임워크가 없다. 한국어 스캔 PDF 한 건 실측에서 Docling 기본 엔진·EasyOCR·Tesseract보다 좋았다(참고치). OCR 엔진은 바꿔 끼울 수 있게 인터페이스 뒤에 둔다 (나중에 Docling·PP-StructureV3 등).

## 설계

```
PDF
 → (암호 PDF → 거절)
 → 페이지마다: 글자 수 ≥ 20자이고 깨진 글자(�, (cid:n)) 비율 < 5%면 '텍스트 페이지', 아니면 'OCR 페이지'
 → 텍스트 페이지: pdfplumber 단어(위치 포함) → 같은 줄(세로 위치가 가까움)끼리 묶음
                  → 줄 안에서 가로 간격이 큰 곳으로 칸을 나눔 (표 머리글·금액 열이 칸으로 살아남음)
 → OCR 페이지:   pypdfium2로 이미지 → RapidOCR 글자 상자(글자, 위치, 신뢰도) → 텍스트 페이지와 같은 방식으로 줄·칸
 → 문서 전체를 시트 하나("PDF")로, 행 번호는 페이지를 넘어 이어서 (표가 다음 쪽으로 넘어가도 머리글 판별이 이어짐)
 → ExtractedDocument (지금과 같은 형식) → 기존 파이프라인
```

- `ExtractedDocument`에 `source_kind`(`xlsx`·`csv`·`pdf-text`·`pdf-ocr`·`pdf-mixed`)와 `ocr_low_confidence: list[str]`(신뢰도 < 0.8인 **숫자 칸**의 행 ref)을 더한다. 행 ref는 `PDF!{행}`이고, 쪽 번호는 `page_of: dict[ref, int]`로 따로 둔다 (근거 표시용).
- **OCR 숫자 확인**: `ocr_low_confidence`가 있으면 기준선 경고(`BenchFinding`, code `OCR_LOW_CONFIDENCE`)로 올린다 → 청구서 해석 확인 카드에 "OCR로 읽은 숫자 n개는 신뢰도가 낮아요 (쪽·줄)"로 나오고, 승인 검토의 `BENCH_WARN`·보고서 리스크로 이어진다. 총액 대조·업계 기준선이 숫자 오류를 한 번 더 잡는다.
- **OCR 엔진이 없을 때** (`OCR_ENGINE=off` 또는 패키지 없음): OCR 페이지가 있으면 `UnsupportedFile("스캔된 페이지가 있어 OCR이 필요해요. 텍스트가 있는 PDF나 엑셀로 올려 주세요")`.
- 설정: `OCR_ENGINE`(`rapidocr` 기본 / `off`), `OCR_MIN_CONF`(0.8), 업로드 한도는 지금 `MAX_UPLOAD_MB` 그대로.
- FE: 업로드 `accept`에 `.pdf` 추가, 안내 문구 "엑셀·CSV·PDF".
- 의존성: 런타임 `pdfplumber`, 선택 `[ocr]` extra에 `rapidocr` + `onnxruntime`(이미지에 포함), 개발용 `reportlab`(샘플 PDF 생성).

## 검증

| 검증 | 기준 |
|---|---|
| 텍스트 PDF판 r1~r5 (엑셀 샘플을 같은 표로 그린 PDF) | 엑셀과 같은 결과: 연간 운영원가 1.00배(±1%), 줄 분류 전부 정답 |
| 스캔판 r1~r5 (텍스트 PDF를 이미지로 바꿔 다시 PDF로) | **조용한 오답 0건**: 각 샘플이 ±1%이거나, 총액 불일치·OCR 저신뢰·기준선 경고 중 하나가 뜬다. 정확도는 표로 기록 |
| 섞인 PDF (텍스트 1쪽 + 스캔 1쪽) | `source_kind == "pdf-mixed"`, 두 쪽 모두 행으로 |
| 암호 PDF · OCR 꺼짐 + 스캔 | 안내 문구와 함께 거절 |
| 기존 테스트 전부 | 그대로 통과 |

## 빌더 재량

- 줄 묶기 세로 허용치(글자 높이의 0.5배 권장), 칸 나누기 가로 간격(평균 글자 폭의 2배 권장) — 샘플로 조정.
- 표 영역을 pdfplumber `find_tables()`로 따로 다룰지 여부 (줄·칸 방식으로 샘플이 통과하면 생략).
- reportlab 샘플의 한글 글꼴: 내장 CID 글꼴(`HYSMyeongJo-Medium`)로 시작하고, pdfplumber가 글자를 못 뽑으면 macOS·리눅스에 흔한 TTF로 바꾼다.
