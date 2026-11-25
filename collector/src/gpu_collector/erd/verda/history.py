"""Verda 과거 가격 → ERD 초안 스키마 적재 (집계 출처: Wayback Machine에 저장된 instance-types API 응답).

| 출처(data_source.source_name) | 원천 | 시점 |
| verda_api_wayback | web.archive.org 저장본: api.datacrunch.io(옛 도메인)·api.verda.com /v1/instance-types | 저장된 날만(불규칙) |

Verda는 가격 이력 API(/v1/instance-types/price-history)를 410으로 없앴고, 과거 가격 문서도 없다.
Wayback 저장본이 유일한 과거 자료다. 약정 할인율 API 저장본은 없어, 약정은 가격 페이지 저장본의 할인율로 추정한다(verda/rates.py).

- 집계 출처라 근거 역할은 aggregator다. 원천 확인(primary_verified)은 적재 뒤 erd/verify.py가 판정한다
  (그 무렵 매일 수집과 같은지).
- 관측 시각(collected_at)은 저장 시각이다. 실제로 받은 시각은 근거의 captured_at과 실행 기록에 남는다.
- 지역별 가격(location_prices)이 있는 저장본은 지역별 상품에, 없는 저장본(2026-11-15 이전)은 최상위 가격을
  지역 미상(region_code NULL) 상품에 붙인다. 최상위 가격은 지역 가격과 다를 때가 있어 특정 지역으로 옮기지 않는다.
- 같은 저장본은 다시 적재하지 않는다. 오래된 것부터 처리해 상품 구성 버전이 시간 순서를 따르게 한다.
- 과거 자료로는 알림을 만들지 않는다. 요청 사이에 간격을 두고, 403·429는 우회하지 않고 그 저장본을 실패로 남긴다.
"""
import gzip
import json
import sys
import time
from datetime import datetime, timezone

import requests

from ... import fetch
from .. import db, verify as verifier
from . import collect as verda, rates

COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-verda-wayback 1"
SOURCE_WAYBACK = "verda_api_wayback"
ARCHIVED_URLS = ("api.datacrunch.io/v1/instance-types", "api.verda.com/v1/instance-types")
CDX_URL = ("https://web.archive.org/cdx/search/cdx?url={url}&output=json&filter=statuscode:200"
           "&fl=timestamp,original,digest&collapse=digest")
RAW_URL = "https://web.archive.org/web/{timestamp}id_/{original}"
VIEW_URL = "https://web.archive.org/web/{timestamp}/{original}"
REQUEST_CONFIG = {"connect_timeout_s": 20, "read_idle_timeout_s": 90}
SECONDS_BETWEEN_REQUESTS = 2


