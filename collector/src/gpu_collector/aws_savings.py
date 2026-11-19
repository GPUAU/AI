"""AWS Savings Plans 가격 파일에서 대상 인스턴스의 요율을 읽는다.

요율은 선납을 시간당으로 나눠 반영한 값이다. No Upfront만 실제 시간당 요금이므로 명목을 계산한다.
파일에 GPU 수가 없어 같은 리전의 EC2 상품에서 확인한 사양을 받아 쓴다.
"""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

import ijson

from . import aws_offer, normalize
from .aws_offer import ParseResult
from .model import FeeItem, Listing, ProductRecord

def usage_pattern(instances):
    """리전 접두어가 붙을 수 있는 Linux 공유 테넌시 사용 유형. 예: APN2-BoxUsage:p6-b300.48xlarge"""
    names = "|".join(re.escape(name) for name in sorted(instances))
    return re.compile(rf"^(?:[A-Z0-9]+-)?BoxUsage:(?P<instance>{names})$")


USAGE = usage_pattern(["p6-b300.48xlarge", "p5.48xlarge"])


def read_savings(path, instances=None):
    """대상 인스턴스 요율이 있는 플랜만 남긴다. 잘린 JSON이면 ijson이 예외를 낸다.

    instances: 요율을 읽을 인스턴스 유형 모음. 생략하면 USAGE의 기본 대상.
    """
    pattern = USAGE if instances is None else usage_pattern(instances)
    header = {}
    with open(path, "rb") as source:
        for prefix, _event, value in ijson.parse(source):
            if prefix in ("offerCode", "version", "publicationDate"):
                header[prefix] = value
            if prefix == "products":
                break
    plans = {}
    with open(path, "rb") as source:
        for plan in ijson.items(source, "products.item"):
            plans[plan["sku"]] = plan
    terms = []
    with open(path, "rb") as source:
        for term in ijson.items(source, "terms.savingsPlan.item"):
            rates = [rate for rate in term.get("rates", []) if _is_target(rate, pattern)]
            if rates:
                terms.append({**term, "rates": rates})
    used = {term["sku"]: plans[term["sku"]] for term in terms if term["sku"] in plans}
    return header, used, terms


def _is_target(rate, pattern=USAGE):
    return (rate.get("discountedOperation") == "RunInstances"
            and rate.get("discountedServiceCode", "AmazonEC2") == "AmazonEC2"
            and pattern.match(rate.get("discountedUsageType", "")) is not None)


def build_listings(header, plans, terms, region, specs):
    result = ParseResult()
    observed_at = _timestamp(header.get("publicationDate"))
    for term in sorted(terms, key=lambda value: value["sku"]):
        plan = plans.get(term["sku"])
        if plan is None:
            result.issues.append({"listing_key": term["sku"], "reason": "plan_not_in_products"})
            continue
        for rate in sorted(term["rates"], key=lambda value: value["discountedUsageType"]):
            listing = _build_listing(plan, term, rate, region, specs, observed_at, result.issues)
            if listing is not None:
                result.listings.append(listing)
    return result


def _build_listing(plan, term, rate, region, specs, observed_at, issues):
    usage = rate["discountedUsageType"]
    instance = USAGE.match(usage).group("instance")
    listing_key = f"{term['sku']}.{usage}.{rate['discountedOperation']}"
    lease = term.get("leaseContractLength") or {}
    months = int(lease["duration"]) * 12 if lease.get("unit") == "year" and lease.get("duration") else None
    if months is None:
        issues.append({"listing_key": listing_key, "reason": f"unknown_lease: {lease}"})
        return None

    attributes = plan.get("attributes") or {}
    option = attributes.get("purchaseOption")
    family = plan.get("productFamily")
    spec = specs.get(instance)
    if spec is None:
        issues.append({"listing_key": listing_key, "reason": "ec2_spec_not_found"})
        spec = {}

    gpu_count = spec.get("gpu_count")
    value = (rate.get("discountedRate") or {}).get("price")
    amount = _amount(value)
    effective_at = _timestamp(term.get("effectiveDate"))
    item = FeeItem(
        item_index=0,
        raw_price_text=f"{value} USD / {rate.get('unit')}: {family} {months}mo {option} {usage}",
        raw_amount=amount,
        component="recurring_fee",
        raw_currency=(rate.get("discountedRate") or {}).get("currency"),
        raw_unit=rate.get("unit"),
        raw_locator=f"terms.savingsPlan[{term['sku']}].rates[{usage}]",
        # Partial·All Upfront 요율에는 선납이 이미 포함되어 있다.
        fee_inclusion="separate" if option == "No Upfront" else "included",
        price_basis="instance",
        price_period="hour",
        observed_at=observed_at,
        effective_at=effective_at,
        missing_reasons={} if amount is not None else {"raw_amount": "parse_failed"},
    )
    items = [item]
    if option == "No Upfront":
        items.append(FeeItem(
            item_index=1,
            raw_price_text="purchaseOption: No Upfront",
            raw_amount=Decimal(0),
            component="upfront_fee",
            raw_currency="USD",
            raw_locator=f"products[{term['sku']}].attributes.purchaseOption",
            fee_inclusion="separate",
            price_basis="instance",
            price_period="contract",
            observed_at=observed_at,
            effective_at=effective_at,
        ))
        normalized = normalize.commitment(0, 1, amount, Decimal(0), gpu_count, months)
    else:
        normalized = normalize.blended_commitment(0, amount, gpu_count, months)

    memory = (spec.get("attrs") or {}).get("memory")
    key_parts = [region, instance, term["sku"], family, f"{months}mo", option, f"gpu{gpu_count}",
                 f"vcpu{spec.get('vcpu_count')}", f"mem{memory}", "compute"]
    product = ProductRecord(
        product_key="|".join(str(part) for part in key_parts),
        gpu_model_name=aws_offer.TARGET_INSTANCES[instance],
        gpu_model_raw=instance,
        provider_sku=term["sku"],
        product_name_raw=instance,
        product_section=f"Savings Plan {family}",
        gpu_count=gpu_count,
        service_type="vm",
        purchase_type="commitment",
        term_min_months=months,
        term_max_months=months,
        region_raw=spec.get("region_raw"),
        region_codes=[region],
        countries=[aws_offer.COUNTRY_BY_REGION[region]] if region in aws_offer.COUNTRY_BY_REGION else None,
        cost_scope="compute_instance",
        vcpu_count=spec.get("vcpu_count"),
        system_ram_gb=spec.get("system_ram_gb"),
        gpu_memory_raw=spec.get("gpu_memory_raw"),
        storage_raw=spec.get("storage_raw"),
        interconnect_raw=spec.get("interconnect_raw"),
        attrs={"savings_plan": True, "plan_family": family, "purchase_option": option,
               "instance_family": attributes.get("instanceType"), "purchase_term": attributes.get("purchaseTerm"),
               "usage_type": usage, "operation": rate["discountedOperation"],
               "discounted_sku": rate.get("discountedSku"), "rate_code": rate.get("rateCode"), "memory": memory},
        missing_reasons={"form_factor": "unknown", "min_order_quantity": "unknown", "min_order_unit": "unknown",
                         "interconnect_types": "unknown", "interconnect_guaranteed": "unknown",
                         "support_raw": "unknown", "comparison_region": "not_decided"},
    )
    return Listing(listing_key, product, items, normalized)


def _amount(value):
    try:
        return Decimal(str(value)) if value is not None else None
    except InvalidOperation:
        return None


def _timestamp(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
