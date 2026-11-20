"""Oracle Cloud(OCI) 공개 정가 API 수집 → ERD 초안 스키마 적재.

| 출처(data_source.source_name) | 계약 | 원천 |
| oracle_price_list_api | on_demand | GET apexapps.oracle.com/pls/apex/cetools/api/v1/products/?currencyCode=USD |
|                       |           | (OCI 문서 'Estimate Your Monthly Cost' > 'Accessing List Pricing for OCI Products'가 안내하는 공개 API) |
| oracle_compute_shapes_docs | (사양 근거) | OCI Compute Shapes 문서·compute 가격 페이지·GPU Memory Fabric 문서. 수기로 확인한 값을
|                            |             | 아래 SHAPES에 두고 근거로 남긴다 |

- 가격은 GPU 1장·시간 정가(PAY_AS_YOU_GO) 품목이라 GPU 수로 나누지 않는다. 리전 구분이 없는 단일 가격이다(region_code NULL).
- 품목에는 서버 구성이 없다. 모델별 베어메탈 구성(BM.GPU.B300.8 등)을 상품으로 두고 사양은 Shapes 문서를 근거로 연결한다.
  노드 안 상호연결은 Oracle 원문이 있는 모델만 확인으로 둔다(DR-07): H100·H200·B200·GB200은 compute 가격 페이지의
  'GPU Interconnect: NVIDIA NVLINK'(2026-01-16 저장본, 현재 페이지에는 이 칸이 빠짐), GB200·GB300은 GPU Memory Fabric
  문서의 'NVIDIA NVLink 72'(지원 구성 BM.GPU.GB200.4·GB300.4). B300은 원문이 없어 미확인.
- vCPU는 compute 가격 페이지의 '1 OCPU is the equivalent of 2 vCPUs for x86-based compute'로 x86 구성만 채운다.
  Grace(Arm, GB200·GB300)는 환산 문구가 없어 비워 둔다. 예전에 미확인(NULL)으로 적재한 행은 구성 버전을 올리지 않고
  그 행에 채운다(fill_known_specs, AWS specs와 같은 원칙).
- 'OCI - Compute - GPU - {모델}' 품목만 쓴다. NVIDIA AI Enterprise(소프트웨어 라이선스 별도 요금)·H100T·Cloud@Customer 등은
  GPU 컴퓨팅 정가가 아니라 제외하고 근거에 남긴다.
- 스팟(선점형)·약정 가격은 API에 없어 수집하지 않는다.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from ... import fetch
from .. import db, runs

PROVIDER = "Oracle"
COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-oracle 1"
RULE_VERSION = "v3"
SPIKE_RULE = "OR-06 직전 대비 ±30% 초과"
SPIKE_THRESHOLD = Decimal("0.30")

SOURCE_API = "oracle_price_list_api"
SOURCE_SHAPES = "oracle_compute_shapes_docs"
API_URL = "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/?currencyCode=USD"
SHAPES_URL = "https://docs.oracle.com/en-us/iaas/Content/Compute/References/computeshapes.htm"
REQUEST_CONFIG = {"connect_timeout_s": 15, "read_idle_timeout_s": 120}

GPU_ITEM = re.compile(r"^(?:OCI|Oracle Cloud Infrastructure) - Compute - GPU - (B300|GB300|B200|GB200|H200|H100)$")
GPU_RELATED = re.compile(r"\b(?:G?B[23]00|H[12]00\w*)\b")
METRIC = "GPU Per Hour"
MODEL = "PAY_AS_YOU_GO"
RAW_TERMS = "OCI 정가(Pay As You Go), GPU 1장·시간. 리전 구분 없는 단일 가격 품목. 서버 구성은 Shapes 문서 기준"

SHAPES_VERSION = "docs.oracle.com Compute Shapes·oracle.com compute 가격 페이지 2026-11-20 확인 (2)"
PRICING_PAGE = "https://www.oracle.com/cloud/compute/pricing/"
PRICING_PAGE_ARCHIVE = "https://web.archive.org/web/20260116/https://www.oracle.com/cloud/compute/pricing/"
MEMORY_FABRIC_DOC = "https://docs.oracle.com/en-us/iaas/Content/Compute/Concepts/gpu-memory-fabric.htm"
OCPU_QUOTE = ("OCPUs represent physical CPU cores. Most CPU architectures, including x86, execute two threads per physical "
              "core, so 1 OCPU is the equivalent of 2 vCPUs for x86-based compute.")
PAGE_NVLINK = "compute 가격 페이지 GPU 표 'GPU Interconnect: NVIDIA NVLINK' (2026-01-16 저장본)"
FABRIC_NVLINK = ("GPU Memory Fabric 문서: 'NVIDIA NVLink 72 supports up to 18 Compute hosts each' / "
                 "'Supported Compute shapes are BM.GPU.GB200.4 and BM.GPU.GB300.4'")
# Compute Shapes 문서(2026-11-20 수기 확인). 베어메탈 GPU 구성 표의 GPU 수·GPU 메모리(합계)·CPU 메모리·로컬 디스크·RDMA.
# vcpus: x86 구성의 OCPU × 2(가격 페이지 문구), Grace는 None. nvlink: Oracle 원문이 있을 때만(근거 목록), 없으면 None.
SHAPES = {
    "B300": {"shape": "BM.GPU.B300.8", "gpus": 8, "gpu_memory": 2100, "ram": 4096, "disk": "8 x 3.84 TB NVMe",
             "rdma": "8 x 800 Gbps RDMA", "vcpus": 256, "nvlink": None,
             "quotes": ["BM.GPU.B300.8 GPU: 8x NVIDIA B300 Tensor Core GPUs 263 GB",
                        "CPU: 2 x 64-core Intel Emerald Rapids 8592+", "OCPU 128, GPU Memory 2100 GB, CPU Memory 4096 GB"]},
    "GB300": {"shape": "BM.GPU.GB300.4", "gpus": 4, "gpu_memory": 1112, "ram": 960, "disk": "4 x 7.68 TB NVMe",
              "rdma": "8 x 400 Gbps RDMA", "vcpus": None,
              "nvlink": ("NVIDIA NVLink (NVL72 랙, NVLink 72 도메인)", [FABRIC_NVLINK]),
              "quotes": ["BM.GPU.GB300.4 GPU: 4 x NVIDIA Blackwell B300 278 GB", "CPU: 2 x 72-core NVIDIA Grace",
                         "OCPU 144, GPU Memory 1112 GB, CPU Memory 960 GB"]},
    "B200": {"shape": "BM.GPU.B200.8", "gpus": 8, "gpu_memory": 1440, "ram": 4096, "disk": "8 x 3.84 TB NVMe",
             "rdma": "8 x 400 Gbps RDMA", "vcpus": 256, "nvlink": ("NVIDIA NVLink", [PAGE_NVLINK]),
             "quotes": ["BM.GPU.B200.8 GPU: 8x NVIDIA B200 Tensor Core GPUs 180 GB",
                        "CPU: 2 x 64-core Intel Emerald Rapids 8592+", "OCPU 128, GPU Memory 1440 GB, CPU Memory 4096 GB"]},
    "GB200": {"shape": "BM.GPU.GB200.4", "gpus": 4, "gpu_memory": 768, "ram": 960, "disk": "4 x 7.68 TB NVMe",
              "rdma": "4 x 400 Gbps RDMA", "vcpus": None,
              "nvlink": ("NVIDIA NVLink (NVL72 랙, NVLink 72 도메인)", [PAGE_NVLINK, FABRIC_NVLINK]),
              "quotes": ["BM.GPU.GB200.4 GPU: 4 x NVIDIA Blackwell B200 192 GB", "CPU: 2 x 72-core NVIDIA Grace",
                         "OCPU 144, GPU Memory 768 GB, CPU Memory 960 GB"]},
    "H200": {"shape": "BM.GPU.H200.8", "gpus": 8, "gpu_memory": 1128, "ram": 3072, "disk": "8 x 3.84 TB NVMe",
             "rdma": "8 x 400 Gbps RDMA", "vcpus": 224, "nvlink": ("NVIDIA NVLink", [PAGE_NVLINK]),
             "quotes": ["BM.GPU.H200.8 GPU: 8x NVIDIA H200 Tensor Core GPUs 141 GB",
                        "CPU: 2 x 56-core Intel Sapphire Rapids 8480+", "OCPU 112, GPU Memory 1128 GB, CPU Memory 3072 GB"]},
    "H100": {"shape": "BM.GPU.H100.8", "gpus": 8, "gpu_memory": 640, "ram": 2048, "disk": "16 x 3.84 TB NVMe",
             "rdma": "8 x 2 x 200 Gbps RDMA", "vcpus": 224, "nvlink": ("NVIDIA NVLink", [PAGE_NVLINK]),
             "quotes": ["BM.GPU.H100.8: GPU: 8x H100 80 GB", "CPU: Intel Sapphire Rapids 8480+ 2x 56c",
                        "OCPU 112, GPU Memory 640 GB, CPU Memory 2048 GB"]},
}
SPEC_ATTRIBUTES = ("gpu_count", "vram_gb_per_gpu", "ram_gb", "storage_description", "inter_node_network")
EXPECTED_ERRORS = (fetch.FetchError, OSError, ValueError, LookupError)


class SourceInvalid(ValueError):
    """원천은 받았으나 적재 전 검증을 통과하지 못했다(검증 실패)."""


@dataclass
class Hit:
    product: dict
    offer: dict
    observation: dict
    components: list
    excerpt: dict


@dataclass
class TargetResult:
    hits: list = field(default_factory=list)
    reason: str | None = None


# 매일 수집 ----------------------------------------------------------------------

def collect(conn, *, trigger_type="manual", now=None, date=None, get_json=fetch.get_json):
    """Oracle 수집 1회. date: 수집 기준일(KST, 생략하면 오늘). 같은 기준일에 이미 적재했으면 건너뛴다. 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    day = runs.collection_date(date, now)
    targets = [t for t in db.enabled_targets(conn, PROVIDER) if t["source_name"] == SOURCE_API]
    if not targets:
        raise LookupError("no enabled Oracle price list collection_target")
    run_id = db.begin_run(conn, now(), trigger_type, COLLECTOR_VERSION)
    fill_known_specs(conn, db.provider_id(conn, PROVIDER))
    conn.commit()
    context = {"provider": db.provider_id(conn, PROVIDER), "rule": db.rule(conn, RULE_VERSION), "run_id": run_id,
               "now": now, "date": day, "docs": docs_targets(conn), "docs_source": source_id(conn, SOURCE_SHAPES)}
    try:
        if runs.group_done(conn, context, targets):
            outcome = runs.skipped({"source": SOURCE_API})
        else:
            try:
                body = get_json(API_URL, REQUEST_CONFIG)
                checked = now()
                version, items, excluded = read_items(body)
                results = target_results(targets, items)
                counts = write_group(conn, context, targets, results, checked, version, excluded)
                conn.commit()
                outcome = {"source": SOURCE_API, "status": "success", "source_version": version, **counts}
            except EXPECTED_ERRORS as error:
                outcome = _fail(conn, context, targets, error)
    except Exception as error:
        conn.rollback()
        db.finish_run(conn, run_id, now(), "failed", f"unexpected: {type(error).__name__}")
        conn.commit()
        raise
    status = runs.run_status([outcome])
    reason = None if status == "success" else f"{SOURCE_API}: {outcome['reason']}"[:2000]
    db.finish_run(conn, run_id, now(), status, reason)
    conn.commit()
    return {"run_id": run_id, "date": day, "status": status, "groups": [outcome]}


