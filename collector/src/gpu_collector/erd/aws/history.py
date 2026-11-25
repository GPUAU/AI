"""AWS 공식 과거 자료 → ERD 초안 스키마 적재 (FR-56 '공식 과거 자료').

| 출처(data_source.source_name) | 원천 | 시점 단위 |
| aws_ec2_price_list_history | EC2 가격 파일 과거 버전 (offers/v1.0/aws/AmazonEC2/index.json) | AWS가 남긴 월별 버전 1개 |
| aws_savings_plans_price_list_history | Savings Plans 가격 파일 과거 버전 (…/AWSComputeSavingsPlan/current/index.json) | 달마다 마지막 버전 |
| aws_ec2_spot_price_history | DescribeSpotPriceHistory 기간 조회 | 가격이 바뀐 기록마다. AWS가 최근 90일만 준다 |

- 관측 시각(collected_at)과 점검 시각은 원천의 시각이다(가격 파일 버전의 공개 시각, 스팟 기록 시각).
  실제로 받은 시각은 근거의 captured_at과 실행 기록(collection_run)에 남는다.
- 자체 관측과 구분하도록 출처·수집 대상을 따로 둔다. 화면은 data_source로 출처 유형을 표시한다.
- 같은 버전·같은 스팟 기록은 다시 적재하지 않는다(중복 방지 키). 실패한 묶음은 다음 실행에서 다시 처리한다.
- 과거 자료로는 급변·구성 변경 알림을 만들지 않는다. 알림은 새 수집을 운영하기 위한 것이다.
- 오래된 버전부터 처리해 상품 구성 버전 번호가 시간 순서를 따르게 한다. 이미 적재된 것보다 앞선 자료인데 구성이
  어느 버전과도 다르면 적재하지 않고 검증 실패로 남긴다(db.history_product).
- 달마다 버전 하나만 읽으므로, 한 달 안에서 두 번 이상 바뀐 Savings Plans 요율은 마지막 것만 남는다.
"""
import sys
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ... import fetch
from ...download import cached_download
from .. import db
from . import collect as aws, spot as aws_spot

COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-aws-history 1"
SOURCE_EC2 = "aws_ec2_price_list_history"
SOURCE_SP = "aws_savings_plans_price_list_history"
SOURCE_SPOT = "aws_ec2_spot_price_history"
SOURCES = (SOURCE_EC2, SOURCE_SP, SOURCE_SPOT)
EC2_VERSIONS = f"{aws.PRICING_HOST}/offers/v1.0/aws/AmazonEC2/index.json"
SP_VERSIONS = f"{aws.PRICING_HOST}/savingsPlan/v1.0/aws/AWSComputeSavingsPlan/current/index.json"
SPOT_DAYS = 90
SPOT_MAX_PAGES = 200


@dataclass
class FileJob:
    source_name: str
    region: str
    month: str
    version: str
    published: datetime
    group: list
    url: str | None = None              # EC2: 리전 파일 주소
    region_index_url: str | None = None  # Savings Plans: 버전의 리전 목록 주소
    note: dict = field(default_factory=dict)


