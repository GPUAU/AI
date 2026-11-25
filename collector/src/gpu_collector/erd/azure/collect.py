"""Azure 공개가격 수집 → ERD 초안 스키마 적재.

| 출처(data_source.source_name) | 계약 (collection_target.contract_filter) | 원천 |
| azure_retail_prices_api | on_demand, spot, reservations, savings_plans (+ B300·VR200 등재 확인) | Azure Retail Prices API |
| azure_vm_size_docs | (사양 근거) | Microsoft Learn VM 크기 문서. 수기로 확인한 값을 아래 SPECS에 두고 근거로 남긴다 |

- 수집 범위는 collection_target(enabled)에서 읽는다. region_filter가 NULL이면 API에 나온 모든 리전이다.
- 가격이 같아도 실행마다 관측을 추가한다. 원천에 없는 오퍼는 만들지 않고 미발견 점검으로 남긴다.
- 대상 행: 대표 미터 리전, Linux, 가격 유형 Consumption(일반·Spot)·Reservation, preview API의 Savings Plan 요율.
  Windows·DevTest·Low Priority는 범위 밖이다.
- 예약 가격은 단위가 '1 Hour'로 표기되지만 값은 약정 총액이다. 총액으로 보고 시간당 값이 온디맨드보다 낮은지
  확인한다. 맞지 않으면 적재하지 않고 제외 사유를 남긴다.
- 같은 SKU·리전·가격 유형에 적용 시작일이 다른 행이 함께 있으면 기준 시각 이전에 시작한 가장 최근 행을 쓴다.
- 과거 이력은 azure_history(GitHub 스냅샷)가 같은 행 선택·환산 코드로 적재한다.
"""
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from ... import fetch
from .. import db, runs

PROVIDER = "Azure"
COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-azure 1"
RULE_VERSION = "v2"
SPIKE_RULE = "OR-06 직전 대비 ±30% 초과"
SPIKE_THRESHOLD = Decimal("0.30")

SOURCE_API = "azure_retail_prices_api"
SOURCE_DOCS = "azure_vm_size_docs"
API_URL = "https://prices.azure.com/api/retail/prices"
API_HOST = "https://prices.azure.com/"
# Savings Plan 요율(savingsPlan)은 preview 버전 응답에만 있다
API_VERSION = "2023-01-01-preview"
REQUEST_CONFIG = {"connect_timeout_s": 15, "read_idle_timeout_s": 60}
MAX_PAGES = 20
SECONDS_BETWEEN_REQUESTS = 2
GIB_TO_GB = Decimal("1.073741824")
DOCS_BASE = "https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/"

