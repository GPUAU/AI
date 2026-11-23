"""Azure 과거 가격 → ERD 초안 스키마 적재 (집계 출처: GitHub에 매일 저장된 Retail Prices API 스냅샷).

| 출처(data_source.source_name) | 원천 | 시점 단위 |
| azure_retail_prices_snapshot | sbroenne/azureretailprices-exporter 릴리스 CSV (Retail Prices API 전체를 매일 저장) | 2025-10-12부터 하루 1개(빠진 날 있음) |

Microsoft는 Retail Prices API의 과거 가격을 제공하지 않는다. 이 저장소가 매일 받아 둔 CSV가 유일한 과거 자료다.

- 집계 출처라 근거 역할은 aggregator다. 원천 확인(primary_verified)은 적재 뒤 erd/verify.py가 판정한다
  (그 무렵 매일 수집, 또는 적용 시작일이 스냅샷 이전인 매일 수집과 같은지).
- 관측 시각(collected_at)은 릴리스 공개 시각, 적용 시각(effective_from)은 행의 effectiveStartDate다.
  실제로 받은 시각은 근거의 captured_at과 실행 기록에 남는다.
- 행 선택·검증·환산은 매일 수집(azure/collect.py)과 같다. 스냅샷에도 이상값(예: 2026-05-03의 10년 예약)이 있어 같은
  검증으로 거른다. 빠진 날·빠진 행은 채우지 않는다(미확보).
- 기본은 달마다 첫 스냅샷이다(--every month). 같은 스냅샷은 다시 적재하지 않는다. 과거 자료로는 알림을 만들지 않는다.
- 오래된 스냅샷부터 처리해 상품 구성 버전이 시간 순서를 따르게 한다.
"""
import csv
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from ... import fetch
from .. import db, verify as verifier
from . import collect as azure

COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-azure-snapshot 1"
SOURCE_SNAPSHOT = "azure_retail_prices_snapshot"
REPO = "sbroenne/azureretailprices-exporter"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases?per_page=100&page={{page}}"
ASSET = "azure-retail-prices-usd.csv"
DOWNLOAD_CONFIG = {"connect_timeout_s": 15, "read_idle_timeout_s": 120, "total_timeout_s": 1800}
MAX_RELEASE_PAGES = 20


