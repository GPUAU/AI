"""Scaleway 과거 가격 → ERD 초안 스키마 적재 (집계 출처: Wayback Machine에 저장된 GPU 가격 페이지).

| 출처(data_source.source_name) | 원천 | 시점 |
| scaleway_gpu_pricing_wayback | web.archive.org 저장본: www.scaleway.com/en/pricing/gpu/ | 저장된 날만(대략 월 1회, 2024-05~) |

카탈로그 API의 Wayback 저장본은 첫 페이지(100건)뿐이라 대상이 없다. 가격 페이지 저장본만 쓴다.
- 2025년 이후 저장본: 페이지 내장 JSON(__NEXT_DATA__)의 상품별 zones[].price.perMinute(정확값, EUR/분). 리전별 상품.
  상품 구성은 매일 수집이 만든 같은 상품(offer_id × zone)의 최신 구성을 이어받는다(옛 sku 형식 /compute/…가 달라서).
  그런 상품이 없으면 저장본의 hardware로 만든다.
- 2024년 저장본: 내장 JSON이 없고 HTML 표에 €/시간만 있다. 리전 표기가 없어 리전 미상(region_code NULL) 상품으로 둔다.
- 집계 출처라 근거 역할은 aggregator다. 같은 오퍼의 최근 매일 수집 가격과 같으면 원천 확인(primary_verified=true),
  다르면 false, 매일 수집 가격이 없으면 미확인(NULL).
- 관측 시각은 저장 시각, USD 환산은 매일 수집과 같은 규칙 v4(저장일 이하 최신 교차환율). 알림은 만들지 않는다.
- 같은 저장본은 다시 적재하지 않는다. 오래된 것부터 처리한다.
"""
import gzip
import html as html_lib
import json
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

from ... import fetch
from .. import db, runs
from . import collect as scaleway

COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-scaleway-wayback 1"
SOURCE_WAYBACK = "scaleway_gpu_pricing_wayback"
ARCHIVED_URL = "www.scaleway.com/en/pricing/gpu/"
CDX_URL = ("https://web.archive.org/cdx/search/cdx?url={url}&matchType=prefix&output=json&filter=statuscode:200"
           "&fl=timestamp,original,digest&collapse=digest")
RAW_URL = "https://web.archive.org/web/{timestamp}id_/{original}"
VIEW_URL = "https://web.archive.org/web/{timestamp}/{original}"
REQUEST_CONFIG = {"connect_timeout_s": 20, "read_idle_timeout_s": 180}
SECONDS_BETWEEN_REQUESTS = 6      # Wayback이 연속 요청을 끊는다(연결 오류). 다시 실행하면 이어서 적재
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
INSTANCES = {name for name, spec in scaleway.SPECS.items() if spec["quote"] is not None}
# 2024년 HTML 표 한 줄: 상품명 | 24 cores | 1 GPU | 240 GB | … | €2.52 | /hour
TABLE_ROW = re.compile(r"(?P<name>(?:B300|H100)[A-Z0-9-]+) \| (?P<cpu>\d+) (?:cores|vCPUs) \| (?P<gpus>\d+) GPUs? \| "
                       r"(?P<ram>[\d,]+) GB .{0,120}?€(?P<price>[\d.,]+) \| /hour")


