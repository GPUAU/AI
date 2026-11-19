"""AWS Price List Bulk API의 EC2 지역 파일에서 대상 인스턴스의 가격을 읽는다.

필터는 공통 처리 규칙 부록을 따른다. CapacityBlock과 예약 용량 SKU는 대상이 아니다.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

import ijson

from . import normalize
from .model import FeeItem, Listing, ProductRecord

REGION = "us-east-1"
OFFER_URL = f"https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/{REGION}/index.json"
TARGET_INSTANCES = {"p6-b300.48xlarge": "B300", "p5.48xlarge": "H100"}
PRODUCT_FILTER = {
    "marketoption": "OnDemand",
    "capacitystatus": "Used",
    "tenancy": "Shared",
    "operatingSystem": "Linux",
    "preInstalledSw": "NA",
    "operation": "RunInstances",
}
COUNTRY_BY_REGION = {"us-east-1": "US", "ap-northeast-2": "KR"}
LEASE_MONTHS = {"1yr": 12, "3yr": 36}
KEPT_ATTRS = ("marketoption", "capacitystatus", "tenancy", "operatingSystem", "preInstalledSw", "operation",
              "usagetype", "networkPerformance", "physicalProcessor", "memory")


@dataclass
class Offer:
    header: dict
    products: dict
    on_demand: dict
    reserved: dict
    tracked_instances: list = field(default_factory=list)  # 가격은 수집하지 않고 등재만 확인하는 인스턴스


@dataclass
class ParseResult:
    listings: list = field(default_factory=list)
    issues: list = field(default_factory=list)


def read_offer(path, targets=None):
    """파일을 끝까지 읽는다. 잘린 JSON이면 ijson이 예외를 낸다.

    targets: 가격을 읽을 인스턴스 유형 모음. 생략하면 TARGET_INSTANCES.
    """
    targets = TARGET_INSTANCES if targets is None else targets
    header = {}
    with open(path, "rb") as source:
        for prefix, _event, value in ijson.parse(source):
            if prefix in ("formatVersion", "offerCode", "version", "publicationDate"):
                header[prefix] = value
            if prefix == "products":
                break
    products = {}
    tracked = set()
    with open(path, "rb") as source:
        for sku, product in ijson.kvitems(source, "products"):
            attributes = product.get("attributes", {})
            instance_type = str(attributes.get("instanceType"))
            if instance_type in targets and _matches_filter(attributes):
                products[sku] = product
            if "gb300" in instance_type.lower() and attributes.get("operation") == "RunInstances":
                tracked.add(instance_type)
    on_demand = _read_terms(path, "OnDemand", products)
    reserved = _read_terms(path, "Reserved", products)
    return Offer(header, products, on_demand, reserved, sorted(tracked))


def _read_terms(path, section, products):
    found = {}
    with open(path, "rb") as source:
        for sku, terms in ijson.kvitems(source, f"terms.{section}"):
            if sku in products:
                found[sku] = terms
    return found


def _matches_filter(attributes):
    return all(attributes.get(key) == value for key, value in PRODUCT_FILTER.items())


def build_listings(offer):
    result = ParseResult()
    observed_at = _timestamp(offer.header.get("publicationDate"))
    for section, terms_by_sku in (("OnDemand", offer.on_demand), ("Reserved", offer.reserved)):
        for sku, terms in sorted(terms_by_sku.items()):
            attributes = offer.products[sku]["attributes"]
            for term_code, term in sorted(terms.items()):
                try:
                    listing = _build_listing(section, sku, attributes, term_code, term, observed_at, result.issues)
                except (KeyError, ValueError, InvalidOperation, TypeError) as error:
                    result.issues.append({"listing_key": f"{sku}.{term_code}", "reason": f"held: {error!r}"})
                    continue
                if listing is not None:
                    result.listings.append(listing)
    return result


def _build_listing(section, sku, attributes, term_code, term, observed_at, issues):
    listing_key = f"{sku}.{term_code}"
    effective_at = _timestamp(term.get("effectiveDate"))
    gpu_count = _int_or_none(attributes.get("gpu"))
    term_attributes = term.get("termAttributes") or {}
    lease = term_attributes.get("LeaseContractLength")
    months = LEASE_MONTHS.get(lease)
    if section == "Reserved" and months is None:
        issues.append({"listing_key": listing_key, "reason": f"unknown_lease: {lease}"})
        return None

    product = _product_record(section, sku, attributes, term_code, term_attributes, gpu_count, months)
    dimensions = sorted(term["priceDimensions"].items())
    items = [_fee_item(index, section, listing_key, rate_code, dimension, observed_at, effective_at)
             for index, (rate_code, dimension) in enumerate(dimensions)]

    if section == "OnDemand":
        return Listing(listing_key, product, items, _normalize_on_demand(items, gpu_count, listing_key, issues))

    items, normalized = _normalize_reserved(items, gpu_count, months, term_attributes, observed_at, effective_at,
                                            listing_key, issues)
    return Listing(listing_key, product, items, normalized)


def _product_record(section, sku, attributes, term_code, term_attributes, gpu_count, months):
    instance_type = attributes["instanceType"]
    region = attributes.get("regionCode", REGION)
    vcpu = _int_or_none(attributes.get("vcpu"))
    memory_value, memory_unit = _split_quantity(attributes.get("memory"))
    lease = term_attributes.get("LeaseContractLength")
    offering_class = term_attributes.get("OfferingClass")
    purchase_option = term_attributes.get("PurchaseOption")
    # 4절: 계열을 가르는 조건의 실제 값을 키에 넣는다.
    key_parts = [region, instance_type, sku, section, term_code, f"gpu{gpu_count}", f"vcpu{vcpu}",
                 f"mem{attributes.get('memory')}", "compute"]
    if section == "Reserved":
        key_parts.extend([lease, offering_class, purchase_option])
    attrs = {name: attributes[name] for name in KEPT_ATTRS if name in attributes}
    attrs.update({"offer_term_code": term_code, "ram_unit_raw": memory_unit})
    if section == "Reserved":
        attrs.update({"lease_contract_length": lease, "offering_class": offering_class,
                      "purchase_option": purchase_option})
    return ProductRecord(
        product_key="|".join(str(part) for part in key_parts),
        gpu_model_name=TARGET_INSTANCES[instance_type],
        gpu_model_raw=instance_type,
        provider_sku=sku,
        product_name_raw=instance_type,
        product_section=f"EC2 {section}",
        gpu_count=gpu_count,
        service_type="vm",
        purchase_type="on_demand" if section == "OnDemand" else "commitment",
        term_min_months=months,
        term_max_months=months,
        region_raw=attributes.get("location"),
        region_codes=[region],
        countries=[COUNTRY_BY_REGION[region]] if region in COUNTRY_BY_REGION else None,
        cost_scope="compute_instance",
        vcpu_count=vcpu,
        system_ram_gb=memory_value,
        gpu_memory_raw=attributes.get("gpuMemory"),
        storage_raw=attributes.get("storage"),
        # 네트워크 대역폭 원문만 남긴다. 상호연결 종류와 보장 여부는 파일에 없어 추정하지 않는다.
        interconnect_raw=(f"networkPerformance: {attributes['networkPerformance']}"
                          if attributes.get("networkPerformance") else None),
        attrs=attrs,
        missing_reasons={"form_factor": "unknown", "min_order_quantity": "unknown", "min_order_unit": "unknown",
                         "interconnect_types": "unknown", "interconnect_guaranteed": "unknown",
                         "support_raw": "unknown", "comparison_region": "not_decided"},
    )


def _fee_item(index, section, listing_key, rate_code, dimension, observed_at, effective_at):
    unit = dimension.get("unit")
    value = (dimension.get("pricePerUnit") or {}).get("USD")
    amount, missing = _amount(value)
    description = dimension.get("description", "")
    if unit == "Hrs":
        component = "usage_rate" if section == "OnDemand" else "recurring_fee"
        price_period = "hour"
    elif unit == "Quantity":
        component = "upfront_fee"
        price_period = "contract"
    else:
        component = "other"
        price_period = None
    return FeeItem(
        item_index=index,
        raw_price_text=f"{value} USD / {unit}: {description}",
        raw_amount=amount,
        component=component,
        raw_currency="USD" if value is not None else None,
        raw_unit=unit,
        raw_locator=f"terms.{section}.{listing_key.split('.')[0]}.{listing_key}.priceDimensions.{rate_code}",
        fee_inclusion=None if section == "OnDemand" else "separate",
        price_basis="instance",
        price_period=price_period,
        observed_at=observed_at,
        effective_at=effective_at,
        missing_reasons=missing,
    )


def _normalize_on_demand(items, gpu_count, listing_key, issues):
    hourly = [item for item in items if item.component == "usage_rate"]
    if len(items) != 1 or len(hourly) != 1:
        issues.append({"listing_key": listing_key, "reason": "unexpected_on_demand_dimensions"})
        return [normalized for item in items
                for normalized in normalize.usage_rate(item.item_index, None, gpu_count)]
    return normalize.usage_rate(hourly[0].item_index, hourly[0].raw_amount, gpu_count)


def _normalize_reserved(items, gpu_count, months, term_attributes, observed_at, effective_at, listing_key, issues):
    hourly = [item for item in items if item.component == "recurring_fee"]
    upfront = [item for item in items if item.component == "upfront_fee"]
    other = [item for item in items if item.component == "other"]
    if len(hourly) != 1 or len(upfront) > 1 or other:
        issues.append({"listing_key": listing_key, "reason": "unexpected_reserved_dimensions"})
        anchor = items[0].item_index
        return items, normalize.commitment(anchor, None, None, None, gpu_count, months)

    if upfront:
        return items, normalize.commitment(hourly[0].item_index, upfront[0].item_index, hourly[0].raw_amount,
                                           upfront[0].raw_amount, gpu_count, months)

    purchase_option = term_attributes.get("PurchaseOption")
    if purchase_option != "No Upfront":
        # 선납금이 있어야 할 옵션인데 항목이 없으면 총비용을 알 수 없다.
        issues.append({"listing_key": listing_key, "reason": f"missing_upfront_for: {purchase_option}"})
        return items, normalize.commitment(hourly[0].item_index, None, hourly[0].raw_amount, None, gpu_count, months)

    # 6절: 원문이 선납 없음을 명시하면 0으로 기록해 미확인과 구분한다.
    zero_upfront = FeeItem(
        item_index=len(items),
        raw_price_text="PurchaseOption: No Upfront",
        raw_amount=Decimal(0),
        component="upfront_fee",
        raw_currency="USD",
        raw_locator=f"termAttributes.PurchaseOption ({listing_key})",
        fee_inclusion="separate",
        price_basis="instance",
        price_period="contract",
        observed_at=observed_at,
        effective_at=effective_at,
    )
    normalized = normalize.commitment(hourly[0].item_index, zero_upfront.item_index, hourly[0].raw_amount,
                                      Decimal(0), gpu_count, months)
    return items + [zero_upfront], normalized


def _amount(value):
    if value is None:
        return None, {"raw_amount": "parse_failed"}
    try:
        return Decimal(value), {}
    except (InvalidOperation, TypeError):
        return None, {"raw_amount": "parse_failed"}


def _timestamp(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _split_quantity(value):
    """'4096 GiB' -> (Decimal('4096'), 'GiB'). 단위는 환산하지 않고 attrs에 남긴다."""
    if not value:
        return None, None
    parts = value.replace(",", "").split()
    try:
        return Decimal(parts[0]), parts[1] if len(parts) > 1 else None
    except (InvalidOperation, IndexError):
        return None, None
