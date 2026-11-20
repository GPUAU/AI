"""Oracle 과거 정가 → ERD 초안 스키마 적재 (집계 출처: SkyPilot 카탈로그의 OCI vms.csv 커밋 이력).

| 출처(data_source.source_name) | 원천 | 시점 |
| oracle_skypilot_catalog | github.com/skypilot-org/skypilot-catalog catalogs/v5~v8/oci/vms.csv | 파일이 바뀐 커밋마다(불규칙) |

SkyPilot의 OCI 수집기(skypilot-org/skypilot sky/catalog/data_fetchers/fetch_oci.py)는 OCI 공개 정가 API
(apexapps.oracle.com/.../cetools/api/v1/products/)의 PAY_AS_YOU_GO 가격으로 vms.csv를 만든다. 정가 API 저장본(Wayback)이
없는 2026-05 이전 시점을 보완한다. 카탈로그 저장소에 LICENSE 파일이 없어 이용 조건은 미확인으로 둔다.

- Price 열은 노드(베어메탈 구성) 시간당 USD다. GPU 수(AcceleratorCount)로 나눠 환산한다.
- OCI 가격은 리전마다 같다. 대상 구성의 리전별 가격이 서로 다르면 검증 실패로 남긴다.
- 관측 시각(collected_at)은 커밋 시각이다. 가격이 적용된 시각이 아니라 카탈로그에 반영된 시각이다.
- 같은 커밋이 여러 스키마 버전 파일을 바꿨으면 가장 높은 버전 하나만 쓴다. 같은 커밋은 다시 적재하지 않는다.
- 상품은 매일 수집이 만든 같은 구성(같은 베어메탈 구성) 행에 붙인다. 카탈로그 가격이 지금 공식 API 값과 같으면 원천 확인.
- 과거 자료로는 알림을 만들지 않는다.
"""
import csv
import io
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

from ... import fetch
from .. import db, runs
from . import collect as oracle, history

COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-oracle-skypilot 1"
SOURCE_SKYPILOT = "oracle_skypilot_catalog"
REPO = "skypilot-org/skypilot-catalog"
VERSIONS = ("v5", "v6", "v7", "v8")
PATH = "catalogs/{version}/oci/vms.csv"
COMMITS_URL = "https://api.github.com/repos/" + REPO + "/commits?path={path}&per_page=100"
RAW_URL = "https://raw.githubusercontent.com/" + REPO + "/{sha}/{path}"
VIEW_URL = "https://github.com/" + REPO + "/blob/{sha}/{path}"
REQUEST_CONFIG = {"connect_timeout_s": 20, "read_idle_timeout_s": 120}
SECONDS_BETWEEN_REQUESTS = 1
RAW_TERMS = ("SkyPilot 카탈로그 vms.csv의 Price(노드 시간당 USD, OCI 공개 정가 API 기반). 관측 시각은 카탈로그 커밋 시각이며 "
             "가격 적용 시각이 아님")


