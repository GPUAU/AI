"""Nebius 과거 가격 → ERD 초안 스키마 적재.

| 출처(data_source.source_name) | 원천 | 범위 |
| nebius_spot_price_history_api | GET api.nebius.cloud/billing/v1alpha1/spot-price/list-series (공식, 인증 없음) | 최근 30일, 1시간 간격 |
| nebius_pricing_docs_wayback | Wayback에 저장된 docs.nebius.com/compute/resources/pricing.md (집계) | 저장된 날만(2026-05~) |

- 스팟 시계열: 1시간 간격의 마지막 가격(METHOD_LAST_PRICE). 점의 시각(간격 끝)을 관측 시각으로 쓰고, 같은 플랫폼의 프리셋마다
  같은 값을 붙인다. 같은 오퍼·시각은 다시 적재하지 않아 매일 실행해도 새 점만 쌓인다. 30일이 지나면 원천에서 사라진다.
- 문서 저장본: 매일 수집과 같은 해석(parse_pricing)으로 저장 시각에 적용되던 USD 표만 쓴다. 온디맨드와, 시작가('From')가
  아닌 고정 선점형 가격(과거 스팟)을 적재한다. 근거 역할은 aggregator, 지금 문서 값과 같으면 원천 확인(true).
- 오래된 것부터 처리한다. 과거 자료로는 알림을 만들지 않는다.
"""
import sys
import time
from datetime import datetime, timedelta, timezone

from ... import fetch
from .. import db
from . import collect as nebius

COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-nebius-history 1"
SOURCE_SERIES = "nebius_spot_price_history_api"
SOURCE_WAYBACK = "nebius_pricing_docs_wayback"
SERIES_URL = nebius.SERIES_URL
ARCHIVED_URL = "docs.nebius.com/compute/resources/pricing.md"
CDX_URL = ("https://web.archive.org/cdx/search/cdx?url={url}&output=json&filter=statuscode:200"
           "&fl=timestamp,original,digest&collapse=digest")
RAW_URL = "https://web.archive.org/web/{timestamp}id_/{original}"
VIEW_URL = "https://web.archive.org/web/{timestamp}/{original}"
MAX_DAYS = 29
REQUEST_CONFIG = {"connect_timeout_s": 20, "read_idle_timeout_s": 120}


