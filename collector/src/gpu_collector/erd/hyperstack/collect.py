"""Hyperstack(NexGen Cloud) API 수집 → ERD 스키마 적재.

| 출처(data_source.source_name) | 계약 | 원천 |
| hyperstack_api | on_demand, spot | GET infrahub-api.nexgencloud.com/v1 pricebook·core/flavors·core/regions (API 키) |
| hyperstack_product_pages | (사양 근거) | hyperstack.cloud 상품·가격 페이지. 수기 확인 문구를 SPECS에 둔다 |

- pricebook은 GPU 종류별 장당 시간가(리전 구분 없음). flavor(리전별 구성)의 gpu 값으로 붙인다. 상품 = flavor × 리전.
- 계정 할인가(discount_applied=true 또는 value != original_value)가 대상에 보이면 검증 실패로 남기고 적재하지 않는다.
  공개 정가 조회 전제(계약 없는 계정)가 깨진 것이라 알아야 하고, 계약 조건은 T&C 8.1 기밀 소지가 있다.
- 상품은 flavor 단위다. 스팟 flavor(n3-…-spot)는 기능 제약(local_storage_only 등)이 달라 별도 상품으로 둔다.
  온디맨드↔스팟을 짝지을 때는 product_id가 아니라 (provider, original_model_name, region_code, gpu_count)로 묶는다.
- 대상 모델로 보이지만 SPECS에 없는 GPU 종류(예: H100-80G-SXM5-IB)는 적재하지 않고 대상 점검 근거(excluded)에 남긴다.
- 스팟은 '-spot' GPU 종류다. 공개 페이지 기준 온디맨드의 80%.
- 예약가는 API에 없어 수집하지 않는다. bigroot(루트 디스크만 큰 API 전용 변형)는 같은 가격이라 제외한다.
- vCPU·RAM·로컬 디스크는 GPU 가격에 포함된다(pricebook 단가 0). 추가 볼륨·공인 IP는 별도.
"""
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from ... import fetch
from ...env import MissingSecret, secret
from .. import db, runs

PROVIDER = "Hyperstack"
COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-hyperstack 1"
RULE_VERSION = "v3"
SPIKE_RULE = "OR-06 직전 대비 ±30% 초과"
SPIKE_THRESHOLD = Decimal("0.30")
KEY_ENV = "HYPERSTACK_API_KEY"

SOURCE_API = "hyperstack_api"
SOURCE_PAGES = "hyperstack_product_pages"
BASE_URL = "https://infrahub-api.nexgencloud.com/v1"
PRICEBOOK_URL = f"{BASE_URL}/pricebook"
FLAVORS_URL = f"{BASE_URL}/core/flavors"
REGIONS_URL = f"{BASE_URL}/core/regions"
REQUEST_CONFIG = {"connect_timeout_s": 15, "read_idle_timeout_s": 60}

