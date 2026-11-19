"""Nebius 공개 가격 수집 → ERD 초안 스키마 적재.

| 출처(data_source.source_name) | 계약 | 원천 |
| nebius_pricing_docs | on_demand, commitment(견적 문의) | docs.nebius.com/compute/resources/pricing.md (공식 문서 가격표, 마크다운) |
| nebius_spot_price_api | spot | GET api.nebius.cloud/billing/v1alpha1/spot-price (공식 문서상 공개·인증 없음) |
| nebius_vm_types_docs | (사양 근거) | VM 유형·프리셋·지역 문서. 수기로 확인한 값을 아래 PLATFORMS에 두고 근거로 남긴다 |

- 상품 = GPU 플랫폼 × 프리셋(1GPU·8GPU) × 지역. 가격은 GPU 1장·시간 단위라 GPU 수로 나누지 않는다.
- 온디맨드: 문서 가격표의 USD 표. 적용 기간 표('from …'·'before …')가 있으면 기준 시각에 적용되는 표만 쓴다.
  문서의 선점형 'From $…'은 시작가라 쓰지 않고, 스팟은 공개 API 값을 쓴다.
- 스팟: SKU(플랫폼 × 지역)별 현재 가격과 적용 시각(effectiveTime). 같은 플랫폼의 프리셋마다 같은 값을 붙인다.
- 약정: 공개 가격이 없어(영업 문의) 관측을 만들지 않고 대상 점검을 '견적 문의'로 남긴다.
- VR200은 가격을 수집하지 않고 가격표·스팟 API에 등재됐는지만 매일 확인한다(FR-027 자동 확인).
- 스토리지(부트 디스크)는 VM 가격에 포함되지 않는다. 노드 간 네트워크는 GPU 클러스터용 8GPU 프리셋에만 둔다.
- 과거 이력은 nebius/history.py(스팟 30일 시계열 API, 문서 Wayback 저장본).
"""
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

from ... import fetch
from .. import db, runs

PROVIDER = "Nebius"
COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-nebius 1"
RULE_VERSION = "v3"
SPIKE_RULE = "OR-06 직전 대비 ±30% 초과"
SPIKE_THRESHOLD = Decimal("0.30")

SOURCE_DOCS = "nebius_pricing_docs"
SOURCE_SPOT = "nebius_spot_price_api"
SOURCE_TYPES = "nebius_vm_types_docs"
PRICING_URL = "https://docs.nebius.com/compute/resources/pricing.md"
SPOT_URL = "https://api.nebius.cloud/billing/v1alpha1/spot-price"
TYPES_URL = "https://docs.nebius.com/compute/virtual-machines/types.md"
REGIONS_URL = "https://docs.nebius.com/overview/regions.md"
COMMITMENT_URL = "https://docs.nebius.com/signup-billing/billing-models/overview"
REQUEST_CONFIG = {"connect_timeout_s": 15, "read_idle_timeout_s": 60}
SECONDS_BETWEEN_REQUESTS = 2
MAX_PAGES = 20
GIB_TO_GB = Decimal("1.073741824")