def backfill(conn, *, since, until=None, now=None, get_json=fetch.get_json, get_bytes=None, pause=time.sleep,
             log=None):
    """since·until: 'YYYY-MM'(저장 월). 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    log = log or (lambda message: print(message, file=sys.stderr, flush=True))
    get_bytes = get_bytes or _get_bytes
    until = until or now().strftime("%Y-%m")
    targets = [t for t in db.enabled_targets(conn, scaleway.PROVIDER) if t["source_name"] == SOURCE_WAYBACK]
    if not targets:
        raise LookupError("no enabled Scaleway Wayback collection_target")
    context = {"provider": db.provider_id(conn, scaleway.PROVIDER), "rule": db.rule(conn, scaleway.RULE_VERSION),
               "now": now, "docs": scaleway.docs_targets(conn),
               "docs_source": scaleway.source_id(conn, scaleway.SOURCE_PAGE)}
    context["run_id"] = db.begin_run(conn, now(), "manual", COLLECTOR_VERSION)
    conn.commit()
    try:
        captures = [c for c in list_captures(get_json) if since <= c["month"] <= until]
        jobs = [c for c in captures if not all(db.check_exists(conn, _target_key(t, c)) for t in targets)]
        log(f"저장본 {len(jobs)}개 처리 예정 ({since}~{until}). 이미 적재한 저장본은 건너뜀")
        outcomes = []
        for index, capture in enumerate(jobs):
            if index:
                pause(SECONDS_BETWEEN_REQUESTS)
            outcomes.append(_process(conn, context, targets, capture, get_bytes))
            log(_line(outcomes[-1]))
    except Exception as error:
        conn.rollback()
        db.finish_run(conn, context["run_id"], now(), "failed", f"unexpected: {type(error).__name__}")
        conn.commit()
        raise
    ok = sum(1 for outcome in outcomes if outcome["status"] == "success")
    status = "success" if ok == len(outcomes) else ("failed" if ok == 0 else "partial_failure")
    failed = [f"{o['timestamp']}: {o['reason']}" for o in outcomes if o["status"] != "success"]
    db.finish_run(conn, context["run_id"], now(), status, "; ".join(failed)[:2000] or None)
    conn.commit()
    return {"run_id": context["run_id"], "status": status, "groups": outcomes}


# 저장본 목록·원문 ---------------------------------------------------------------

def list_captures(get_json):
    body = get_json(CDX_URL.format(url=ARCHIVED_URL), REQUEST_CONFIG)
    if not isinstance(body, list):
        raise scaleway.SourceInvalid("unexpected_cdx_format")
    header, rows = (body[0], body[1:]) if body else ([], [])
    found = {}
    for row in rows:
        record = dict(zip(header, row))
        stamp = record.get("timestamp") or ""
        if len(stamp) != 14 or not stamp.isdigit():
            continue
        at = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        found.setdefault(stamp, {"timestamp": stamp, "at": at, "month": f"{stamp[:4]}-{stamp[4:6]}",
                                 "original": record.get("original"), "digest": record.get("digest")})
    return sorted(found.values(), key=lambda c: c["timestamp"])


def _get_bytes(url, config):
    timeout = (config.get("connect_timeout_s", 15), config.get("read_idle_timeout_s", 60))
    try:
        response = fetch.send(lambda: requests.get(url, headers={"User-Agent": fetch.USER_AGENT}, timeout=timeout))
    except requests.RequestException as error:
        raise fetch.FetchError(f"request_failed: {type(error).__name__}") from error
    if response.status_code != 200:
        raise fetch.FetchError(f"http_{response.status_code}")
    return response.content


def read_page(body):
    """저장본 → [{name, zone, unit, amount, hardware, gpus, vcpu, ram_gb}]. 형식을 알 수 없으면 검증 실패."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    text = body.decode("utf8", "replace")
    match = NEXT_DATA.search(text)
    if match:
        try:
            data = json.loads(match.group(1))
        except ValueError:
            raise scaleway.SourceInvalid("invalid_next_data") from None
        rows = _from_json(data)
        if rows:
            return rows
    return _from_table(text)          # 2024~2025 초 저장본: 내장 JSON에 상품 가격이 없고 HTML 표에만 있다


def _offer_name(node):
    """상품명. 저장본에 따라 name(2026~) 또는 apiId(2025, name은 'NVIDIA H100' 같은 묶음 이름)에 있다."""
    for key in ("name", "apiId"):
        if node.get(key) in INSTANCES:
            return node[key]
    return None


def _walk(node):
    if isinstance(node, dict):
        if _offer_name(node) and isinstance(node.get("zones"), list):
            yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _from_json(data):
    rows, seen = [], set()
    for item in _walk(data):
        hardware = item.get("hardware") or {}
        name = _offer_name(item)
        for zone in item["zones"]:
            per_minute = ((zone.get("price") or {}).get("perMinute") or {})
            value = per_minute.get("value") or {}
            if not zone.get("zone") or not value:
                continue
            key = (name, zone["zone"])
            if key in seen:
                continue                  # 같은 상품이 페이지에 여러 번 나온다
            seen.add(key)
            if value.get("currencyCode") != "EUR":
                raise scaleway.SourceInvalid(f"unexpected_currency: {key} {value.get('currencyCode')}")
            amount = Decimal(value.get("units") or 0) + Decimal(value.get("nanos") or 0) / Decimal(10 ** 9)
            rows.append({"name": name, "zone": zone["zone"], "unit": "minute", "amount": amount,
                         "gpus": (hardware.get("gpu") or {}).get("count"),
                         "vcpu": ((hardware.get("cpu") or {}).get("virtual") or {}).get("count"), "ram_gb": None,
                         "sku": per_minute.get("sku"), "source": "__NEXT_DATA__ perMinute"})
    return rows