PAGE_VERSION = "hyperstack.cloud 2026-11-22 확인"
# GPU 종류(pricebook name = flavor gpu) → 모델·사양 근거. 상호연결은 페이지에 문구가 있는 것만 채운다.
SPECS = {
    "B300-SXM": {"model": "B300", "vram": 288, "interconnect": "NVLink Switch System, 8 GPU 합계 14.4 TB/s",
                 "urls": ["https://www.hyperstack.cloud/nvidia-hgx-b300", "https://www.hyperstack.cloud/gpu-pricing"],
                 "quotes": ["The dual NVIDIA NVLink Switch System provides 14.4 TB/s of aggregate GPU interconnect bandwidth",
                            "NVIDIA B300 | VRAM (GB) 288 | $7.40"]},
    "B200-SXM": {"model": "B200", "vram": 192, "interconnect": "5세대 NVLink, GPU 간 1.8 TB/s",
                 "urls": ["https://www.hyperstack.cloud/nvidia-blackwell-b200"],
                 "quotes": ["GPU Memory: 192 GB HBM3e", "5th Generation NVLink: 1.8TB/s, PCIe Gen6: 256GB/s"]},
    "H200-141G-SXM5": {"model": "H200", "vram": 141, "interconnect": None,
                       "urls": ["https://www.hyperstack.cloud/nvidia-h200-sxm"], "quotes": ["141GB HBM3e"]},
    "H100-80G-SXM5": {"model": "H100", "vram": 80, "interconnect": "NVLink·NVSwitch, 900 GB/s",
                      "urls": ["https://www.hyperstack.cloud/nvidia-h100-sxm"],
                      "quotes": ["The HGX SXM5 H100 utilises NVLink and NVSwitch technologies",
                                 "NVLink: 900GB/s PCIe / Gen5: 128GB/s"]},
    "H100-80G-PCIe-NVLink": {"model": "H100", "vram": 80, "interconnect": "NVLink 브리지 3개, 600 GB/s",
                             "urls": ["https://www.hyperstack.cloud/h100-pcie"],
                             "quotes": ["Three built-in NVLink bridges deliver 600 GB/s bidirectional bandwidth",
                                        "Graphics Memory: 80 GB HBM3"]},
    "H100-80G-PCIe": {"model": "H100", "vram": 80, "interconnect": None,
                      "urls": ["https://www.hyperstack.cloud/h100-pcie"], "quotes": ["Graphics Memory: 80 GB HBM3"]},
}
SPOT_SUFFIX = "-spot"
EXCLUDE_FLAVOR = re.compile(r"bigroot", re.I)
TARGET_GPU = re.compile(r"(?<!G)(?:B300|B200)|H200|H100", re.I)    # GB200·GB300(랙스케일)은 대상 아님
OFFER_BY_KIND = {"on_demand": "Hyperstack on-demand (분 단위 과금)", "spot": "Hyperstack Spot VM"}
RAW_TERMS = {"on_demand": "Hyperstack 온디맨드, 선불 크레딧·분 단위 과금. 추가 볼륨·공인 IP 별도",
             "spot": "Hyperstack Spot VM, 예고 없이 회수될 수 있음, SLA 없음"}
STORAGE = "루트 디스크 {disk}, 임시 디스크 {ephemeral} 포함. 추가 볼륨 별도"
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
    gpu_type: str


@dataclass
class TargetResult:
    hits: list = field(default_factory=list)
    reason: str | None = None
    note: dict = field(default_factory=dict)


# 매일 수집 ----------------------------------------------------------------------

def collect(conn, *, trigger_type="manual", now=None, date=None, get_json=fetch.get_json, key=None):
    """Hyperstack 수집 1회. date: 수집 기준일(KST, 생략하면 오늘). 같은 기준일에 이미 적재했으면 건너뛴다."""
    now = now or (lambda: datetime.now(timezone.utc))
    day = runs.collection_date(date, now)
    targets = [t for t in db.enabled_targets(conn, PROVIDER) if t["source_name"] == SOURCE_API]
    if not targets:
        raise LookupError("no enabled Hyperstack API collection_target")
    if key is None:
        try:
            key = secret(KEY_ENV)
        except MissingSecret as error:
            raise LookupError(str(error)) from None
    run_id = db.begin_run(conn, now(), trigger_type, COLLECTOR_VERSION)
    conn.commit()
    context = {"provider": db.provider_id(conn, PROVIDER), "rule": db.rule(conn, RULE_VERSION), "run_id": run_id,
               "now": now, "date": day, "docs": docs_targets(conn), "docs_source": source_id(conn, SOURCE_PAGES)}
    try:
        if runs.group_done(conn, context, targets):
            outcome = runs.skipped({"source": SOURCE_API})
        else:
            try:
                headers = {"api_key": key, "accept": "application/json"}
                pricebook = read_pricebook(get_json(PRICEBOOK_URL, REQUEST_CONFIG, headers))
                flavors, unknown = read_flavors(get_json(FLAVORS_URL, REQUEST_CONFIG, headers))
                countries = read_regions(get_json(REGIONS_URL, REQUEST_CONFIG, headers))
                checked = now()
                results = target_results(targets, pricebook, flavors, countries, unknown)
                counts = write_group(conn, context, targets, results, checked)
                conn.commit()
                outcome = {"source": SOURCE_API, "status": "success", **counts}
            except EXPECTED_ERRORS as error:
                outcome = _fail(conn, context, targets, error)
                if isinstance(error, fetch.FetchError) and str(error) == "http_401":     # 403은 WAF 등 일시 차단일 수 있음
                    db.finish_run(conn, run_id, now(), "failed", f"{SOURCE_API}: {outcome['reason']}"[:2000])
                    conn.commit()
                    raise LookupError(f"Hyperstack API 키 거부({error}): {KEY_ENV} 확인") from None
    except LookupError:
        raise
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


