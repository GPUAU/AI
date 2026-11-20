"""Scaleway 공개 카탈로그 API 수집 → ERD 초안 스키마 적재.

| 출처(data_source.source_name) | 계약 | 원천 |
| scaleway_product_catalog_api | on_demand | GET api.scaleway.com/product-catalog/v2alpha1/public-catalog/products (인증 없음, 페이지 단위) |
|                              |           | 공식 Go SDK scaleway-sdk-go PublicCatalogAPI.ListPublicCatalogProducts가 제공하는 공개 조회 |
| scaleway_gpu_pricing_page | (사양 근거) | www.scaleway.com/en/pricing/gpu/ 내장 JSON의 gpuRam·useNvlink. 수기 확인 값을 SPECS에 둔다 |

- 대상: Instance B300-SXM-8-288G, H100-SXM-8/4/2-80G, H100-1/2-80G(PCIe), Elastic Metal EM-T620E-H100 시간당.
  상품 = offer_id × zone. status=general_availability만(retired 제외).
- 제외: Elastic Metal 월 요금·설치비, Dedibox(기한 없는 월 구독의 1회 설치비라 약정으로 환산하면 단가가 왜곡된다),
  Inference Dedicated(관리형 추론), QaaS. 스팟·약정 상품은 없다.
- 가격은 EUR. Instance는 분 단가, Elastic Metal은 시간 단가. USD 환산은 수출입은행 교차환율(EUR→KRW ÷ USD→KRW)로
  관측일(KST) 이하 최신 기준일 환율을 쓴다(erd/fx/rates.py). 쓴 환율은 calculation_inputs에 남긴다. 환율이 없으면
  환산은 not_computable로 남긴다. 매일 12시 collect fx → collect scaleway → convert krw 순서를 전제한다.
- 스토리지는 Instance 가격에 포함되지 않는다(Block·Scratch 별도). 노드 간 네트워크는 비운다: API 값(Internal 20 Gb/s)은
  일반 VPC 대역폭이라 GPU 클러스터망이 아니다(근거에만 남김).
- 카탈로그가 v2alpha1(알파)이라 필드가 바뀌면 검증 실패로 남긴다.
"""
import re
from dataclasses import dataclass, field
from datetime import date as date_type, datetime, timezone
from decimal import Decimal

from ... import fetch
from .. import db, runs
from ..fx import rates

PROVIDER = "Scaleway"
COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-scaleway 1"
RULE_VERSION = "v4"
SPIKE_RULE = "OR-06 직전 대비 ±30% 초과"
SPIKE_THRESHOLD = Decimal("0.30")

SOURCE_API = "scaleway_product_catalog_api"
SOURCE_PAGE = "scaleway_gpu_pricing_page"
API_URL = "https://api.scaleway.com/product-catalog/v2alpha1/public-catalog/products?page_size=1000&page={page}"
PAGE_URL = "https://www.scaleway.com/en/pricing/gpu/"
REQUEST_CONFIG = {"connect_timeout_s": 15, "read_idle_timeout_s": 120}
MAX_PAGES = 20
MINUTES_PER_HOUR = 60

