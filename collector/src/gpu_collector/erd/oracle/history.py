"""Oracle 과거 정가 → ERD 초안 스키마 적재 (집계 출처: Wayback Machine에 저장된 공개 정가 API 응답).

| 출처(data_source.source_name) | 원천 | 시점 |
| oracle_price_list_api_wayback | web.archive.org 저장본: apexapps.oracle.com/pls/apex/cetools/api/v1/products/ | 저장된 날만(불규칙) |

Oracle은 가격 이력 API가 없고, oracle.com 가격 페이지는 가격 칸을 브라우저에서 같은 API로 채워(HTML에 값 없음)
과거 페이지 저장본에도 가격이 없다. 정가 API 응답 저장본(2026-05부터)이 유일한 과거 자료다.

- 전체 목록 저장본만 쓴다. partNumber로 한 품목만 조회한 저장본과 USD가 없는 통화(currencyCode=BRL 등) 저장본은 건너뛴다.
- 집계 출처라 근거 역할은 aggregator다. 저장본 가격이 지금 공식 API 값과 같으면 원천 확인(primary_verified=true),
  다르면 미확인(false)으로 남긴다.
- 관측 시각(collected_at)은 저장 시각이다. 원천의 lastUpdated는 근거의 source_version에 남긴다.
- 같은 저장본은 다시 적재하지 않는다. 오래된 것부터 처리해 상품 구성 버전이 시간 순서를 따르게 한다.
- 과거 자료로는 알림을 만들지 않는다. 요청 사이에 간격을 두고, 403·429는 우회하지 않고 그 저장본을 실패로 남긴다.
"""
import gzip
import json
import sys
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import requests

from ... import fetch
from .. import db, runs
from . import collect as oracle

COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-oracle-wayback 1"
SOURCE_WAYBACK = "oracle_price_list_api_wayback"
ARCHIVED_URL = "apexapps.oracle.com/pls/apex/cetools/api/v1/products/"
CDX_URL = ("https://web.archive.org/cdx/search/cdx?url={url}&matchType=prefix&output=json&filter=statuscode:200"
           "&fl=timestamp,original,digest&collapse=digest")
RAW_URL = "https://web.archive.org/web/{timestamp}id_/{original}"
VIEW_URL = "https://web.archive.org/web/{timestamp}/{original}"
REQUEST_CONFIG = {"connect_timeout_s": 20, "read_idle_timeout_s": 120}
SECONDS_BETWEEN_REQUESTS = 2