# 원천 검증 ---------------------------------------------------------------------

def read_pricebook(body):
    """반환: {name: 행}. 응답은 봉투 없는 배열."""
    if not isinstance(body, list) or not body:
        raise SourceInvalid("unexpected_pricebook_format")
    rows = {}
    for row in body:
        if isinstance(row, dict) and row.get("name"):
            if row["name"] in rows:
                raise SourceInvalid(f"duplicate_pricebook_name: {row['name']}")
            rows[row["name"]] = row
    return rows


def read_flavors(body):
    """반환: (대상 GPU 종류 flavor 목록, 대상 모델로 보이지만 모르는 GPU 종류 [{gpu, flavor, region}])."""
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise SourceInvalid("unexpected_flavors_format")
    found, unknown, seen = [], [], set()
    for group in body["data"]:
        for flavor in (group or {}).get("flavors") or []:
            gpu = flavor.get("gpu") or ""
            if base_type(gpu) is None:
                if TARGET_GPU.search(gpu):
                    unknown.append({"gpu": gpu, "flavor": flavor.get("name"), "region": flavor.get("region_name")})
                continue
            key = (flavor.get("name"), flavor.get("region_name"))
            if not all(key) or not isinstance(flavor.get("gpu_count"), int) or flavor["gpu_count"] < 1:
                raise SourceInvalid(f"missing_flavor_fields: {key}")
            for name in ("cpu", "ram"):
                value = flavor.get(name)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                          or not math.isfinite(value) or value <= 0):
                    raise SourceInvalid(f"invalid_flavor_{name}: {key} {value!r}")
            if key in seen:
                raise SourceInvalid(f"duplicate_flavor: {key}")
            seen.add(key)
            found.append(flavor)
    return found, unknown


def read_regions(body):
    """반환: {리전 이름: 국가 코드}."""
    if not isinstance(body, dict) or not isinstance(body.get("regions"), list):
        raise SourceInvalid("unexpected_regions_format")
    return {r["name"]: r.get("country") if re.fullmatch(r"[A-Z]{2}", str(r.get("country"))) else None
            for r in body["regions"] if isinstance(r, dict) and r.get("name")}


def base_type(gpu):
    """GPU 종류 → (대상 기본 종류, 계약). 대상이 아니면 None."""
    kind = "spot" if gpu.endswith(SPOT_SUFFIX) else "on_demand"
    base = gpu[:-len(SPOT_SUFFIX)] if kind == "spot" else gpu
    return (base, kind) if base in SPECS else None


def list_price(row, where):
    """공개 정가. 계정 할인이 붙은 행이면 검증 실패(공개 정가 조회 전제가 깨짐)."""
    try:
        value, original = Decimal(str(row.get("value"))), Decimal(str(row.get("original_value")))
    except (InvalidOperation, TypeError):
        raise SourceInvalid(f"invalid_price: {where} {row.get('value')!r}") from None
    if not value.is_finite() or not original.is_finite() or value <= 0:
        raise SourceInvalid(f"invalid_price: {where} {value}")
    if row.get("discount_applied") is not False or value != original:
        raise SourceInvalid(f"account_discount_detected: {where} (계정 할인가는 공개 정가가 아님)")
    return value


# 원천 → 대상별 결과 ------------------------------------------------------------