PAGE_VERSION = "www.scaleway.com/en/pricing/gpu/ 내장 JSON 2026-11-18 저장본 확인(2026-11-20)"
# 가격 페이지 내장 JSON(__NEXT_DATA__)의 gpu·gpuRam(2026-11-18 저장본 수기 확인). gpuRam은 GPU 1장의 메모리(GiB)이고
# 상품명 표기(288G·80G)와 같다. 다른 사업자처럼 표기값을 vram_gb_per_gpu에 넣는다.
# EM-T620E-H100은 페이지에 GPU 메모리·연결 방식이 없어 비운다.
SPECS = {
    "B300-SXM-8-288G": {"model": "B300", "vram_gib": 288, "nvlink": True, "quote": '"gpu":{"count":8,"type":"B300-SXM","useNvlink":true}, "gpuRam":{"size":309237645312}'},
    "H100-SXM-8-80G": {"model": "H100", "vram_gib": 80, "nvlink": True, "quote": '"gpu":{"count":8,"type":"H100-SXM","useNvlink":true}, "gpuRam":{"size":85899345920}'},
    "H100-SXM-4-80G": {"model": "H100", "vram_gib": 80, "nvlink": True, "quote": '"gpu":{"count":4,"type":"H100-SXM","useNvlink":true}'},
    "H100-SXM-2-80G": {"model": "H100", "vram_gib": 80, "nvlink": True, "quote": '"gpu":{"count":2,"type":"H100-SXM","useNvlink":true}'},
    "H100-2-80G": {"model": "H100", "vram_gib": 80, "nvlink": False, "quote": '"gpu":{"count":2,"type":"H100-PCIe","useNvlink":false}'},
    "H100-1-80G": {"model": "H100", "vram_gib": 80, "nvlink": False, "quote": '"gpu":{"count":1,"type":"H100-PCIe","useNvlink":false}'},
    "EM-T620E-H100": {"model": "H100", "vram_gib": None, "nvlink": None, "quote": None},
}
CATEGORIES = {"Instance": ("instance", "minute"), "Elastic Metal": ("node", "hour")}
COUNTRY = {"fr": "FR", "pl": "PL", "nl": "NL"}
STORAGE = "가격에 미포함(별도 과금). 카탈로그 storage: {}"
RAW_TERMS = {"minute": "Scaleway Instance 분 단위 과금(EUR). 스토리지·IP 별도",
             "hour": "Scaleway Elastic Metal 시간 단위 과금(EUR). 월 요금·설치비 상품은 수집하지 않음"}
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
    unit: str
    currency: str


@dataclass
class TargetResult:
    hits: list = field(default_factory=list)
    reason: str | None = None


# 매일 수집 ----------------------------------------------------------------------

def collect(conn, *, trigger_type="manual", now=None, date=None, get_json=fetch.get_json):
    """Scaleway 수집 1회. date: 수집 기준일(KST, 생략하면 오늘). 같은 기준일에 이미 적재했으면 건너뛴다."""
    now = now or (lambda: datetime.now(timezone.utc))
    day = runs.collection_date(date, now)
    targets = [t for t in db.enabled_targets(conn, PROVIDER) if t["source_name"] == SOURCE_API]
    if not targets:
        raise LookupError("no enabled Scaleway catalog collection_target")
    run_id = db.begin_run(conn, now(), trigger_type, COLLECTOR_VERSION)
    conn.commit()
    context = {"provider": db.provider_id(conn, PROVIDER), "rule": db.rule(conn, RULE_VERSION), "run_id": run_id,
               "now": now, "date": day, "docs": docs_targets(conn), "docs_source": source_id(conn, SOURCE_PAGE)}
    try:
        if runs.group_done(conn, context, targets):
            outcome = runs.skipped({"source": SOURCE_API})
        else:
            try:
                products = fetch_catalog(get_json)
                checked = now()
                results = target_results(targets, read_products(products))
                counts = write_group(conn, context, targets, results, checked)
                conn.commit()
                outcome = {"source": SOURCE_API, "status": "success", "catalog_products": len(products), **counts}
            except EXPECTED_ERRORS as error:
                outcome = _fail(conn, context, targets, error)
    except Exception as error:
        conn.rollback()
        db.finish_run(conn, run_id, now(), "failed", f"unexpected: {type(error).__name__}")
        conn.commit()
        raise
    status = runs.run_status([outcome])
    db.finish_run(conn, run_id, now(), status, None if status == "success" else f"{SOURCE_API}: {outcome['reason']}"[:2000])
    conn.commit()
    return {"run_id": run_id, "date": day, "status": status, "groups": [outcome]}


def _fail(conn, context, targets, error):
    processing = "validation_failed" if isinstance(error, SourceInvalid) else "collection_failed"
    reason = f"{type(error).__name__}: {error}"
    runs.record_failure(conn, context, targets, processing, reason, key=lambda t: runs.failure_key(context, t))
    return {"source": SOURCE_API, "status": processing, "reason": reason}


