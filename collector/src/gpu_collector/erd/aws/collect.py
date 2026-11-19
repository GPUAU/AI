"""AWS 공개가격 수집 → ERD 초안 스키마 적재.

| 출처(data_source.source_name) | 계약 (collection_target.contract_filter) | 원천 |
| aws_ec2_price_list | on_demand, reserved_instances (+ GB300 등재 확인) | EC2 가격 파일(리전별) |
| aws_savings_plans_price_list | savings_plans | Savings Plans 가격 파일(리전별) |
| aws_ec2_spot_api | spot | DescribeSpotPriceHistory (IAM 키, 가용영역 단위) |
| aws_ec2_pricing_api | on_demand, reserved_instances (+ GB300 등재 확인) | Price List Query API GetProducts (IAM 키) |
| aws_savings_plans_api | savings_plans | DescribeSavingsPlansOfferingRates (IAM 키) |
| aws_ec2_instance_docs | (사양 근거) | AWS 인스턴스 사양 문서·제품 페이지. 수기로 확인한 값을 아래 SPECS에 두고 근거로 남긴다 |

매일 수집은 API 출처(aws_ec2_pricing_api·aws_savings_plans_api·aws_ec2_spot_api)를 쓴다. 대상 상품만 받아 수 KB다.
가격 파일 출처는 같은 값을 파일 전체(수백 MB)로 받으므로 기본으로 꺼 두고(collection_target.enabled), 과거 버전 적재
(aws_history)와 API 장애 시 대체용으로 남긴다.

- 수집 범위는 collection_target(enabled)에서 읽는다. 출처·리전 묶음마다 따로 처리해 한 묶음의 실패가 다른 묶음의
  결과를 지우지 않는다. 실행 상태는 성공 / 부분 실패 / 실패.
- 가격이 같아도 실행마다 관측을 추가한다(DR-01). 원천에 없는 오퍼는 만들지 않고 미발견 점검으로 남긴다(DR-11).
- Savings Plans·스팟은 같은 리전 EC2 상품의 사양을 쓴다. 그래서 EC2 가격 파일을 먼저 처리한다.
- 스팟 가격은 가용영역마다 다르다. 가용영역 ID를 region_code로 쓰는 상품을 따로 둔다.
- 가격 파일에는 GPU 모델명과 GPU 간 연결 방식이 없다. 모델 원문(original_model_name)과 노드 안 상호연결은 공식 문서
  (SPECS)에서 채우고, 상품마다 사양 근거(product_evidence)를 한 번 연결한다. 노드 간 네트워크는 가격 파일 원문을 둔다.
"""
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import ijson

from ... import aws_offer, aws_savings, fetch
from ...download import cached_download
from ...env import MissingSecret
from .. import db, runs
from . import api as aws_api, spot as aws_spot

PROVIDER = "AWS"
COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-aws 1"
RULE_VERSION = "v1"
SPIKE_RULE = "OR-06 직전 대비 ±30% 초과"
SPIKE_THRESHOLD = Decimal("0.30")

SOURCE_EC2 = "aws_ec2_price_list"
SOURCE_SP = "aws_savings_plans_price_list"
SOURCE_SPOT = "aws_ec2_spot_api"
SOURCE_EC2_API = "aws_ec2_pricing_api"
SOURCE_SP_API = "aws_savings_plans_api"
SOURCE_DOCS = "aws_ec2_instance_docs"
# Savings Plans·스팟이 EC2 상품의 사양을 이어받으므로 EC2 출처를 먼저 처리한다
SOURCE_ORDER = (SOURCE_EC2, SOURCE_EC2_API, SOURCE_SP, SOURCE_SP_API, SOURCE_SPOT)

PRICING_HOST = "https://pricing.us-east-1.amazonaws.com"
EC2_INDEX = f"{PRICING_HOST}/offers/v1.0/aws/AmazonEC2/current/region_index.json"
SP_INDEX = f"{PRICING_HOST}/savingsPlan/v1.0/aws/AWSComputeSavingsPlan/current/region_index.json"
REQUEST_CONFIG = {"connect_timeout_s": 15, "read_idle_timeout_s": 60, "total_timeout_s": 900}

# 모델 → AWS 인스턴스 유형. GB300은 가격 파일에 인스턴스가 없어 등재 여부만 확인한다.
INSTANCE_BY_MODEL = {"B300": "p6-b300.48xlarge", "B200": "p6-b200.48xlarge", "H100": "p5.48xlarge"}
DETECT_ONLY = {"GB300": "gb300"}

