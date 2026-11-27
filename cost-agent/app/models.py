"""에이전트가 주고받는 데이터 형식.

ItemType·ItemStatus·Period·Currency 값은 GPUAU/BE의 CostSimulationRequestDto와 같아야 한다.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ItemType(str, Enum):
    ACQUISITION = "ACQUISITION"  # GPU·서버 취득비 (1회)
    GPU = "GPU"  # GPU 임차·리스
    MEMORY = "MEMORY"
    NETWORK_STORAGE = "NETWORK_STORAGE"  # 회선·대역폭·스토리지·백업
    RACK_FACILITY = "RACK_FACILITY"  # 랙·상면·시설 사용료
    RENT = "RENT"  # 공간·층 임대료, 코로케이션
    OPERATIONS = "OPERATIONS"  # 운영·관리·기술지원·보안관제·유지보수
    FINANCING = "FINANCING"  # 금융비용 (청구서 매핑 대상 아님)
    ELECTRICITY = "ELECTRICITY"  # 전력·전기료·동력비
    OTHER = "OTHER"


class ItemStatus(str, Enum):
    PUBLIC_SOURCE = "PUBLIC_SOURCE"
    USER_INPUT = "USER_INPUT"
    NOT_INCLUDED = "NOT_INCLUDED"
    INCLUDED_IN_OTHER = "INCLUDED_IN_OTHER"


class Period(str, Enum):
    ONE_TIME = "ONE_TIME"
    MONTH = "MONTH"
    YEAR = "YEAR"


class Currency(str, Enum):
    USD = "USD"
    KRW = "KRW"


class LineRole(str, Enum):
    """청구서 한 줄의 역할. COST만 원가에 들어간다."""

    COST = "COST"
    SUBTOTAL = "SUBTOTAL"
    TOTAL = "TOTAL"  # 청구 총액 (부가세 포함 여부는 검증 단계에서 판단)
    TAX = "TAX"
    DISCOUNT = "DISCOUNT"
    IGNORE = "IGNORE"  # 제목·머리글·메모 등


# ---------------------------------------------------------------- 엑셀 추출 결과


class SheetRow(BaseModel):
    ref: str  # "시트명!행번호"
    sheet: str
    row: int
    cells: list[str]  # 화면에 보이는 글자 (빈 칸은 "")
    numbers: dict[int, float] = Field(default_factory=dict)  # 열 번호(0부터) → 숫자

    @property
    def label(self) -> str:
        """숫자가 아닌 첫 번째 글자 칸들을 이어 붙인 항목명."""
        parts = [c for i, c in enumerate(self.cells) if c and i not in self.numbers]
        return " ".join(parts[:3])


class ExtractedDocument(BaseModel):
    file_name: str
    sheets: list[str]
    rows: list[SheetRow]
    currency_hint: Currency | None = None
    truncated: bool = False
    source_kind: str = "xlsx"  # xlsx | csv | pdf-text | pdf-ocr | pdf-mixed
    ocr_low_confidence: list[str] = Field(default_factory=list)  # OCR 신뢰도가 낮은 숫자가 있는 행 ref
    page_of: dict[str, int] = Field(default_factory=dict)  # PDF 행 ref → 쪽 번호


class ContractInfo(BaseModel):
    """청구서 머리글의 계약 정보 (업계 기준선 검사·사내 비용 제안에 쓴다)."""

    power_kw: float | None = None
    racks: int | None = None
    gpus: int | None = None


# ---------------------------------------------------------------- 매핑 결과


class RowMapping(BaseModel):
    """매퍼(LLM·규칙·템플릿)가 한 줄에 대해 내린 판단. 금액 숫자는 들어 있지 않다."""

    ref: str
    role: LineRole
    item_type: ItemType | None = None
    amount_col: int | None = None  # 금액이 들어 있는 열 번호. 숫자는 코드가 그 칸에서 읽는다.
    included_items: list[ItemType] = Field(default_factory=list)  # 이 줄에 묶여 포함된 다른 항목
    period: Period | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)
    reason: str = ""


class DocumentMapping(BaseModel):
    vendor: str | None = None
    currency: Currency | None = None
    default_period: Period | None = None
    rows: list[RowMapping]
    llm_input: str | None = None  # 외부(또는 사내) LLM으로 실제 보낸 내용 — 담당자 확인용
    llm_destination: str | None = None
    llm_masked: bool | None = None


class MappedLine(BaseModel):
    ref: str
    label: str
    role: LineRole
    item_type: ItemType | None
    amount: float | None
    period: Period | None
    included_items: list[ItemType] = Field(default_factory=list)
    confidence: float
    reason: str
    source: str  # llm | rules | template | user


class IssueLevel(str, Enum):
    ERROR = "ERROR"  # 계산 불가
    WARNING = "WARNING"  # 담당자 확인 필요
    INFO = "INFO"


class Issue(BaseModel):
    level: IssueLevel
    code: str
    message: str
    ref: str | None = None
    items: list[ItemType] = Field(default_factory=list)  # 관련 원가 항목 (누락 표시용)


class CostItemDraft(BaseModel):
    """BE CostItem으로 보낼 초안. 기간은 BE 규칙에 맞게 YEAR 또는 ONE_TIME으로 맞춘다."""

    item_type: ItemType
    status: ItemStatus
    amount: float | None = None
    currency: Currency | None = None
    period: Period | None = None
    included_in: ItemType | None = None
    source_refs: list[str] = Field(default_factory=list)


class Reconciliation(BaseModel):
    cost_sum: float
    document_total: float | None = None
    tax: float | None = None
    matched: bool | None = None  # None = 대조할 총액 없음
    difference: float | None = None
    basis: str = ""


class Interpretation(BaseModel):
    """에이전트가 청구서를 읽으며 기본 해석을 바꾼 것 하나. 담당자가 해석 확인 카드에서 해제할 수 있다."""

    code: str  # interpret.InterpCode 값
    message: str
    applied: bool = True
    impact: float | None = None  # 이 해석만 끈 연간 운영원가 ÷ 지금 연간 운영원가
    refs: list[str] = Field(default_factory=list)


class BenchFinding(BaseModel):
    """업계 기준선과 크게 다른 값 (benchmarks)."""

    code: str  # RENT_PER_KW | POWER_PER_KW | KW_PER_GPU | COST_PER_GPU_HOUR
    message: str
    value: float
    low: float
    high: float
    source: str


class BillDraft(BaseModel):
    id: str
    file_name: str
    fingerprint: str
    mapper: str  # 이번 매핑에 쓴 방식
    vendor: str | None = None
    currency: Currency | None = None
    period: Period | None = None
    lines: list[MappedLine]
    reconciliation: Reconciliation
    cost_items: list[CostItemDraft]
    issues: list[Issue]
    needs_review: bool
    missing_items: list[ItemType] = Field(default_factory=list)  # 청구서에 없는 큰 비용 (화면에서 빨간 표시)
    unit_multiplier: float = 1.0  # 금액 단위 배수 (천원 → 1,000)
    billing_months: float = 1.0  # 이 청구서가 몇 개월치인가
    contract: ContractInfo = Field(default_factory=ContractInfo)
    interpretations: list[Interpretation] = Field(default_factory=list)
    bench: list[BenchFinding] = Field(default_factory=list)
    llm: "LLMDisclosure | None" = None


class LLMDisclosure(BaseModel):
    """이번 매핑에서 LLM에 무엇을 보냈는지. 담당자가 직접 확인할 수 있게 그대로 돌려준다."""

    destination: str
    masked: bool
    sent: str


class LineCorrection(BaseModel):
    ref: str
    role: LineRole | None = None
    item_type: ItemType | None = None
    included_items: list[ItemType] | None = None
    period: Period | None = None


class CorrectionRequest(BaseModel):
    corrections: list[LineCorrection] = Field(default_factory=list)
    save_template: bool = True


class SimulationInput(BaseModel):
    """청구서에 없는 값. 담당자가 입력하고, 비우면 기본값을 쓴다."""

    model_id: str = Field(pattern=r"[1-9][0-9]{0,18}")
    gpu_count: int = Field(gt=0)
    acquisition_amount: float | None = Field(default=None, ge=0)  # GPU·서버 취득비 (청구서 밖)
    acquisition_currency: Currency = Currency.USD
    depreciation_years: int = Field(default=5, gt=0, le=100)
    utilization: float = Field(default=0.7, gt=0, le=1)
    pricing_method: str = Field(default="COST_MARKUP", pattern="REVENUE_MARGIN|COST_MARKUP")
    target_rate: float = 0.3
    fx_krw_per_usd: float | None = Field(default=None, gt=0)
    interest_rate_type: str = Field(default="NOT_APPLIED", pattern="NOT_APPLIED|ASSUMED|ACTUAL_LOAN")
    annual_interest_rate: float | None = Field(default=None, ge=0)  # 0.05 = 연 5%

BillDraft.model_rebuild()