# 모델 → Azure VM 크기와 공식 사양 문서 값(2026-11-17 확인). VM은 NVL72 랙의 일부 GPU 구성이라 instance다(DR-010).
# H100은 AWS p5와 같은 SXM 8GPU 구성인 ND96isr만 둔다. 문서에서 차이를 확인하지 못한 변형(is·flex·noIB·isf·isrf·
# ND128isrf)과 GPU가 다른 NC H100 v5(H100 NVL)는 넣지 않는다.
SPECS = {
    "H100": {
        "sku_name": "Standard_ND96isr_H100_v5",
        "doc_url": DOCS_BASE + "ndh100v5-series", "doc_version": "ms.date 2026-09-25 (updated 2026-09-27)",
        "original_model_name": "Nvidia H100 GPU (80GB)", "gpu_count": 8, "vram_gb_per_gpu": Decimal(80),
        "vcpu_count": 96, "ram_gib": Decimal(1900),
        "storage_description": "Temp 1 disk 1,024 GiB; NVMe 8 disks 28 TiB",
        "intra_node_interconnect": "NVLINK 4.0 (VM 내 GPU 간)",
        "inter_node_network": "InfiniBand: GPU당 400 Gbps NVIDIA Quantum-2 CX7, VM당 3.2 Tbps",
        "quotes": ["The ND H100 v5 series starts with a single VM and eight NVIDIA H100 Tensor Core GPUs.",
                   "Each GPU within the VM is provided with its own dedicated, topology-agnostic 400 Gbps NVIDIA "
                   "Quantum-2 CX7 InfiniBand connection.",
                   "Each GPU features NVLINK 4.0 connectivity for communication within the VM",
                   "Processor | 96 vCPUs; Memory | 1,900 GiB; Accelerators | 8 GPUs | Nvidia H100 GPU (80GB)",
                   "Temp Storage Disks 1, 1,024 GiB; Max NVMe Disks 8, 28 TiB"],
    },
    "GB200": {
        "sku_name": "Standard_ND128isr_NDR_GB200_v6",
        "doc_url": DOCS_BASE + "nd-gb200-v6-series", "doc_version": "ms.date 2025-03-20 (updated 2026-10-07)",
        "original_model_name": "Nvidia Blackwell GPU (192 GiB)", "gpu_count": 4, "vram_gb_per_gpu": Decimal(192),
        "vcpu_count": 128, "ram_gib": Decimal(900),
        "storage_description": "Temp 4 disks 16 TiB (NVMe)",
        "intra_node_interconnect": "5세대 NVLink, VM당 4× 1.8 TB/s (NVL72 도메인 최대 72 GPU)",
        "inter_node_network": "InfiniBand: VM당 4× 400 NVIDIA Quantum-2 CX7 (문서 본문 표기 '4× 400 GB/s')",
        "quotes": ["Each ND-GB200-v6 VM is powered by two NVIDIA Grace CPUs and four NVIDIA Blackwell GPUs. The GPUs "
                   "are interconnected via fifth-generation NVLink, providing a total of 4× 1.8 TB/s NVLink bandwidth "
                   "per VM.",
                   "the VM offers a scale-out backend network with 4× 400 GB/s NVIDIA Quantum-2 CX7 InfiniBand "
                   "connections per VM",
                   "Processor | 128 vCPUs; Memory | 900 GiB; Accelerators | 4 GPUs | Nvidia Blackwell GPU (192 GiB)",
                   "Accelerators (Qty.) 4; Accelerator-Memory (GB) 192 (사양표는 GiB, 가속기 표는 GB로 표기)"],
    },
    "GB300": {
        "sku_name": "Standard_ND128isr_GB300_v6",
        "doc_url": DOCS_BASE + "nd-gb300-v6-series", "doc_version": "ms.date 2026-08-12 (updated 2026-10-07)",
        "original_model_name": "Nvidia Blackwell Ultra GPU (288GB)", "gpu_count": 4, "vram_gb_per_gpu": Decimal(288),
        "vcpu_count": 128, "ram_gib": Decimal(864),
        "storage_description": "Temp 4 disks 16 TiB (NVMe)",
        "intra_node_interconnect": "5세대 NVLink, VM당 4× 1.8 TB/s (NVL72 도메인 최대 72 GPU)",
        "inter_node_network": "InfiniBand: GPU당 800 Gbps (VM당 4×800)",
        "quotes": ["Each ND-GB300-v6 VM has two NVIDIA Grace CPUs and four NVIDIA Blackwell B300 GPUs. The GPUs are "
                   "interconnected via fifth-generation NVLINK, providing a total of 4× 1.8 TB/s NVLINK bandwidth per "
                   "VM. Each GPU also has 800 Gbps InfiniBand connectivity",
                   "Processor | 128 vCPUs; Memory | 864 GiB; Accelerators | 4 GPUs | Nvidia Blackwell Ultra GPU (288GB)"],
    },
}
MODEL_BY_SKU = {spec["sku_name"]: model for model, spec in SPECS.items()}
SPEC_ATTRIBUTES = ("original_model_name", "gpu_count", "vram_gb_per_gpu", "vcpu_count", "ram_gb", "storage_description",
                   "inter_node_network")

# 가격을 수집하지 않고 등재 여부만 확인하는 모델. GB300 VM 이름에 B300이 들어가므로 앞 글자를 본다.
DETECT = {
    "B300": lambda row: re.search(r"(?<![A-Za-z])B300", row.get("armSkuName") or ""),
    "VR200": lambda row: "VR200" in (row.get("armSkuName") or "").upper() or "Rubin" in (row.get("productName") or ""),
}
DETECT_FILTER = ("serviceName eq 'Virtual Machines' and (contains(armSkuName,'B300') or contains(armSkuName,'VR200') "
                 "or contains(productName,'Rubin'))")