def backfill(conn, *, since, until=None, now=None, get_json=fetch.get_json, get_text=None, pause=time.sleep,
             verify=True, log=None):
    """since·until: 'YYYY-MM'(커밋 월). 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    log = log or (lambda message: print(message, file=sys.stderr, flush=True))
    get_text = get_text or _get_text
    until = until or now().strftime("%Y-%m")
    targets = [t for t in db.enabled_targets(conn, oracle.PROVIDER) if t["source_name"] == SOURCE_SKYPILOT]
    if not targets:
        raise LookupError("no enabled Oracle SkyPilot collection_target")
    context = {"provider": db.provider_id(conn, oracle.PROVIDER), "rule": db.rule(conn, oracle.RULE_VERSION),
               "now": now, "docs": oracle.docs_targets(conn),
               "docs_source": oracle.source_id(conn, oracle.SOURCE_SHAPES)}
    context["run_id"] = db.begin_run(conn, now(), "manual", COLLECTOR_VERSION)
    oracle.fill_known_specs(conn, context["provider"])       # 미확인 vCPU를 먼저 채워 구성 비교가 어긋나지 않게
    conn.commit()
    try:
        verified, verify_note = history.primary_prices(get_json, now) if verify else (None, "원천 확인 안 함(--no-verify)")
        log(f"원천 확인 기준: {verify_note}")
        commits = [c for c in list_commits(get_json, pause) if since <= c["month"] <= until]
        jobs = [c for c in commits if not all(db.check_exists(conn, _target_key(t, c)) for t in targets)]
        log(f"카탈로그 커밋 {len(jobs)}개 처리 예정 ({since}~{until}). 이미 적재한 커밋은 건너뜀")
        outcomes = []
        for index, commit in enumerate(jobs):
            if index:
                pause(SECONDS_BETWEEN_REQUESTS)
            outcomes.append(_process(conn, context, targets, commit, get_text, verified))
            log(_line(outcomes[-1]))
    except Exception as error:
        conn.rollback()
        db.finish_run(conn, context["run_id"], now(), "failed", f"unexpected: {type(error).__name__}")
        conn.commit()
        raise
    ok = sum(1 for outcome in outcomes if outcome["status"] == "success")
    status = "success" if ok == len(outcomes) else ("failed" if ok == 0 else "partial_failure")
    failed = [f"{o['sha'][:12]}: {o['reason']}" for o in outcomes if o["status"] != "success"]
    db.finish_run(conn, context["run_id"], now(), status, "; ".join(failed)[:2000] or None)
    conn.commit()
    return {"run_id": context["run_id"], "status": status, "groups": outcomes}


# 커밋 목록 ---------------------------------------------------------------------

def list_commits(get_json, pause):
    """[{sha, at, month, path, version}] 시간 순. 같은 커밋이 여러 버전 파일을 바꿨으면 가장 높은 버전만 남긴다."""
    found = {}
    for index, version in enumerate(VERSIONS):
        if index:
            pause(SECONDS_BETWEEN_REQUESTS)
        path = PATH.format(version=version)
        body = get_json(COMMITS_URL.format(path=path), REQUEST_CONFIG)
        if not isinstance(body, list):
            raise oracle.SourceInvalid("unexpected_github_commits_format")
        for row in body:
            sha = row.get("sha") if isinstance(row, dict) else None
            stamp = ((row.get("commit") or {}).get("committer") or {}).get("date") if sha else None
            if not sha or not stamp:
                continue
            at = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)
            found[sha] = {"sha": sha, "at": at, "month": f"{at:%Y-%m}", "path": path, "version": version}
    return sorted(found.values(), key=lambda c: (c["at"], c["sha"]))


def _get_text(url, config):
    timeout = (config.get("connect_timeout_s", 15), config.get("read_idle_timeout_s", 60))
    try:
        response = fetch.send(lambda: requests.get(url, headers={"User-Agent": fetch.USER_AGENT}, timeout=timeout))
    except requests.RequestException as error:
        raise fetch.FetchError(f"request_failed: {type(error).__name__}") from error
    if response.status_code != 200:
        raise fetch.FetchError(f"http_{response.status_code}")
    return response.text


def read_catalog(text):
    """vms.csv → {베어메탈 구성: (GPU 수, 노드 시간당 USD)}. 대상 구성만, 리전별 가격이 다르면 검증 실패."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not {"InstanceType", "AcceleratorCount", "Price"} <= set(reader.fieldnames):
        raise oracle.SourceInvalid("unexpected_catalog_columns")
    shapes = {spec["shape"] for spec in oracle.SHAPES.values()}
    seen = {}
    for row in reader:
        shape = row.get("InstanceType")
        if shape not in shapes or not (row.get("Price") or "").strip():
            continue
        try:
            price = Decimal(row["Price"].strip())
            gpus = int(Decimal(row["AcceleratorCount"]))
        except (InvalidOperation, ValueError, TypeError):
            raise oracle.SourceInvalid(f"invalid_catalog_row: {shape} {row.get('Price')!r}") from None
        if price <= 0 or gpus <= 0:
            raise oracle.SourceInvalid(f"non_positive_catalog_row: {shape} {price} {gpus}")
        seen.setdefault(shape, set()).add((gpus, price))
    result = {}
    for shape, values in seen.items():
        if len(values) != 1:
            raise oracle.SourceInvalid(f"region_prices_differ: {shape} {sorted(values)}")
        result[shape] = next(iter(values))
    return result


# 처리 ------------------------------------------------------------------------

def _process(conn, context, targets, commit, get_text, verified):
    outcome = {"sha": commit["sha"], "path": commit["path"], "at": commit["at"].isoformat()}
    try:
        catalog = read_catalog(get_text(RAW_URL.format(**commit), REQUEST_CONFIG))
        counts = _write(conn, context, targets, commit, catalog, verified)
        conn.commit()
        outcome.update(status="success", **counts)
    except oracle.EXPECTED_ERRORS as error:
        processing = "validation_failed" if isinstance(error, (oracle.SourceInvalid, db.HistoryOrderError)) \
            else "collection_failed"
        runs.record_failure(conn, context, targets, processing, f"{type(error).__name__}: {error}",
                            key=lambda t: f"skypilot-failed:{context['run_id']}:{t['target_id']}:{commit['sha'][:12]}")
        outcome.update(status=processing, reason=f"{type(error).__name__}: {error}")
    return outcome