def target_results(targets, pricebook, flavors, countries, unknown=()):
    results = {}
    for target in targets:
        kind = target["contract_filter"]
        if kind not in OFFER_BY_KIND:
            raise SourceInvalid(f"unsupported Hyperstack target: {target['model_name']} {kind}")
        result, excluded = TargetResult(), []
        for flavor in flavors:
            base, flavor_kind = base_type(flavor["gpu"])
            if SPECS[base]["model"] != target["model_name"] or flavor_kind != kind:
                continue
            if EXCLUDE_FLAVOR.search(flavor["name"]):
                excluded.append({"flavor": flavor["name"], "region": flavor["region_name"],
                                 "reason": "bigroot_variant(같은 가격, 루트 디스크만 큰 API 전용 변형)"})
                continue
            row = pricebook.get(flavor["gpu"])
            if row is None:
                excluded.append({"flavor": flavor["name"], "region": flavor["region_name"], "reason": "pricebook_missing"})
                continue
            amount = list_price(row, flavor["gpu"])
            result.hits.append(make_hit(target, flavor, base, kind, amount, row, countries))
        excluded += [{**u, "reason": "unknown_gpu_type"} for u in unknown
                     if re.search(rf"(?<!G){target['model_name']}", u["gpu"], re.I)
                     and ("spot" if u["gpu"].endswith(SPOT_SUFFIX) else "on_demand") == kind]
        result.note = {"excluded": excluded} if excluded else {}
        if not result.hits:
            result.reason = f"flavor·pricebook에 {target['model_name']} {kind} 없음" + (
                f" (제외 {len(excluded)}건)" if excluded else "")
        results[target["target_id"]] = result
    return results


def make_product(target, flavor, base, countries):
    region = flavor["region_name"]
    return {
        "provider_id": target["provider_id"], "gpu_model_id": target["gpu_model_id"],
        "original_product_name": flavor["name"], "region_code": region, "system_type": "instance",
        "service_option": "standard", "sku": None, "original_model_name": base, "gpu_count": flavor["gpu_count"],
        "vram_gb_per_gpu": Decimal(SPECS[base]["vram"]), "vcpu_count": flavor.get("cpu"),
        "ram_gb": Decimal(str(flavor["ram"])) if flavor.get("ram") is not None else None,
        "storage_description": STORAGE.format(disk=_gb(flavor.get("disk")), ephemeral=_gb(flavor.get("ephemeral"))),
        "spec_scope": "instance", "inter_node_network": None, "country_code": countries.get(region),
    }


def make_hit(target, flavor, base, kind, amount, row, countries):
    raw = f"{amount} USD / GPU / hour (pricebook {flavor['gpu']}, flavor {flavor['name']}, {flavor['region_name']})"
    offer = {"contract_type": kind, "contract_product_type": OFFER_BY_KIND[kind], "commitment_months": None,
             "payment_option": None}
    observation = {"effective_from": None, "price_certainty": "firm", "raw_price_text": raw,
                   "original_currency": "USD", "original_time_unit": "hour", "price_applies_to": "gpu",
                   "raw_terms": RAW_TERMS[kind]}
    components = [{"component_type": "hourly_rate", "amount": amount, "currency": "USD", "time_unit": "hour",
                   "applies_to": "gpu", "raw_value": raw}]
    excerpt = {"pricebook": {k: row.get(k) for k in ("id", "name", "value", "original_value", "discount_applied",
                                                     "start_time", "end_time")},
               "flavor": {k: flavor.get(k) for k in ("id", "name", "region_name", "gpu", "gpu_count", "cpu", "ram",
                                                     "disk", "ephemeral", "stock_available", "features")},
               "flavors_url": FLAVORS_URL, "regions_url": REGIONS_URL}
    return Hit(kind, make_product(target, flavor, base, countries), offer, observation, components, excerpt, base)


# 환산 (규칙 v3: GPU 1장 가격은 나누지 않음) ------------------------------------------

def normalize(hit, component_ids):
    hourly = hit.components[0]["amount"]
    inputs = {"component_ids": component_ids, "gpu_count": hit.product.get("gpu_count"), "applies_to": "gpu",
              "formula": "GPU 1장 시간당 정가(GPU 수로 나누지 않음)"}
    return {"nominal": hourly, "effective": hourly, "status": "success", "reason": None, "inputs": inputs}


# 적재 -------------------------------------------------------------------------