TERM_MONTHS = {"1 Year": 12, "3 Years": 36, "5 Years": 60}
CONTRACT_BY_KIND = {"on_demand": "on_demand", "spot": "spot", "reservation": "reservations",
                    "savings_plans": "savings_plans"}
OFFER_BY_KIND = {"on_demand": ("on_demand", "Pay-as-you-go"), "spot": ("spot", "Spot"),
                 "reservation": ("commitment", "Reserved VM Instances"),
                 "savings_plans": ("commitment", "Savings Plan for Compute")}
EXCERPT_FIELDS = ("armSkuName", "armRegionName", "location", "productId", "productName", "skuId", "skuName", "meterId",
                  "meterName", "type", "reservationTerm", "retailPrice", "unitPrice", "unitOfMeasure", "currencyCode",
                  "effectiveStartDate", "effectiveEndDate", "isPrimaryMeterRegion")

COUNTRY_BY_REGION = {
    "australiacentral": "AU", "australiacentral2": "AU", "australiaeast": "AU", "australiasoutheast": "AU",
    "austriaeast": "AT", "belgiumcentral": "BE", "brazilsouth": "BR", "brazilsoutheast": "BR",
    "canadacentral": "CA", "canadaeast": "CA", "centralindia": "IN", "centralus": "US", "chilecentral": "CL",
    "eastasia": "HK", "eastus": "US", "eastus2": "US", "francecentral": "FR", "francesouth": "FR",
    "germanynorth": "DE", "germanywestcentral": "DE", "indonesiacentral": "ID", "israelcentral": "IL",
    "italynorth": "IT", "japaneast": "JP", "japanwest": "JP", "jioindiacentral": "IN", "jioindiawest": "IN",
    "koreacentral": "KR", "koreasouth": "KR", "malaysiawest": "MY", "mexicocentral": "MX", "newzealandnorth": "NZ",
    "northcentralus": "US", "northeurope": "IE", "norwayeast": "NO", "norwaywest": "NO", "polandcentral": "PL",
    "qatarcentral": "QA", "southafricanorth": "ZA", "southafricawest": "ZA", "southcentralus": "US",
    "southcentralus2": "US", "southeastasia": "SG", "southeastus": "US", "southindia": "IN", "spaincentral": "ES",
    "swedencentral": "SE", "switzerlandnorth": "CH", "switzerlandwest": "CH", "uaecentral": "AE", "uaenorth": "AE",
    "uksouth": "GB", "ukwest": "GB", "usgovarizona": "US", "usgovtexas": "US", "usgovvirginia": "US",
    "westcentralus": "US", "westeurope": "NL", "westindia": "IN", "westus": "US", "westus2": "US", "westus3": "US",
}
EXPECTED_ERRORS = (fetch.FetchError, OSError, ValueError, LookupError)


class SourceInvalid(ValueError):
    """원천은 받았으나 적재 전 검증을 통과하지 못했다(검증 실패)."""


@dataclass
class Hit:
    kind: str
    product: dict
    offer: dict
    observation: dict
    components: list
    excerpt: dict
    source_url: str
    source_version: str | None
    row_key: tuple


@dataclass
class TargetResult:
    hits: list = field(default_factory=list)
    source_url: str = ""
    source_version: str | None = None
    disclosure: str | None = None
    reason: str | None = None
    note: dict = field(default_factory=dict)


# 매일 수집 ----------------------------------------------------------------------