def _fail(conn, context, targets, error):
    processing = "validation_failed" if isinstance(error, SourceInvalid) else "collection_failed"
    reason = f"{type(error).__name__}: {error}"
    runs.record_failure(conn, context, targets, processing, reason, key=lambda t: runs.failure_key(context, t))
    return {"source": SOURCE_API, "status": processing, "reason": reason}


# 원천 → 대상별 결과 ------------------------------------------------------------

def read_items(body):
    """응답 검증. 반환: (lastUpdated, {모델: (품목, USD 가격)}, 제외한 GPU 관련 품목 목록)."""
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise SourceInvalid("unexpected_price_list_format")
    items, excluded = {}, []
    for item in body["items"]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("displayName") or "")
        match = GPU_ITEM.match(name)
        if not match:
            if GPU_RELATED.search(name):
                excluded.append({"partNumber": item.get("partNumber"), "displayName": name,
                                 "metricName": item.get("metricName")})
            continue
        model = match.group(1)
        if item.get("metricName") != METRIC:
            raise SourceInvalid(f"unexpected_metric: {item.get('partNumber')} {item.get('metricName')!r}")
        if model in items:
            raise SourceInvalid(f"duplicate_gpu_item: {model} {items[model][0].get('partNumber')} {item.get('partNumber')}")
        items[model] = (item, usd_price(item))
    return body.get("lastUpdated"), items, excluded


