"""Verda 공개 API 수집 → ERD 초안 스키마 적재.

| 출처(data_source.source_name) | 계약 (collection_target.contract_filter) | 원천 |
| verda_instance_types_api | on_demand, spot | GET /v1/instance-types (공식 명세상 인증 없음) |
| verda_long_term_periods_api | commitment | GET /v1/long-term/periods/instances (인증 없음) |
| verda_product_pages | (사양 근거) | verda.com 모델 페이지. 수기로 확인한 문구를 아래 SPECS에 두고 근거로 남긴다 |

- 가격은 location_prices(지역별)만 쓴다. 최상위 price_per_hour·spot_price는 어느 지역 가격과도 다를 때가 있어
  (2026-11-18 H100 등 18개 상품) 원문으로만 보존한다.
- 상품 = instance_type × 지역(FIN-01·02·03, 핀란드 헬싱키). 모델명이 '… CC'인 상품은 기밀 컴퓨팅이다.
- 약정: API는 기간별 할인율만 준다. 같은 실행의 온디맨드 관측에 할인율을 적용한 가정 시나리오로 남긴다
  (관측 price_certainty=estimate, 환산 result_status=assumption). 결제는 선납이다(문서 'paid up-front').
- 스토리지는 인스턴스에 포함되지 않는다(별도 블록 스토리지). 노드 간 네트워크는 단일 인스턴스라 비운다.
- USD가 아닌 가격은 적재하지 않고 제외 사유를 남긴다.
- VR200은 가격을 수집하지 않고 instance-types·cluster-types에 등재됐는지만 매일 확인한다(FR-027 자동 확인).
- 과거 이력은 verda/history.py(Wayback 저장본)가 같은 상품·환산 코드로 적재한다.
"""
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from ... import fetch
from .. import db

PROVIDER = "Verda"
COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-verda 1"
RULE_VERSION = "v3"
SPIKE_RULE = "OR-06 직전 대비 ±30% 초과"
SPIKE_THRESHOLD = Decimal("0.30")

SOURCE_INSTANCES = "verda_instance_types_api"
SOURCE_LONG_TERM = "verda_long_term_periods_api"
SOURCE_PAGES = "verda_product_pages"
INSTANCE_URL = "https://api.verda.com/v1/instance-types"
LONG_TERM_URL = "https://api.verda.com/v1/long-term/periods/instances"
CLUSTER_URL = "https://api.verda.com/v1/cluster-types"
# 가격 없이 등재 여부만 확인하는 모델. 이름·모델 필드에서 찾는다.
DETECT = {"VR200": re.compile(r"VR200|Rubin|\bVR\d", re.I)}
REQUEST_CONFIG = {"connect_timeout_s": 15, "read_idle_timeout_s": 60}
SECONDS_BETWEEN_REQUESTS = 2

# API model 값 → (ERD 모델, 서비스 옵션)
MODEL_BY_API = {"B300": ("B300", "standard"), "B300 CC": ("B300", "confidential"), "GB300": ("GB300", "standard"),
                "B200": ("B200", "standard"), "B200 CC": ("B200", "confidential"), "H200": ("H200", "standard"),
                "H100": ("H100", "standard")}
# 공식 문서 'Locations and Sustainability': FIN-01·02·03 헬싱키, ICE-01 레이캬네스바이르
COUNTRY_BY_LOCATION = {"FIN-01": "FI", "FIN-02": "FI", "FIN-03": "FI", "ICE-01": "IS"}
LOCATIONS_DOC = "https://docs.verda.com/welcome-to-verda/locations-and-sustainability/"
STORAGE = "인스턴스에 포함되지 않음(별도 블록 스토리지). API storage.description: {}"