def collect(conn, *, trigger_type="manual", now=None, date=None, get_json=fetch.get_json, pause=time.sleep):
    """Azure 수집 1회(Retail Prices API). date: 수집 기준일(KST, 생략하면 오늘). 반환: 실행 요약.

    같은 수집 기준일에 이미 적재했으면 API를 호출하지 않고 건너뛴다."""
    now = now or (lambda: datetime.now(timezone.utc))
    day = runs.collection_date(date, now)
    provider = db.provider_id(conn, PROVIDER)
    rule = db.rule(conn, RULE_VERSION)
    targets = [t for t in db.enabled_targets(conn, PROVIDER) if t["source_name"] == SOURCE_API]
    if not targets:
        raise LookupError("no enabled Azure collection_target")
    run_id = db.begin_run(conn, now(), trigger_type, COLLECTOR_VERSION)
    conn.commit()
    context = {"provider": provider, "rule": rule, "run_id": run_id, "now": now, "date": day,
               "docs": docs_targets(conn), "docs_source": source_id(conn, SOURCE_DOCS)}
    if runs.group_done(conn, context, targets):
        outcome = runs.skipped({"source": SOURCE_API})
        db.finish_run(conn, run_id, now(), "success")
        conn.commit()
        return {"run_id": run_id, "date": day, "status": "success", "groups": [outcome]}
    try:
        price_url = api_url(price_filter(targets))
        rows = fetch_rows(price_url, get_json, pause)
        detect_url = api_url(DETECT_FILTER)
        detected = fetch_rows(detect_url, get_json, pause) if any(t["model_name"] in DETECT for t in targets) else []
        checked = now()
        results = target_results(targets, rows, detected, checked, price_url, detect_url, None, "Retail Prices API")
        counts = write_group(conn, context, targets, results, checked)
        conn.commit()
        outcome = {"source": SOURCE_API, "status": "success", **counts}
    except EXPECTED_ERRORS as error:
        processing = "validation_failed" if isinstance(error, SourceInvalid) else "collection_failed"
        record_failure(conn, context, targets, processing, f"{type(error).__name__}: {error}",
                       key=lambda t: runs.failure_key(context, t))
        outcome = {"source": SOURCE_API, "status": processing, "reason": f"{type(error).__name__}: {error}"}
    except Exception as error:
        conn.rollback()
        db.finish_run(conn, run_id, now(), "failed", f"unexpected: {type(error).__name__}")
        conn.commit()
        raise
    status = "success" if outcome["status"] == "success" else "failed"
    db.finish_run(conn, run_id, now(), status, None if status == "success" else outcome["reason"][:2000])
    conn.commit()
    return {"run_id": run_id, "date": day, "status": status, "groups": [outcome]}


def price_filter(targets):
    skus = sorted({SPECS[t["model_name"]]["sku_name"] for t in targets if t["model_name"] in SPECS})
    if not skus:
        raise LookupError("no Azure price target")
    return "serviceName eq 'Virtual Machines' and (" + " or ".join(f"armSkuName eq '{s}'" for s in skus) + ")"


def api_url(odata_filter):
    return f"{API_URL}?api-version={API_VERSION}&$filter={quote(odata_filter)}"


def fetch_rows(url, get_json, pause):
    """NextPageLink를 따라 모든 항목을 받는다. 다른 호스트로 가는 링크는 따르지 않는다."""
    rows = []
    for page in range(MAX_PAGES):
        if page:
            pause(SECONDS_BETWEEN_REQUESTS)
        body = get_json(url, REQUEST_CONFIG)
        if not isinstance(body, dict) or not isinstance(body.get("Items"), list):
            raise SourceInvalid("unexpected_retail_prices_format")
        rows.extend(body["Items"])
        url = body.get("NextPageLink")
        if not url:
            return rows
        if not url.startswith(API_HOST):
            raise SourceInvalid("unexpected_next_page_host")
    raise SourceInvalid(f"page_limit_exceeded: {MAX_PAGES}")


# 원천 행 → 대상별 결과 (매일 수집·스냅샷 공통) ---------------------------------------------

def expand_savings_plans(rows):
    """preview 응답의 Consumption 행 안 savingsPlan 요율을 별도 행(type SavingsPlans)으로 펼친다.

    스냅샷 CSV는 이미 펼쳐진 형태(type SavingsPlans)라 그대로 둔다."""
    out = []
    for row in rows:
        out.append(row)
        for plan in row.get("savingsPlan") or []:
            out.append({**{k: v for k, v in row.items() if k != "savingsPlan"}, "type": "SavingsPlans",
                        "reservationTerm": plan.get("term"), "retailPrice": plan.get("retailPrice"),
                        "unitPrice": plan.get("unitPrice")})
    return out