# 인스턴스 유형별 공식 문서 값(2026-11-18 수기 확인). 모델 원문은 사양 표 'Accelerators' 열에서 'N x '를 뺀 값이다.
AC_DOC = "https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html"
AC_PAGE = "https://aws.amazon.com/ec2/instance-types/accelerated-computing/"
DOC_VERSION = "docs.aws.amazon.com·aws.amazon.com 2026-11-18 확인"
SPECS = {
    "p6-b300.48xlarge": {
        "original_model_name": "NVIDIA B300 GPU",
        "intra_node_interconnect": "NVLink(인스턴스 8 GPU가 단일 NVLink 도메인), GPU 간 1800 GB/s",
        "doc_urls": [AC_DOC, "https://aws.amazon.com/blogs/aws/accelerate-large-scale-ai-applications-with-the-new-amazon-ec2-p6-b300-instances/",
                     AC_PAGE],
        "quotes": ["p6-b300.48xlarge | 4096.00 | Intel Xeon Emerald Rapids | 192 | … | 8 x NVIDIA B300 GPU | 2148 GiB (8 x 268 GiB)",
                   "P6-B300.48xlarge | 192 | 4TB | 8x NVIDIA B300 GPU | 2144GB HBM3e | GPU-GPU interconnect 1800 GB/s | 6.4 Tbps",
                   "allowing large models to reside within a single NVIDIA NVLink domain",
                   "deliver 6.4Tbps Elastic Fabric Adapter (EFA) networking bandwidth"]},
    "p6-b200.48xlarge": {
        "original_model_name": "NVIDIA B200 GPU",
        "intra_node_interconnect": "NVSwitch(NVLink), GPU 간 1800 GB/s, 총 양방향 14.4 TB/s",
        "doc_urls": [AC_DOC, AC_PAGE, "https://aws.amazon.com/ec2/instance-types/p6/"],
        "quotes": ["p6-b200.48xlarge | 2048.00 | Intel Xeon Emerald Rapids | 192 | … | 8 x NVIDIA B200 GPU | 1432 GiB (8 x 179 GiB)",
                   "1800 GB/s peer-to-peer GPU communication with NVIDIA NVSwitch",
                   "up to 14.4 TBp/s of total bidirectional NVLink bandwidth", "EFA network bandwidth 3.2 Tbps"]},
    "p5.48xlarge": {
        "original_model_name": "NVIDIA H100 GPU",
        "intra_node_interconnect": "NVSwitch, GPU 간 900 GB/s",
        "doc_urls": [AC_DOC, AC_PAGE, "https://aws.amazon.com/ec2/instance-types/p5/"],
        "quotes": ["p5.48xlarge | 2048.00 | AMD EPYC 7R13 | 192 | … | 8 x NVIDIA H100 GPU | 640 GiB (8 x 80 GiB)",
                   "900 GB/s peer-to-peer GPU communication with NVIDIA NVSwitch",
                   "P5, P5e, and P5en instances provide up to 3,200 Gbps of EFA networking"]},
}
# 근거로 연결하는 속성. VRAM은 문서(GiB)와 가격 파일(GB) 단위가 달라 연결하지 않는다.
SPEC_ATTRIBUTES = ("original_model_name", "gpu_count", "vcpu_count", "ram_gb")
COUNTRY_BY_REGION = {"us-east-1": "US", "us-east-2": "US", "us-west-1": "US", "us-west-2": "US",
                     "ap-northeast-1": "JP", "ap-northeast-2": "KR", "ap-northeast-3": "JP",
                     "sa-east-1": "BR", "eu-west-2": "GB", "ap-south-2": "IN", "us-gov-east-1": "US"}
PAYMENT = {"No Upfront": "no_upfront", "Partial Upfront": "partial_upfront", "All Upfront": "all_upfront"}
LEASE_MONTHS = {"1yr": 12, "3yr": 36}
PLAN_NAME = {"ComputeSavingsPlans": "Compute Savings Plans", "EC2InstanceSavingsPlans": "EC2 Instance Savings Plans"}
PLAN_FAMILY_BY_API_TYPE = {"Compute": "ComputeSavingsPlans", "EC2Instance": "EC2InstanceSavingsPlans"}
SECONDS_PER_YEAR = 31536000
GIB_TO_GB = Decimal("1.073741824")
EXPECTED_ERRORS = (fetch.FetchError, aws_spot.SpotApiError, aws_api.AwsApiError, MissingSecret, ijson.JSONError, OSError, ValueError,
                   LookupError)


class SourceInvalid(ValueError):
    """원천은 받았으나 적재 전 검증을 통과하지 못했다(검증 실패)."""


@dataclass
class Hit:
    """원천에서 확인한 오퍼 가격 하나."""
    kind: str                      # on_demand / reserved / savings_plans / spot
    product: dict                  # 상품 구성. product_id가 있으면 기존 상품을 그대로 쓴다
    offer: dict
    observation: dict
    components: list
    excerpt: dict
    source_url: str
    source_version: str | None = None


@dataclass
class TargetResult:
    hits: list = field(default_factory=list)
    source_url: str = ""
    source_version: str | None = None
    disclosure: str | None = None    # 정하지 않으면 확인한 오퍼가 있을 때 공개가, 없으면 미확인
    reason: str | None = None