PAGE_VERSION = "verda.com 2026-11-18 확인"
# 모델 페이지 문구(2026-11-18 수기 확인). GPU 2장 이상 상품의 노드 안 상호연결 근거로 쓴다.
SPECS = {
    "B300": {"doc_url": "https://verda.com/b300", "intra_node_interconnect": "NVLink, GPU 간 1.8 TB/s",
             "quotes": ["GPU Instances: 1x, 2x, 4x, 8x with NVLink", "NVLink bandwidth 1.8 TB/s",
                        "GPU VRAM 268 GB (1x)"]},
    "GB300": {"doc_url": "https://verda.com/gb300",
              "intra_node_interconnect": "NVLink v5, GPU 간 1.8 TB/s (NVL72 랙 트레이의 일부 GPU 구성)",
              "quotes": ["NVLink v5", "1x GB300 GPU $14.56/h", "288 GB of HBM3e per GPU",
                         "tray: 4 GB300 GPUs and 2 Grace CPUs; rack: 18 trays, 72 GPUs"]},
    "B200": {"doc_url": "https://verda.com/b200", "intra_node_interconnect": "NVLink, GPU 간 1.8 TB/s",
             "quotes": ["GPU Instances: 1x, 2x, 4x, 8x with NVLink", "NVLink bandwidth 1.8 TB/s",
                        "GPU VRAM 180 GB (1x)"]},
    "H200": {"doc_url": "https://verda.com/h200", "intra_node_interconnect": "NVLink, GPU 간 900 GB/s",
             "quotes": ["GPU Instances: 1x, 2x, 4x, 8x with NVLink", "NVLink bandwidth 900 GB/s",
                        "GPU VRAM 141 GB (1x)"]},
    "H100": {"doc_url": "https://verda.com/h100", "intra_node_interconnect": "4세대 NVLink, GPU 간 900 GB/s",
             "quotes": ["GPU Instances: 1x, 2x, 4x, 8x with NVLink", "fourth generation NVLink",
                        "NVLink bandwidth 900 GB/s", "GPU VRAM 80 GB (1x)"]},
}
SPEC_ATTRIBUTES = ("gpu_count", "vram_gb_per_gpu", "country_code")

OFFER_BY_KIND = {"on_demand": ("on_demand", "Pay As You Go"), "spot": ("spot", "Spot"),
                 "commitment": ("commitment", "Long-term contract")}
FIELD_BY_KIND = {"on_demand": "price_per_hour", "spot": "spot_price"}
RAW_TERMS = {"on_demand": "Verda Pay As You Go(10분 단위 과금)", "spot": "Verda Spot; 회수될 수 있음"}
EXCERPT_FIELDS = ("id", "instance_type", "model", "name", "description", "price_per_hour", "spot_price",
                  "dynamic_price", "max_dynamic_price", "currency", "p2p", "deploy_warning")
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
    base: dict | None = None         # 약정: 할인율을 적용한 온디맨드 관측 {observation_id, hourly}


@dataclass
class TargetResult:
    hits: list = field(default_factory=list)
    source_url: str = ""
    source_version: str | None = None
    disclosure: str | None = None
    reason: str | None = None
    note: dict = field(default_factory=dict)


# 매일 수집 ----------------------------------------------------------------------