DOC_VERSION = "docs.nebius.com 2026-11-18 확인"
# VM 유형 문서(2026-11-18 확인). 프리셋 = (이름, GPU 수, vCPU, RAM GiB). 8GPU 프리셋만 GPU 클러스터와 호환된다.
PLATFORMS = {
    "gpu-b300-sxm": {"model": "B300", "name": "NVIDIA® B300 NVLink", "vram": 270,
                     "infiniband": "InfiniBand 800 Gbps (8× ConnectX-8), GPU 클러스터 호환 프리셋",
                     "presets": [("1gpu-24vcpu-346gb", 1, 24, 346), ("8gpu-192vcpu-2768gb", 8, 192, 2768)]},
    "gpu-b200-sxm": {"model": "B200", "name": "NVIDIA® B200 NVLink", "vram": 180,
                     "infiniband": "InfiniBand 400 Gbps (8× ConnectX-7), GPU 클러스터 호환 프리셋",
                     "presets": [("1gpu-20vcpu-224gb", 1, 20, 224), ("8gpu-160vcpu-1792gb", 8, 160, 1792)]},
    "gpu-b200-sxm-a": {"model": "B200", "name": "NVIDIA® B200 NVLink", "vram": 180,
                       "infiniband": "InfiniBand 400 Gbps (8× ConnectX-7), GPU 클러스터 호환 프리셋",
                       "presets": [("1gpu-20vcpu-224gb", 1, 20, 224), ("8gpu-160vcpu-1792gb", 8, 160, 1792)]},
    "gpu-h200-sxm": {"model": "H200", "name": "NVIDIA® H200 NVLink", "vram": 141,
                     "infiniband": "InfiniBand 400 Gbps (8× ConnectX-7), GPU 클러스터 호환 프리셋",
                     "presets": [("1gpu-16vcpu-200gb", 1, 16, 200), ("8gpu-128vcpu-1600gb", 8, 128, 1600)]},
    "gpu-h100-sxm": {"model": "H100", "name": "NVIDIA® H100 NVLink", "vram": 80,
                     "infiniband": "InfiniBand 400 Gbps (8× ConnectX-7), GPU 클러스터 호환 프리셋",
                     "presets": [("1gpu-16vcpu-200gb", 1, 16, 200), ("8gpu-128vcpu-1600gb", 8, 128, 1600)]},
}
# 가격표 제목에 플랫폼 ID가 없는 섹션
PLATFORM_BY_HEADING = {"NVIDIA® H200 NVLink": "gpu-h200-sxm", "NVIDIA® H100 NVLink": "gpu-h100-sxm"}
# 지역 문서(2026-11-18 확인)
COUNTRY_BY_REGION = {"eu-north1": "FI", "eu-north2": "IS", "eu-south1": "ES", "eu-west1": "FR", "eu-west2": "FR",
                     "me-west1": "IL", "us-central1": "US", "us-north1": "US", "uk-south1": "GB", "uk-south2": "GB"}
STORAGE = "VM 가격에 포함되지 않음(부트 디스크·볼륨 별도 과금)"
OFFER_BY_KIND = {"on_demand": ("on_demand", "On-demand"), "spot": ("spot", "Preemptible (spot)")}
RAW_TERMS = {"on_demand": "Nebius 온디맨드 VM, GPU 1장·시간 가격(초 단위 과금)",
             "spot": "Nebius 선점형(preemptible) VM 스팟 가격. 최대 15분마다 바뀌고 온디맨드보다 높아질 수 있음"}
EXPECTED_ERRORS = (fetch.FetchError, OSError, ValueError, LookupError)

ROW = re.compile(r"^\|(.+)\|$")
PRICE = re.compile(r"(?P<from>From\s*)?\\?\$\s?(?P<amount>[\d,]+(?:\.\d+)?)")
PERIOD = re.compile(r"\b(?P<kind>from|before)\s+(?P<first>[A-Z][a-z]+ \d{1,2})(?P<year>, \d{4})?"
                    r"(?:\s+until\s+(?P<until>[A-Z][a-z]+ \d{1,2}, \d{4}))?")
REGION = re.compile(r"`([a-z]+-[a-z]+\d+)`")
# 가격 없이 등재 여부만 확인하는 모델
DETECT = {"VR200": re.compile(r"VR200|Vera Rubin|\bRubin\b|gpu-vr\d|gpu-r\d00", re.I)}


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


@dataclass
class TargetResult:
    hits: list = field(default_factory=list)
    source_url: str = ""
    source_version: str | None = None
    disclosure: str | None = None
    reason: str | None = None
    note: dict = field(default_factory=dict)


# 매일 수집 ----------------------------------------------------------------------