def row_kind(row):
    kind, sku_name = row.get("type"), row.get("skuName") or ""
    if kind == "Reservation":
        return "reservation"
    if kind == "SavingsPlans":
        return "savings_plans"
    if kind == "Consumption" and not sku_name.endswith("Low Priority"):
        return "spot" if sku_name.endswith(" Spot") else "on_demand"
    return None


def select_rows(rows, as_of):
    """대상 SKU 가격 행 고르기. 반환: ({(sku, region, kind, months): row}, 제외 목록, 대체된 행 수)."""
    candidates, excluded = {}, []
    for row in expand_savings_plans(rows):
        if (row.get("serviceName") != "Virtual Machines" or row.get("armSkuName") not in MODEL_BY_SKU
                or not _true(row.get("isPrimaryMeterRegion")) or "Windows" in (row.get("productName") or "")):
            continue
        kind = row_kind(row)
        if kind is None:
            continue
        months = None
        if kind in ("reservation", "savings_plans"):
            months = TERM_MONTHS.get(row.get("reservationTerm"))
            if months is None:
                excluded.append(_excluded(row, kind, f"unknown_term: {row.get('reservationTerm')}"))
                continue
        if row.get("currencyCode") != "USD" or row.get("unitOfMeasure") != "1 Hour":
            excluded.append(_excluded(row, kind, f"unexpected_unit: {row.get('currencyCode')} {row.get('unitOfMeasure')}"))
            continue
        candidates.setdefault((row["armSkuName"], row["armRegionName"], kind, months), []).append(row)

    chosen, superseded = {}, 0
    for key, group in candidates.items():
        started = [r for r in group if (_timestamp(r.get("effectiveStartDate")) or as_of) <= as_of] or group
        chosen[key] = max(started, key=lambda r: r.get("effectiveStartDate") or "")
        superseded += len(group) - 1

    # 예약 총액 확인: 총액 ÷ 약정 시간 < 온디맨드 시간당 < 총액. 아니면 단위를 확정할 수 없어 적재하지 않는다
    for key in [k for k in chosen if k[2] == "reservation"]:
        row, months = chosen[key], key[3]
        on_demand = chosen.get((key[0], key[1], "on_demand", None))
        total = _decimal(row.get("retailPrice"), key)
        hourly = _decimal(on_demand.get("retailPrice"), key) if on_demand else None
        if hourly is None or not (total / (months * 730) < hourly < total):
            excluded.append(_excluded(row, "reservation", "reservation_total_not_plausible" if hourly is not None
                                      else "reservation_without_on_demand"))
            del chosen[key]
    return chosen, excluded, superseded


def target_results(targets, rows, detected_rows, as_of, price_url, detect_url, version, label):
    """반환: {target_id: TargetResult}. 행은 원천 응답 그대로(스냅샷은 CSV 행)."""
    chosen, excluded, superseded = select_rows(rows, as_of)
    results = {}
    for target in targets:
        model, contract = target["model_name"], target["contract_filter"]
        if model in DETECT:
            names = sorted({r.get("armSkuName") or r.get("productName") for r in detected_rows if DETECT[model](r)})
            result = TargetResult(source_url=detect_url, source_version=version,
                                  disclosure="public" if names else None)
            result.reason = (f"{label}에 등재: {', '.join(names)}. 수집 대상 추가 필요" if names
                             else f"{label}에 {model} VM 없음. 미공개로 단정하지 않음")
            results[target["target_id"]] = result
            continue
        if model not in SPECS or contract not in CONTRACT_BY_KIND.values():
            raise SourceInvalid(f"unsupported Azure target: {model} {contract}")
        sku = SPECS[model]["sku_name"]
        result = TargetResult(source_url=price_url, source_version=version)
        for key in sorted(k for k in chosen if k[0] == sku and CONTRACT_BY_KIND[k[2]] == contract
                          and (target["region_filter"] is None or k[1] == target["region_filter"])):
            result.hits.append(make_hit(target, model, chosen[key], key, price_url, version))
        dropped = [e for e in excluded if e["armSkuName"] == sku and CONTRACT_BY_KIND[e["kind"]] == contract]
        result.note = {"excluded_rows": dropped, "superseded_rows": superseded}
        if not result.hits:
            result.reason = f"{label}에 {sku} {contract} 가격 없음" + (f" (제외 {len(dropped)}행)" if dropped else "")
        results[target["target_id"]] = result
    return results