def backfill(conn, *, since, until=None, now=None, get_json=fetch.get_json, get_bytes=None, pause=time.sleep,
             verify=True, log=None):
    """since·until: 'YYYY-MM'(저장 월). 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    log = log or (lambda message: print(message, file=sys.stderr, flush=True))
    get_bytes = get_bytes or _get_bytes
    until = until or now().strftime("%Y-%m")
    targets = [t for t in db.enabled_targets(conn, verda.PROVIDER) if t["source_name"] == SOURCE_WAYBACK]
    if not targets:
        raise LookupError("no enabled Verda Wayback collection_target (sql/erd_seed.sql)")
    context = {"provider": db.provider_id(conn, verda.PROVIDER), "rule": db.rule(conn, verda.RULE_VERSION),
               "now": now, "docs": verda.docs_targets(conn), "docs_source": verda.source_id(conn, verda.SOURCE_PAGES)}
    context["run_id"] = db.begin_run(conn, now(), "manual", COLLECTOR_VERSION)
    conn.commit()

    captures = [c for c in list_captures(get_json, pause) if since <= c["month"] <= until]
    jobs = [c for c in captures if not all(db.check_exists(conn, _target_key(t, c)) for t in targets)]
    log(f"저장본 {len(jobs)}개 처리 예정 ({since}~{until}). 이미 적재한 저장본은 건너뜀")

    outcomes = []
    for index, capture in enumerate(jobs):
        if index:
            pause(SECONDS_BETWEEN_REQUESTS)
        outcomes.append(_process(conn, context, targets, capture, get_bytes))
        log(_line(outcomes[-1]))

    outcomes += rates.backfill(conn, context, get_json=get_json, get_bytes=get_bytes, pause=pause, log=log)
    ok = sum(1 for outcome in outcomes if outcome["status"] == "success")
    status = "success" if ok == len(outcomes) else ("failed" if ok == 0 else "partial_failure")
    failed = [f"{o['timestamp']}: {o['reason']}" for o in outcomes if o["status"] != "success"]
    checked = verifier.reverify(conn, sources={SOURCE_WAYBACK, rates.SOURCE_RATES}) if verify else None
    db.finish_run(conn, context["run_id"], now(), status, "; ".join(failed)[:2000] or None)
    conn.commit()
    return {"run_id": context["run_id"], "status": status, "verification": checked, "groups": outcomes}


# 저장본 목록 -------------------------------------------------------------------

def list_captures(get_json, pause):
    """[{timestamp, at, month, original, digest}] 시간 순. 내용이 같은 연속 저장본은 CDX가 하나로 묶는다."""
    found = {}
    for index, url in enumerate(ARCHIVED_URLS):
        if index:
            pause(SECONDS_BETWEEN_REQUESTS)
        body = get_json(CDX_URL.format(url=url), REQUEST_CONFIG)
        if not isinstance(body, list):
            raise verda.SourceInvalid("unexpected_cdx_format")
        header, rows = (body[0], body[1:]) if body else ([], [])
        for row in rows:
            record = dict(zip(header, row))
            stamp = record.get("timestamp") or ""
            if len(stamp) != 14 or not stamp.isdigit():
                continue
            at = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            found.setdefault(stamp, {"timestamp": stamp, "at": at, "month": f"{stamp[:4]}-{stamp[4:6]}",
                                     "original": record.get("original"), "digest": record.get("digest")})
    return sorted(found.values(), key=lambda c: c["timestamp"])


# 처리 ------------------------------------------------------------------------

def _get_bytes(url, config):
    """저장본 원문을 받는다. Wayback의 id_ 응답은 원래 응답 본문 그대로라 gzip일 수 있다."""
    timeout = (config.get("connect_timeout_s", 15), config.get("read_idle_timeout_s", 60))
    try:
        response = requests.get(url, headers={"User-Agent": fetch.USER_AGENT}, timeout=timeout)
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
        raise verda.SourceInvalid("capture_not_json") from error


def _process(conn, context, targets, capture, get_bytes):
    outcome = {"timestamp": capture["timestamp"], "original": capture["original"]}
    raw_url = RAW_URL.format(**capture)
    try:
        rows = verda.read_instances(read_capture(get_bytes(raw_url, REQUEST_CONFIG)))
        by_location = any(isinstance(r.get("location_prices"), list) and r["location_prices"] for r in rows)
        view_url = VIEW_URL.format(**capture)
        version = f"wayback {capture['timestamp']} digest:{capture['digest']}"
        results = verda.instance_results(targets, rows, view_url, version, f"저장본 {capture['timestamp']}",
                                         as_of_location=by_location)
        counts = _write(conn, context, targets, results, capture)
        conn.commit()
        outcome.update(status="success", by_location=by_location, **counts)
    except verda.EXPECTED_ERRORS as error:
        processing = "validation_failed" if isinstance(error, (verda.SourceInvalid, db.HistoryOrderError)) \
            else "collection_failed"
        verda.record_failure(conn, context, targets, processing, f"{type(error).__name__}: {error}",
                             key=lambda t: f"wayback-failed:{context['run_id']}:{t['target_id']}:{capture['timestamp']}")
        outcome.update(status=processing, reason=f"{type(error).__name__}: {error}")
    except Exception as error:
        conn.rollback()
        db.finish_run(conn, context["run_id"], context["now"](), "failed", f"unexpected: {type(error).__name__}")
        conn.commit()
        raise
    return outcome


def _write(conn, context, targets, results, capture):
    run_id, captured, as_of = context["run_id"], context["now"](), capture["at"]
    counts = {"targets": 0, "offers": 0, "skipped": 0, "not_found": 0}
    for target in targets:
        key = _target_key(target, capture)
        if db.check_exists(conn, key):
            counts["skipped"] += 1
            continue
        counts["targets"] += 1
        result = results[target["target_id"]]
        seen = set()
        for hit in result.hits:
            product_id = db.history_product(conn, hit.product, as_of)
            offer_id = db.ensure_offer(conn, product_id, hit.offer)
            if offer_id in seen:
                raise verda.SourceInvalid(f"duplicate_offer_in_capture: offer {offer_id}")
            seen.add(offer_id)
            check_id, _ = db.add_check(conn, run_id, target["target_id"], "offer", as_of, "public", "success", True,
                                       f"{key}:offer:{offer_id}", product_id=product_id, offer_id=offer_id)
            verda.ensure_spec_evidence(conn, context, target, product_id, hit.product["gpu_count"], captured)
            db.add_evidence(conn, check_id, target["source_id"], "aggregator", hit.source_url, hit.source_version,
                            {**hit.excerpt, "primary_source": verda.INSTANCE_URL, "archived_url": capture["original"]},
                            captured)
            verda.write_observation(conn, context, offer_id, check_id, hit, as_of, captured)
        counts["offers"] += len(result.hits)
        found = bool(result.hits)
        if not found:
            counts["not_found"] += 1
        check_id, _ = db.add_check(conn, run_id, target["target_id"], "target", as_of,
                                   "public" if found else "unknown", "success" if found else "not_found", True, key,
                                   reason=result.reason)
        db.add_evidence(conn, check_id, target["source_id"], "aggregator", result.source_url, result.source_version,
                        {"scope": verda.scope(target), "kind": "aggregator_history",
                         "primary_source": verda.INSTANCE_URL, "offers_found": len(result.hits), **result.note},
                        captured)
    return counts


def _target_key(target, capture):
    return f"wayback:{target['target_id']}:{capture['timestamp']}"


def _line(outcome):
    if outcome["status"] != "success":
        return f"{outcome['timestamp']}: {outcome['status']} {outcome.get('reason', '')}"
    where = "지역별" if outcome["by_location"] else "최상위 가격(지역 미상)"
    return (f"{outcome['timestamp']}: success {where} 대상 {outcome['targets']} 오퍼 {outcome['offers']} "
            f"미발견 {outcome['not_found']} "
            f"건너뜀 {outcome['skipped']}")