def backfill(conn, *, since, until=None, now=None, get_json=fetch.get_json, get_bytes=None, pause=time.sleep,
             verify=True, log=None):
    """since·until: 'YYYY-MM'(저장 월). 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    log = log or (lambda message: print(message, file=sys.stderr, flush=True))
    get_bytes = get_bytes or _get_bytes
    until = until or now().strftime("%Y-%m")
    targets = [t for t in db.enabled_targets(conn, oracle.PROVIDER) if t["source_name"] == SOURCE_WAYBACK]
    if not targets:
        raise LookupError("no enabled Oracle Wayback collection_target")
    context = {"provider": db.provider_id(conn, oracle.PROVIDER), "rule": db.rule(conn, oracle.RULE_VERSION),
               "now": now, "docs": oracle.docs_targets(conn),
               "docs_source": oracle.source_id(conn, oracle.SOURCE_SHAPES)}
    context["run_id"] = db.begin_run(conn, now(), "manual", COLLECTOR_VERSION)
    conn.commit()
    try:
        verified, verify_note = primary_prices(get_json, now) if verify else (None, "원천 확인 안 함(--no-verify)")
        log(f"원천 확인 기준: {verify_note}")
        captures, ignored = list_captures(get_json)
        captures = [c for c in captures if since <= c["month"] <= until]
        jobs = [c for c in captures if not all(db.check_exists(conn, _target_key(t, c)) for t in targets)]
        log(f"저장본 {len(jobs)}개 처리 예정 ({since}~{until}). 이미 적재한 저장본은 건너뜀. 전체 목록이 아닌 저장본 "
            f"{len(ignored)}개 제외")
        outcomes = []
        for index, capture in enumerate(jobs):
            if index:
                pause(SECONDS_BETWEEN_REQUESTS)
            outcomes.append(_process(conn, context, targets, capture, get_bytes, verified))
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
    return {"run_id": context["run_id"], "status": status, "ignored_captures": ignored, "groups": outcomes}


# 저장본 목록 -------------------------------------------------------------------

def list_captures(get_json):
    """반환: ([{timestamp, at, month, original, digest}] 시간 순, [제외한 저장본]). 전체 USD 목록 저장본만 남긴다."""
    body = get_json(CDX_URL.format(url=ARCHIVED_URL), REQUEST_CONFIG)
    if not isinstance(body, list):
        raise oracle.SourceInvalid("unexpected_cdx_format")
    header, rows = (body[0], body[1:]) if body else ([], [])
    found, ignored = {}, []
    for row in rows:
        record = dict(zip(header, row))
        stamp, original = record.get("timestamp") or "", record.get("original") or ""
        if len(stamp) != 14 or not stamp.isdigit():
            continue
        query = parse_qs(urlparse(original).query)
        currency = (query.get("currencyCode") or [None])[0]
        if "partNumber" in query or currency not in (None, "USD"):
            ignored.append({"timestamp": stamp, "original": original,
                            "reason": "한 품목만 조회" if "partNumber" in query else f"USD 아님({currency})"})
            continue
        at = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        found.setdefault(stamp, {"timestamp": stamp, "at": at, "month": f"{stamp[:4]}-{stamp[4:6]}",
                                 "original": original, "digest": record.get("digest")})
    return sorted(found.values(), key=lambda c: c["timestamp"]), ignored


def primary_prices(get_json, now):
    """지금 공식 API의 {모델: (품목 번호, 가격)}. 받지 못하면 (None, 사유)."""
    try:
        _version, items, _excluded = oracle.read_items(get_json(oracle.API_URL, oracle.REQUEST_CONFIG))
    except (fetch.FetchError, oracle.SourceInvalid) as error:
        return None, f"공식 API를 받지 못해 원천 확인을 미확인(NULL)으로 남김: {type(error).__name__}: {error}"
    checked = now()
    prices = {model: (item.get("partNumber"), amount) for model, (item, amount) in items.items()}
    return {"prices": prices, "at": checked}, f"{checked:%Y-%m-%d %H:%M} UTC 공식 API {len(prices)}개 가격"


# 처리 ------------------------------------------------------------------------

def _get_bytes(url, config):
    """저장본 원문을 받는다. Wayback의 id_ 응답은 원래 응답 본문 그대로라 gzip일 수 있다."""
    timeout = (config.get("connect_timeout_s", 15), config.get("read_idle_timeout_s", 60))
    try:
        response = fetch.send(lambda: requests.get(url, headers={"User-Agent": fetch.USER_AGENT}, timeout=timeout))
    except requests.RequestException as error:
        raise fetch.FetchError(f"request_failed: {type(error).__name__}") from error
    if response.status_code != 200:
        raise fetch.FetchError(f"http_{response.status_code}")
    return response.content


def read_capture(body):
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    try:
        return json.loads(body)
    except ValueError as error:
        raise oracle.SourceInvalid("capture_not_json") from error


def _process(conn, context, targets, capture, get_bytes, verified):
    outcome = {"timestamp": capture["timestamp"], "original": capture["original"]}
    try:
        version, items, excluded = oracle.read_items(read_capture(get_bytes(RAW_URL.format(**capture), REQUEST_CONFIG)))
        results = oracle.target_results(targets, items)
        counts = _write(conn, context, targets, results, capture, version, excluded, verified)
        conn.commit()
        outcome.update(status="success", source_version=version, **counts)
    except oracle.EXPECTED_ERRORS as error:
        processing = "validation_failed" if isinstance(error, (oracle.SourceInvalid, db.HistoryOrderError)) \
            else "collection_failed"
        runs.record_failure(conn, context, targets, processing, f"{type(error).__name__}: {error}",
                            key=lambda t: f"wayback-failed:{context['run_id']}:{t['target_id']}:{capture['timestamp']}")
        outcome.update(status=processing, reason=f"{type(error).__name__}: {error}")
    return outcome


def _write(conn, context, targets, results, capture, version, excluded, verified):
    run_id, captured, as_of = context["run_id"], context["now"](), capture["at"]
    view_url = VIEW_URL.format(**capture)
    source_version = f"wayback {capture['timestamp']} digest:{capture['digest']} lastUpdated:{version}"
    counts = {"targets": 0, "offers": 0, "verified": 0, "unverified": 0, "skipped": 0, "not_found": 0}
    for target in targets:
        key = _target_key(target, capture)
        if db.check_exists(conn, key):
            counts["skipped"] += 1
            continue
        counts["targets"] += 1
        result = results[target["target_id"]]
        for hit in result.hits:
            product_id = db.history_product(conn, hit.product, as_of)
            offer_id = db.ensure_offer(conn, product_id, hit.offer)
            check_id, _ = db.add_check(conn, run_id, target["target_id"], "offer", as_of, "public", "success", True,
                                       f"{key}:offer:{offer_id}", product_id=product_id, offer_id=offer_id)
            oracle.ensure_spec_evidence(conn, context, target, product_id, captured)
            primary, verified_at = None, None
            if verified is not None:
                current = verified["prices"].get(target["model_name"])
                primary = current == (hit.product["sku"], hit.components[0]["amount"])
                verified_at = verified["at"] if primary else None
                counts["verified" if primary else "unverified"] += 1
            db.add_evidence(conn, check_id, target["source_id"], "aggregator", view_url, source_version,
                            {**hit.excerpt, "primary_source": oracle.API_URL, "archived_url": capture["original"]},
                            captured, primary_verified=primary, verified_at=verified_at)
            oracle.write_observation(conn, context, offer_id, check_id, hit, as_of, captured)
        counts["offers"] += len(result.hits)
        found = bool(result.hits)
        if not found:
            counts["not_found"] += 1
        check_id, _ = db.add_check(conn, run_id, target["target_id"], "target", as_of,
                                   "public" if found else "unknown", "success" if found else "not_found", True, key,
                                   reason=result.reason)
        db.add_evidence(conn, check_id, target["source_id"], "aggregator", view_url, source_version,
                        {"scope": oracle.scope(target), "kind": "aggregator_history", "primary_source": oracle.API_URL,
                         "offers_found": len(result.hits),
                         **({"excluded_gpu_items": excluded} if excluded else {})}, captured)
    return counts


def _target_key(target, capture):
    return f"wayback:{target['target_id']}:{capture['timestamp']}"


def _line(outcome):
    if outcome["status"] != "success":
        return f"{outcome['timestamp']}: {outcome['status']} {outcome.get('reason', '')}"
    return (f"{outcome['timestamp']}: success lastUpdated {outcome['source_version']} 대상 {outcome['targets']} "
            f"오퍼 {outcome['offers']} (원천 확인 {outcome['verified']} / 미확인 {outcome['unverified']}) "
            f"미발견 {outcome['not_found']} 건너뜀 {outcome['skipped']}")