def make_hit(target, model, row, key, url, version):
    sku, region, kind, months = key
    spec = SPECS[model]
    product = {
        "provider_id": target["provider_id"], "gpu_model_id": target["gpu_model_id"],
        "original_product_name": sku, "region_code": region, "system_type": "instance", "service_option": "standard",
        "sku": row.get("productId") or None, "original_model_name": spec["original_model_name"],
        "gpu_count": spec["gpu_count"], "vram_gb_per_gpu": spec["vram_gb_per_gpu"], "vcpu_count": spec["vcpu_count"],
        "ram_gb": spec["ram_gib"] * GIB_TO_GB, "storage_description": spec["storage_description"],
        "spec_scope": "instance", "inter_node_network": spec["inter_node_network"],
        "country_code": COUNTRY_BY_REGION.get(region),
    }
    contract_type, product_type = OFFER_BY_KIND[kind]
    offer = {"contract_type": contract_type, "contract_product_type": product_type, "commitment_months": months,
             # Azure 예약·Savings Plan은 선결제와 월 분할의 총액이 같아 선납 방식 값 3개 중 하나로 정할 수 없다
             "payment_option": None}
    value = row.get("retailPrice")
    amount = _decimal(value, key)
    base = f"{value} USD / {row.get('unitOfMeasure')}: {row.get('productName')} {row.get('skuName')} {row.get('type')}"
    if kind == "reservation":
        text = f"{base} {row.get('reservationTerm')} (표기 단위는 1 Hour지만 값은 약정 총액)"
        components = [_component("commitment_total", amount, "contract", text)]
        raw_terms = (f"Azure Reserved VM Instance {row.get('reservationTerm')}; 약정 총액. 선결제·월 분할 총액 동일")
        time_unit = "contract"
    else:
        text = base + (f" {row.get('reservationTerm')}" if kind == "savings_plans" else "")
        components = [_component("hourly_rate", amount, "hour", text)]
        raw_terms = {"on_demand": "Azure 종량제(Pay-as-you-go) Linux",
                     "spot": "Azure Spot Linux; 변동가, 회수될 수 있음",
                     "savings_plans": f"Azure Savings Plan for Compute {row.get('reservationTerm')}; 시간당 약정 요율. "
                                      "선결제·월 분할 동일"}[kind]
        time_unit = "hour"
    observation = {"effective_from": _timestamp(row.get("effectiveStartDate")), "price_certainty": "firm",
                   "raw_price_text": text, "original_currency": "USD", "original_time_unit": time_unit,
                   "price_applies_to": "instance", "raw_terms": raw_terms}
    excerpt = {name: row.get(name) for name in EXCERPT_FIELDS if row.get(name) not in (None, "")}
    # 원천 확인 비교용 키. API(JSON 숫자)와 스냅샷(CSV 문자열)의 표기 차이(551221.0 / 551221)를 없앤다
    return Hit(kind, product, offer, observation, components, excerpt, url, version,
               (sku, region, kind, months, format(amount.normalize(), "f"), (row.get("effectiveStartDate") or "")[:10]))


# 환산 (DR-006) --------------------------------------------------------------------