def fetch_catalog(get_json):
    """카탈로그 전 페이지. total_count만큼 받지 못하면 검증 실패(일부만 받은 목록으로 미발견을 판정하지 않는다)."""
    products, total = [], None
    for page in range(1, MAX_PAGES + 1):
        body = get_json(API_URL.format(page=page), REQUEST_CONFIG)
        if not isinstance(body, dict) or not isinstance(body.get("products"), list):
            raise SourceInvalid("unexpected_catalog_format")
        total = body.get("total_count")
        products += body["products"]
        if not body["products"] or (isinstance(total, int) and len(products) >= total):
            break
    if not isinstance(total, int) or len(products) < total:
        raise SourceInvalid(f"incomplete_catalog: {len(products)} / {total}")
    return products


# 원천 → 대상별 결과 ------------------------------------------------------------

def read_products(products):
    """대상 상품만. 반환: [(모델, 상품, 단위)]."""
    found = []
    for item in products:
        if not isinstance(item, dict) or item.get("status") != "general_availability":
            continue
        name = item.get("product")
        category = CATEGORIES.get(item.get("product_category"))
        if name not in SPECS or category is None:
            continue
        unit = (item.get("unit_of_measure") or {}).get("unit")
        if unit != category[1]:
            continue                      # Elastic Metal 월 요금·설치비는 같은 상품명이라 단위로 거른다
        if (item.get("unit_of_measure") or {}).get("size") != 1:
            raise SourceInvalid(f"unexpected_unit_size: {item.get('sku')}")
        found.append((SPECS[name]["model"], item, unit))
    return found


def target_results(targets, found):
    results = {}
    for target in targets:
        if target["contract_filter"] != "on_demand":
            raise SourceInvalid(f"unsupported Scaleway target: {target['model_name']} {target['contract_filter']}")
        result = TargetResult()
        result.hits = [make_hit(target, item, unit) for model, item, unit in found if model == target["model_name"]]
        if not result.hits:
            result.reason = f"공개 카탈로그에 {target['model_name']} GPU 상품 없음"
        results[target["target_id"]] = result
    return results


def price(item):
    value = ((item.get("price") or {}).get("retail_price") or {})
    units, nanos, currency = value.get("units"), value.get("nanos"), value.get("currency_code")
    if not isinstance(units, int) or not isinstance(nanos, int) or not currency:
        raise SourceInvalid(f"invalid_price: {item.get('sku')} {value!r}")
    amount = Decimal(units) + Decimal(nanos) / Decimal(10 ** 9)
    if amount <= 0:
        raise SourceInvalid(f"non_positive_price: {item.get('sku')} {amount}")
    return amount, currency


def zone_of(item):
    zone = (item.get("locality") or {}).get("zone")
    if not zone:
        raise SourceInvalid(f"no_zone: {item.get('sku')} {item.get('locality')!r}")
    return zone


def make_product(target, item, system_type):
    hardware = (item.get("properties") or {}).get("hardware") or {}
    gpu, cpu, ram = hardware.get("gpu") or {}, hardware.get("cpu") or {}, hardware.get("ram") or {}
    name = item["product"]
    spec = SPECS[name]
    count = gpu.get("count")
    if not isinstance(count, int) or count < 1:
        raise SourceInvalid(f"no_gpu_count: {item.get('sku')}")
    vcpu = (cpu.get("virtual") or {}).get("count") or cpu.get("threads")
    zone = zone_of(item)
    return {
        "provider_id": target["provider_id"], "gpu_model_id": target["gpu_model_id"], "original_product_name": name,
        "region_code": zone, "system_type": system_type, "service_option": "standard", "sku": item.get("sku"),
        "original_model_name": gpu.get("type"), "gpu_count": count,
        # GPU 메모리는 제조사 표기값(288·80)을 그대로 둔다(다른 사업자와 같은 기준). RAM은 바이트를 GB(10^9)로.
        "vram_gb_per_gpu": Decimal(spec["vram_gib"]) if spec["vram_gib"] else None,
        "vcpu_count": vcpu, "ram_gb": Decimal(ram["size"]) / Decimal(10 ** 9) if ram.get("size") else None,
        "storage_description": STORAGE.format((hardware.get("storage") or {}).get("description")),
        "spec_scope": system_type, "inter_node_network": None,
        "country_code": COUNTRY.get(zone.split("-")[0]),
    }