def backfill(conn, *, days=MAX_DAYS, only=None, now=None, get_json=fetch.get_json, get_text=None,
             pause=time.sleep, verify=True, log=None):
    """only: {'spot','docs'} 중 일부(생략하면 둘 다). 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    log = log or (lambda message: print(message, file=sys.stderr, flush=True))
    get_text = get_text or nebius.fetch_text
    only = only or {"spot", "docs"}
    targets = db.enabled_targets(conn, nebius.PROVIDER)
    series_targets = [t for t in targets if t["source_name"] == SOURCE_SERIES]
    wayback_targets = [t for t in targets if t["source_name"] == SOURCE_WAYBACK]
    context = {"provider": db.provider_id(conn, nebius.PROVIDER), "rule": db.rule(conn, nebius.RULE_VERSION),
               "now": now, "docs": nebius.spec_targets(conn), "docs_source": nebius.source_id(conn, nebius.SOURCE_TYPES)}
    context["run_id"] = db.begin_run(conn, now(), "manual", COLLECTOR_VERSION)
    conn.commit()
    outcomes = []
    try:
        if "spot" in only and series_targets:
            outcomes.extend(spot_series(conn, context, series_targets, min(days, MAX_DAYS), get_json, pause, log))
        if "docs" in only and wayback_targets:
            outcomes.extend(docs_captures(conn, context, wayback_targets, get_json, get_text, pause, verify, log))
    except Exception as error:
        conn.rollback()
        db.finish_run(conn, context["run_id"], now(), "failed", f"unexpected: {type(error).__name__}")
        conn.commit()
        raise
    ok = sum(1 for outcome in outcomes if outcome["status"] == "success")
    status = "success" if ok == len(outcomes) else ("failed" if ok == 0 else "partial_failure")
    failed = [f"{o['group']}: {o['reason']}" for o in outcomes if o["status"] != "success"]
    db.finish_run(conn, context["run_id"], now(), status, "; ".join(failed)[:2000] or None)
    conn.commit()
    return {"run_id": context["run_id"], "status": status, "groups": outcomes}


# 스팟 30일 시계열 (공식) ------------------------------------------------------------

def spot_series(conn, context, targets, days, get_json, pause, log):
    start = (context["now"]() - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
    try:
        series = fetch_series(get_json, pause, start)
    except nebius.EXPECTED_ERRORS as error:
        processing = "validation_failed" if isinstance(error, nebius.SourceInvalid) else "collection_failed"
        nebius.record_failure(conn, context, targets, processing, f"{type(error).__name__}: {error}",
                              key=lambda t: f"series-failed:{context['run_id']}:{t['target_id']}")
        return [{"group": "spot-series", "status": processing, "reason": f"{type(error).__name__}: {error}"}]
    captured = context["now"]()
    by_model = {t["model_name"]: t for t in targets}
    outcomes = []
    for item in series:
        platform = (item.get("computeInstance") or {}).get("platform")
        if platform not in nebius.PLATFORMS or nebius.PLATFORMS[platform]["model"] not in by_model:
            continue
        target = by_model[nebius.PLATFORMS[platform]["model"]]
        label = f"spot-series {platform} {item.get('region')}"
        try:
            counts = _write_series(conn, context, target, item, platform, captured)
            conn.commit()
            outcomes.append({"group": label, "status": "success", **counts})
        except nebius.EXPECTED_ERRORS + (db.HistoryOrderError,) as error:
            conn.rollback()
            outcomes.append({"group": label, "status": "validation_failed", "reason": f"{type(error).__name__}: {error}"})
        log(_line(outcomes[-1]))
    for target in targets:
        key = f"series:{context['run_id']}:{target['target_id']}:target"
        found = [o for o in outcomes if o["status"] == "success"
                 and nebius.PLATFORMS[o["group"].split()[1]]["model"] == target["model_name"]]
        check_id, _ = db.add_check(conn, context["run_id"], target["target_id"], "target", captured,
                                   "public" if found else "unknown", "success" if found else "not_found", True, key,
                                   reason=None if found else "spot-price/list-series에 대상 플랫폼 없음")
        db.add_evidence(conn, check_id, target["source_id"], "official", SERIES_URL, None,
                        {"scope": nebius.scope(target), "start": start.isoformat(), "granularity": "GRANULARITY_HOUR_1",
                         "method": "METHOD_LAST_PRICE", "series": len(found),
                         "points_added": sum(o["added"] for o in found)}, captured)
    conn.commit()
    return outcomes


fetch_series = nebius.fetch_series      # 매일 수집과 같은 시계열 조회


def _write_series(conn, context, target, item, platform, captured):
    if str(item.get("currency", "")).lower() != "usd" or item.get("unitTitle") != "GPU hour":
        raise nebius.SourceInvalid(f"unexpected_unit: {item.get('currency')} {item.get('unitTitle')}")
    points = item.get("points")
    if not isinstance(points, list):
        raise nebius.SourceInvalid("series_points_missing")
    run_id, counts = context["run_id"], {"added": 0, "skipped": 0}
    for preset in nebius.PLATFORMS[platform]["presets"]:
        product = nebius.make_product(target, platform, preset, item.get("region"))
        offer = {"contract_type": "spot", "contract_product_type": nebius.OFFER_BY_KIND["spot"][1],
                 "commitment_months": None, "payment_option": None}
        product_id = offer_id = None
        prefix = f"series:{target['target_id']}:{product['original_product_name']}@{product['region_code']}:"
        existing = {row[0] for row in conn.execute(
            "SELECT deduplication_key FROM collection_check WHERE deduplication_key LIKE %s", (prefix + "%",))}
        for point in sorted(points, key=lambda p: p.get("timestamp") or ""):
            at = nebius._timestamp(point.get("timestamp"))
            key = f"{prefix}{point.get('timestamp')}"
            if at is None or key in existing:
                counts["skipped"] += 1
                continue
            if at > captured:            # 끝나지 않은 시간 구간의 값은 아직 바뀔 수 있어 다음에 적재한다
                counts["skipped"] += 1
                continue
            amount = nebius._decimal(point.get("value"), (item.get("skuId"), point.get("timestamp")))
            if product_id is None:
                product_id = db.history_product(conn, product, at)
                offer_id = db.ensure_offer(conn, product_id, offer)
            if db.observation_at(conn, offer_id, at):     # 매일 수집이 이미 적재한 기록
                counts["skipped"] += 1
                continue
            raw = (f"{point.get('value')} {item.get('currency')} / {item.get('unitTitle')} ({item.get('productTitle')}, "
                   f"{item.get('region')}, 1시간 간격 마지막 가격, {point.get('timestamp')})")
            hit = nebius.Hit("spot", product, offer,
                             {"effective_from": None, "price_certainty": "firm", "raw_price_text": raw,
                              "original_currency": "USD", "original_time_unit": "hour", "price_applies_to": "gpu",
                              "raw_terms": nebius.RAW_TERMS["spot"] + "; spot-price/list-series 1시간 간격의 마지막 가격"},
                             [{"component_type": "hourly_rate", "amount": amount, "currency": "USD", "time_unit": "hour",
                               "applies_to": "gpu", "raw_value": raw}],
                             {"platform": platform, "preset": preset[0], "region": item.get("region"),
                              "skuId": item.get("skuId"), "timestamp": point.get("timestamp")}, SERIES_URL, None)
            check_id, _ = db.add_check(conn, run_id, target["target_id"], "offer", at, "public", "success", True, key,
                                       product_id=product_id, offer_id=offer_id)
            nebius.ensure_spec_evidence(conn, context, target, product_id, hit, captured)
            db.add_evidence(conn, check_id, target["source_id"], "official", SERIES_URL, None, hit.excerpt, captured)
            nebius.write_observation(conn, context, offer_id, check_id, hit, at, captured)
            counts["added"] += 1
    return counts


# 문서 Wayback 저장본 (집계) -----------------------------------------------------------

def docs_captures(conn, context, targets, get_json, get_text, pause, verify, log):
    verified = None
    if verify:
        try:
            current = nebius.parse_pricing(get_text(nebius.PRICING_URL, nebius.REQUEST_CONFIG), context["now"]())
            verified = {"keys": _price_keys(current), "at": context["now"]()}
        except nebius.EXPECTED_ERRORS as error:
            log(f"원천 확인 안 함: {type(error).__name__}: {error}")
    body = get_json(CDX_URL.format(url=ARCHIVED_URL), REQUEST_CONFIG)
    if not isinstance(body, list):
        raise nebius.SourceInvalid("unexpected_cdx_format")
    header = body[0] if body else []
    captures = sorted((dict(zip(header, row)) for row in body[1:]), key=lambda c: c["timestamp"])
    jobs = [c for c in captures if not all(db.check_exists(conn, _capture_key(t, c)) for t in targets)]
    log(f"문서 저장본 {len(jobs)}개 처리 예정. 이미 적재한 저장본은 건너뜀")
    outcomes = []
    for index, capture in enumerate(jobs):
        if index:
            pause(nebius.SECONDS_BETWEEN_REQUESTS)
        at = datetime.strptime(capture["timestamp"], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        label = f"docs {capture['timestamp']}"
        try:
            sections = nebius.parse_pricing(get_text(RAW_URL.format(**capture), REQUEST_CONFIG), at)
            version = f"wayback {capture['timestamp']} digest:{capture.get('digest')}"
            results = nebius.docs_results(targets, sections, VIEW_URL.format(**capture), version,
                                          f"문서 저장본 {capture['timestamp']}")
            counts = _write_capture(conn, context, targets, results, capture, at, verified)
            conn.commit()
            outcomes.append({"group": label, "status": "success", **counts})
        except nebius.EXPECTED_ERRORS + (db.HistoryOrderError,) as error:
            processing = "validation_failed" if isinstance(error, (nebius.SourceInvalid, db.HistoryOrderError)) \
                else "collection_failed"
            nebius.record_failure(conn, context, targets, processing, f"{type(error).__name__}: {error}",
                                  key=lambda t: f"docs-failed:{context['run_id']}:{t['target_id']}:{capture['timestamp']}")
            outcomes.append({"group": label, "status": processing, "reason": f"{type(error).__name__}: {error}"})
        log(_line(outcomes[-1]))
    return outcomes


def _write_capture(conn, context, targets, results, capture, at, verified):
    run_id, captured = context["run_id"], context["now"]()
    counts = {"added": 0, "verified": 0, "unverified": 0, "skipped": 0}
    for target in targets:
        key = _capture_key(target, capture)
        if db.check_exists(conn, key):
            counts["skipped"] += 1
            continue
        result = results[target["target_id"]]
        for hit in result.hits:
            product_id = db.history_product(conn, hit.product, at)
            offer_id = db.ensure_offer(conn, product_id, hit.offer)
            check_id, _ = db.add_check(conn, run_id, target["target_id"], "offer", at, "public", "success", True,
                                       f"{key}:offer:{offer_id}", product_id=product_id, offer_id=offer_id)
            nebius.ensure_spec_evidence(conn, context, target, product_id, hit, captured)
            primary = verified_at = None
            if verified is not None:
                primary = _hit_key(hit) in verified["keys"]
                verified_at = verified["at"] if primary else None
                counts["verified" if primary else "unverified"] += 1
            db.add_evidence(conn, check_id, target["source_id"], "aggregator", hit.source_url, hit.source_version,
                            {**hit.excerpt, "primary_source": nebius.PRICING_URL}, captured,
                            primary_verified=primary, verified_at=verified_at)
            nebius.write_observation(conn, context, offer_id, check_id, hit, at, captured)
            counts["added"] += 1
        found = bool(result.hits) or result.disclosure is not None
        check_id, _ = db.add_check(conn, run_id, target["target_id"], "target", at,
                                   result.disclosure or ("public" if result.hits else "unknown"),
                                   "success" if found else "not_found", True, key, reason=result.reason)
        db.add_evidence(conn, check_id, target["source_id"], "aggregator", result.source_url, result.source_version,
                        {"scope": nebius.scope(target), "kind": "aggregator_history",
                         "primary_source": nebius.PRICING_URL, "offers_found": len(result.hits), **result.note},
                        captured)
    return counts


def _price_keys(sections):
    keys = set()
    for platform, section in sections.items():
        for kind, name in (("on_demand", "on_demand"), ("spot", "spot_fixed")):
            if section[name] is not None:
                for region in section["regions"]:
                    keys.add((platform, region, kind, format(section[name]["amount"].normalize(), "f")))
    return keys


def _hit_key(hit):
    return (hit.excerpt["platform"], hit.excerpt["region"], hit.kind,
            format(hit.components[0]["amount"].normalize(), "f"))


def _capture_key(target, capture):
    return f"wayback:{target['target_id']}:{capture['timestamp']}"


def _line(outcome):
    if outcome["status"] != "success":
        return f"{outcome['group']}: {outcome['status']} {outcome.get('reason', '')}"
    extra = (f" (원천 확인 {outcome['verified']} / 미확인 {outcome['unverified']})" if "verified" in outcome else "")
    return f"{outcome['group']}: success 추가 {outcome['added']} 건너뜀 {outcome['skipped']}{extra}"