def usd_price(item):
    """USD Pay As You Go 단가 하나. 구간 요금이나 여러 값이면 검증 실패."""
    localized = item.get("currencyCodeLocalizations", item.get("prices"))
    usd = [entry for entry in localized or [] if isinstance(entry, dict) and entry.get("currencyCode") == "USD"]
    prices = [price for entry in usd for price in entry.get("prices") or [] if price.get("model") == MODEL]
    if len(prices) != 1 or any(prices[0].get(key) for key in ("rangeMin", "rangeMax")):
        raise SourceInvalid(f"unexpected_usd_prices: {item.get('partNumber')} {prices!r}"[:300])
    try:
        amount = Decimal(str(prices[0].get("value")))
    except (InvalidOperation, TypeError):
        raise SourceInvalid(f"invalid_amount: {item.get('partNumber')} {prices[0].get('value')!r}") from None
    if not amount.is_finite() or amount <= 0:
        raise SourceInvalid(f"non_positive_amount: {item.get('partNumber')} {amount}")
    return amount


def target_results(targets, items):
    results = {}
    for target in targets:
        if target["contract_filter"] != "on_demand":
            raise SourceInvalid(f"unsupported Oracle target: {target['model_name']} {target['contract_filter']}")
        result = TargetResult()
        found = items.get(target["model_name"])
        if found is None:
            result.reason = f"정가 API에 'OCI - Compute - GPU - {target['model_name']}' 품목 없음"
        else:
            result.hits.append(make_hit(target, *found))
        results[target["target_id"]] = result
    return results