def _from_table(text):
    plain = html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " | ",
                                    re.sub(r"(?s)<script.*?</script>|<style.*?</style>", "", text))))
    plain = re.sub(r"(?:\s*\|\s*)+", " | ", plain)
    rows, seen = [], set()
    for match in TABLE_ROW.finditer(plain):
        name = match.group("name")
        if name not in INSTANCES or name in seen:
            continue
        seen.add(name)
        try:
            amount = Decimal(match.group("price").replace(",", ""))
        except InvalidOperation:
            raise scaleway.SourceInvalid(f"invalid_table_price: {name} {match.group('price')}") from None
        rows.append({"name": name, "zone": None, "unit": "hour", "amount": amount, "gpus": int(match.group("gpus")),
                     "vcpu": int(match.group("cpu")), "ram_gb": Decimal(match.group("ram").replace(",", "")),
                     "sku": None, "source": "HTML 표 €/hour"})
    if not rows:
        raise scaleway.SourceInvalid("no_target_rows_in_table")
    return rows


# 처리 ------------------------------------------------------------------------

def _process(conn, context, targets, capture, get_bytes):
    outcome = {"timestamp": capture["timestamp"], "original": capture["original"]}
    try:
        rows = read_page(get_bytes(RAW_URL.format(**capture), REQUEST_CONFIG))
        counts = _write(conn, context, targets, capture, rows)
        conn.commit()
        outcome.update(status="success", **counts)
    except scaleway.EXPECTED_ERRORS as error:
        processing = "validation_failed" if isinstance(error, (scaleway.SourceInvalid, db.HistoryOrderError)) \
            else "collection_failed"
        runs.record_failure(conn, context, targets, processing, f"{type(error).__name__}: {error}",
                            key=lambda t: f"wayback-failed:{context['run_id']}:{t['target_id']}:{capture['timestamp']}")
        outcome.update(status=processing, reason=f"{type(error).__name__}: {error}")
    return outcome


def history_spec(conn, target, row):
    """같은 상품(offer_id × zone)의 최신 구성을 이어받는다. 없으면 저장본 값으로 만든다."""
    columns = db.PRODUCT_IDENTITY + db.CONFIG_FIELDS
    existing = conn.execute(
        f"""SELECT {", ".join(columns)} FROM product
            WHERE provider_id = %s AND gpu_model_id = %s AND original_product_name = %s
              AND region_code IS NOT DISTINCT FROM %s AND system_type = 'instance' AND service_option = 'standard'
            ORDER BY configuration_version DESC LIMIT 1""",
        (target["provider_id"], target["gpu_model_id"], row["name"], row["zone"])).fetchone()
    if existing:
        return dict(zip(columns, existing))
    spec = scaleway.SPECS[row["name"]]
    if not row["gpus"]:
        raise scaleway.SourceInvalid(f"no_gpu_count: {row['name']}")
    gpu_type = f"{spec['model']}-SXM" if "SXM" in row["name"] else f"{spec['model']}-PCIe"
    zone = row["zone"]
    return {"provider_id": target["provider_id"], "gpu_model_id": target["gpu_model_id"],
            "original_product_name": row["name"], "region_code": zone, "system_type": "instance",
            "service_option": "standard", "sku": None, "original_model_name": gpu_type, "gpu_count": row["gpus"],
            "vram_gb_per_gpu": Decimal(spec["vram_gib"]) if spec["vram_gib"] else None, "vcpu_count": row["vcpu"],
            "ram_gb": row["ram_gb"], "storage_description": "가격에 미포함(별도 과금). 저장본 표 기준",
            "spec_scope": "instance", "inter_node_network": None,
            "country_code": scaleway.COUNTRY.get(zone.split("-")[0]) if zone else None}


