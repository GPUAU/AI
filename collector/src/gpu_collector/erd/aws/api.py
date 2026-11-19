"""AWS 가격 조회 API (Signature Version 4, POST). 매일 수집에서 큰 가격 파일 대신 대상 상품만 받는다.

| 함수 | API | 필요한 IAM 권한 |
| get_products | Price List Query API GetProducts | pricing:GetProducts |
| savings_rates | Savings Plans DescribeSavingsPlansOfferingRates | savingsplans:DescribeSavingsPlansOfferingRates |

두 API 모두 조회 시점에 공개 중인 현재 가격만 준다. 날짜를 지정하는 인자가 없다(과거는 가격 파일 버전으로 받는다).
키는 AWS_ACCESS_KEY_ID·AWS_SECRET_ACCESS_KEY 환경 변수에서 읽고, 로그·예외에 남기지 않는다.
"""
import hashlib
import hmac
import json
from datetime import datetime, timezone

import requests

from ...fetch import USER_AGENT
from ...env import secret

PRICING_HOST = "api.pricing.us-east-1.amazonaws.com"
SAVINGS_HOST = "savingsplans.amazonaws.com"
MAX_PAGES = 50
# 사용 중 용량의 일반 Linux 공유 테넌시 온디맨드 상품(Capacity Blocks·예약 용량 제외). EC2 가격 파일 필터와 같다.
EC2_FILTERS = {"operatingSystem": "Linux", "tenancy": "Shared", "preInstalledSw": "NA", "capacitystatus": "Used",
               "marketoption": "OnDemand", "operation": "RunInstances"}


class AwsApiError(Exception):
    """이번 회차를 실패로 기록해야 하는 API 오류. 비밀값을 담지 않는다."""


def ec2_filters(region, instance_type=None, contains=None):
    filters = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    filters += [{"Type": "TERM_MATCH", "Field": field, "Value": value} for field, value in EC2_FILTERS.items()]
    if instance_type:
        filters.append({"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type})
    if contains:
        filters.append({"Type": "CONTAINS", "Field": "instanceType", "Value": contains})
    return filters


def get_products(filters, now=None, http_post=requests.post):
    """반환: 상품 문서 목록 [{product:{sku, attributes}, terms:{OnDemand, Reserved}, version, publicationDate}]."""
    body = {"ServiceCode": "AmazonEC2", "FormatVersion": "aws_v1", "MaxResults": 100, "Filters": filters}
    documents = []
    for _page in range(MAX_PAGES):
        data = _post(PRICING_HOST, "us-east-1", "pricing", "/", body, now, http_post,
                     content_type="application/x-amz-json-1.1", target="AWSPriceListService.GetProducts")
        try:
            documents.extend(json.loads(item) for item in data.get("PriceList") or [])
        except ValueError as error:
            raise AwsApiError("invalid_price_list_item") from error
        if not data.get("NextToken"):
            return documents
        body["NextToken"] = data["NextToken"]
    raise AwsApiError(f"page_limit_exceeded: {MAX_PAGES}")


def savings_rates(region, instance_types, now=None, http_post=requests.post):
    """반환: Savings Plans 요율 목록(searchResults 항목 그대로). Linux/UNIX·공유 테넌시만."""
    body = savings_body(region, instance_types)
    rows = []
    for _page in range(MAX_PAGES):
        data = _post(SAVINGS_HOST, "us-east-1", "savingsplans", "/DescribeSavingsPlansOfferingRates", body, now,
                     http_post, content_type="application/json")
        page = data.get("searchResults") or []
        rows.extend(page)
        # 결과가 남지 않았는데도 토큰을 주는 경우가 있어 빈 페이지에서도 멈춘다
        if not data.get("nextToken") or not page:
            return rows
        body["nextToken"] = data["nextToken"]
    raise AwsApiError(f"page_limit_exceeded: {MAX_PAGES}")


def savings_body(region, instance_types):
    return {"filters": [{"name": "instanceType", "values": sorted(instance_types)},
                        {"name": "region", "values": [region]},
                        {"name": "productDescription", "values": ["Linux/UNIX"]},
                        {"name": "tenancy", "values": ["shared"]}],
            "products": ["EC2"], "serviceCodes": ["AmazonEC2"], "maxResults": 1000}


def _post(host, region, service, path, body, now, http_post, *, content_type, target=None):
    now = now or datetime.now(timezone.utc)
    access_key, secret_key = secret("AWS_ACCESS_KEY_ID"), secret("AWS_SECRET_ACCESS_KEY")
    payload = json.dumps(body).encode()
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    headers = {"content-type": content_type, "host": host, "x-amz-date": amz_date}
    if target:
        headers["x-amz-target"] = target
    signed = ";".join(sorted(headers))
    canonical = "\n".join(["POST", path, "", "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed,
                           hashlib.sha256(payload).hexdigest()])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret_key).encode()
    for part in (day, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, "
                                f"Signature={signature}")
    headers["User-Agent"] = USER_AGENT
    try:
        response = http_post(f"https://{host}{path}", data=payload, headers=headers, timeout=(15, 60))
    except requests.RequestException as error:
        raise AwsApiError(f"request_failed: {type(error).__name__}") from error
    try:
        data = response.json()
    except ValueError as error:
        raise AwsApiError(f"invalid_json: http_{response.status_code}") from error
    if response.status_code != 200:
        # 메시지에는 계정 ARN이 들어 있어 남기지 않고 오류 종류만 남긴다
        kind = data.get("__type") or response.headers.get("x-amzn-ErrorType") or "unknown"
        raise AwsApiError(f"http_{response.status_code}: {str(kind).split('#')[-1].split(':')[0][:80]}")
    return data