def make_product(target, item):
    spec = SHAPES[target["model_name"]]
    return {
        "provider_id": target["provider_id"], "gpu_model_id": target["gpu_model_id"],
        "original_product_name": spec["shape"], "region_code": None, "system_type": "node",
        "service_option": "standard", "sku": item.get("partNumber"), "original_model_name": item.get("displayName"),
        "gpu_count": spec["gpus"], "vram_gb_per_gpu": Decimal(spec["gpu_memory"]) / spec["gpus"],
        "vcpu_count": spec["vcpus"],
        "ram_gb": Decimal(spec["ram"]), "storage_description": f"로컬 디스크 {spec['disk']}", "spec_scope": "node",
        "inter_node_network": f"클러스터 네트워크 {spec['rdma']}", "country_code": None,
    }


def make_hit(target, item, amount):
    raw = f"{amount} USD / {item.get('metricName')} ({item.get('partNumber')} {item.get('displayName')}, {MODEL})"
    offer = {"contract_type": "on_demand", "contract_product_type": "Pay As You Go", "commitment_months": None,
             "payment_option": None}
    observation = {"effective_from": None, "price_certainty": "firm", "raw_price_text": raw,
                   "original_currency": "USD", "original_time_unit": "hour", "price_applies_to": "gpu",
                   "raw_terms": RAW_TERMS}
    components = [{"component_type": "hourly_rate", "amount": amount, "currency": "USD", "time_unit": "hour",
                   "applies_to": "gpu", "raw_value": raw}]
    excerpt = {key: item.get(key) for key in ("partNumber", "displayName", "metricName", "serviceCategory")}
    excerpt["usd_pay_as_you_go"] = str(amount)
    return Hit(make_product(target, item), offer, observation, components, excerpt)


