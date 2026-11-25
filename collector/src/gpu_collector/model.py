"""수집기가 DB 적재기에 넘기는 레코드. 필드 의미는 docs/02_설계/데이터_필드_요구사항_매핑.md를 따른다."""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass
class ProductRecord:
    product_key: str
    gpu_model_name: str
    gpu_model_raw: str
    provider_sku: str | None = None
    product_name_raw: str | None = None
    product_section: str | None = None
    form_factor: str | None = None
    gpu_count: int | None = None
    service_type: str | None = None
    purchase_type: str | None = None
    term_min_months: int | None = None
    term_max_months: int | None = None
    interruption_terms: str | None = None
    region_raw: str | None = None
    region_codes: list[str] | None = None
    countries: list[str] | None = None
    cost_scope: str | None = None
    vcpu_count: int | None = None
    system_ram_gb: Decimal | None = None
    gpu_memory_raw: str | None = None
    storage_raw: str | None = None
    interconnect_raw: str | None = None
    interconnect_types: list[str] | None = None
    attrs: dict = field(default_factory=dict)
    missing_reasons: dict = field(default_factory=dict)


@dataclass
class FeeItem:
    item_index: int
    raw_price_text: str
    raw_amount: Decimal | None
    component: str
    raw_currency: str | None = None
    raw_unit: str | None = None
    raw_locator: str | None = None
    fee_inclusion: str | None = None
    price_basis: str | None = None
    price_period: str | None = None
    price_kind: str | None = "fixed"
    availability: str | None = "unknown"
    disclosure_status: str = "public"
    observed_at: datetime | None = None
    effective_at: datetime | None = None
    missing_reasons: dict = field(default_factory=dict)


@dataclass
class Normalized:
    item_index: int
    price_metric: str
    status: str
    usd_per_gpu_hour: Decimal | None = None
    formula: str | None = None
    inputs: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    # 함께 계산한 요금 항목의 위치. 적재할 때 관측 ID로 바꿔 inputs에 넣는다.
    combined_item_indexes: list[int] = field(default_factory=list)


@dataclass
class Listing:
    listing_key: str
    product: ProductRecord
    items: list[FeeItem]
    normalized: list[Normalized]