def existing_spec(conn, target, shape):
    """매일 수집이 만든 같은 베어메탈 구성의 최신 상품 값. 없으면 None(품목 정보 없이 상품을 만들면 이후 매일 수집과
    구성이 달라져 새 구성 버전·config_changed가 생기므로 만들지 않는다)."""
    columns = db.PRODUCT_IDENTITY + db.CONFIG_FIELDS
    row = conn.execute(
        f"""SELECT {", ".join(columns)} FROM product
            WHERE provider_id = %s AND gpu_model_id = %s AND original_product_name = %s AND region_code IS NULL
              AND system_type = 'node' AND service_option = 'standard'
            ORDER BY configuration_version DESC LIMIT 1""",
        (target["provider_id"], target["gpu_model_id"], shape)).fetchone()
    return dict(zip(columns, row)) if row else None


def make_hit(target, spec, gpus, price, commit):
    raw = f"{price} USD / hour ({spec['original_product_name']}, {gpus} GPU, SkyPilot {commit['path']} @{commit['sha'][:12]})"
    offer = {"contract_type": "on_demand", "contract_product_type": "Pay As You Go", "commitment_months": None,
             "payment_option": None}
    observation = {"effective_from": None, "price_certainty": "firm", "raw_price_text": raw,
                   "original_currency": "USD", "original_time_unit": "hour", "price_applies_to": "instance",
                   "raw_terms": RAW_TERMS}
    components = [{"component_type": "hourly_rate", "amount": price, "currency": "USD", "time_unit": "hour",
                   "applies_to": "instance", "raw_value": raw}]
    excerpt = {"InstanceType": spec["original_product_name"], "AcceleratorCount": gpus, "Price": str(price),
               "commit": commit["sha"], "path": commit["path"]}
    return oracle.Hit(spec, offer, observation, components, excerpt)


def _write(conn, context, targets, commit, catalog, verified):
    run_id, captured, as_of = context["run_id"], context["now"](), commit["at"]
    view_url = VIEW_URL.format(**commit)
    version = f"skypilot-catalog {commit['sha'][:12]} {commit['path']}"
    counts = {"targets": 0, "offers": 0, "verified": 0, "unverified": 0, "skipped": 0, "not_found": 0}
    for target in targets:
        key = _target_key(target, commit)
        if db.check_exists(conn, key):
            counts["skipped"] += 1
            continue
        counts["targets"] += 1
        shape = oracle.SHAPES[target["model_name"]]["shape"]
        found = catalog.get(shape)
        if found is not None:
            gpus, price = found
            spec = existing_spec(conn, target, shape)
            if spec is None:
                raise oracle.SourceInvalid(f"no_daily_product: {shape} 상품이 없음. collect oracle을 먼저 실행")
            if gpus != spec["gpu_count"]:
                raise oracle.SourceInvalid(f"gpu_count_mismatch: {shape} catalog {gpus} vs product {spec['gpu_count']}")
            hit = make_hit(target, spec, gpus, price, commit)
            product_id = db.history_product(conn, spec, as_of)
            offer_id = db.ensure_offer(conn, product_id, hit.offer)
            check_id, _ = db.add_check(conn, run_id, target["target_id"], "offer", as_of, "public", "success", True,
                                       f"{key}:offer:{offer_id}", product_id=product_id, offer_id=offer_id)
            oracle.ensure_spec_evidence(conn, context, target, product_id, captured)
            primary, verified_at = None, None
            if verified is not None:
                current = verified["prices"].get(target["model_name"])
                primary = current is not None and current[1] == price / gpus
                verified_at = verified["at"] if primary else None
                counts["verified" if primary else "unverified"] += 1
            db.add_evidence(conn, check_id, target["source_id"], "aggregator", view_url, version,
                            {**hit.excerpt, "primary_source": oracle.API_URL}, captured,
                            primary_verified=primary, verified_at=verified_at)
            oracle.write_observation(conn, context, offer_id, check_id, hit, as_of, captured)
            counts["offers"] += 1
        else:
            counts["not_found"] += 1
        check_id, _ = db.add_check(conn, run_id, target["target_id"], "target", as_of,
                                   "public" if found else "unknown", "success" if found else "not_found", True, key,
                                   reason=None if found else f"이 커밋의 {commit['path']}에 {shape} 없음")
        db.add_evidence(conn, check_id, target["source_id"], "aggregator", view_url, version,
                        {"scope": oracle.scope(target), "kind": "aggregator_history", "primary_source": oracle.API_URL,
                         "shape": shape, "offers_found": 1 if found else 0}, captured)
    return counts


def _target_key(target, commit):
    return f"skypilot:{target['target_id']}:{commit['sha'][:12]}"


def _line(outcome):
    if outcome["status"] != "success":
        return f"{outcome['sha'][:12]} {outcome['path']}: {outcome['status']} {outcome.get('reason', '')}"
    return (f"{outcome['at'][:10]} {outcome['sha'][:12]} {outcome['path']}: success 대상 {outcome['targets']} "
            f"오퍼 {outcome['offers']} (원천 확인 {outcome['verified']} / 미확인 {outcome['unverified']}) "
            f"미발견 {outcome['not_found']} 건너뜀 {outcome['skipped']}")