def collect(conn, *, trigger_type="manual", now=None, get_json=fetch.get_json, pause=time.sleep):
    """Verda 수집 1회. 인스턴스(온디맨드·스팟) 다음에 약정(할인율)을 처리한다. 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    targets = db.enabled_targets(conn, PROVIDER)
    instance_targets = [t for t in targets if t["source_name"] == SOURCE_INSTANCES and t["contract_filter"]]
    detect_targets = [t for t in targets if t["source_name"] == SOURCE_INSTANCES and t["model_name"] in DETECT]
    long_term_targets = [t for t in targets if t["source_name"] == SOURCE_LONG_TERM]
    if not instance_targets:
        raise LookupError("no enabled Verda instance-types collection_target")
    run_id = db.begin_run(conn, now(), trigger_type, COLLECTOR_VERSION)
    conn.commit()
    context = {"provider": db.provider_id(conn, PROVIDER), "rule": db.rule(conn, RULE_VERSION), "run_id": run_id,
               "now": now, "docs": docs_targets(conn), "docs_source": source_id(conn, SOURCE_PAGES)}
    outcomes, bases = [], {}
    try:
        try:
            body = get_json(INSTANCE_URL, REQUEST_CONFIG)
            rows = read_instances(body)
            clusters = []
            if detect_targets:
                pause(SECONDS_BETWEEN_REQUESTS)
                clusters = get_json(CLUSTER_URL, REQUEST_CONFIG)
                if not isinstance(clusters, list):
                    raise SourceInvalid("unexpected_cluster_types_format")
            checked = now()
            results = instance_results(instance_targets, rows, INSTANCE_URL, None, "instance-types")
            results.update(detect_results(detect_targets, body, clusters))
            counts = write_group(conn, context, instance_targets + detect_targets, results, checked, bases=bases)
            conn.commit()
            outcomes.append({"source": SOURCE_INSTANCES, "status": "success", **counts})
        except EXPECTED_ERRORS as error:
            bases.clear()
            outcomes.append(_fail(conn, context, instance_targets + detect_targets, SOURCE_INSTANCES, error))
        if long_term_targets:
            try:
                if not bases:
                    raise SourceInvalid("on_demand_prices_not_available: 같은 실행의 온디맨드 관측이 없어 할인율을 적용할 수 없음")
                pause(SECONDS_BETWEEN_REQUESTS)
                periods = read_periods(get_json(LONG_TERM_URL, REQUEST_CONFIG))
                checked = now()
                results = commitment_results(long_term_targets, periods, bases, LONG_TERM_URL)
                counts = write_group(conn, context, long_term_targets, results, checked)
                conn.commit()
                outcomes.append({"source": SOURCE_LONG_TERM, "status": "success", **counts})
            except EXPECTED_ERRORS as error:
                outcomes.append(_fail(conn, context, long_term_targets, SOURCE_LONG_TERM, error))
    except Exception as error:
        conn.rollback()
        db.finish_run(conn, run_id, now(), "failed", f"unexpected: {type(error).__name__}")
        conn.commit()
        raise
    ok = sum(1 for outcome in outcomes if outcome["status"] == "success")
    status = "success" if ok == len(outcomes) else ("failed" if ok == 0 else "partial_failure")
    failed = [f"{o['source']}: {o['reason']}" for o in outcomes if o["status"] != "success"]
    db.finish_run(conn, run_id, now(), status, "; ".join(failed)[:2000] or None)
    conn.commit()
    return {"run_id": run_id, "status": status, "groups": outcomes}


def _fail(conn, context, targets, source, error):
    processing = "validation_failed" if isinstance(error, SourceInvalid) else "collection_failed"
    reason = f"{type(error).__name__}: {error}"
    record_failure(conn, context, targets, processing, reason,
                   key=lambda t: f"{context['run_id']}:{t['target_id']}:target")
    return {"source": source, "status": processing, "reason": reason}


# 원천 → 대상별 결과 (매일 수집·Wayback 공통) ---------------------------------------------

def read_instances(body):
    """instance-types 응답 검증. 반환: 대상 모델 행 목록."""
    if not isinstance(body, list):
        raise SourceInvalid("unexpected_instance_types_format")
    rows = []
    for row in body:
        if not isinstance(row, dict) or row.get("model") not in MODEL_BY_API:
            continue
        if not row.get("instance_type") or not isinstance((row.get("gpu") or {}).get("number_of_gpus"), int):
            raise SourceInvalid(f"missing_instance_fields: {row.get('instance_type')!r}")
        rows.append(row)
    return rows


def read_periods(body):
    """long-term/periods 응답 검증. 반환: [(code, months, 할인율 %, 원문 행)] (사용 가능한 기간만)."""
    if not isinstance(body, list):
        raise SourceInvalid("unexpected_long_term_format")
    periods = []
    for row in body:
        if not isinstance(row, dict) or row.get("is_enabled") is not True:
            continue
        value, unit = row.get("unit_value"), row.get("unit_name")
        months = value * {"month": 1, "year": 12}.get(unit, 0) if isinstance(value, int) else 0
        percent = _decimal(row.get("discount_percentage"), row.get("code"))
        if not months or not 0 <= percent < 100:
            raise SourceInvalid(f"unexpected_period: {row.get('code')!r} {unit} {value} {percent}")
        periods.append((row.get("code"), months, percent, row))
    return sorted(periods, key=lambda p: p[1])


def instance_results(targets, rows, url, version, label, *, as_of_location=True):
    """반환: {target_id: TargetResult}. as_of_location=False면 지역별 가격 없는 과거 응답(최상위 가격, 지역 미상)."""
    results = {}
    for target in targets:
        kind = target["contract_filter"]
        if kind not in FIELD_BY_KIND:
            raise SourceInvalid(f"unsupported Verda instance target: {target['model_name']} {kind}")
        result = TargetResult(source_url=url, source_version=version)
        excluded = []
        for row in rows:
            model, option = MODEL_BY_API[row["model"]]
            if model != target["model_name"]:
                continue
            for location, value in prices(row, kind, as_of_location):
                if str(row.get("currency", "")).lower() != "usd":
                    excluded.append({"instance_type": row["instance_type"], "location": location,
                                     "reason": f"non_usd_currency: {row.get('currency')}"})
                    continue
                if value is None:
                    excluded.append({"instance_type": row["instance_type"], "location": location,
                                     "reason": f"{FIELD_BY_KIND[kind]}_missing"})
                    continue
                result.hits.append(make_hit(target, row, option, location, kind, value, url, version))
        result.note = {"excluded": excluded} if excluded else {}
        if not result.hits:
            result.reason = f"{label}에 {target['model_name']} {kind} 가격 없음" + (
                f" (제외 {len(excluded)}건)" if excluded else "")
        results[target["target_id"]] = result
    return results


def detect_results(targets, instances, clusters):
    """등재 여부만 확인하는 대상. 찾으면 공개(가격 미수집, 대상 추가 필요), 없으면 미발견(미공개로 단정하지 않음)."""
    results = {}
    for target in targets:
        pattern = DETECT[target["model_name"]]
        names = sorted({str(row.get(key)) for row in list(instances) + list(clusters) if isinstance(row, dict)
                        for key in ("instance_type", "cluster_type", "model", "name")
                        if row.get(key) and pattern.search(str(row.get(key)))})
        result = TargetResult(source_url=f"{INSTANCE_URL} , {CLUSTER_URL}", disclosure="public" if names else None)
        result.reason = (f"instance-types·cluster-types에 등재: {', '.join(names)}. 가격 수집 대상 추가 필요" if names
                         else f"instance-types·cluster-types에 {target['model_name']}(Rubin) 없음. 미공개로 단정하지 않음")
        result.note = {"detected": names}
        results[target["target_id"]] = result
    return results


def prices(row, kind, by_location=True):
    """[(지역 코드 또는 None, 가격 원문)]. by_location=False(지역별 가격이 없던 과거 응답)면 최상위 가격 하나."""
    name = FIELD_BY_KIND[kind]
    if not by_location:
        return [(None, row.get(name))]
    locations = row.get("location_prices")
    if not isinstance(locations, list) or not locations:
        raise SourceInvalid(f"location_prices_missing: {row['instance_type']}")
    return [(item.get("location_code"), item.get(name)) for item in locations]


def make_product(target, row, option, location):
    gpu_count = row["gpu"]["number_of_gpus"]
    vram = (row.get("gpu_memory") or {}).get("size_in_gigabytes")
    vcpu = (row.get("cpu") or {}).get("number_of_cores")
    ram = (row.get("memory") or {}).get("size_in_gigabytes")
    return {
        "provider_id": target["provider_id"], "gpu_model_id": target["gpu_model_id"],
        "original_product_name": row["instance_type"], "region_code": location, "system_type": "instance",
        "service_option": option, "sku": row.get("id") or None, "original_model_name": row.get("name") or None,
        "gpu_count": gpu_count,
        "vram_gb_per_gpu": Decimal(str(vram)) / gpu_count if vram is not None and gpu_count else None,
        "vcpu_count": vcpu, "ram_gb": Decimal(str(ram)) if ram is not None else None,
        "storage_description": STORAGE.format((row.get("storage") or {}).get("description")),
        "spec_scope": "instance", "inter_node_network": None,
        "country_code": COUNTRY_BY_LOCATION.get(location) if location else None,
    }


def make_hit(target, row, option, location, kind, value, url, version):
    product = make_product(target, row, option, location)
    contract_type, product_type = OFFER_BY_KIND[kind]
    offer = {"contract_type": contract_type, "contract_product_type": product_type, "commitment_months": None,
             "payment_option": None}
    amount = _decimal(value, (row["instance_type"], location, kind))
    place = location or "지역 미표기(최상위 가격)"
    text = f"{value} {row.get('currency')}/hour ({row['instance_type']} {place} {FIELD_BY_KIND[kind]})"
    observation = {"effective_from": None, "price_certainty": "firm", "raw_price_text": text,
                   "original_currency": "USD", "original_time_unit": "hour", "price_applies_to": "instance",
                   "raw_terms": RAW_TERMS[kind] + ("" if location else "; 지역별 가격이 없던 응답의 최상위 가격(지역 미상)")}
    components = [_component("hourly_rate", amount, "hour", text)]
    excerpt = {name: row.get(name) for name in EXCERPT_FIELDS if row.get(name) not in (None, "")}
    excerpt["location_code"] = location
    return Hit(kind, product, offer, observation, components, excerpt, url, version,
               (row["instance_type"], location, kind, None, format(amount.normalize(), "f")))


def commitment_results(targets, periods, bases, url):
    """bases: {product_id: {target_model, product, observation_id, hourly, instance_type, location}} (같은 실행의 온디맨드)."""
    results = {}
    for target in targets:
        if target["contract_filter"] != "commitment":
            raise SourceInvalid(f"unsupported Verda long-term target: {target['model_name']} {target['contract_filter']}")
        result = TargetResult(source_url=url)
        for product_id, base in sorted(bases.items()):
            if base["model"] != target["model_name"]:
                continue
            for code, months, percent, row in periods:
                result.hits.append(make_commitment_hit(base, code, months, percent, row, url))
        if not result.hits:
            result.reason = (f"long-term/periods에 사용 가능한 기간 없음" if not periods
                             else f"같은 실행에 {target['model_name']} 온디맨드 관측 없음")
        results[target["target_id"]] = result
    return results


def make_commitment_hit(base, code, months, percent, row, url):
    offer = {"contract_type": "commitment", "contract_product_type": OFFER_BY_KIND["commitment"][1],
             "commitment_months": months, "payment_option": "all_upfront"}
    text = f"{row.get('name')}: {percent}% off on-demand ({code}, discount_percentage)"
    observation = {"effective_from": None, "price_certainty": "estimate", "raw_price_text": text,
                   "original_currency": None, "original_time_unit": None, "price_applies_to": "instance",
                   "raw_terms": (f"Verda 장기 계약 {months}개월, 선납(paid up-front). API는 기간별 할인율만 공개해 약정가는 "
                                 "온디맨드 × (1 − 할인율) 가정. 할인율의 지역·상품별 적용 범위는 명시되지 않음")}
    components = [{"component_type": "discount_rate", "amount": None, "currency": None, "time_unit": None,
                   "applies_to": "instance", "discount_rate": percent / 100, "raw_value": text}]
    excerpt = {"period": row, "base_instance_type": base["instance_type"], "base_location": base["location"],
               "base_observation_id": base["observation_id"], "base_hourly": str(base["hourly"])}
    return Hit("commitment", base["product"], offer, observation, components, excerpt, url, None,
               (base["instance_type"], base["location"], "commitment", months, str(percent)), base=base)


# 환산 (DR-006, 규칙 v3) ------------------------------------------------------------

def normalize(hit, component_ids, monthly_hours):
    """반환: {nominal, effective, status, reason, inputs}. 금액은 반올림하지 않는다."""
    gpu_count = hit.product.get("gpu_count")
    inputs = {"component_ids": component_ids, "gpu_count": gpu_count, "monthly_hours": str(monthly_hours),
              "commitment_months": hit.offer.get("commitment_months"), "kind": hit.kind}
    if not gpu_count:
        return _not_computable(inputs, "GPU 수 미확인")
    if hit.kind == "commitment":
        rate = next(c["discount_rate"] for c in hit.components if c["component_type"] == "discount_rate")
        base = hit.base
        value = base["hourly"] * (1 - rate) / gpu_count
        return {"nominal": None, "effective": value, "status": "assumption",
                "reason": (f"약정가 미공개: 같은 실행의 온디맨드 관측 {base['observation_id']}에 할인율 "
                           f"{rate * 100}%를 적용한 가정(선납, 약정기간 100% 사용)"),
                "inputs": {**inputs, "base_observation_id": base["observation_id"],
                           "base_hourly": str(base["hourly"]), "discount_rate": str(rate),
                           "formula": "온디맨드 시간당 × (1 − 할인율) ÷ GPU 수 (가정 시나리오)"}}
    hourly = next((c["amount"] for c in hit.components if c["component_type"] == "hourly_rate"), None)
    if hourly is None:
        return _not_computable(inputs, "시간당 요금 없음")
    value = hourly / gpu_count
    return {"nominal": value, "effective": value, "status": "success", "reason": None,
            "inputs": {**inputs, "formula": "시간당 요금 ÷ GPU 수"}}


def _not_computable(inputs, reason):
    return {"nominal": None, "effective": None, "status": "not_computable", "reason": reason, "inputs": inputs}


# 적재 -------------------------------------------------------------------------

def write_group(conn, context, targets, results, checked_at, *, bases=None):
    """매일 수집 적재. bases가 주어지면 온디맨드 관측을 약정 계산용으로 모은다."""
    run_id = context["run_id"]
    counts = {"targets": len(targets), "offers": 0, "not_found": 0, "offers_missing": 0}
    for target in targets:
        result = results[target["target_id"]]
        seen = set()
        for hit in result.hits:
            product_id, previous = db.ensure_product(conn, hit.product)
            offer_id = db.ensure_offer(conn, product_id, hit.offer)
            check_id, created = db.add_check(conn, run_id, target["target_id"], "offer", checked_at, "public",
                                             "success", True, f"{run_id}:{target['target_id']}:offer:{offer_id}",
                                             product_id=product_id, offer_id=offer_id)
            if not created:
                raise SourceInvalid(f"duplicate_offer_in_run: offer {offer_id}")
            if previous is not None:
                db.add_alert(conn, "config_changed", checked_at, f"config_changed:{product_id}", run_id=run_id,
                             target_id=target["target_id"], check_id=check_id, source_id=target["source_id"],
                             rule_version="DR-05", reason=f"상품 {previous}의 구성이 바뀌어 새 구성 버전 {product_id}로 적재")
            ensure_spec_evidence(conn, context, target, product_id, hit.product["gpu_count"], checked_at)
            db.add_evidence(conn, check_id, target["source_id"], "official", hit.source_url, hit.source_version,
                            hit.excerpt, checked_at)
            observation_id = write_observation(conn, context, offer_id, check_id, hit, checked_at, checked_at)
            if hit.kind != "commitment":
                check_spike(conn, context, target, offer_id, check_id, observation_id, checked_at)
            if bases is not None and hit.kind == "on_demand":
                bases[product_id] = {"model": target["model_name"], "product": hit.product,
                                     "observation_id": observation_id, "hourly": hit.components[0]["amount"],
                                     "instance_type": hit.product["original_product_name"],
                                     "location": hit.product["region_code"]}
            seen.add(offer_id)
        counts["offers"] += len(result.hits)
        found = bool(result.hits) or result.disclosure is not None
        check_id, _ = db.add_check(conn, run_id, target["target_id"], "target", checked_at,
                                   result.disclosure or ("public" if result.hits else "unknown"),
                                   "success" if found else "not_found", True,
                                   f"{run_id}:{target['target_id']}:target", reason=result.reason)
        db.add_evidence(conn, check_id, target["source_id"], "official", result.source_url, result.source_version,
                        {"scope": scope(target), "offers_found": len(result.hits), **result.note}, checked_at)
        if not found:
            counts["not_found"] += 1
        for offer_id, product_id in sorted(db.previously_seen_offers(conn, target["target_id"]).items()):
            if offer_id in seen:
                continue
            db.add_check(conn, run_id, target["target_id"], "offer", checked_at, "unknown", "not_found", True,
                         f"{run_id}:{target['target_id']}:offer:{offer_id}", product_id=product_id, offer_id=offer_id,
                         reason="이전에 확인한 오퍼가 이번 수집 범위에서 발견되지 않음")
            counts["offers_missing"] += 1
    return counts


def write_observation(conn, context, offer_id, check_id, hit, collected_at, calculated_at):
    rule = context["rule"]
    observation_id = db.add_observation(conn, offer_id, check_id, {**hit.observation, "collected_at": collected_at})
    component_ids = [db.add_component(conn, observation_id, component) for component in hit.components]
    db.add_normalization(conn, observation_id, rule["rule_id"], normalize(hit, component_ids, rule["monthly_hours"]),
                         calculated_at)
    return observation_id


def ensure_spec_evidence(conn, context, target, product_id, gpu_count, captured_at):
    """새 상품(구성 버전)에 모델 페이지 근거를 연결한다. GPU 2장 이상이면 노드 안 상호연결을 확인으로 바꾼다."""
    if db.has_product_evidence(conn, product_id):
        return
    docs = context["docs"].get(target["gpu_model_id"])
    spec = SPECS.get(target["model_name"])
    if docs is None or spec is None:
        return        # 사양 근거 대상이 없으면 상호연결은 미확인으로 남는다
    check_id, _ = db.add_check(conn, context["run_id"], docs["target_id"], "product", captured_at, "public", "success",
                               True, f"spec:{product_id}:{PAGE_VERSION}", product_id=product_id)
    evidence_id = db.add_evidence(conn, check_id, context["docs_source"], "spec", spec["doc_url"], PAGE_VERSION,
                                  {"quotes": spec["quotes"], "locations_doc": LOCATIONS_DOC,
                                   "locations_quote": "FIN-01, FIN-02, FIN-03: Helsinki, Finland",
                                   "verified_by": "manual", "verified_on": "2026-11-18"}, captured_at)
    db.add_product_evidence(conn, product_id, evidence_id, SPEC_ATTRIBUTES,
                            spec["intra_node_interconnect"] if gpu_count and gpu_count > 1 else None)


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


def record_failure(conn, context, targets, processing, reason, *, key):
    """작업을 되돌리고 대상별 실패 점검과 알림을 남긴다. 실패를 공개 상태로 바꾸지 않는다."""
    conn.rollback()
    run_id, checked_at = context["run_id"], context["now"]()
    for target in targets:
        db.add_check(conn, run_id, target["target_id"], "target", checked_at, "unknown", processing, False,
                     key(target), reason=reason[:1000])
    first = targets[0]
    db.add_alert(conn, "collection_failed", checked_at, f"collection_failed:{key(first)}", run_id=run_id,
                 source_id=first["source_id"], reason=f"{first['source_name']}: {reason}"[:2000])
    conn.commit()


# 공통 -------------------------------------------------------------------------

def docs_targets(conn):
    """{gpu_model_id: 사양 근거 수집 대상}."""
    return {t["gpu_model_id"]: t for t in db.enabled_targets(conn, PROVIDER) if t["source_name"] == SOURCE_PAGES}


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


def _decimal(value, where):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError) as error:
        raise SourceInvalid(f"invalid_amount: {where} {value!r}") from error
    if amount < 0:
        raise SourceInvalid(f"negative_amount: {where} {value!r}")
    return amount