def make_hit(target, item, unit):
    system_type = CATEGORIES[item["product_category"]][0]
    amount, currency = price(item)
    raw = f"{amount} {currency} / {unit} ({item.get('sku')}, {item.get('description')})"
    offer = {"contract_type": "on_demand", "contract_product_type": f"{item['product_category']} pay-as-you-go",
             "commitment_months": None, "payment_option": None}
    observation = {"effective_from": None, "price_certainty": "firm", "raw_price_text": raw,
                   "original_currency": currency, "original_time_unit": unit, "price_applies_to": "instance",
                   "raw_terms": RAW_TERMS[unit]}
    components = [{"component_type": "hourly_rate" if unit == "hour" else "list_price", "amount": amount,
                   "currency": currency, "time_unit": unit, "applies_to": "instance", "raw_value": raw}]
    hardware = (item.get("properties") or {}).get("hardware") or {}
    excerpt = {"sku": item.get("sku"), "product": item.get("product"), "variant": item.get("variant"),
               "description": item.get("description"), "status": item.get("status"),
               "price": item.get("price"), "unit_of_measure": item.get("unit_of_measure"),
               "network": (hardware.get("network") or {}).get("description"),
               "ram": (hardware.get("ram") or {}).get("description"), "cpu": (hardware.get("cpu") or {}).get("description")}
    return Hit(make_product(target, item, system_type), offer, observation, components, excerpt, unit, currency)


# 환산 (규칙 v4) ----------------------------------------------------------------

def normalize(conn, hit, component_ids, observed_on):
    """EUR(분·시간) → USD/GPU·시간. 반환: {nominal, effective, status, reason, inputs}."""
    gpu_count = hit.product["gpu_count"]
    amount = hit.components[0]["amount"]
    per_hour = amount * MINUTES_PER_HOUR if hit.unit == "minute" else amount
    inputs = {"component_ids": component_ids, "gpu_count": gpu_count, "time_unit": hit.unit,
              "currency": hit.currency, "observed_on": observed_on.isoformat()}
    fx = rates.usd_rate(conn, hit.currency, observed_on)
    if fx is None:
        return {"nominal": None, "effective": None, "status": "not_computable",
                "reason": f"{observed_on} 이하 {hit.currency}·USD 같은 기준일 환율 없음", "inputs": inputs}
    value = per_hour * fx["rate"] / gpu_count
    inputs.update(currency_rate_id=fx["currency_rate_id"], usd_rate_id=fx["usd_rate_id"],
                  reference_date=fx["reference_date"].isoformat() if fx["reference_date"] else None,
                  currency_krw=str(fx.get("currency_krw")), usd_krw=str(fx.get("usd_krw")),
                  formula=("분 단가 × 60" if hit.unit == "minute" else "시간 단가")
                  + f" × ({hit.currency}→KRW ÷ USD→KRW) ÷ GPU 수")
    return {"nominal": value, "effective": value, "status": "success", "reason": None, "inputs": inputs}


# 적재 -------------------------------------------------------------------------

def write_group(conn, context, targets, results, checked_at):
    run_id = context["run_id"]
    counts = {"targets": len(targets), "offers": 0, "not_found": 0, "offers_missing": 0, "not_computable": 0}
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
            ensure_spec_evidence(conn, context, target, product_id, hit.product["original_product_name"], checked_at)
            db.add_evidence(conn, check_id, target["source_id"], "official", API_URL.format(page="N"), None,
                            hit.excerpt, checked_at)
            status = write_observation(conn, context, offer_id, check_id, hit, checked_at, checked_at)
            counts["not_computable"] += status == "not_computable"
            seen.add(offer_id)
        counts["offers"] += len(result.hits)
        found = bool(result.hits)
        check_id, _ = db.add_check(conn, run_id, target["target_id"], "target", checked_at,
                                   "public" if found else "unknown", "success" if found else "not_found", True,
                                   runs.target_key(context, target), reason=result.reason)
        db.add_evidence(conn, check_id, target["source_id"], "official", API_URL.format(page="N"), None,
                        {"scope": scope(target), "offers_found": len(result.hits)}, checked_at)
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