def normalize(kind, offer, components, component_ids, gpu_count, monthly_hours):
    """반환: {nominal, effective, status, reason, inputs}. 금액은 반올림하지 않는다."""
    months = offer.get("commitment_months")
    inputs = {"component_ids": component_ids, "gpu_count": gpu_count, "monthly_hours": str(monthly_hours),
              "commitment_months": months, "kind": kind}
    if not gpu_count:
        return _not_computable(inputs, "GPU 수 미확인")
    if kind == "reservation":
        total = next((c["amount"] for c in components if c["component_type"] == "commitment_total"), None)
        if total is None or not months:
            return _not_computable(inputs, "약정 총액·기간 미확인")
        hours = months * monthly_hours
        return {"nominal": None, "effective": total / hours / gpu_count, "status": "success",
                "reason": "약정 총액만 공개되어 명목 단가 없음",
                "inputs": {**inputs, "contract_hours": str(hours),
                           "formula": "약정 총액 ÷ (약정개월 × 730) ÷ GPU 수 (DR-006, 약정 100% 사용 가정)"}}
    hourly = next((c["amount"] for c in components if c["component_type"] == "hourly_rate"), None)
    if hourly is None:
        return _not_computable(inputs, "시간당 요금 없음")
    value = hourly / gpu_count
    return {"nominal": value, "effective": value, "status": "success", "reason": None,
            "inputs": {**inputs, "formula": "시간당 요금 ÷ GPU 수"}}


def _not_computable(inputs, reason):
    return {"nominal": None, "effective": None, "status": "not_computable", "reason": reason, "inputs": inputs}


# 적재 -------------------------------------------------------------------------

def write_group(conn, context, targets, results, checked_at):
    """매일 수집 적재. 오퍼마다 점검·근거·관측·가격 요소·환산을, 대상마다 범위 점검을 남긴다."""
    run_id = context["run_id"]
    counts = {"targets": len(targets), "offers": 0, "not_found": 0, "offers_missing": 0}
    for target in targets:
        result = results[target["target_id"]]
        seen = set()
        for hit in result.hits:
            product_id, previous = db.ensure_product(conn, hit.product)
            offer_id = db.ensure_offer(conn, product_id, hit.offer)
            check_id, created = db.add_check(conn, run_id, target["target_id"], "offer", checked_at, "public",
                                             "success", True, runs.offer_key(context, target, offer_id),
                                             product_id=product_id, offer_id=offer_id)
            if not created:
                raise SourceInvalid(f"duplicate_offer_in_run: offer {offer_id}")
            if previous is not None:
                db.add_alert(conn, "config_changed", checked_at, f"config_changed:{product_id}", run_id=run_id,
                             target_id=target["target_id"], check_id=check_id, source_id=target["source_id"],
                             rule_version="DR-05", reason=f"상품 {previous}의 구성이 바뀌어 새 구성 버전 {product_id}로 적재")
            ensure_spec_evidence(conn, context, target, product_id, checked_at)
            db.add_evidence(conn, check_id, target["source_id"], "official", hit.source_url, hit.source_version,
                            hit.excerpt, checked_at)
            observation_id = write_observation(conn, context, offer_id, check_id, hit, checked_at, checked_at)
            check_spike(conn, context, target, offer_id, check_id, observation_id, checked_at)
            seen.add(offer_id)
        counts["offers"] += len(result.hits)
        found = bool(result.hits) or result.disclosure is not None
        check_id, _ = db.add_check(conn, run_id, target["target_id"], "target", checked_at,
                                   result.disclosure or ("public" if result.hits else "unknown"),
                                   "success" if found else "not_found", True, runs.target_key(context, target),
                                   reason=result.reason)
        db.add_evidence(conn, check_id, target["source_id"], "official", result.source_url, result.source_version,
                        {"scope": scope(target), "offers_found": len(result.hits), **result.note}, checked_at)
        if not found:
            counts["not_found"] += 1
        for offer_id, product_id in sorted(db.previously_seen_offers(conn, target["target_id"]).items()):
            if offer_id in seen:
                continue
            db.add_check(conn, run_id, target["target_id"], "offer", checked_at, "unknown", "not_found", True,
                         runs.offer_key(context, target, offer_id), product_id=product_id, offer_id=offer_id,
                         reason="이전에 확인한 오퍼가 이번 수집 범위에서 발견되지 않음")
            counts["offers_missing"] += 1
    return counts


