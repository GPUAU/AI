"""AWS 가격 파일·Savings Plans 파일의 합성 문서. 네트워크 없이 AWS 수집기를 시험할 때 쓴다."""

PUBLISHED = "2026-11-07T00:28:52Z"


def attributes(instance, **overrides):
    base = {
        "instanceType": instance, "location": "US East (N. Virginia)", "regionCode": "us-east-1", "gpu": "8",
        "vcpu": "192", "memory": "2048 GiB", "storage": "8 x 3840 NVMe SSD", "gpuMemory": "640 GB",
        "networkPerformance": "3200 Gigabit", "marketoption": "OnDemand", "capacitystatus": "Used",
        "tenancy": "Shared", "operatingSystem": "Linux", "preInstalledSw": "NA", "operation": "RunInstances",
    }
    base.update(overrides)
    return {"sku": "X", "productFamily": "Compute Instance", "attributes": base}


def dimension(code, unit, usd, description="d"):
    return code, {"rateCode": code, "unit": unit, "description": description, "pricePerUnit": {"USD": usd}}


def term(sku, code, dimensions, **term_attributes):
    return {f"{sku}.{code}": {"sku": sku, "offerTermCode": code, "effectiveDate": "2026-11-01T00:00:00Z",
                              "termAttributes": term_attributes, "priceDimensions": dict(dimensions)}}


def on_demand(sku, usd):
    return {sku: term(sku, "JRTCKXETXF", [dimension(f"{sku}.OD.1", "Hrs", usd)])}


def reserved(sku):
    terms = {}
    terms.update(term(sku, "PARTIAL3Y", [dimension(f"{sku}.P.1", "Quantity", "289290.2400000000", "Upfront Fee"),
                                         dimension(f"{sku}.P.2", "Hrs", "11.0080000000")],
                      LeaseContractLength="3yr", OfferingClass="standard", PurchaseOption="Partial Upfront"))
    terms.update(term(sku, "NOUP3Y", [dimension(f"{sku}.N.1", "Hrs", "23.7776000000")],
                      LeaseContractLength="3yr", OfferingClass="standard", PurchaseOption="No Upfront"))
    terms.update(term(sku, "ALL1Y", [dimension(f"{sku}.A.1", "Quantity", "350400.0000000000", "Upfront Fee"),
                                     dimension(f"{sku}.A.2", "Hrs", "0.0000000000")],
                      LeaseContractLength="1yr", OfferingClass="convertible", PurchaseOption="All Upfront"))
    return {sku: terms}


def offer_document(b300_price="142.4160000000", include_b300=True):
    products = {
        "H100SKU": attributes("p5.48xlarge"),
        "DEDICATED": attributes("p5.48xlarge", tenancy="Dedicated"),
        "CAPBLOCK": attributes("p6-b300.48xlarge", marketoption="CapacityBlock", operation="RunInstances:000g:CB"),
        "SUSE": attributes("p5.48xlarge", operatingSystem="SUSE"),
        "OTHER": attributes("m5.large"),
    }
    demand = {**on_demand("H100SKU", "55.0400000000"), **on_demand("DEDICATED", "99"), **on_demand("CAPBLOCK", "0.125")}
    if include_b300:
        products["B300SKU"] = attributes("p6-b300.48xlarge", memory="4096 GiB", gpuMemory="2144 GB HBM3e")
        demand.update(on_demand("B300SKU", b300_price))
    return {"formatVersion": "v1.0", "offerCode": "AmazonEC2", "version": "20261107002852",
            "publicationDate": PUBLISHED, "products": products,
            "terms": {"OnDemand": demand, "Reserved": reserved("H100SKU")}}



def rate(usage, price, operation="RunInstances"):
    return {"discountedSku": "EC2SKU", "discountedUsageType": usage, "discountedOperation": operation,
            "discountedServiceCode": "AmazonEC2", "rateCode": f"{usage}.{operation}", "unit": "Hrs",
            "discountedRate": {"price": price, "currency": "USD"}}


def savings_document(prefix=""):
    box = f"{prefix}BoxUsage:p6-b300.48xlarge"
    plan = lambda sku, family, option: {"sku": sku, "productFamily": family,
                                        "attributes": {"purchaseOption": option, "purchaseTerm": "3yr"}}
    term = lambda sku, rates: {"sku": sku, "effectiveDate": "2026-11-01T00:00:00Z",
                               "leaseContractLength": {"duration": 3, "unit": "year"}, "rates": rates}
    return {
        "version": "20261107012235", "publicationDate": "2026-11-07T01:22:35Z", "offerCode": "AWSComputeSavingsPlan",
        "products": [plan("NOUP", "EC2InstanceSavingsPlans", "No Upfront"),
                     plan("PART", "EC2InstanceSavingsPlans", "Partial Upfront"),
                     plan("OTHER", "ComputeSavingsPlans", "All Upfront")],
        "terms": {"savingsPlan": [
            term("NOUP", [rate(box, "61.5237"), rate(box, "70", "RunInstances:000g"),
                          rate(f"{prefix}DedicatedUsage:p6-b300.48xlarge", "70"),
                          rate(f"{prefix}UnusedBox:p6-b300.48xlarge", "70")]),
            term("PART", [rate(box, "56.9664")]),
            term("OTHER", [rate(f"{prefix}BoxUsage:m5.large", "1")]),
        ]},
    }