def collect(conn, *, trigger_type="manual", now=None, date=None, get_text=None, get_json=fetch.get_json,
            pause=time.sleep):
    """Nebius 수집 1회: 문서 가격표(온디맨드·약정 상태) → 스팟 API. date: 수집 기준일(KST, 생략하면 오늘).

    같은 수집 기준일에 이미 적재한 묶음(문서 가격표·스팟)은 건너뛴다. 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    day = runs.collection_date(date, now)
    get_text = get_text or fetch_text
    targets = db.enabled_targets(conn, PROVIDER)
    docs_targets_ = [t for t in targets if t["source_name"] == SOURCE_DOCS and t["contract_filter"]]
    detect_targets = [t for t in targets if t["source_name"] == SOURCE_DOCS and t["model_name"] in DETECT]
    spot_targets = [t for t in targets if t["source_name"] == SOURCE_SPOT]
    if not docs_targets_ and not spot_targets:
        raise LookupError("no enabled Nebius collection_target")
    run_id = db.begin_run(conn, now(), trigger_type, COLLECTOR_VERSION)
    conn.commit()
    context = {"provider": db.provider_id(conn, PROVIDER), "rule": db.rule(conn, RULE_VERSION), "run_id": run_id,
               "now": now, "date": day, "docs": spec_targets(conn), "docs_source": source_id(conn, SOURCE_TYPES)}
    outcomes = []
    try:
        if docs_targets_ and runs.group_done(conn, context, docs_targets_ + detect_targets):
            outcomes.append(runs.skipped({"source": SOURCE_DOCS}))
        elif docs_targets_:
            try:
                checked = now()
                text = get_text(PRICING_URL, REQUEST_CONFIG)
                sections = parse_pricing(text, checked)
                results = docs_results(docs_targets_, sections, PRICING_URL, None, "공식 문서 가격표")
                results.update(detect_results(detect_targets, text))
                counts = write_group(conn, context, docs_targets_ + detect_targets, results, checked, role="official")
                conn.commit()
                outcomes.append({"source": SOURCE_DOCS, "status": "success", **counts})
            except EXPECTED_ERRORS as error:
                outcomes.append(_fail(conn, context, docs_targets_ + detect_targets, SOURCE_DOCS, error))
        if spot_targets and runs.group_done(conn, context, spot_targets):
            outcomes.append(runs.skipped({"source": SOURCE_SPOT}))
        elif spot_targets:
            try:
                if docs_targets_:
                    pause(SECONDS_BETWEEN_REQUESTS)
                items = fetch_spot(get_json, pause)
                checked = now()
                results = spot_results(spot_targets, items, SPOT_URL)
                counts = write_group(conn, context, spot_targets, results, checked, role="official")
                conn.commit()
                outcomes.append({"source": SOURCE_SPOT, "status": "success", **counts})
            except EXPECTED_ERRORS as error:
                outcomes.append(_fail(conn, context, spot_targets, SOURCE_SPOT, error))
    except Exception as error:
        conn.rollback()
        db.finish_run(conn, run_id, now(), "failed", f"unexpected: {type(error).__name__}")
        conn.commit()
        raise
    status = runs.run_status(outcomes)
    failed = [f"{o['source']}: {o['reason']}" for o in outcomes if o["status"] not in ("success", "skipped")]
    db.finish_run(conn, run_id, now(), status, "; ".join(failed)[:2000] or None)
    conn.commit()
    return {"run_id": run_id, "date": day, "status": status, "groups": outcomes}


def _fail(conn, context, targets, source, error):
    processing = "validation_failed" if isinstance(error, SourceInvalid) else "collection_failed"
    reason = f"{type(error).__name__}: {error}"
    record_failure(conn, context, targets, processing, reason,
                   key=lambda t: runs.failure_key(context, t))
    return {"source": source, "status": processing, "reason": reason}


def fetch_text(url, config):
    """문서(마크다운) 원문을 받는다. 일시적 오류는 짧게 다시 시도하고, 그래도 안 되거나 403이면 실패로 남긴다."""
    timeout = (config.get("connect_timeout_s", 15), config.get("read_idle_timeout_s", 60))
    try:
        response = fetch.send(lambda: requests.get(url, headers={"User-Agent": fetch.USER_AGENT}, timeout=timeout))
    except requests.RequestException as error:
        raise fetch.FetchError(f"request_failed: {type(error).__name__}") from error
    if response.status_code != 200:
        raise fetch.FetchError(f"http_{response.status_code}")
    response.encoding = response.encoding or "utf-8"
    return response.text


def fetch_spot(get_json, pause):
    items, token = [], None
    for page in range(MAX_PAGES):
        if page:
            pause(SECONDS_BETWEEN_REQUESTS)
        url = f"{SPOT_URL}?pageSize=100" + (f"&pageToken={token}" if token else "")
        body = get_json(url, REQUEST_CONFIG)
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise SourceInvalid("unexpected_spot_price_format")
        items.extend(body["items"])
        token = body.get("nextPageToken")
        if not token:
            return items
    raise SourceInvalid(f"page_limit_exceeded: {MAX_PAGES}")


# 문서 가격표 해석 (매일 수집·Wayback 공통) ------------------------------------------------

def parse_pricing(text, as_of):
    """반환: {플랫폼: {heading, regions, on_demand, spot_fixed, skipped}}. as_of에 적용되는 표의 USD 행만 쓴다.

    on_demand·spot_fixed: {amount, raw, period, effective_from} 또는 None. 'From $…'(시작가)는 skipped에 남긴다."""
    if not isinstance(text, str) or "#### " not in text:
        raise SourceInvalid("unexpected_pricing_doc_format")
    sections = {}
    parts = re.split(r"^#### (.+)$", text, flags=re.M)
    for heading, body in zip(parts[1::2], parts[2::2]):
        platform = heading_platform(heading)
        if platform is None:
            continue
        section = {"heading": heading.strip(), "regions": regions(body), "on_demand": None, "spot_fixed": None,
                   "skipped": []}
        for row, period in usd_rows(body):
            item, price, per = row
            if per != "1 GPU hour" or item.endswith((". GPU", ". CPU", ". RAM")):
                continue
            if not applies(period, as_of):
                continue
            kind = "spot_fixed" if item.startswith("Preemptible") else "on_demand"
            match = PRICE.search(price)
            if match is None:
                section["skipped"].append({"item": item, "price": price, "reason": "price_not_parsed"})
                continue
            if match.group("from"):
                section["skipped"].append({"item": item, "price": price, "reason": "starting_from"})
                continue
            if section[kind] is None:
                section[kind] = {"amount": _decimal(match.group("amount").replace(",", ""), (platform, item)),
                                 "raw": f"{item} | {price} | {per}" + (f" ({period[2]})" if period else ""),
                                 "effective_from": period[0] if period else None}
        sections[platform] = section
    if not sections:
        raise SourceInvalid("no_target_platform_in_pricing_doc")
    return sections


def heading_platform(heading):
    match = re.search(r"(gpu-[a-z0-9-]+)\s*$", heading.strip())
    if match:
        return match.group(1) if match.group(1) in PLATFORMS else None
    name = re.sub(r"\s+with\s+.*$", "", heading.split(",")[0]).strip()
    return PLATFORM_BY_HEADING.get(name)


def regions(body):
    for line in body.splitlines():
        if "available" in line and "region" in line:
            return REGION.findall(line)
    return []


def usd_rows(body):
    """[((항목, 가격, 단위), 기간 또는 None)]. 기간 = (시작 또는 None, 끝 또는 None, 원문).

    USD 표만 본다. 표기: 현재 '> [tab] USD', 과거 '<Tab title="USD">'·'**USD**'."""
    starts = [i for i in (body.find("[tab] USD"), body.find('<Tab title="USD">'), body.find("**USD**")) if i >= 0]
    if not starts:
        return []
    start = min(starts)
    ends = [i for i in (body.find("[tab] ILS", start), body.find('<Tab title="ILS">', start),
                        body.find("**ILS**", start)) if i >= 0]
    block = body[start:min(ends) if ends else len(body)]
    rows, period = [], None
    for line in block.splitlines():
        line = line.strip().lstrip(">").strip()
        match = ROW.match(line)
        if not match:
            continue
        cells = [c.strip() for c in match.group(1).split("|")]
        if len(cells) < 3 or set(cells[0]) <= {"-", " "}:
            continue
        if cells[0].startswith("**Item"):
            period = parse_period(cells[0])
            continue
        rows.append(((cells[0], cells[1], cells[2]), period))
    return rows


def parse_period(label):
    """'from October 1, 2026' / 'before October 1, 2026' / 'from October 1 until October 8, 2026' → (시작, 끝, 원문)."""
    found = PERIOD.search(label)
    if found is None:
        return None
    until = _date(found.group("until")) if found.group("until") else None
    year = found.group("year") or (f", {until.year}" if until else None)
    if year is None:
        raise SourceInvalid(f"period_without_year: {label}")
    first = _date(found.group("first") + year)
    text = found.group(0)
    return (first, until, text) if found.group("kind") == "from" else (None, first, text)


def applies(period, as_of):
    if period is None:
        return True
    start, end, _label = period
    return (start is None or as_of >= start) and (end is None or as_of < end)


def docs_results(targets, sections, url, version, label):
    """on_demand 대상: 플랫폼 × 프리셋 × 지역 온디맨드. commitment 대상: 견적 문의 상태. spot 대상(Wayback만): 고정 선점형 가격."""
    results = {}
    for target in targets:
        kind = target["contract_filter"]
        result = TargetResult(source_url=url, source_version=version)
        if kind == "commitment":
            result.disclosure = "contact_sales"
            result.source_url = COMMITMENT_URL
            result.reason = "약정가 미공개: 장기 약정 할인은 법인 대상 영업 문의(공식 문서). 관측을 만들지 않음"
            results[target["target_id"]] = result
            continue
        field_name = {"on_demand": "on_demand", "spot": "spot_fixed"}.get(kind)
        if field_name is None:
            raise SourceInvalid(f"unsupported Nebius docs target: {target['model_name']} {kind}")
        skipped = []
        for platform, section in sorted(sections.items()):
            if PLATFORMS[platform]["model"] != target["model_name"]:
                continue
            skipped.extend({"platform": platform, **s} for s in section["skipped"])
            price = section[field_name]
            if price is None:
                continue
            for region in section["regions"]:
                for preset in PLATFORMS[platform]["presets"]:
                    result.hits.append(make_hit(target, platform, preset, region, kind, price["amount"],
                                                price["raw"], price["effective_from"], url, version,
                                                {"heading": section["heading"], "regions": section["regions"]}))
        result.note = {"skipped_rows": skipped} if skipped else {}
        if not result.hits:
            result.reason = f"{label}에 {target['model_name']} {kind} 가격 없음" + (
                f" (시작가 등 제외 {len(skipped)}행)" if skipped else "")
        results[target["target_id"]] = result
    return results


def detect_results(targets, text):
    """등재 여부만 확인하는 대상. 가격표 문서에 모델이 보이면 공개(가격 수집 대상 추가 필요), 없으면 미발견."""
    results = {}
    for target in targets:
        found = sorted({m.group(0) for m in DETECT[target["model_name"]].finditer(text)})
        result = TargetResult(source_url=PRICING_URL, disclosure="public" if found else None)
        result.reason = (f"공식 가격표에 등재 표현: {', '.join(found)}. 가격 수집 대상 추가 필요" if found
                         else f"공식 가격표에 {target['model_name']}(Vera Rubin) 없음. 미공개로 단정하지 않음")
        result.note = {"detected": found}
        results[target["target_id"]] = result
    return results


def spot_results(targets, items, url):
    results = {}
    for target in targets:
        if target["contract_filter"] != "spot":
            raise SourceInvalid(f"unsupported Nebius spot target: {target['model_name']} {target['contract_filter']}")
        result = TargetResult(source_url=url)
        excluded = []
        for item in items:
            platform = (item.get("computeInstance") or {}).get("platform")
            if platform not in PLATFORMS or PLATFORMS[platform]["model"] != target["model_name"]:
                continue
            price = item.get("price") or {}
            if str(price.get("currency", "")).lower() != "usd" or price.get("unitTitle") != "GPU hour":
                excluded.append({"skuId": item.get("skuId"), "reason": f"unexpected_unit: {price}"})
                continue
            effective = _timestamp(item.get("effectiveTime"))
            for preset in PLATFORMS[platform]["presets"]:
                result.hits.append(make_hit(
                    target, platform, preset, item.get("region"), "spot", _decimal(price.get("value"), item.get("skuId")),
                    f"{price.get('value')} {price.get('currency')} / {price.get('unitTitle')} "
                    f"({item.get('productTitle')}, {item.get('region')}, {item.get('skuId')})", effective, url, None,
                    {"skuId": item.get("skuId"), "productTitle": item.get("productTitle"), "effectiveTime": item.get("effectiveTime")}))
        result.note = {"excluded": excluded} if excluded else {}
        if not result.hits:
            result.reason = f"spot-price에 {target['model_name']} 스팟 가격 없음"
        results[target["target_id"]] = result
    return results


def make_product(target, platform, preset, region):
    name, gpus, vcpu, ram_gib = preset
    spec = PLATFORMS[platform]
    return {
        "provider_id": target["provider_id"], "gpu_model_id": target["gpu_model_id"],
        "original_product_name": f"{platform} {name}", "region_code": region, "system_type": "instance",
        "service_option": "standard", "sku": None, "original_model_name": spec["name"], "gpu_count": gpus,
        "vram_gb_per_gpu": Decimal(spec["vram"]), "vcpu_count": vcpu, "ram_gb": Decimal(ram_gib) * GIB_TO_GB,
        "storage_description": STORAGE, "spec_scope": "instance",
        "inter_node_network": spec["infiniband"] if gpus == 8 else None,
        "country_code": COUNTRY_BY_REGION.get(region),
    }


def make_hit(target, platform, preset, region, kind, amount, raw, effective_from, url, version, extra):
    contract_type, product_type = OFFER_BY_KIND[kind]
    offer = {"contract_type": contract_type, "contract_product_type": product_type, "commitment_months": None,
             "payment_option": None}
    observation = {"effective_from": effective_from, "price_certainty": "firm", "raw_price_text": raw,
                   "original_currency": "USD", "original_time_unit": "hour", "price_applies_to": "gpu",
                   "raw_terms": RAW_TERMS[kind]}
    components = [{"component_type": "hourly_rate", "amount": amount, "currency": "USD", "time_unit": "hour",
                   "applies_to": "gpu", "raw_value": raw}]
    excerpt = {"platform": platform, "preset": preset[0], "region": region, **extra}
    return Hit(kind, make_product(target, platform, preset, region), offer, observation, components, excerpt, url,
               version)


# 환산 (DR-006, 규칙 v3) ------------------------------------------------------------

def normalize(hit, component_ids, monthly_hours):
    inputs = {"component_ids": component_ids, "gpu_count": hit.product.get("gpu_count"),
              "monthly_hours": str(monthly_hours), "kind": hit.kind, "applies_to": "gpu"}
    hourly = next((c["amount"] for c in hit.components if c["component_type"] == "hourly_rate"), None)
    if hourly is None:
        return {"nominal": None, "effective": None, "status": "not_computable", "reason": "시간당 요금 없음",
                "inputs": inputs}
    return {"nominal": hourly, "effective": hourly, "status": "success", "reason": None,
            "inputs": {**inputs, "formula": "GPU 1장 시간당 가격(GPU 수로 나누지 않음)"}}


# 적재 -------------------------------------------------------------------------

def write_group(conn, context, targets, results, checked_at, *, role):
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
            ensure_spec_evidence(conn, context, target, product_id, hit, checked_at)
            db.add_evidence(conn, check_id, target["source_id"], role, hit.source_url, hit.source_version,
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
        db.add_evidence(conn, check_id, target["source_id"], role, result.source_url, result.source_version,
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
    db.add_normalization(conn, observation_id, rule["rule_id"], normalize(hit, component_ids, rule["monthly_hours"]),
                         calculated_at)
    return observation_id


def ensure_spec_evidence(conn, context, target, product_id, hit, captured_at):
    """새 상품에 VM 유형·지역 문서 근거를 연결한다. GPU 2장 이상이면 노드 안 상호연결(NVLink)을 확인으로 바꾼다."""
    if db.has_product_evidence(conn, product_id):
        return
    docs = context["docs"].get(target["gpu_model_id"])
    if docs is None:
        return
    platform = hit.excerpt["platform"]
    spec = PLATFORMS[platform]
    gpus = hit.product["gpu_count"]
    preset = next(p for p in spec["presets"] if p[0] == hit.excerpt["preset"])
    check_id, _ = db.add_check(conn, context["run_id"], docs["target_id"], "product", captured_at, "public", "success",
                               True, f"spec:{product_id}:{DOC_VERSION}", product_id=product_id)
    evidence_id = db.add_evidence(
        conn, check_id, context["docs_source"], "spec", TYPES_URL, DOC_VERSION,
        {"platform": platform, "platform_name": spec["name"],
         "quotes": [f"{spec['name']} | {platform}", f"`{preset[0]}` | {preset[1]} | {preset[2]} | {preset[3]}",
                    f"GPU memory {spec['vram']} GB; InfiniBand {spec['infiniband'].split(',')[0].removeprefix('InfiniBand ')}"],
         "regions_doc": REGIONS_URL, "region_country": f"{hit.product['region_code']}: {hit.product['country_code']}",
         "verified_by": "manual", "verified_on": "2026-11-18"}, captured_at)
    attributes = ("gpu_count", "vram_gb_per_gpu", "vcpu_count", "ram_gb", "country_code") + (
        ("inter_node_network",) if gpus == 8 else ())
    db.add_product_evidence(conn, product_id, evidence_id, attributes,
                            f"NVLink (플랫폼 '{spec['name']}')" if gpus > 1 else None)


def check_spike(conn, context, target, offer_id, check_id, observation_id, checked_at):
    if target["contract_filter"] == "spot":
        return        # 스팟은 하루에도 여러 번 크게 바뀌므로 급변 알림 대상에서 뺀다
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


record_failure = runs.record_failure


# 공통 -------------------------------------------------------------------------

def spec_targets(conn):
    return {t["gpu_model_id"]: t for t in db.enabled_targets(conn, PROVIDER) if t["source_name"] == SOURCE_TYPES}


def source_id(conn, name):
    row = conn.execute("SELECT source_id FROM data_source WHERE source_name = %s", (name,)).fetchone()
    if row is None:
        raise LookupError(f"data_source not seeded: {name}")
    return row[0]


def scope(target):
    return {"model": target["model_name"], "region": target["region_filter"] or "all",
            "contract": target["contract_filter"], "scope_version": target["scope_version"]}


def _decimal(value, where):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError) as error:
        raise SourceInvalid(f"invalid_amount: {where} {value!r}") from error
    if amount < 0:
        raise SourceInvalid(f"negative_amount: {where} {value!r}")
    return amount


def _date(text):
    return datetime.strptime(text, "%B %d, %Y").replace(tzinfo=timezone.utc)


def _timestamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