def backfill(conn, *, since, until=None, only=None, regions=None, spot_days=SPOT_DAYS, cache_dir="var/cache", keep_files=False,
             now=None, get_json=fetch.get_json, downloader=fetch.download, spot_history=aws_spot.price_history,
             workers=3, log=None):
    """since·until: 'YYYY-MM'(가격 파일 버전의 적용 월). only: 처리할 출처 이름 모음. regions: 처리할 리전 모음.

    반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    log = log or (lambda message: print(message, file=sys.stderr, flush=True))
    until = until or now().strftime("%Y-%m")
    targets = [t for t in db.enabled_targets(conn, aws.PROVIDER)
               if t["source_name"] in SOURCES and (only is None or t["source_name"] in only)
               and (regions is None or t["region_filter"] in regions)]
    if not targets:
        raise LookupError("no enabled AWS history collection_target (sql/erd_seed.sql)")
    context = {"provider": db.provider_id(conn, aws.PROVIDER), "rule": db.rule(conn, aws.RULE_VERSION), **aws.spec_context(conn),
               "now": now, "cache_dir": Path(cache_dir), "get_json": get_json, "downloader": downloader,
               "keep_files": keep_files}
    context["run_id"] = db.begin_run(conn, now(), "manual", COLLECTOR_VERSION)
    conn.commit()

    groups = OrderedDict()
    for target in sorted(targets, key=lambda t: (SOURCES.index(t["source_name"]), t["region_filter"] or "")):
        groups.setdefault((target["source_name"], target["region_filter"]), []).append(target)
    jobs = _file_jobs(conn, context, groups, since, until)
    log(f"과거 가격 파일 {len(jobs)}개 처리 예정 ({since}~{until}). 이미 적재한 버전은 건너뜀")

    outcomes = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        pending, queued = deque(), iter(jobs)

        def fill():
            while len(pending) < max(1, workers):
                job = next(queued, None)
                if job is None:
                    return
                pending.append((job, pool.submit(_fetch, context, job)))

        fill()
        while pending:
            job, future = pending.popleft()
            fill()
            outcomes.append(_process_file(conn, context, job, future))
            log(_line(outcomes[-1]))

    for (source_name, region), group in groups.items():
        if source_name == SOURCE_SPOT:
            outcomes.append(_process_spot(conn, context, region, group, spot_days, spot_history))
            log(_line(outcomes[-1]))

    ok = sum(1 for outcome in outcomes if outcome["status"] == "success")
    status = "success" if ok == len(outcomes) else ("failed" if ok == 0 else "partial_failure")
    failed = [f"{o['source']}@{o['region']} {o.get('version', '')}: {o['reason']}"
              for o in outcomes if o["status"] != "success"]
    db.finish_run(conn, context["run_id"], now(), status, "; ".join(failed)[:2000] or None)
    conn.commit()
    return {"run_id": context["run_id"], "status": status, "groups": outcomes}


# 버전 고르기 ---------------------------------------------------------------------

def ec2_monthly_versions(get_json, since, until):
    """[(적용 월, 버전)]. AWS는 EC2 가격 파일을 적용 월마다 하나씩 남긴다."""
    index = get_json(EC2_VERSIONS, aws.REQUEST_CONFIG)
    found = []
    for version, meta in (index.get("versions") or {}).items():
        month = (meta.get("versionEffectiveBeginDate") or "")[:7]
        if month and since <= month <= until:
            found.append((month, version))
    return sorted(found)


def sp_monthly_versions(get_json, since, until):
    """[(공개 월, 버전, 리전 목록 주소)]. 달마다 마지막으로 공개된 버전 하나."""
    index = get_json(SP_VERSIONS, aws.REQUEST_CONFIG)
    last = {}
    for entry in index.get("versions") or []:
        published, path = entry.get("publicationDate") or "", entry.get("offerVersionUrl") or ""
        month = published[:7]
        if not (month and path and since <= month <= until):
            continue
        if month not in last or published > last[month][0]:
            last[month] = (published, path)
    return [(month, _version_in(path), aws.PRICING_HOST + path) for month, (_published, path) in sorted(last.items())]


def _file_jobs(conn, context, groups, since, until):
    """적용 월 순서로, 같은 달에서는 EC2를 먼저(Savings Plans가 EC2 상품 구성을 이어받는다)."""
    regions = {name: [region for (source, region) in groups if source == name] for name in (SOURCE_EC2, SOURCE_SP)}
    by_month = {}
    if regions[SOURCE_EC2]:
        for month, version in ec2_monthly_versions(context["get_json"], since, until):
            for region in regions[SOURCE_EC2]:
                url = f"{aws.PRICING_HOST}/offers/v1.0/aws/AmazonEC2/{version}/{region}/index.json"
                by_month.setdefault(month, []).append(FileJob(SOURCE_EC2, region, month, version, _version_time(version),
                                                              groups[(SOURCE_EC2, region)], url=url))
    if regions[SOURCE_SP]:
        for month, version, region_index in sp_monthly_versions(context["get_json"], since, until):
            for region in regions[SOURCE_SP]:
                by_month.setdefault(month, []).append(FileJob(SOURCE_SP, region, month, version, _version_time(version),
                                                              groups[(SOURCE_SP, region)],
                                                              region_index_url=region_index))
    jobs = []
    for month in sorted(by_month):
        for job in sorted(by_month[month], key=lambda j: (SOURCES.index(j.source_name), j.region)):
            # 묶음의 모든 대상이 이 버전을 이미 적재했으면 받지 않는다
            if not all(db.check_exists(conn, _target_key(t, job.version)) for t in job.group):
                jobs.append(job)
    return jobs


# 처리 ------------------------------------------------------------------------

def _fetch(context, job):
    """작업 스레드에서 실행. 반환: (주소, 받은 파일, 이전부터 있던 파일인지)."""
    url = job.url
    if url is None:
        index = context["get_json"](job.region_index_url, aws.REQUEST_CONFIG)
        path = next((r.get("versionUrl") for r in index.get("regions") or [] if r.get("regionCode") == job.region),
                    None)
        if not path:
            raise fetch.FetchError(f"region_not_in_index: {job.region}")
        url = aws.PRICING_HOST + path
    target = context["cache_dir"] / "aws" / url.removeprefix(aws.PRICING_HOST).lstrip("/")
    existed = target.exists() and target.stat().st_size > 0
    return url, cached_download(url, target, aws.REQUEST_CONFIG, context["downloader"]), existed


def _process_file(conn, context, job, future):
    outcome = {"source": job.source_name, "region": job.region, "month": job.month, "version": job.version}
    fetched = None
    try:
        url, fetched, existed = future.result()
        if job.source_name == SOURCE_EC2:
            header, results = aws.ec2_results(job.group, job.region, fetched.path, url, job.version)
        else:
            def base_product(target, instance):
                return db.product_as_of(conn, context["provider"], target["gpu_model_id"], instance, job.region,
                                        job.published)
            header, results = aws.savings_results(job.group, job.region, fetched.path, url, job.version,
                                                  base_product)
        as_of = aws._timestamp(header.get("publicationDate")) or job.published
        counts = _write(conn, context, job.group, results, as_of,
                        target_key=lambda target: _target_key(target, job.version),
                        offer_key=lambda target, hit, offer_id: f"{_target_key(target, job.version)}:offer:{offer_id}",
                        hit_time=lambda hit: as_of, unique_offers=True)
        conn.commit()
        outcome.update(status="success", published=as_of.isoformat(), **counts)
    except aws.EXPECTED_ERRORS as error:
        processing = "validation_failed" if isinstance(error, (aws.SourceInvalid, db.HistoryOrderError)) \
            else "collection_failed"
        _record_failure(conn, context, job.group, processing, f"{type(error).__name__}: {error}", job.version)
        outcome.update(status=processing, reason=f"{type(error).__name__}: {error}")
    except Exception as error:
        _record_failure(conn, context, job.group, "collection_failed", f"unexpected: {type(error).__name__}: {error}",
                        job.version)
        db.finish_run(conn, context["run_id"], context["now"](), "failed", f"unexpected: {type(error).__name__}")
        conn.commit()
        raise
    finally:
        if fetched is not None and not context["keep_files"] and not existed:
            Path(fetched.path).unlink(missing_ok=True)
    return outcome


def _process_spot(conn, context, region, group, spot_days, spot_history):
    end = context["now"]()
    start = end - timedelta(days=spot_days)
    instances = {aws.INSTANCE_BY_MODEL[t["model_name"]] for t in group}
    outcome = {"source": SOURCE_SPOT, "region": region, "month": f"{start:%Y-%m-%d}~{end:%Y-%m-%d}",
               "version": aws_spot.API_VERSION}
    try:
        rows = spot_history(region, instances, start, end, max_pages=SPOT_MAX_PAGES)
        url = aws_spot.request_url(region, aws_spot.history_params(instances, start, end))
        results = {}
        for target in group:
            if target["contract_filter"] != "spot":
                raise aws.SourceInvalid(f"unsupported contract_filter for {SOURCE_SPOT}: {target['contract_filter']}")
            instance = aws.INSTANCE_BY_MODEL[target["model_name"]]
            result = aws.TargetResult(source_url=url, source_version=aws_spot.API_VERSION)
            own = sorted((r for r in rows if r["instance_type"] == instance),
                         key=lambda r: (r["timestamp"], r["availability_zone_id"] or r["availability_zone"] or ""))
            for row in own:
                if row["timestamp"] is None:
                    raise aws.SourceInvalid(f"spot_row_without_timestamp: {instance}")
                base = db.product_as_of(conn, context["provider"], target["gpu_model_id"], instance, region,
                                        row["timestamp"]) or {}
                result.hits.append(aws.spot_hit(target, base, row, region, url))
            if not result.hits:
                result.reason = f"{region} 스팟 가격 API에 {start:%Y-%m-%d}~{end:%Y-%m-%d} {instance} Linux 기록 없음"
            results[target["target_id"]] = result
        counts = _write(conn, context, group, results, end,
                        target_key=lambda target: f"history:{target['target_id']}:spot-run:{context['run_id']}",
                        offer_key=lambda target, hit, offer_id: (
                            f"history:{target['target_id']}:spot:{hit.observation['effective_from'].isoformat()}"
                            f":offer:{offer_id}"),
                        hit_time=lambda hit: hit.observation["effective_from"], unique_offers=False)
        conn.commit()
        outcome.update(status="success", **counts)
    except aws.EXPECTED_ERRORS as error:
        processing = "validation_failed" if isinstance(error, (aws.SourceInvalid, db.HistoryOrderError)) \
            else "collection_failed"
        _record_failure(conn, context, group, processing, f"{type(error).__name__}: {error}", f"spot:{end:%Y%m%d}")
        outcome.update(status=processing, reason=f"{type(error).__name__}: {error}")
    return outcome


# 적재 ------------------------------------------------------------------------

def _write(conn, context, group, results, checked_at, *, target_key, offer_key, hit_time, unique_offers):
    run_id, rule, captured = context["run_id"], context["rule"], context["now"]()
    counts = {"targets": 0, "offers": 0, "skipped": 0, "not_found": 0}
    for target in group:
        key = target_key(target)
        if db.check_exists(conn, key):
            counts["skipped"] += 1
            continue
        counts["targets"] += 1
        result = results[target["target_id"]]
        seen, loaded = set(), 0
        for hit in result.hits:
            at = hit_time(hit)
            if "product_id" in hit.product:
                product_id = hit.product["product_id"]
            else:
                product_id = db.history_product(conn, hit.product, at)
            offer_id = db.ensure_offer(conn, product_id, hit.offer)
            if not unique_offers and db.observation_at(conn, offer_id, at):
                seen.add(offer_id)              # 매일 수집이 이미 적재한 스팟 기록
                continue
            if unique_offers and offer_id in seen:
                raise aws.SourceInvalid(f"duplicate_offer_in_version: offer {offer_id}")
            seen.add(offer_id)
            check_id, created = db.add_check(conn, run_id, target["target_id"], "offer", at, "public", "success", True,
                                             offer_key(target, hit, offer_id), product_id=product_id,
                                             offer_id=offer_id)
            if not created:          # 이전 실행에서 적재한 스팟 기록
                continue
            loaded += 1
            aws.ensure_spec_evidence(conn, context, product_id, captured)
            db.add_evidence(conn, check_id, target["source_id"], "official", hit.source_url, hit.source_version,
                            hit.excerpt, captured)
            observation_id = db.add_observation(conn, offer_id, check_id, {**hit.observation, "collected_at": at})
            component_ids = [db.add_component(conn, observation_id, component) for component in hit.components]
            normalized = aws.normalize(hit.kind, hit.offer, hit.components, component_ids,
                                       hit.product.get("gpu_count"), rule["monthly_hours"])
            db.add_normalization(conn, observation_id, rule["rule_id"], normalized, captured)
        counts["offers"] += loaded
        found = bool(result.hits) or result.disclosure is not None
        if not found:
            counts["not_found"] += 1
        check_id, _ = db.add_check(conn, run_id, target["target_id"], "target", checked_at,
                                   result.disclosure or ("public" if result.hits else "unknown"),
                                   "success" if found else "not_found", True, key, reason=result.reason)
        db.add_evidence(conn, check_id, target["source_id"], "official", result.source_url, result.source_version,
                        {"scope": {"model": target["model_name"], "region": target["region_filter"],
                                   "contract": target["contract_filter"], "scope_version": target["scope_version"]},
                         "kind": "official_history", "offers_found": len(result.hits), "offers_loaded": loaded},
                        captured)
    return counts


def _record_failure(conn, context, group, processing, reason, version):
    """묶음의 작업을 되돌리고 실패 점검과 알림을 남긴다. 실패 키에는 실행 번호를 넣어 다음 실행이 다시 처리한다."""
    conn.rollback()
    run_id, checked_at = context["run_id"], context["now"]()
    for target in group:
        db.add_check(conn, run_id, target["target_id"], "target", checked_at, "unknown", processing, False,
                     f"history-failed:{run_id}:{target['target_id']}:{version}", reason=reason[:1000])
    first = group[0]
    db.add_alert(conn, "collection_failed", checked_at,
                 f"collection_failed:history:{run_id}:{first['source_id']}:{first['region_filter']}:{version}",
                 run_id=run_id, source_id=first["source_id"],
                 reason=f"{first['source_name']}@{first['region_filter']} {version}: {reason}"[:2000])
    conn.commit()


# 공통 -------------------------------------------------------------------------

def _target_key(target, version):
    return f"history:{target['target_id']}:{version}"


def _version_in(path):
    return next((part for part in path.split("/") if part.isdigit() and len(part) >= 14), None)


def _version_time(version):
    return datetime.strptime(version[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def _line(outcome):
    detail = (f"대상 {outcome.get('targets', 0)} 오퍼 {outcome.get('offers', 0)} 미발견 {outcome.get('not_found', 0)}"
              f" 건너뜀 {outcome.get('skipped', 0)}" if outcome["status"] == "success" else outcome.get("reason", ""))
    return f"{outcome['month']} {outcome['source']} {outcome['region']} {outcome['version']}: {outcome['status']} {detail}"
