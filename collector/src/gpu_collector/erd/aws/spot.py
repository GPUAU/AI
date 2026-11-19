"""AWS EC2 DescribeSpotPriceHistory 호출 (Signature Version 4).

키는 AWS_ACCESS_KEY_ID·AWS_SECRET_ACCESS_KEY 환경 변수에서 읽고, 로그·예외에 남기지 않는다.
StartTime과 EndTime을 같은 현재 시각으로 주면 AWS가 그 시각 직전의 마지막 가격을 가용영역별로 돌려준다.
기간을 주면 그 사이 가격이 바뀐 기록을 모두 돌려준다. 조회 가능한 과거는 최근 90일이다.
"""
import hashlib
import hmac
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

from ...fetch import USER_AGENT, send
from ...env import secret

API_VERSION = "2016-11-15"
NS = {"e": f"http://ec2.amazonaws.com/doc/{API_VERSION}/"}
PRODUCT_DESCRIPTION = "Linux/UNIX"
MAX_PAGES = 20


class SpotApiError(Exception):
    """이번 회차를 실패로 기록해야 하는 API 오류. 비밀값을 담지 않는다."""


def endpoint(region):
    return f"https://ec2.{region}.amazonaws.com/"


def current_prices(region, instance_types, now=None, http_get=requests.get):
    """반환: [{instance_type, availability_zone, availability_zone_id, price(Decimal), timestamp(datetime)}]"""
    now = now or datetime.now(timezone.utc)
    return price_history(region, instance_types, now, now, now=now, http_get=http_get)


def price_history(region, instance_types, start, end, now=None, http_get=requests.get, max_pages=MAX_PAGES):
    """start~end 사이에 바뀐 가격 기록과, start 시각에 적용 중이던 가격(가용영역별). AWS는 최근 90일만 준다."""
    now = now or datetime.now(timezone.utc)
    params = history_params(instance_types, start, end)
    params["Version"] = API_VERSION
    rows = []
    for _page in range(max_pages):
        root = _call(region, params, now, http_get)
        rows.extend(_parse_items(root))
        token = root.find("e:nextToken", NS)
        if token is None or not (token.text or "").strip():
            return rows
        params["NextToken"] = token.text.strip()
    raise SpotApiError(f"page_limit_exceeded: {max_pages}")


def history_params(instance_types, start, end):
    """요청 변수(서명용 Version 제외). 근거 URL에도 쓴다."""
    params = {"Action": "DescribeSpotPriceHistory", "StartTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "EndTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "ProductDescription.1": PRODUCT_DESCRIPTION,
              "MaxResults": "1000"}
    for index, name in enumerate(sorted(instance_types), 1):
        params[f"InstanceType.{index}"] = name
    return params


def request_url(region, params):
    query = urllib.parse.urlencode(sorted(params.items()), quote_via=urllib.parse.quote)
    return f"{endpoint(region)}?{query}"


def _call(region, params, now, http_get):
    access_key, secret_key = secret("AWS_ACCESS_KEY_ID"), secret("AWS_SECRET_ACCESS_KEY")
    host = urllib.parse.urlparse(endpoint(region)).netloc
    query = urllib.parse.urlencode(sorted(params.items()), quote_via=urllib.parse.quote)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    canonical = "\n".join(["GET", "/", query, f"host:{host}\nx-amz-date:{amz_date}\n", "host;x-amz-date",
                           hashlib.sha256(b"").hexdigest()])
    scope = f"{day}/{region}/ec2/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret_key).encode()
    for part in (day, region, "ec2", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers = {"x-amz-date": amz_date, "User-Agent": USER_AGENT,
               "Authorization": (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                                 f"SignedHeaders=host;x-amz-date, Signature={signature}")}
    try:
        response = send(lambda: http_get(f"https://{host}/?{query}", headers=headers, timeout=(15, 60)))
    except requests.RequestException as error:
        raise SpotApiError(f"request_failed: {type(error).__name__}") from error
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as error:
        raise SpotApiError(f"invalid_xml: http_{response.status_code}") from error
    if response.status_code != 200:
        code = root.find(".//Code")
        raise SpotApiError(f"http_{response.status_code}: {code.text if code is not None else 'unknown'}")
    return root


def _parse_items(root):
    rows = []
    for item in root.findall("e:spotPriceHistorySet/e:item", NS):
        text = lambda tag: (item.findtext(f"e:{tag}", default="", namespaces=NS) or "").strip() or None
        try:
            price = Decimal(text("spotPrice"))
        except (InvalidOperation, TypeError) as error:
            raise SpotApiError(f"invalid_price: {text('spotPrice')}") from error
        timestamp = text("timestamp")
        rows.append({
            "instance_type": text("instanceType"),
            "product_description": text("productDescription"),
            "availability_zone": text("availabilityZone"),
            "availability_zone_id": text("availabilityZoneId"),
            "price": price,
            "timestamp": datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else None,
        })
    return rows