def write_observation(conn, context, offer_id, check_id, hit, collected_at, calculated_at, *, alerts=True):
    """관측·가격 요소·USD 환산. alerts=False(과거 자료)면 fx_stale·급변 알림을 만들지 않는다. 반환: 환산 상태."""
    observation_id = db.add_observation(conn, offer_id, check_id, {**hit.observation, "collected_at": collected_at})
    component_ids = [db.add_component(conn, observation_id, c) for c in hit.components]
    observed_on = date_type.fromisoformat(runs.collection_date(None, lambda: collected_at))
    result = normalize(conn, hit, component_ids, observed_on)
    db.add_normalization(conn, observation_id, context["rule"]["rule_id"], result, calculated_at)
    reference = result["inputs"].get("reference_date")
    if alerts and reference and rates.stale(observed_on, date_type.fromisoformat(reference)):
        db.add_alert(conn, "fx_stale", calculated_at, f"fx_stale:{hit.currency}:{observed_on}:{reference}",
                     run_id=context["run_id"], source_id=None,
                     reason=f"{PROVIDER} USD 환산: 관측일 {observed_on}에 기준일 {reference} 환율 사용")
    if alerts:
        check_spike(conn, context, offer_id, check_id, observation_id, calculated_at)
    return result["status"]


def ensure_spec_evidence(conn, context, target, product_id, name, captured_at):
    """상품(구성 버전)에 가격 페이지 사양 근거를 한 번 연결한다. 2장 이상 SXM만 NVLink 확인, PCIe 2장은 PCIe."""
    key = f"spec:{product_id}:{PAGE_VERSION}"
    docs = context["docs"].get(target["gpu_model_id"])
    spec = SPECS.get(name)
    if docs is None or spec is None or spec["quote"] is None or db.check_exists(conn, key):
        return False
    check_id, _ = db.add_check(conn, context["run_id"], docs["target_id"], "product", captured_at, "public", "success",
                               True, key, product_id=product_id)
    evidence_id = db.add_evidence(conn, check_id, context["docs_source"], "spec", PAGE_URL, PAGE_VERSION,
                                  {"product": name, "quote": spec["quote"], "verified_by": "manual",
                                   "verified_on": "2026-11-20",
                                   "archive": "https://web.archive.org/web/20261118/https://www.scaleway.com/en/pricing/gpu/"},
                                  captured_at)
    gpu_count = conn.execute("SELECT gpu_count FROM product WHERE product_id = %s", (product_id,)).fetchone()[0]
    interconnect = None
    if gpu_count and gpu_count > 1 and spec["nvlink"] is not None:
        interconnect = "NVIDIA NVLink (SXM)" if spec["nvlink"] else "PCIe (NVLink 없음)"
    db.add_product_evidence(conn, product_id, evidence_id, ("vram_gb_per_gpu",), interconnect)
    return True


def check_spike(conn, context, offer_id, check_id, observation_id, checked_at):
    current = conn.execute("SELECT effective_usd_gpu_hour FROM price_normalization WHERE observation_id = %s",
                           (observation_id,)).fetchone()[0]
    previous = db.previous_effective(conn, offer_id, context["rule"]["rule_id"], observation_id)
    if current is None or previous is None or not previous[1]:
        return
    change = (current - previous[1]) / previous[1]
    if abs(change) > SPIKE_THRESHOLD:
        db.add_alert(conn, "price_spike", checked_at, f"price_spike:{observation_id}", run_id=context["run_id"],
                     check_id=check_id, previous_observation_id=previous[0], current_observation_id=observation_id,
                     rule_version=SPIKE_RULE, reason=f"실효 단가 {previous[1]} → {current} ({change * 100:+.1f}%)")


# 공통 -------------------------------------------------------------------------

def docs_targets(conn):
    return {t["gpu_model_id"]: t for t in db.enabled_targets(conn, PROVIDER) if t["source_name"] == SOURCE_PAGE}


def source_id(conn, name):
    row = conn.execute("SELECT source_id FROM data_source WHERE source_name = %s", (name,)).fetchone()
    if row is None:
        raise LookupError(f"data_source not seeded: {name}")
    return row[0]


def scope(target):
    return {"model": target["model_name"], "region": target["region_filter"] or "all",
            "contract": target["contract_filter"], "scope_version": target["scope_version"]}