def write_group(conn, context, targets, results, checked_at):
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
            ensure_spec_evidence(conn, context, target, product_id, hit.gpu_type, checked_at)
            db.add_evidence(conn, check_id, target["source_id"], "official", PRICEBOOK_URL, None, hit.excerpt,
                            checked_at)
            write_observation(conn, {**context, "target": target}, offer_id, check_id, hit, checked_at, checked_at)
            seen.add(offer_id)
        counts["offers"] += len(result.hits)
        found = bool(result.hits)
        check_id, _ = db.add_check(conn, run_id, target["target_id"], "target", checked_at,
                                   "public" if found else "unknown", "success" if found else "not_found", True,
                                   runs.target_key(context, target), reason=result.reason)
        db.add_evidence(conn, check_id, target["source_id"], "official", PRICEBOOK_URL, None,
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


def write_observation(conn, context, offer_id, check_id, hit, collected_at, calculated_at, *, alerts=True):
    observation_id = db.add_observation(conn, offer_id, check_id, {**hit.observation, "collected_at": collected_at})
    component_ids = [db.add_component(conn, observation_id, c) for c in hit.components]
    db.add_normalization(conn, observation_id, context["rule"]["rule_id"], normalize(hit, component_ids), calculated_at)
    if alerts:
        check_spike(conn, context, context.get("target") or {}, offer_id, check_id, observation_id, calculated_at)
    return observation_id


def ensure_spec_evidence(conn, context, target, product_id, gpu_type, captured_at):
    """상품(구성 버전)에 상품 페이지 사양 근거를 한 번 연결한다. 상호연결은 GPU 2장 이상이고 문구가 있을 때만."""
    key = f"spec:{product_id}:{PAGE_VERSION}"
    docs = context["docs"].get(target["gpu_model_id"])
    spec = SPECS.get(gpu_type)
    if docs is None or spec is None or db.check_exists(conn, key):
        return False
    check_id, _ = db.add_check(conn, context["run_id"], docs["target_id"], "product", captured_at, "public", "success",
                               True, key, product_id=product_id)
    evidence_id = db.add_evidence(conn, check_id, context["docs_source"], "spec", spec["urls"][0], PAGE_VERSION,
                                  {"gpu_type": gpu_type, "urls": spec["urls"], "quotes": spec["quotes"],
                                   "verified_by": "manual", "verified_on": "2026-11-22"}, captured_at)
    gpu_count = conn.execute("SELECT gpu_count FROM product WHERE product_id = %s", (product_id,)).fetchone()[0]
    interconnect = spec["interconnect"] if gpu_count and gpu_count > 1 else None
    db.add_product_evidence(conn, product_id, evidence_id, ("vram_gb_per_gpu",), interconnect)
    return True


def check_spike(conn, context, target, offer_id, check_id, observation_id, checked_at):
    current = conn.execute("SELECT effective_usd_gpu_hour FROM price_normalization WHERE observation_id = %s",
                           (observation_id,)).fetchone()[0]
    previous = db.previous_effective(conn, offer_id, context["rule"]["rule_id"], observation_id)
    if current is None or previous is None or not previous[1]:
        return
    change = (current - previous[1]) / previous[1]
    if abs(change) > SPIKE_THRESHOLD:
        db.add_alert(conn, "price_spike", checked_at, f"price_spike:{observation_id}", run_id=context["run_id"],
                     target_id=target.get("target_id"), check_id=check_id, source_id=target.get("source_id"),
                     previous_observation_id=previous[0], current_observation_id=observation_id,
                     rule_version=SPIKE_RULE, reason=f"실효 단가 {previous[1]} → {current} ({change * 100:+.1f}%)")


# 공통 -------------------------------------------------------------------------

def docs_targets(conn):
    return {t["gpu_model_id"]: t for t in db.enabled_targets(conn, PROVIDER) if t["source_name"] == SOURCE_PAGES}


def source_id(conn, name):
    row = conn.execute("SELECT source_id FROM data_source WHERE source_name = %s", (name,)).fetchone()
    if row is None:
        raise LookupError(f"data_source not seeded: {name}")
    return row[0]


def _gb(value):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    return f"{value} GB" if ok else "미확인"


def scope(target):
    return {"model": target["model_name"], "region": target["region_filter"] or "all",
            "contract": target["contract_filter"], "scope_version": target["scope_version"]}