def normalize(hit, component_ids, monthly_hours):
    """GPU 1장 가격(정가 API)은 그대로, 노드 가격(SkyPilot 카탈로그)은 GPU 수로 나눈다."""
    applies_to = hit.observation["price_applies_to"]
    gpu_count = hit.product.get("gpu_count")
    inputs = {"component_ids": component_ids, "gpu_count": gpu_count, "monthly_hours": str(monthly_hours),
              "applies_to": applies_to}
    hourly = hit.components[0]["amount"]
    if applies_to == "gpu":
        return {"nominal": hourly, "effective": hourly, "status": "success", "reason": None,
                "inputs": {**inputs, "formula": "GPU 1장 시간당 정가(GPU 수로 나누지 않음)"}}
    if not gpu_count:
        return {"nominal": None, "effective": None, "status": "not_computable", "reason": "GPU 수 미확인",
                "inputs": inputs}
    value = hourly / gpu_count
    return {"nominal": value, "effective": value, "status": "success", "reason": None,
            "inputs": {**inputs, "formula": "노드 시간당 가격 ÷ GPU 수"}}


# 적재 -------------------------------------------------------------------------

def write_group(conn, context, targets, results, checked_at, version, excluded):
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
            db.add_evidence(conn, check_id, target["source_id"], "official", API_URL, version, hit.excerpt, checked_at)
            observation_id = write_observation(conn, context, offer_id, check_id, hit, checked_at, checked_at)
            check_spike(conn, context, target, offer_id, check_id, observation_id, checked_at)
            seen.add(offer_id)
        counts["offers"] += len(result.hits)
        found = bool(result.hits)
        check_id, _ = db.add_check(conn, run_id, target["target_id"], "target", checked_at,
                                   "public" if found else "unknown", "success" if found else "not_found", True,
                                   runs.target_key(context, target), reason=result.reason)
        db.add_evidence(conn, check_id, target["source_id"], "official", API_URL, version,
                        {"scope": scope(target), "offers_found": len(result.hits),
                         **({"excluded_gpu_items": excluded} if excluded else {})}, checked_at)
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
    observation_id = db.add_observation(conn, offer_id, check_id, {**hit.observation, "collected_at": collected_at})
    component_ids = [db.add_component(conn, observation_id, c) for c in hit.components]
    db.add_normalization(conn, observation_id, context["rule"]["rule_id"],
                         normalize(hit, component_ids, context["rule"]["monthly_hours"]), calculated_at)
    return observation_id


def ensure_spec_evidence(conn, context, target, product_id, captured_at):
    """상품(구성 버전)에 이 근거 버전(SHAPES_VERSION)의 사양 근거를 한 번 연결한다. 근거 버전이 바뀌면 새로 연결한다.

    Oracle 원문이 있는 모델만 노드 안 상호연결을 확인(confirmed)으로 바꾼다. vCPU는 x86 구성만 근거에 넣는다."""
    key = f"spec:{product_id}:{SHAPES_VERSION}"
    docs = context["docs"].get(target["gpu_model_id"])
    spec = SHAPES.get(target["model_name"])
    if docs is None or spec is None or db.check_exists(conn, key):
        return
    check_id, _ = db.add_check(conn, context["run_id"], docs["target_id"], "product", captured_at, "public", "success",
                               True, key, product_id=product_id)
    excerpt = {"shape": spec["shape"], "quotes": spec["quotes"], "local_disk": spec["disk"], "rdma": spec["rdma"],
               "verified_by": "manual", "verified_on": "2026-11-20"}
    attributes = list(SPEC_ATTRIBUTES)
    if spec["vcpus"] is not None:
        excerpt.update(vcpu=f"OCPU {spec['vcpus'] // 2} × 2 (x86)", vcpu_source=PRICING_PAGE, vcpu_quote=OCPU_QUOTE)
        attributes.append("vcpu_count")
    nvlink = spec["nvlink"]
    if nvlink is not None:
        excerpt.update(interconnect_sources=nvlink[1], interconnect_urls=[PRICING_PAGE_ARCHIVE, MEMORY_FABRIC_DOC])
    evidence_id = db.add_evidence(conn, check_id, context["docs_source"], "spec", SHAPES_URL, SHAPES_VERSION, excerpt,
                                  captured_at)
    gpu_count = conn.execute("SELECT gpu_count FROM product WHERE product_id = %s", (product_id,)).fetchone()[0]
    interconnect = nvlink[0] if nvlink is not None and gpu_count and gpu_count > 1 else None
    db.add_product_evidence(conn, product_id, evidence_id, attributes, interconnect)


