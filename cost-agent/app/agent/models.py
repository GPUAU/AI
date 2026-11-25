"""원가 에이전트 세션 상태. 화면(작업 보드·질문 카드)이 이 구조를 그대로 그린다."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ..models import BillDraft, CostItemDraft


class Step(str, Enum):
    ANALYZE = "ANALYZE"  # 청구서 분석 (+ 총액이 안 맞으면 재분류)
    MISSING = "MISSING"  # 빠진 큰 비용 확인
    ASSUMPTIONS = "ASSUMPTIONS"  # 청구서 밖 가정값
    SIMULATE = "SIMULATE"  # BE 원가 계산
    MARKET = "MARKET"  # 시장 가격 비교 (+ 범위를 벗어나면 마진 조정 후 재계산)
    TERMS = "TERMS"  # 3·5년 계약 가격
    REPORT = "REPORT"  # 견적 근거서
    DONE = "DONE"


STEP_TITLES = {
    Step.ANALYZE: "청구서 분석", Step.MISSING: "누락 확인", Step.ASSUMPTIONS: "가정값 확인",
    Step.SIMULATE: "원가 계산", Step.MARKET: "시장 비교", Step.TERMS: "기간별 가격", Step.REPORT: "견적 근거서",
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