def backfill(conn, *, since, until=None, every="month", cache_dir="var/cache", keep_files=False, now=None,
             get_json=fetch.get_json, downloader=fetch.download, verify=True, log=None):
    """since·until: 'YYYY-MM' 또는 'YYYY-MM-DD'(스냅샷 날짜). every: month(달마다 첫 스냅샷) / week / day. 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    log = log or (lambda message: print(message, file=sys.stderr, flush=True))
    until = until or now().strftime("%Y-%m")
    targets = [t for t in db.enabled_targets(conn, azure.PROVIDER) if t["source_name"] == SOURCE_SNAPSHOT]
    if not targets:
        raise LookupError("no enabled Azure snapshot collection_target (sql/erd_seed.sql)")
    context = {"provider": db.provider_id(conn, azure.PROVIDER), "rule": db.rule(conn, azure.RULE_VERSION),
               "now": now, "docs": azure.docs_targets(conn), "docs_source": azure.source_id(conn, azure.SOURCE_DOCS),
               "cache_dir": Path(cache_dir) / "azure" / "snapshot", "downloader": downloader, "keep_files": keep_files}
    context["run_id"] = db.begin_run(conn, now(), "manual", COLLECTOR_VERSION)
    conn.commit()
    context["cache_dir"].mkdir(parents=True, exist_ok=True)

    releases = pick(list_releases(get_json), since, until, every)
    jobs = [r for r in releases if not all(db.check_exists(conn, _target_key(t, r["tag"])) for t in targets)]
    log(f"스냅샷 {len(jobs)}개 처리 예정 ({since}~{until}, {every}). 이미 적재한 스냅샷은 건너뜀")

    outcomes = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending, queued = deque(), iter(jobs)

        def fill():
            while len(pending) < 2:
                job = next(queued, None)
                if job is None:
                    return
                pending.append((job, pool.submit(_fetch, context, job)))

        fill()
        while pending:
            job, future = pending.popleft()
            fill()
            outcomes.append(_process(conn, context, targets, job, future))
            log(_line(outcomes[-1]))

    ok = sum(1 for outcome in outcomes if outcome["status"] == "success")
    status = "success" if ok == len(outcomes) else ("failed" if ok == 0 else "partial_failure")
    failed = [f"{o['tag']}: {o['reason']}" for o in outcomes if o["status"] != "success"]
    checked = verifier.reverify(conn, sources={SOURCE_SNAPSHOT}) if verify else None
    db.finish_run(conn, context["run_id"], now(), status, "; ".join(failed)[:2000] or None)
    conn.commit()
    return {"run_id": context["run_id"], "status": status, "verification": checked, "groups": outcomes}


# 스냅샷 고르기 -------------------------------------------------------------------

def list_releases(get_json):
    """[{tag, date, published, url, size}] 날짜 순. 날짜 릴리스(prices-YYYY-MM-DD)만 쓴다(latest 제외)."""
    found = []
    for page in range(1, MAX_RELEASE_PAGES + 1):
        body = get_json(RELEASES_API.format(page=page), azure.REQUEST_CONFIG)
        if not isinstance(body, list):
            raise azure.SourceInvalid("unexpected_releases_format")
        if not body:
            return sorted(found, key=lambda r: r["date"])
        for release in body:
            tag = release.get("tag_name") or ""
            asset = next((a for a in release.get("assets") or [] if a.get("name") == ASSET), None)
            if not tag.startswith("prices-") or asset is None:
                continue
            found.append({"tag": tag, "date": tag.removeprefix("prices-"),
                          "published": azure._timestamp(release.get("published_at")),
                          "url": asset.get("browser_download_url"), "size": asset.get("size")})
    raise azure.SourceInvalid(f"release_page_limit_exceeded: {MAX_RELEASE_PAGES}")


def pick(releases, since, until, every):
    """since·until: 'YYYY-MM' 또는 'YYYY-MM-DD'(그 날짜까지 포함)."""
    chosen, seen = [], set()
    for release in releases:
        if not since <= release["date"][:len(since)] or not release["date"][:len(until)] <= until:
            continue
        day = datetime.strptime(release["date"], "%Y-%m-%d")
        bucket = {"month": release["date"][:7], "week": tuple(day.isocalendar()[:2]), "day": release["date"]}[every]
        if bucket not in seen:
            seen.add(bucket)
            chosen.append(release)
    return chosen


# 처리 ------------------------------------------------------------------------

def _fetch(context, job):
    """작업 스레드에서 실행. 반환: (받은 파일, 이전부터 있던 파일인지)."""
    target = context["cache_dir"] / f"{job['tag']}.csv"
    existed = target.exists() and target.stat().st_size > 0 and (not job["size"] or target.stat().st_size == job["size"])
    if existed:
        return fetch.FetchResult(url=job["url"], path=target, sha256="", size=target.stat().st_size), True
    return context["downloader"](job["url"], target, DOWNLOAD_CONFIG), False


def read_rows(path):
    """대상 SKU 행과 등재 확인용 행만 남긴다(파일 전체는 수백 MB)."""
    keep = []
    with open(path, newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            if row.get("serviceName") != "Virtual Machines":
                continue
            if row.get("armSkuName") in azure.MODEL_BY_SKU or any(match(row) for match in azure.DETECT.values()):
                keep.append(row)
    return keep


def _process(conn, context, targets, job, future):
    outcome = {"tag": job["tag"], "published": job["published"].isoformat() if job["published"] else None}
    fetched, existed = None, True
    try:
        fetched, existed = future.result()
        rows = read_rows(fetched.path)
        as_of = job["published"] or datetime.strptime(job["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        price_rows = [r for r in rows if r.get("armSkuName") in azure.MODEL_BY_SKU]
        version = f"{job['tag']} sha256:{fetched.sha256[:16]}" if fetched.sha256 else job["tag"]
        results = azure.target_results(targets, price_rows, rows, as_of, job["url"], job["url"], version,
                                       f"스냅샷 {job['date']}")
        counts = _write(conn, context, targets, results, as_of, job["tag"])
        conn.commit()
        outcome.update(status="success", **counts)
    except azure.EXPECTED_ERRORS as error:
        processing = "validation_failed" if isinstance(error, (azure.SourceInvalid, db.HistoryOrderError)) \
            else "collection_failed"
        azure.record_failure(conn, context, targets, processing, f"{type(error).__name__}: {error}",
                             key=lambda t: f"snapshot-failed:{context['run_id']}:{t['target_id']}:{job['tag']}")
        outcome.update(status=processing, reason=f"{type(error).__name__}: {error}")
    except Exception as error:
        conn.rollback()
        db.finish_run(conn, context["run_id"], context["now"](), "failed", f"unexpected: {type(error).__name__}")
        conn.commit()
        raise
    finally:
        if fetched is not None and not context["keep_files"] and not existed:
            Path(fetched.path).unlink(missing_ok=True)
    return outcome


def _write(conn, context, targets, results, as_of, tag):
    run_id, captured = context["run_id"], context["now"]()
    counts = {"targets": 0, "offers": 0, "skipped": 0, "not_found": 0}
    for target in targets:
        key = _target_key(target, tag)
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
                raise azure.SourceInvalid(f"duplicate_offer_in_snapshot: offer {offer_id}")
            seen.add(offer_id)
            check_id, _ = db.add_check(conn, run_id, target["target_id"], "offer", as_of, "public", "success", True,
                                       f"{key}:offer:{offer_id}", product_id=product_id, offer_id=offer_id)
            azure.ensure_spec_evidence(conn, context, target, product_id, captured)
            db.add_evidence(conn, check_id, target["source_id"], "aggregator", hit.source_url, hit.source_version,
                            {**hit.excerpt, "primary_source": azure.API_URL}, captured)
            azure.write_observation(conn, context, offer_id, check_id, hit, as_of, captured)
        counts["offers"] += len(result.hits)
        found = bool(result.hits) or result.disclosure is not None
        if not found:
            counts["not_found"] += 1
        check_id, _ = db.add_check(conn, run_id, target["target_id"], "target", as_of,
                                   result.disclosure or ("public" if result.hits else "unknown"),
                                   "success" if found else "not_found", True, key, reason=result.reason)
        db.add_evidence(conn, check_id, target["source_id"], "aggregator", result.source_url, result.source_version,
                        {"scope": azure.scope(target), "kind": "aggregator_history", "primary_source": azure.API_URL,
                         "offers_found": len(result.hits), **result.note}, captured)
    return counts


def _target_key(target, tag):
    return f"snapshot:{target['target_id']}:{tag}"


def _line(outcome):
    if outcome["status"] != "success":
        return f"{outcome['tag']}: {outcome['status']} {outcome.get('reason', '')}"
    return (f"{outcome['tag']}: success 대상 {outcome['targets']} 오퍼 {outcome['offers']} "
            f"미발견 {outcome['not_found']} "
            f"건너뜀 {outcome['skipped']}")