def fill_known_specs(conn, provider):
    """예전에 미확인(NULL)으로 적재한 Oracle 상품의 vCPU를 같은 행에 채운다(구성 버전을 올리지 않음). 반환: 채운 행 수.

    값이 바뀐 것이 아니라 몰랐던 값을 공식 근거로 알게 된 것이라, 새 구성 버전·config_changed 알림을 만들지 않는다."""
    filled = 0
    for spec in SHAPES.values():
        if spec["vcpus"] is None:
            continue
        filled += conn.execute(
            """UPDATE product SET vcpu_count = %s
               WHERE provider_id = %s AND original_product_name = %s AND vcpu_count IS NULL""",
            (spec["vcpus"], provider, spec["shape"])).rowcount
    return filled


def attach_specs(conn, *, now=None, trigger_type="manual"):
    """이미 적재한 Oracle 상품에 미확인 사양을 채우고 최신 사양 근거를 연결한다(한 번 실행). 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    provider = db.provider_id(conn, PROVIDER)
    run_id = db.begin_run(conn, now(), trigger_type, "gpu-collector 0.1.0 / erd-oracle-specs 1")
    context = {"run_id": run_id, "docs": docs_targets(conn), "docs_source": source_id(conn, SOURCE_SHAPES)}
    if not context["docs"]:
        raise LookupError("no enabled Oracle spec collection_target")
    filled = fill_known_specs(conn, provider)
    by_model = {t["gpu_model_id"]: t for t in context["docs"].values()}
    linked = 0
    checked_at = now()
    for product_id, gpu_model_id in conn.execute(
            "SELECT product_id, gpu_model_id FROM product WHERE provider_id = %s ORDER BY product_id",
            (provider,)).fetchall():
        target = by_model.get(gpu_model_id)
        if target is None:
            continue
        before = db.check_exists(conn, f"spec:{product_id}:{SHAPES_VERSION}")
        ensure_spec_evidence(conn, context, target, product_id, checked_at)
        linked += 0 if before else 1
    db.finish_run(conn, run_id, now(), "success")
    conn.commit()
    return {"run_id": run_id, "status": "success", "vcpu_filled": filled, "products_linked": linked}


def check_spike(conn, context, target, offer_id, check_id, observation_id, checked_at):
    current = conn.execute("SELECT effective_usd_gpu_hour FROM price_normalization WHERE observation_id = %s",
                           (observation_id,)).fetchone()[0]
    previous = db.previous_effective(conn, offer_id, context["rule"]["rule_id"], observation_id)
    if current is None or previous is None or not previous[1]:
        return
    change = (current - previous[1]) / previous[1]
    if abs(change) > SPIKE_THRESHOLD:
        db.add_alert(conn, "price_spike", checked_at, f"price_spike:{observation_id}", run_id=context["run_id"],
                     target_id=target["target_id"], check_id=check_id, source_id=target["source_id"],
                     previous_observation_id=previous[0], current_observation_id=observation_id,
                     rule_version=SPIKE_RULE, reason=f"실효 단가 {previous[1]} → {current} ({change * 100:+.1f}%)")


# 공통 -------------------------------------------------------------------------

def docs_targets(conn):
    """{gpu_model_id: 사양 근거 수집 대상}."""
    return {t["gpu_model_id"]: t for t in db.enabled_targets(conn, PROVIDER) if t["source_name"] == SOURCE_SHAPES}


def source_id(conn, name):
    row = conn.execute("SELECT source_id FROM data_source WHERE source_name = %s", (name,)).fetchone()
    if row is None:
        raise LookupError(f"data_source not seeded: {name}")
    return row[0]


def scope(target):
    return {"model": target["model_name"], "region": target["region_filter"] or "all",
            "contract": target["contract_filter"], "scope_version": target["scope_version"]}