def make_hit(spec, row, view_url):
    raw = f"{row['amount']} EUR / {row['unit']} ({row['name']}, {row['zone'] or '리전 미표기'}, 저장본 {row['source']})"
    unit = row["unit"]
    terms = scaleway.RAW_TERMS["minute"] + "; Wayback 가격 페이지 저장본" + ("" if row["zone"] else "(리전 미표기, 시간 단가)")
    return scaleway.Hit(
        spec, {"contract_type": "on_demand", "contract_product_type": "Instance pay-as-you-go",
               "commitment_months": None, "payment_option": None},
        {"effective_from": None, "price_certainty": "firm", "raw_price_text": raw, "original_currency": "EUR",
         "original_time_unit": unit, "price_applies_to": "instance", "raw_terms": terms},
        [{"component_type": "hourly_rate" if unit == "hour" else "list_price", "amount": row["amount"],
          "currency": "EUR", "time_unit": unit, "applies_to": "instance", "raw_value": raw}],
        {"name": row["name"], "zone": row["zone"], "amount": str(row["amount"]), "unit": unit, "archived_sku": row["sku"],
         "parsed_from": row["source"], "view_url": view_url}, unit, "EUR")


def latest_daily_amount(conn, offer_id):
    row = conn.execute(
        """SELECT pc.amount, pc.time_unit FROM price_observation ob
           JOIN collection_check c ON c.check_id = ob.check_id AND c.deduplication_key LIKE 'daily:%%'
           JOIN price_component pc ON pc.observation_id = ob.observation_id
           WHERE ob.offer_id = %s ORDER BY ob.collected_at DESC LIMIT 1""", (offer_id,)).fetchone()
    return row


def _write(conn, context, targets, capture, rows):
    run_id, captured, as_of = context["run_id"], context["now"](), capture["at"]
    view_url = VIEW_URL.format(**capture)
    version = f"wayback {capture['timestamp']} digest:{capture['digest']}"
    by_model = {}
    for row in rows:
        by_model.setdefault(scaleway.SPECS[row["name"]]["model"], []).append(row)
    counts = {"targets": 0, "offers": 0, "verified": 0, "unverified": 0, "skipped": 0, "not_found": 0}
    for target in targets:
        key = _target_key(target, capture)
        if db.check_exists(conn, key):
            counts["skipped"] += 1
            continue
        counts["targets"] += 1
        found = by_model.get(target["model_name"], [])
        for row in found:
            spec = history_spec(conn, target, row)
            hit = make_hit(spec, row, view_url)
            product_id = db.history_product(conn, spec, as_of)
            offer_id = db.ensure_offer(conn, product_id, hit.offer)
            check_id, _ = db.add_check(conn, run_id, target["target_id"], "offer", as_of, "public", "success", True,
                                       f"{key}:offer:{offer_id}", product_id=product_id, offer_id=offer_id)
            scaleway.ensure_spec_evidence(conn, context, target, product_id, row["name"], captured)
            daily = latest_daily_amount(conn, offer_id)
            primary = None if daily is None else (daily[1] == row["unit"] and daily[0] == row["amount"])
            if primary is not None:
                counts["verified" if primary else "unverified"] += 1
            db.add_evidence(conn, check_id, target["source_id"], "aggregator", view_url, version,
                            {**hit.excerpt, "primary_source": scaleway.API_URL.format(page="N")}, captured,
                            primary_verified=primary, verified_at=captured if primary else None)
            scaleway.write_observation(conn, context, offer_id, check_id, hit, as_of, captured, alerts=False)
        counts["offers"] += len(found)
        if not found:
            counts["not_found"] += 1
        check_id, _ = db.add_check(conn, run_id, target["target_id"], "target", as_of,
                                   "public" if found else "unknown", "success" if found else "not_found", True, key,
                                   reason=None if found else f"저장본에 {target['model_name']} 상품 없음")
        db.add_evidence(conn, check_id, target["source_id"], "aggregator", view_url, version,
                        {"scope": scaleway.scope(target), "kind": "aggregator_history", "offers_found": len(found),
                         "parsed_from": sorted({r["source"] for r in found})}, captured)
    return counts


def _target_key(target, capture):
    return f"wayback:{target['target_id']}:{capture['timestamp']}"


def _line(outcome):
    if outcome["status"] != "success":
        return f"{outcome['timestamp']}: {outcome['status']} {outcome.get('reason', '')}"
    return (f"{outcome['timestamp']}: success 대상 {outcome['targets']} 오퍼 {outcome['offers']} "
            f"(원천 확인 {outcome['verified']} / 다름 {outcome['unverified']}) 미발견 {outcome['not_found']} "
            f"건너뜀 {outcome['skipped']}")