def collect(conn, *, trigger_type="manual", only=None, cache_dir="var/cache", now=None, date=None,
            get_json=fetch.get_json, downloader=fetch.download, spot_prices=aws_spot.current_prices,
            pricing_api=aws_api):
    """AWS 수집 1회. only: 처리할 출처 이름 모음(생략하면 전체). date: 수집 기준일(KST, 생략하면 오늘).

    같은 수집 기준일에 이미 적재한 (출처, 리전) 묶음은 건너뛴다. 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    day = runs.collection_date(date, now)
    provider = db.provider_id(conn, PROVIDER)
    rule = db.rule(conn, RULE_VERSION)
    targets = [t for t in db.enabled_targets(conn, PROVIDER)
               if t["source_name"] in HANDLERS and (only is None or t["source_name"] in only)]
    if not targets:
        raise LookupError("no enabled AWS collection_target")
    run_id = db.begin_run(conn, now(), trigger_type, COLLECTOR_VERSION)
    conn.commit()

    groups = OrderedDict()
    for target in sorted(targets, key=lambda t: (SOURCE_ORDER.index(t["source_name"]), t["region_filter"] or "")):
        groups.setdefault((target["source_name"], target["region_filter"]), []).append(target)

    context = {"provider": provider, "rule": rule, "run_id": run_id, "now": now, "date": day, "cache_dir": Path(cache_dir),
               "get_json": get_json, "downloader": downloader, "spot_prices": spot_prices, "pricing_api": pricing_api,
               **spec_context(conn)}
    outcomes = []
    for (source_name, region), group in groups.items():
        if runs.group_done(conn, context, group):
            outcomes.append(runs.skipped({"source": source_name, "region": region}))
            continue
        try:
            results = HANDLERS[source_name](conn, context, region, group)
            counts = _write_group(conn, context, group, results)
            conn.commit()
            outcomes.append({"source": source_name, "region": region, "status": "success", **counts})
        except EXPECTED_ERRORS as error:
            processing = "validation_failed" if isinstance(error, SourceInvalid) else "collection_failed"
            _record_failure(conn, context, group, processing, f"{type(error).__name__}: {error}")
            outcomes.append({"source": source_name, "region": region, "status": processing,
                             "reason": f"{type(error).__name__}: {error}"})
        except Exception as error:
            _record_failure(conn, context, group, "collection_failed", f"unexpected: {type(error).__name__}: {error}")
            db.finish_run(conn, run_id, now(), "failed", f"unexpected: {type(error).__name__}")
            conn.commit()
            raise

    status = runs.run_status(outcomes)
    failed = [f"{o['source']}@{o['region']}: {o['reason']}" for o in outcomes if o["status"] not in ("success", "skipped")]
    db.finish_run(conn, run_id, now(), status, "; ".join(failed)[:2000] or None)
    conn.commit()
    return {"run_id": run_id, "date": day, "status": status, "groups": outcomes}


# 출처별 처리 ------------------------------------------------------------------

def _handle_ec2(conn, context, region, group):
    url, version = _versioned_url(context, EC2_INDEX, region)
    fetched = _download(context, url)
    return ec2_results(group, region, fetched.path, url, version)[1]


def ec2_results(group, region, path, url, version):
    """EC2 가격 파일 하나 → (파일 머리말, {target_id: TargetResult}). 과거 버전 적재에서도 쓴다."""
    instances = {INSTANCE_BY_MODEL[t["model_name"]] for t in group if t["model_name"] in INSTANCE_BY_MODEL}
    offer = aws_offer.read_offer(path, targets=instances)
    if offer.header.get("offerCode") != "AmazonEC2" or not offer.header.get("version"):
        raise SourceInvalid("unexpected_ec2_header")
    version = offer.header.get("version") or version
    detect = {DETECT_ONLY[t["model_name"]] for t in group if t["model_name"] in DETECT_ONLY}
    detected = _detect(path, detect) if detect else {}
    return offer.header, _ec2_target_results(group, region, offer, url, version, detected, "EC2 가격 파일")


def _handle_ec2_api(conn, context, region, group):
    """GetProducts로 대상 인스턴스 상품만 받는다. 응답 상품 문서는 가격 파일의 products·terms와 같은 구조다."""
    api = context["pricing_api"]
    instances = sorted({INSTANCE_BY_MODEL[t["model_name"]] for t in group if t["model_name"] in INSTANCE_BY_MODEL})
    products, on_demand, reserved, versions = {}, {}, {}, set()
    for instance in instances:
        for document in api.get_products(aws_api.ec2_filters(region, instance)):
            product = document.get("product") or {}
            sku = product.get("sku")
            if not sku or (product.get("attributes") or {}).get("instanceType") != instance:
                raise SourceInvalid(f"unexpected_get_products_item: {instance}@{region}")
            products[sku] = product
            terms = document.get("terms") or {}
            if terms.get("OnDemand"):
                on_demand[sku] = terms["OnDemand"]
            if terms.get("Reserved"):
                reserved[sku] = terms["Reserved"]
            versions.add((document.get("version"), document.get("publicationDate")))
    detected = {}
    for needle in sorted({DETECT_ONLY[t["model_name"]] for t in group if t["model_name"] in DETECT_ONLY}):
        detected[needle] = sorted({(d.get("product") or {}).get("attributes", {}).get("instanceType", "")
                                   for d in api.get_products(aws_api.ec2_filters(region, contains=needle))})
    # 한 번의 수집 안에서 가격표 버전이 바뀌면 가장 최근 버전을 원본 버전으로 남긴다
    version, published = max(versions, default=(None, None), key=lambda v: v[0] or "")
    offer = aws_offer.Offer({"offerCode": "AmazonEC2", "version": version, "publicationDate": published},
                            products, on_demand, reserved)
    url = (f"https://{aws_api.PRICING_HOST}/?Action=AWSPriceListService.GetProducts&ServiceCode=AmazonEC2"
           f"&regionCode={region}&instanceType={','.join(instances)}")
    return _ec2_target_results(group, region, offer, url, version, detected, "가격 조회 API(GetProducts)")


def _ec2_target_results(group, region, offer, url, version, detected, label):
    results = {}
    for target in group:
        model, contract = target["model_name"], target["contract_filter"]
        result = TargetResult(source_url=url, source_version=version)
        if model in DETECT_ONLY:
            names = detected.get(DETECT_ONLY[model], [])
            result.disclosure = "public" if names else None
            result.reason = (f"{region} {label}에 등재: {', '.join(names)}. 수집 대상 추가 필요" if names
                             else f"{region} {label}에 {model} 인스턴스 없음. 미공개로 단정하지 않음")
        elif contract in ("on_demand", "reserved_instances"):
            section = "OnDemand" if contract == "on_demand" else "Reserved"
            result.hits = list(_ec2_hits(target, offer, region, INSTANCE_BY_MODEL[model], section, url, version))
            if not result.hits:
                result.reason = f"{region} {label}의 {section}에 {INSTANCE_BY_MODEL[model]} 없음"
        else:
            raise SourceInvalid(f"unsupported contract_filter for {target['source_name']}: {contract}")
        results[target["target_id"]] = result
    return results


def _handle_savings(conn, context, region, group):
    url, version = _versioned_url(context, SP_INDEX, region)
    fetched = _download(context, url)
    base_product = lambda target, instance: db.latest_product(conn, context["provider"], target["gpu_model_id"],
                                                              instance, region)
    return savings_results(group, region, fetched.path, url, version, base_product)[1]


def savings_results(group, region, path, url, version, base_product):
    """Savings Plans 가격 파일 하나 → (파일 머리말, {target_id: TargetResult}).

    base_product(target, instance): 사양을 이어받을 EC2 상품. 과거 버전 적재는 그 시점의 구성을 넘긴다.
    """
    instances = {INSTANCE_BY_MODEL[t["model_name"]] for t in group}
    header, plans, terms = aws_savings.read_savings(path, instances=instances)
    if header.get("offerCode") not in (None, "AWSComputeSavingsPlan"):
        raise SourceInvalid(f"unexpected_savings_plans_header: {header.get('offerCode')}")
    version = header.get("version") or version
    pattern = aws_savings.usage_pattern(instances)

    results = {}
    for target in group:
        if target["contract_filter"] != "savings_plans":
            raise SourceInvalid(f"unsupported contract_filter for {SOURCE_SP}: {target['contract_filter']}")
        instance = INSTANCE_BY_MODEL[target["model_name"]]
        result = TargetResult(source_url=url, source_version=version)
        base = base_product(target, instance)
        hits = []
        for term in sorted(terms, key=lambda value: value["sku"]):
            plan = plans.get(term["sku"])
            for rate in term["rates"]:
                match = pattern.match(rate.get("discountedUsageType", ""))
                if plan is None or match is None or match.group("instance") != instance:
                    continue
                if base is None:
                    raise SourceInvalid(f"ec2_product_missing: {instance}@{region} (EC2 가격 파일을 먼저 수집해야 함)")
                hits.append(_savings_hit(base, plan, term, rate, url, version))
        result.hits = hits
        if not hits:
            result.reason = f"{region} Savings Plans 가격 파일에 {instance} 요율 없음"
        results[target["target_id"]] = result
    return header, results


def _handle_savings_api(conn, context, region, group):
    """DescribeSavingsPlansOfferingRates. 응답에 가격표 버전·적용일이 없어 원본 버전과 적용 시각은 미확인(NULL)이다."""
    instances = {INSTANCE_BY_MODEL[t["model_name"]] for t in group}
    rows = context["pricing_api"].savings_rates(region, instances)
    pattern = aws_savings.usage_pattern(instances)
    url = (f"https://{aws_api.SAVINGS_HOST}/DescribeSavingsPlansOfferingRates?region={region}"
           f"&instanceType={','.join(sorted(instances))}&productDescription=Linux/UNIX&tenancy=shared")
    results = {}
    for target in group:
        if target["contract_filter"] != "savings_plans":
            raise SourceInvalid(f"unsupported contract_filter for {SOURCE_SP_API}: {target['contract_filter']}")
        instance = INSTANCE_BY_MODEL[target["model_name"]]
        result = TargetResult(source_url=url, source_version=None)
        base = db.latest_product(conn, context["provider"], target["gpu_model_id"], instance, region)
        seen = set()
        for row in sorted(rows, key=lambda r: ((r.get("savingsPlanOffering") or {}).get("offeringId", ""),
                                               r.get("usageType", ""))):
            match = pattern.match(row.get("usageType", ""))
            if (match is None or match.group("instance") != instance or row.get("operation") != "RunInstances"
                    or row.get("productType") != "EC2"):
                continue
            offering = row.get("savingsPlanOffering") or {}
            family = PLAN_FAMILY_BY_API_TYPE.get(offering.get("planType"))
            seconds = offering.get("durationSeconds")
            if family is None or not seconds or seconds % SECONDS_PER_YEAR:
                raise SourceInvalid(f"unknown_savings_plan_offering: {offering.get('offeringId')} "
                                    f"{offering.get('planType')} {seconds}")
            key = (family, seconds, offering.get("paymentOption"))
            if key in seen:
                continue
            seen.add(key)
            if base is None:
                raise SourceInvalid(f"ec2_product_missing: {instance}@{region} (EC2 출처를 먼저 수집해야 함)")
            sku = offering.get("offeringId")
            plan = {"sku": sku, "productFamily": family, "attributes": {"purchaseOption": offering.get("paymentOption")}}
            term = {"sku": sku, "effectiveDate": None,
                    "leaseContractLength": {"duration": seconds // SECONDS_PER_YEAR, "unit": "year"}}
            rate = {"discountedUsageType": row["usageType"], "discountedOperation": row["operation"],
                    "discountedRate": {"price": row.get("rate"), "currency": offering.get("currency")},
                    "unit": row.get("unit"), "properties": row.get("properties")}
            result.hits.append(_savings_hit(base, plan, term, rate, url, None))
        if not result.hits:
            result.reason = f"{region} Savings Plans 요율 API에 {instance} 요율 없음"
        results[target["target_id"]] = result
    return results


def _handle_spot(conn, context, region, group):
    instances = {INSTANCE_BY_MODEL[t["model_name"]] for t in group}
    checked = context["now"]()
    rows = context["spot_prices"](region, instances, now=checked)
    stamp = checked.strftime("%Y-%m-%dT%H:%M:%SZ")
    url = aws_spot.request_url(region, {
        "Action": "DescribeSpotPriceHistory", "ProductDescription.1": aws_spot.PRODUCT_DESCRIPTION,
        "StartTime": stamp, "EndTime": stamp,
        **{f"InstanceType.{i}": name for i, name in enumerate(sorted(instances), 1)}})
    results = {}
    for target in group:
        if target["contract_filter"] != "spot":
            raise SourceInvalid(f"unsupported contract_filter for {SOURCE_SPOT}: {target['contract_filter']}")
        instance = INSTANCE_BY_MODEL[target["model_name"]]
        base = db.latest_product(conn, context["provider"], target["gpu_model_id"], instance, region) or {}
        result = TargetResult(source_url=url, source_version=aws_spot.API_VERSION)
        for row in sorted((r for r in rows if r["instance_type"] == instance),
                          key=lambda r: r["availability_zone_id"] or r["availability_zone"] or ""):
            result.hits.append(spot_hit(target, base, row, region, url))
        if not result.hits:
            result.reason = f"{region} 스팟 가격 API에 {instance} Linux 가격 없음"
        results[target["target_id"]] = result
    return results


HANDLERS = {SOURCE_EC2: _handle_ec2, SOURCE_EC2_API: _handle_ec2_api, SOURCE_SP: _handle_savings,
            SOURCE_SP_API: _handle_savings_api, SOURCE_SPOT: _handle_spot}


# 원천 → 상품·오퍼·가격 --------------------------------------------------------

def _ec2_hits(target, offer, region, instance, section, url, version):
    terms_by_sku = offer.on_demand if section == "OnDemand" else offer.reserved
    for sku, product in sorted(offer.products.items()):
        attributes = product.get("attributes", {})
        if attributes.get("instanceType") != instance or attributes.get("regionCode", region) != region:
            continue
        spec = _ec2_product(target, sku, attributes, region)
        for term_code, term in sorted((terms_by_sku.get(sku) or {}).items()):
            yield _ec2_hit(spec, attributes, section, term_code, term, url, version)


def _ec2_product(target, sku, attributes, region):
    gpu_count = _int(attributes.get("gpu"))
    vram_total, vram_unit = _quantity(attributes.get("gpuMemory"))
    ram, ram_unit = _quantity(attributes.get("memory"))
    if ram is not None and ram_unit == "GiB":
        ram = ram * GIB_TO_GB
    elif ram_unit not in (None, "GB"):
        ram = None
    network = attributes.get("networkPerformance")
    return {
        "provider_id": target["provider_id"], "gpu_model_id": target["gpu_model_id"],
        "original_product_name": attributes["instanceType"], "region_code": region,
        "system_type": "instance", "service_option": "standard",
        "sku": sku, "original_model_name": SPECS.get(attributes["instanceType"], {}).get("original_model_name"),
        "gpu_count": gpu_count,
        "vram_gb_per_gpu": vram_total / gpu_count if vram_total is not None and vram_unit == "GB" and gpu_count else None,
        "vcpu_count": _int(attributes.get("vcpu")), "ram_gb": ram,
        "storage_description": attributes.get("storage"), "spec_scope": "instance",
        # 가격 파일에는 대역폭만 있고 네트워크 종류(EFA 등)는 없다. 원문을 그대로 둔다.
        "inter_node_network": f"networkPerformance: {network}" if network else None,
        "country_code": COUNTRY_BY_REGION.get(region),
    }


def _ec2_hit(spec, attributes, section, term_code, term, url, version):
    term_attributes = term.get("termAttributes") or {}
    dimensions = sorted((term.get("priceDimensions") or {}).items())
    if section == "OnDemand":
        kind = "on_demand"
        offer = {"contract_type": "on_demand", "contract_product_type": "On-Demand"}
    else:
        kind = "reserved"
        lease, option = term_attributes.get("LeaseContractLength"), term_attributes.get("PurchaseOption")
        if lease not in LEASE_MONTHS or option not in PAYMENT:
            raise SourceInvalid(f"unknown_reserved_terms: {term_code} {lease} {option}")
        offer = {"contract_type": "commitment",
                 "contract_product_type": f"Reserved Instances ({term_attributes.get('OfferingClass')})",
                 "commitment_months": LEASE_MONTHS[lease], "payment_option": PAYMENT[option]}

    components, texts = [], []
    for _code, dimension in dimensions:
        unit = dimension.get("unit")
        value = (dimension.get("pricePerUnit") or {}).get("USD")
        amount = _decimal(value, term_code)
        text = f"{value} USD / {unit}: {dimension.get('description', '')}"
        texts.append(text)
        if unit == "Hrs":
            components.append(_component("hourly_rate", amount, "hour", text))
        elif unit == "Quantity":
            components.append(_component("upfront_fee", amount, "contract", text))
        else:
            raise SourceInvalid(f"unknown_price_unit: {term_code} {unit}")
    if sum(c["component_type"] == "hourly_rate" for c in components) != 1:
        raise SourceInvalid(f"unexpected_dimensions: {term_code}")
    if kind == "reserved" and offer["payment_option"] == "no_upfront" and not any(
            c["component_type"] == "upfront_fee" for c in components):
        # 원문이 선납 없음을 명시하므로 0으로 남긴다(미확인과 구분)
        components.append(_component("upfront_fee", Decimal(0), "contract", "PurchaseOption: No Upfront"))

    raw_terms = f"EC2 {section} {term_code}"
    if kind == "reserved":
        raw_terms += (f" {term_attributes.get('LeaseContractLength')} {term_attributes.get('OfferingClass')}"
                      f" {term_attributes.get('PurchaseOption')}")
    observation = {"effective_from": _timestamp(term.get("effectiveDate")), "price_certainty": "firm",
                   "raw_price_text": " | ".join(texts), "original_currency": "USD", "original_time_unit": "hour",
                   "price_applies_to": "instance", "raw_terms": raw_terms}
    excerpt = {"product": {"sku": spec["sku"], "attributes": attributes}, "term": term}
    return Hit(kind, spec, offer, observation, components, excerpt, url, version)


def _savings_hit(base, plan, term, rate, url, version):
    family = plan.get("productFamily")
    option = (plan.get("attributes") or {}).get("purchaseOption")
    lease = term.get("leaseContractLength") or {}
    months = int(lease["duration"]) * 12 if lease.get("unit") == "year" and lease.get("duration") else None
    if family not in PLAN_NAME or option not in PAYMENT or months is None:
        raise SourceInvalid(f"unknown_savings_plan: {term['sku']} {family} {option} {lease}")
    value = (rate.get("discountedRate") or {}).get("price")
    currency = (rate.get("discountedRate") or {}).get("currency")
    if currency != "USD":
        raise SourceInvalid(f"unexpected_currency: {term['sku']} {currency}")
    amount = _decimal(value, term["sku"])
    blended = option != "No Upfront"
    text = (f"{value} USD / {rate.get('unit')}: {PLAN_NAME[family]} {months}개월 {option} "
            f"{rate['discountedUsageType']}" + (" (선납 반영 요율)" if blended else ""))
    components = [_component("hourly_rate", amount, "hour", text)]
    if not blended:
        components.append(_component("upfront_fee", Decimal(0), "contract", "purchaseOption: No Upfront"))
    offer = {"contract_type": "commitment", "contract_product_type": PLAN_NAME[family],
             "commitment_months": months, "payment_option": PAYMENT[option]}
    observation = {"effective_from": _timestamp(term.get("effectiveDate")), "price_certainty": "firm",
                   "raw_price_text": text, "original_currency": "USD", "original_time_unit": "hour",
                   "price_applies_to": "instance",
                   "raw_terms": f"Savings Plan {term['sku']} {PLAN_NAME[family]} {months}개월 {option}"
                                + ("; 요율에 선납이 반영되어 선납금을 따로 알 수 없음" if blended else "")}
    excerpt = {"plan": plan, "term": {k: v for k, v in term.items() if k != "rates"}, "rate": rate}
    return Hit("savings_plans", base, offer, observation, components, excerpt, url, version)


def spot_hit(target, base, row, region, url):
    zone = row["availability_zone_id"] or row["availability_zone"]
    if not zone:
        raise SourceInvalid(f"spot_row_without_zone: {row['instance_type']}")
    spec = {"provider_id": target["provider_id"], "gpu_model_id": target["gpu_model_id"],
            "original_product_name": row["instance_type"], "region_code": zone,
            "system_type": "instance", "service_option": "standard"}
    # 사양은 같은 리전 EC2 상품에서 이어받는다. EC2 상품이 아직 없으면 미확인(NULL)으로 둔다.
    for name in db.CONFIG_FIELDS:
        spec[name] = base.get(name)
    spec["country_code"] = COUNTRY_BY_REGION.get(region)
    text = f"{row['price']} USD / Hrs: spot {row['instance_type']} {row['product_description']} {row['availability_zone']}"
    observation = {"effective_from": row["timestamp"], "price_certainty": "firm", "raw_price_text": text,
                   "original_currency": "USD", "original_time_unit": "hour", "price_applies_to": "instance",
                   "raw_terms": (f"EC2 Spot {row['product_description']} {region} "
                                 f"{row['availability_zone']}({row['availability_zone_id']}); 변동가, 회수될 수 있음")}
    offer = {"contract_type": "spot", "contract_product_type": "Spot"}
    components = [_component("hourly_rate", row["price"], "hour", text)]
    excerpt = {k: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, Decimal) else v)
               for k, v in row.items()}
    return Hit("spot", spec, offer, observation, components, excerpt, url, aws_spot.API_VERSION)


# 적재 -------------------------------------------------------------------------

def _write_group(conn, context, group, results):
    run_id, checked_at = context["run_id"], context["now"]()
    counts = {"targets": len(group), "offers": 0, "not_found": 0, "offers_missing": 0}
    for target in group:
        result = results[target["target_id"]]
        seen = set()
        for hit in result.hits:
            seen.add(_write_hit(conn, context, target, hit, checked_at))
        counts["offers"] += len(result.hits)
        found = bool(result.hits) or result.disclosure is not None
        disclosure = result.disclosure or ("public" if result.hits else "unknown")
        check_id, _ = db.add_check(conn, run_id, target["target_id"], "target", checked_at, disclosure,
                                   "success" if found else "not_found", True,
                                   runs.target_key(context, target), reason=result.reason)
        db.add_evidence(conn, check_id, target["source_id"], "official", result.source_url, result.source_version,
                        {"scope": {"model": target["model_name"], "region": target["region_filter"],
                                   "contract": target["contract_filter"], "scope_version": target["scope_version"]},
                         "offers_found": len(result.hits)}, checked_at)
        if not found:
            counts["not_found"] += 1
        # 전에 가격을 확인한 오퍼가 이번 범위 확인에서 빠졌으면 오퍼 미발견으로 남긴다(DR-08 최근 미발견 제외)
        for offer_id, product_id in sorted(db.previously_seen_offers(conn, target["target_id"]).items()):
            if offer_id in seen:
                continue
            db.add_check(conn, run_id, target["target_id"], "offer", checked_at, "unknown", "not_found", True,
                         runs.offer_key(context, target, offer_id), product_id=product_id,
                         offer_id=offer_id, reason="이전에 확인한 오퍼가 이번 수집 범위에서 발견되지 않음")
            counts["offers_missing"] += 1
    return counts


def _write_hit(conn, context, target, hit, checked_at):
    run_id, rule = context["run_id"], context["rule"]
    if "product_id" in hit.product:
        product_id, previous = hit.product["product_id"], None
    else:
        product_id, previous = db.ensure_product(conn, hit.product)
    offer_id = db.ensure_offer(conn, product_id, hit.offer)
    check_id, created = db.add_check(conn, run_id, target["target_id"], "offer", checked_at, "public", "success",
                                     True, runs.offer_key(context, target, offer_id),
                                     product_id=product_id, offer_id=offer_id)
    if not created:
        raise SourceInvalid(f"duplicate_offer_in_run: offer {offer_id}")
    if previous is not None:
        db.add_alert(conn, "config_changed", checked_at, f"config_changed:{product_id}", run_id=run_id,
                     target_id=target["target_id"], check_id=check_id, source_id=target["source_id"],
                     rule_version="DR-05", reason=f"상품 {previous}의 구성이 바뀌어 새 구성 버전 {product_id}로 적재")
    ensure_spec_evidence(conn, context, product_id, checked_at)
    db.add_evidence(conn, check_id, target["source_id"], "official", hit.source_url, hit.source_version,
                    hit.excerpt, checked_at)
    observation_id = db.add_observation(conn, offer_id, check_id, {**hit.observation, "collected_at": checked_at})
    component_ids = [db.add_component(conn, observation_id, component) for component in hit.components]
    gpu_count = hit.product.get("gpu_count")
    result = normalize(hit.kind, hit.offer, hit.components, component_ids, gpu_count, rule["monthly_hours"])
    db.add_normalization(conn, observation_id, rule["rule_id"], result, checked_at)
    _check_spike(conn, context, target, offer_id, check_id, observation_id, result, checked_at)
    return offer_id


# 사양 근거 ----------------------------------------------------------------------

def spec_context(conn):
    """사양 근거 대상({gpu_model_id: 대상})과 출처 ID. seed에 없으면 빈 값(근거 연결 안 함)."""
    targets = {t["gpu_model_id"]: t for t in db.enabled_targets(conn, PROVIDER) if t["source_name"] == SOURCE_DOCS}
    row = conn.execute("SELECT source_id FROM data_source WHERE source_name = %s", (SOURCE_DOCS,)).fetchone()
    return {"docs": targets, "docs_source": row[0] if row else None}


def ensure_spec_evidence(conn, context, product_id, captured_at):
    """상품(구성 버전)에 공식 문서 근거를 한 번 연결하고 노드 안 상호연결을 확인으로 바꾼다(GPU 2장 이상)."""
    if not context.get("docs") or db.has_product_evidence(conn, product_id):
        return False
    row = conn.execute("SELECT gpu_model_id, original_product_name, gpu_count FROM product WHERE product_id = %s",
                       (product_id,)).fetchone()
    docs, spec = context["docs"].get(row[0]), SPECS.get(row[1])
    if docs is None or spec is None:
        return False
    check_id, _ = db.add_check(conn, context["run_id"], docs["target_id"], "product", captured_at, "public", "success",
                               True, f"spec:{product_id}:{DOC_VERSION}", product_id=product_id)
    evidence_id = db.add_evidence(conn, check_id, context["docs_source"], "spec", spec["doc_urls"][0], DOC_VERSION,
                                  {"instance_type": row[1], "doc_urls": spec["doc_urls"], "quotes": spec["quotes"],
                                   "verified_by": "manual", "verified_on": "2026-11-18"}, captured_at)
    db.add_product_evidence(conn, product_id, evidence_id, SPEC_ATTRIBUTES,
                            spec["intra_node_interconnect"] if row[2] and row[2] > 1 else None)
    return True


def attach_specs(conn, *, now=None, trigger_type="manual"):
    """이미 적재한 AWS 상품에 모델 원문과 사양 근거를 채운다(한 번 실행). 반환: 실행 요약.

    모델 원문은 미확인(NULL)을 채우는 것이라 구성 버전을 올리지 않고 그 행에 기록한다. 이후 수집은 같은 값을 만든다."""
    now = now or (lambda: datetime.now(timezone.utc))
    provider = db.provider_id(conn, PROVIDER)
    run_id = db.begin_run(conn, now(), trigger_type, "gpu-collector 0.1.0 / erd-aws-specs 1")
    context = {"run_id": run_id, **spec_context(conn)}
    if not context["docs"]:
        raise LookupError("no enabled AWS spec collection_target (sql/erd_seed.sql)")
    filled = 0
    for instance, spec in SPECS.items():
        filled += conn.execute(
            """UPDATE product SET original_model_name = %s
               WHERE provider_id = %s AND original_product_name = %s AND original_model_name IS NULL""",
            (spec["original_model_name"], provider, instance)).rowcount
    linked = 0
    checked_at = now()
    for (product_id,) in conn.execute("SELECT product_id FROM product WHERE provider_id = %s ORDER BY product_id",
                                      (provider,)).fetchall():
        linked += ensure_spec_evidence(conn, context, product_id, checked_at)
    db.finish_run(conn, run_id, now(), "success")
    conn.commit()
    return {"run_id": run_id, "status": "success", "model_name_filled": filled, "products_linked": linked}


def _check_spike(conn, context, target, offer_id, check_id, observation_id, result, checked_at):
    current = result["effective"]
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


def _record_failure(conn, context, group, processing, reason):
    """묶음의 작업을 되돌리고 대상별 실패 점검과 알림을 남긴다. 실패를 공개 상태로 바꾸지 않는다."""
    conn.rollback()
    run_id, checked_at = context["run_id"], context["now"]()
    for target in group:
        db.add_check(conn, run_id, target["target_id"], "target", checked_at, "unknown", processing, False,
                     f"{run_id}:{target['target_id']}:target", reason=reason[:1000])
    first = group[0]
    db.add_alert(conn, "collection_failed", checked_at,
                 f"collection_failed:{run_id}:{first['source_id']}:{first['region_filter']}", run_id=run_id,
                 source_id=first["source_id"], reason=f"{first['source_name']}@{first['region_filter']}: {reason}"[:2000])
    conn.commit()


# 환산 (DR-02·DR-03) ------------------------------------------------------------

def normalize(kind, offer, components, component_ids, gpu_count, monthly_hours):
    """반환: {nominal, effective, status, reason, inputs}. 금액은 반올림하지 않는다."""
    hourly = next((c["amount"] for c in components if c["component_type"] == "hourly_rate"), None)
    upfront_items = [(cid, c) for cid, c in zip(component_ids, components) if c["component_type"] == "upfront_fee"]
    upfront = upfront_items[0][1]["amount"] if upfront_items else None
    months = offer.get("commitment_months")
    inputs = {"component_ids": component_ids, "gpu_count": gpu_count, "monthly_hours": str(monthly_hours),
              "commitment_months": months, "kind": kind}
    if not gpu_count:
        return _not_computable(inputs, "GPU 수 미확인")
    if hourly is None:
        return _not_computable(inputs, "시간당 요금 없음")

    if kind in ("on_demand", "spot"):
        value = hourly / gpu_count
        return _ok(value, value, inputs, "시간당 요금 ÷ GPU 수")
    if kind == "savings_plans" and offer["payment_option"] != "no_upfront":
        return _ok(None, hourly / gpu_count, inputs, "선납 반영 시간당 요율 ÷ GPU 수 (DR-03)",
                   reason="선납이 반영된 요율만 공개되어 명목 단가 없음")
    if kind in ("reserved", "savings_plans"):
        if upfront is None:
            return _not_computable(inputs, "선납금 미확인")
        if not months:
            return _not_computable(inputs, "약정 기간 미확인")
        hours = months * monthly_hours
        effective = (upfront / hours + hourly) / gpu_count
        nominal = hourly / gpu_count if hourly > 0 else None
        reason = None if nominal is not None else "전액 선납이라 시간당 요금이 없어 명목 단가 없음"
        return _ok(nominal, effective, {**inputs, "contract_hours": str(hours)},
                   "(선납금 ÷ 약정 시간 + 시간당 요금) ÷ GPU 수 (DR-03, 약정 100% 사용 가정)", reason=reason)
    raise ValueError(f"unknown kind: {kind}")


def _ok(nominal, effective, inputs, formula, reason=None):
    return {"nominal": nominal, "effective": effective, "status": "success", "reason": reason,
            "inputs": {**inputs, "formula": formula}}


def _not_computable(inputs, reason):
    return {"nominal": None, "effective": None, "status": "not_computable", "reason": reason, "inputs": inputs}


# 받기 -------------------------------------------------------------------------

def _versioned_url(context, index_url, region):
    index = context["get_json"](index_url, REQUEST_CONFIG)
    regions = index.get("regions")
    if isinstance(regions, dict):        # EC2: {"us-east-1": {"currentVersionUrl": ...}}
        path = (regions.get(region) or {}).get("currentVersionUrl")
    else:                                # Savings Plans: [{"regionCode": ..., "versionUrl": ...}]
        path = next((r.get("versionUrl") for r in regions or [] if r.get("regionCode") == region), None)
    if not path:
        raise fetch.FetchError(f"region_not_in_index: {region}")
    parts = [part for part in path.split("/") if part]
    version = next((part for part in parts if part.isdigit() and len(part) >= 14), None)
    return PRICING_HOST + path, version


def _download(context, url):
    path = url.removeprefix(PRICING_HOST).lstrip("/")
    return cached_download(url, context["cache_dir"] / "aws" / path, REQUEST_CONFIG, context["downloader"])


def _detect(path, needles):
    """가격을 수집하지 않는 모델(GB300)의 인스턴스 등재 여부. 파일을 한 번 더 훑는다."""
    found = {needle: set() for needle in needles}
    with open(path, "rb") as source:
        for _sku, product in ijson.kvitems(source, "products"):
            name = str((product.get("attributes") or {}).get("instanceType", "")).lower()
            for needle in needles:
                if needle in name:
                    found[needle].add(name)
    return {needle: sorted(names) for needle, names in found.items()}


# 공통 -------------------------------------------------------------------------

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


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quantity(value):
    """'2144 GB HBM3e' → (Decimal('2144'), 'GB'). 읽지 못하면 (None, None)."""
    if not value:
        return None, None
    parts = str(value).replace(",", "").split()
    try:
        return Decimal(parts[0]), (parts[1] if len(parts) > 1 else None)
    except (InvalidOperation, IndexError):
        return None, None


def _timestamp(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
