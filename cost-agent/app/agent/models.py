"""원가 에이전트 세션 상태. 화면(작업 보드·질문 카드)이 이 구조를 그대로 그린다."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ..costsheet import CostSheet
from ..models import BenchFinding, BillDraft, CostItemDraft


class Step(str, Enum):
    ANALYZE = "ANALYZE"  # 청구서 분석 (+ 총액이 안 맞으면 재분류)
    MISSING = "MISSING"  # 빠진 큰 비용 확인
    ASSUMPTIONS = "ASSUMPTIONS"  # 청구서 밖 가정값
    SIMULATE = "SIMULATE"  # BE 원가 계산
    MARKET = "MARKET"  # 시장 가격 비교 (+ 범위를 벗어나면 마진 조정 후 재계산)
    TERMS = "TERMS"  # 3·5년 계약 가격
    REVIEW = "REVIEW"  # 승인 검토 (분해·손익분기·민감도·판정)
    REPORT = "REPORT"  # 견적 근거서
    DONE = "DONE"


STEP_TITLES = {
    # MISSING은 여러 문서 세션에서 '기준표 확인'으로 바꿔 보여 준다
    Step.ANALYZE: "청구서 분석", Step.MISSING: "누락 확인", Step.ASSUMPTIONS: "가정값 확인",
    Step.SIMULATE: "원가 계산", Step.MARKET: "시장 비교", Step.TERMS: "기간별 가격",
    Step.REVIEW: "승인 검토", Step.REPORT: "견적 근거서",
}


class Status(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"  # 담당자 답변 대기
    DONE = "DONE"
    WARNING = "WARNING"  # 끝났지만 확인할 점이 있음
    ERROR = "ERROR"


class BoardStep(BaseModel):
    key: Step
    title: str
    status: Status = Status.PENDING
    summary: str = ""
    details: list[str] = Field(default_factory=list)


class Option(BaseModel):
    value: str
    label: str
    description: str = ""


class FieldSpec(BaseModel):
    name: str
    label: str
    type: str = "number"  # number | select | text
    required: bool = True
    value: Any = None  # 제안값
    hint: str = ""  # 제안 근거
    options: list[Option] = Field(default_factory=list)
    show_for: list[str] = Field(default_factory=list)  # 특정 선택지일 때만 표시


class Question(BaseModel):
    id: str
    step: Step
    title: str
    text: str
    options: list[Option] = Field(default_factory=list)  # 하나를 고르는 버튼
    fields: list[FieldSpec] = Field(default_factory=list)  # 입력칸
    submit_label: str = "확인"


class Answer(BaseModel):
    question_id: str
    choice: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)


class Event(BaseModel):
    at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    step: Step
    kind: str  # think | tool | ask | answer | result | warn
    message: str


class Goal(BaseModel):
    model_id: str | None = None
    gpu_count: int | None = None
    terms_years: list[int] = Field(default_factory=lambda: [3, 5])


class MarketView(BaseModel):
    model_name: str
    count: int
    providers: int
    min: float | None = None
    median: float | None = None
    max: float | None = None
    candidate: float | None = None
    percentile: float | None = None  # 제안가보다 싼 공개가격 비율
    position: str = ""
    samples: list[dict] = Field(default_factory=list)


class TermPrice(BaseModel):
    years: int
    discount_rate: float
    price: float


class BreakdownRow(BaseModel):
    """원가 한 묶음의 연간·시간당 금액 (BE annualCost 기준으로 나눈 것)."""

    group: str  # 공간 | 전력 | 운영 | 감가상각 | 금융비용
    item_types: list[str] = Field(default_factory=list)
    annual_usd: float
    hourly_usd: float
    share: float  # 원가 대비 비중 0~1
    refs: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    """승인 검토의 발견 사항. 문구는 코드 템플릿."""

    code: str
    level: str  # 재검토 | 조건부
    message: str
    fix_step: str | None = None  # Step 값
    reason: str = ""  # 담당자가 적은 진행 사유


class Approval(BaseModel):
    breakdown: list[BreakdownRow] = Field(default_factory=list)
    breakdown_ok: bool = False
    interest_applied: bool = False
    breakeven: dict[str, float] = Field(default_factory=dict)  # candidate | 3y | 5y → 손익분기 가동률
    sensitivity: list[dict] = Field(default_factory=list)  # {key, label, price_delta, grade}
    benchmark: list[dict] = Field(default_factory=list)  # {group, share, ref_share, ratio}
    findings: list[Finding] = Field(default_factory=list)
    verdict: str = ""  # 승인 권고 | 조건부 | 재검토
    reasons: list[str] = Field(default_factory=list)


class AgentSession(BaseModel):
    id: str
    goal: Goal
    step: Step = Step.ANALYZE
    board: list[BoardStep]
    question: Question | None = None
    events: list[Event] = Field(default_factory=list)
    draft: BillDraft | None = None
    remap_tries: int = 0
    missing_answers: dict[str, dict] = Field(default_factory=dict)  # 그룹 → 답
    assumptions: dict[str, Any] = Field(default_factory=dict)
    final_items: list[CostItemDraft] = Field(default_factory=list)
    simulation: dict | None = None
    request: dict | None = None
    market: MarketView | None = None
    margin_adjusted: bool = False
    terms: list[TermPrice] = Field(default_factory=list)
    summary: list[str] = Field(default_factory=list)
    report_ready: bool = False
    # 에이전트(그래프) 판단에 쓰는 상태
    engine: str = "workflow"  # workflow | graph
    mismatch_ok: bool = False  # 총액 불일치를 담당자가 알고 진행
    interpret_ok: bool = False  # 청구서 해석 확인 카드에 담당자가 답함
    checks: list[BenchFinding] = Field(default_factory=list)
    approval: Approval | None = None  # 승인 검토 결과
    review_basis: str | None = None  # 마지막 승인 검토가 기준으로 삼은 입력 지문
    checks_passed: list[str] = Field(default_factory=list)  # 보고서 자동 검증 통과
    checks_failed: list[str] = Field(default_factory=list)  # 가정값·원가 계산 뒤의 업계 기준선 경고 (막지 않음)
    sim_basis: str | None = None  # 마지막 원가 계산의 입력 지문
    market_basis: str | None = None  # 마지막 시장 비교가 기준으로 삼은 원가 계산
    decisions: list[dict] = Field(default_factory=list)  # 판단자 선택 기록 {tool, reason, by}
    # 여러 부서 문서 세션 (문서가 둘 이상이거나 청구서가 아닌 문서가 있으면). 청구서 한 장이면 비어 있다
    docs: list[dict] = Field(default_factory=list)  # {name, kind, reason}
    sheet: CostSheet | None = None  # 원가 기준표
    sheet_answers: dict[str, dict] = Field(default_factory=dict)  # 칸 → 담당자 답
    primary_bill: str | None = None  # ANALYZE 단계가 다루는 청구서 (첫 청구서)
    extra_bills: list[tuple[str, BillDraft]] = Field(default_factory=list)  # 두 번째부터의 청구서
    plan_check: dict | None = None  # 사업기획팀 검산 대비 {ok, hourly_krw, plan_hourly, price_krw, plan_price, message}
    public_fx: str | None = None  # 공개 환율 (BE 수출입은행) — 기준표 환율 칸과 차이 표시