def write_observation(conn, context, offer_id, check_id, hit, collected_at, calculated_at):
    rule = context["rule"]
    observation_id = db.add_observation(conn, offer_id, check_id, {**hit.observation, "collected_at": collected_at})
    component_ids = [db.add_component(conn, observation_id, component) for component in hit.components]
    result = normalize(hit.kind, hit.offer, hit.components, component_ids, hit.product.get("gpu_count"),
                       rule["monthly_hours"])
    db.add_normalization(conn, observation_id, rule["rule_id"], result, calculated_at)
    return observation_id


def ensure_spec_evidence(conn, context, target, product_id, captured_at):
    """새 상품(구성 버전)에 사양 문서 근거를 연결하고 노드 안 상호연결을 확인으로 바꾼다. 상품마다 한 번."""
    if db.has_product_evidence(conn, product_id):
        return
    docs = context["docs"].get(target["gpu_model_id"])
    if docs is None:
        return        # 사양 근거 대상이 없으면 상호연결은 미확인으로 남는다
    spec = SPECS[target["model_name"]]
    check_id, _ = db.add_check(conn, context["run_id"], docs["target_id"], "product", captured_at, "public", "success",
                               True, f"spec:{product_id}:{spec['doc_version']}", product_id=product_id)
    evidence_id = db.add_evidence(conn, check_id, context["docs_source"], "spec", spec["doc_url"], spec["doc_version"],
                                  {"sku": spec["sku_name"], "quotes": spec["quotes"], "verified_by": "manual",
                                   "verified_on": "2026-11-17"}, captured_at)
    db.add_product_evidence(conn, product_id, evidence_id, SPEC_ATTRIBUTES, spec["intra_node_interconnect"])


def check_spike(conn, context, target, offer_id, check_id, observation_id, checked_at):
    current = conn.execute("SELECT effective_usd_gpu_hour FROM price_normalization WHERE observation_id = %s",
                           (observation_id,)).fetchone()[0]
    if current is None:
        return
    previous = db.previous_effective(conn, offer_id, context["rule"]["rule_id"], observation_id)
    if previous is None or not previous[1]:
        return
    change = (current - previous[1]) / previous[1]
    if abs(change) > SPIKE_THRESHOLD:
        db.add_alert(conn, "price_spike", checked_at, f"price_spike:{observation_id}", run_id=context["run_id"],
                     target_id=target["target_id"], check_id=check_id, source_id=target["source_id"],
                     previous_observation_id=previous[0], current_observation_id=observation_id,
                     rule_version=SPIKE_RULE, reason=f"실효 단가 {previous[1]} → {current} ({change * 100:+.1f}%)")


record_failure = runs.record_failure


# 공통 -------------------------------------------------------------------------

def docs_targets(conn):
    """{gpu_model_id: 사양 근거 수집 대상}."""
    return {t["gpu_model_id"]: t for t in db.enabled_targets(conn, PROVIDER) if t["source_name"] == SOURCE_DOCS}


def source_id(conn, name):
    row = conn.execute("SELECT source_id FROM data_source WHERE source_name = %s", (name,)).fetchone()
    if row is None:
        raise LookupError(f"data_source not seeded: {name}")
    return row[0]


def scope(target):
    return {"model": target["model_name"], "region": target["region_filter"] or "all",
            "contract": target["contract_filter"], "scope_version": target["scope_version"]}


def _component(component_type, amount, time_unit, raw_value):
    return {"component_type": component_type, "amount": amount, "currency": "USD", "time_unit": time_unit,
            "applies_to": "instance", "raw_value": raw_value}


def _excluded(row, kind, reason):
    return {"armSkuName": row.get("armSkuName"), "armRegionName": row.get("armRegionName"), "kind": kind,
            "reservationTerm": row.get("reservationTerm"), "retailPrice": row.get("retailPrice"), "reason": reason}


def _true(value):
    return value is True or str(value).lower() == "true"


def _decimal(value, where):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError) as error:
        raise SourceInvalid(f"invalid_amount: {where} {value!r}") from error
    if amount < 0:
        raise SourceInvalid(f"negative_amount: {where} {value!r}")
    return amount


def _timestamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
