"""Hyperstack 과거 가격 → ERD 스키마 적재 (집계 출처: Wayback Machine에 저장된 가격 페이지).

| 출처(data_source.source_name) | 원천 | 시점 |
| hyperstack_gpu_pricing_wayback | web.archive.org 저장본: www.hyperstack.cloud/gpu-pricing | 저장된 날만(2023-11~) |

- 페이지 가격은 GPU 장당 시간가(USD)라 매일 수집(pricebook)과 같은 기준이다. 리전·구성이 없어 GPU 종류별
  리전 미상 상품(original_product_name = pricebook GPU 종류)으로 둔다.
- 형식: 2023~2024 초 '$ 4.30 per GPU', 2024~2025 초 '$ 3.44 per Hour'(+ 예약 'Starts from'), 2025-07~ 온디맨드·예약·스팟 표.
  온디맨드·스팟만 읽는다. 예약 'Starting from'·'Starts from'은 시작가라 읽지 않는다.
- 집계 출처라 근거 역할은 aggregator. 원천 확인(primary_verified)은 적재 뒤 erd/verify.py가 판정한다
  (같은 GPU 종류·계약의 그 무렵 매일 수집과 같은지, 과거 가격을 오늘 가격과 비교하지 않는다).
- 2024-02 이전 페이지는 vCPU·RAM·로컬 NVMe가 별도 과금(Add-ons)이라 그 사실을 raw_terms에 남긴다.
- 오래된 것부터 처리한다. 급변·구성 변경 알림은 만들지 않는다(저장본을 받지 못하면 수집 실패로는 기록한다).
"""
import gzip
import html as html_lib
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests

from ... import fetch
from .. import db, runs, verify as verifier
from . import collect as hyperstack

COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-hyperstack-wayback 1"
SOURCE_WAYBACK = "hyperstack_gpu_pricing_wayback"
ARCHIVED_URL = "www.hyperstack.cloud/gpu-pricing"
CDX_URL = ("https://web.archive.org/cdx/search/cdx?url={url}&output=json&filter=statuscode:200"
           "&fl=timestamp,original,digest&collapse=digest")
RAW_URL = "https://web.archive.org/web/{timestamp}id_/{original}"
VIEW_URL = "https://web.archive.org/web/{timestamp}/{original}"
REQUEST_CONFIG = {"connect_timeout_s": 20, "read_idle_timeout_s": 180}
SECONDS_BETWEEN_REQUESTS = 6

# 온디맨드 행: 이름 | VRAM | pCPU | RAM | $ 가격 (per GPU / per Hour / 없음)
ON_DEMAND_ROW = re.compile(r"(?:^| \| )(?P<label>[A-Za-z][^|]{0,45}?) \| (?P<vram>\d+) \| (?P<cpu>\d+) \| (?P<ram>\d+) \| "
                           r"\$ ?(?:\| )?(?P<price>\d+(?:\.\d+)?)")
SPOT_SECTION = re.compile(r"Spot VM \| Pricing \| (?P<body>.*?)(?= \| On-Demand CPU| \| Storage \| Pricing| \| Spot VM \| Pricing|$)")
SPOT_ROW = re.compile(r"(?:^| \| )(?P<label>[A-Za-z][^|$]{1,40}?) \| \$ ?(?P<price>\d+(?:\.\d+)?)")
ADD_ONS = re.compile(r"Add-ons \| Price \| vCPU \| \$ ?[\d.]+ per GPU/hour")
TARGET_LABEL = re.compile(r"H100|H200|B200|B300", re.I)
# 페이지 표기 → pricebook GPU 종류 ('NVIDIA '·'HGX '·' 80GB' 제거 후)
GPU_BY_LABEL = {
    "H100 PCIe": "H100-80G-PCIe", "H100": "H100-80G-PCIe",
    "H100 PCIe NVLink": "H100-80G-PCIe-NVLink", "H100 NVLink": "H100-80G-PCIe-NVLink",
    "H100 SXM": "H100-80G-SXM5", "SXM5 H100": "H100-80G-SXM5",
    "H200 SXM": "H200-141G-SXM5", "H200": "H200-141G-SXM5",
    "B200": "B200-SXM", "B300": "B300-SXM",
}


def backfill(conn, *, since, until=None, now=None, get_json=fetch.get_json, get_bytes=None, pause=time.sleep,
             log=None):
    """since·until: 'YYYY-MM'(저장 월). 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    log = log or (lambda message: print(message, file=sys.stderr, flush=True))
    get_bytes = get_bytes or _get_bytes
    until = until or now().strftime("%Y-%m")
    targets = [t for t in db.enabled_targets(conn, hyperstack.PROVIDER) if t["source_name"] == SOURCE_WAYBACK]
    if not targets:
        raise LookupError("no enabled Hyperstack Wayback collection_target")
    context = {"provider": db.provider_id(conn, hyperstack.PROVIDER), "rule": db.rule(conn, hyperstack.RULE_VERSION),
               "now": now, "docs": hyperstack.docs_targets(conn),
               "docs_source": hyperstack.source_id(conn, hyperstack.SOURCE_PAGES)}
    context["run_id"] = db.begin_run(conn, now(), "manual", COLLECTOR_VERSION)
    conn.commit()
    try:
        try:
            captures = [c for c in list_captures(get_json) if since <= c["month"] <= until]
        except hyperstack.EXPECTED_ERRORS as error:
            reason = f"cdx: {type(error).__name__}: {error}"
            processing = "validation_failed" if isinstance(error, hyperstack.SourceInvalid) else "collection_failed"
            runs.record_failure(conn, context, targets, processing, reason,
                                key=lambda t: f"wayback-failed:{context['run_id']}:{t['target_id']}:cdx")
            db.finish_run(conn, context["run_id"], now(), "failed", reason[:2000])
            conn.commit()
            return {"run_id": context["run_id"], "status": "failed", "groups": [{"status": processing,
                                                                                 "timestamp": "cdx", "reason": reason}]}
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
    checked = verifier.reverify(conn, sources={SOURCE_WAYBACK})
    db.finish_run(conn, context["run_id"], now(), status, "; ".join(failed)[:2000] or None)
    conn.commit()
    return {"run_id": context["run_id"], "status": status, "verification": checked, "groups": outcomes}


# 저장본 목록·원문 ---------------------------------------------------------------

def list_captures(get_json):
    body = get_json(CDX_URL.format(url=ARCHIVED_URL), REQUEST_CONFIG)
    if not isinstance(body, list):
        raise hyperstack.SourceInvalid("unexpected_cdx_format")
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


def plain_text(body):
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    text = body.decode("utf8", "replace")
    text = re.sub(r"(?s)<script\b.*?</script\s*>|<style\b.*?</style\s*>|<!--.*?-->", "", text)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " | ", text))
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"(?:\s*\|\s*)+", " | ", text)


def gpu_type(label):
    key = re.sub(r"\s+", " ", re.sub(r"\bNVIDIA\b|\bHGX\b|\b80 ?GB\b", "", label)).strip()
    return GPU_BY_LABEL.get(key)


def read_page(body):
    """저장본 → [{gpu, kind, amount, label, format, add_ons}]. 대상 표기를 모르면 검증 실패(조용히 빠뜨리지 않는다)."""
    text = plain_text(body)
    rows, seen = [], {}
    add_ons = bool(ADD_ONS.search(text))

    def add(label, price, kind, form):
        label = label.strip()
        if not TARGET_LABEL.search(label):
            return
        gpu = gpu_type(label)
        if gpu is None:
            raise hyperstack.SourceInvalid(f"unknown_label: {label!r}")
        try:
            amount = Decimal(price)
        except InvalidOperation:
            raise hyperstack.SourceInvalid(f"invalid_price: {label} {price!r}") from None
        if amount <= 0:
            raise hyperstack.SourceInvalid(f"non_positive_price: {label} {amount}")
        key = (gpu, kind)
        if key in seen:
            if seen[key] != amount:          # 같은 표가 탭마다 반복된다. 값이 다르면 고르지 않는다
                raise hyperstack.SourceInvalid(f"conflicting_prices: {key} {seen[key]} vs {amount}")
            return
        seen[key] = amount
        rows.append({"gpu": gpu, "kind": kind, "amount": amount, "label": label, "format": form, "add_ons": add_ons})

    for match in ON_DEMAND_ROW.finditer(text):
        tail = text[match.end():match.end() + 12]
        form = "per GPU" if "per GPU" in tail else ("per Hour" if re.search(r"[Pp]er Hour", tail) else "표")
        add(match.group("label"), match.group("price"), "on_demand", form)
    for section in SPOT_SECTION.finditer(text):      # 반응형 페이지는 같은 표가 반복된다(값이 다르면 add가 실패)
        for match in SPOT_ROW.finditer(section.group("body")):
            add(match.group("label"), match.group("price"), "spot", "스팟 표")
    if not any(r["kind"] == "on_demand" for r in rows):
        raise hyperstack.SourceInvalid("no_target_rows")
    return rows


# 처리 ------------------------------------------------------------------------

def _process(conn, context, targets, capture, get_bytes):
    outcome = {"timestamp": capture["timestamp"], "original": capture["original"]}
    try:
        rows = read_page(get_bytes(RAW_URL.format(**capture), REQUEST_CONFIG))
        counts = _write(conn, context, targets, capture, rows)
        conn.commit()
        outcome.update(status="success", **counts)
    except hyperstack.EXPECTED_ERRORS as error:
        processing = "validation_failed" if isinstance(error, (hyperstack.SourceInvalid, db.HistoryOrderError)) \
            else "collection_failed"
        runs.record_failure(conn, context, targets, processing, f"{type(error).__name__}: {error}",
                            key=lambda t: f"wayback-failed:{context['run_id']}:{t['target_id']}:{capture['timestamp']}")
        outcome.update(status=processing, reason=f"{type(error).__name__}: {error}")
    return outcome


def history_spec(target, gpu):
    """리전·구성 미상 GPU 단가 상품. 사양은 GPU 종류에서 확정되는 VRAM만."""
    return {"provider_id": target["provider_id"], "gpu_model_id": target["gpu_model_id"],
            "original_product_name": gpu, "region_code": None, "system_type": "instance",
            "service_option": "standard", "sku": None, "original_model_name": gpu, "gpu_count": None,
            "vram_gb_per_gpu": Decimal(hyperstack.SPECS[gpu]["vram"]), "vcpu_count": None, "ram_gb": None,
            "storage_description": None, "spec_scope": None, "inter_node_network": None, "country_code": None}


def make_hit(spec, row, view_url):
    kind = row["kind"]
    raw = f"{row['amount']} USD / GPU / hour ({row['label']}, 저장본 {row['format']})"
    return hyperstack.Hit(
        kind, spec, {"contract_type": kind, "contract_product_type": hyperstack.OFFER_BY_KIND[kind],
                     "commitment_months": None, "payment_option": None},
        {"effective_from": None, "price_certainty": "firm", "raw_price_text": raw, "original_currency": "USD",
         "original_time_unit": "hour", "price_applies_to": "gpu",
         "raw_terms": hyperstack.RAW_TERMS[kind] + "; Wayback 가격 페이지 저장본(리전·구성 미표기)"
                      + ("; 당시 vCPU·RAM·로컬 NVMe 별도 과금(Add-ons), GPU 가격만 적재" if row["add_ons"] else "")},
        [{"component_type": "hourly_rate", "amount": row["amount"], "currency": "USD", "time_unit": "hour",
          "applies_to": "gpu", "raw_value": raw}],
        {"label": row["label"], "gpu_type": row["gpu"], "kind": kind, "amount": str(row["amount"]),
         "parsed_from": row["format"], "view_url": view_url}, row["gpu"])


def _write(conn, context, targets, capture, rows):
    run_id, captured, as_of = context["run_id"], context["now"](), capture["at"]
    view_url = VIEW_URL.format(**capture)
    version = f"wayback {capture['timestamp']} digest:{capture['digest']}"
    counts = {"targets": 0, "offers": 0, "skipped": 0, "not_found": 0}
    for target in targets:
        key = _target_key(target, capture)
        if db.check_exists(conn, key):
            counts["skipped"] += 1
            continue
        counts["targets"] += 1
        found = [r for r in rows if hyperstack.SPECS[r["gpu"]]["model"] == target["model_name"]
                 and r["kind"] == target["contract_filter"]]
        for row in found:
            spec = history_spec(target, row["gpu"])
            hit = make_hit(spec, row, view_url)
            product_id = db.history_product(conn, spec, as_of)
            offer_id = db.ensure_offer(conn, product_id, hit.offer)
            check_id, created = db.add_check(conn, run_id, target["target_id"], "offer", as_of, "public", "success",
                                             True, f"{key}:offer:{offer_id}", product_id=product_id, offer_id=offer_id)
            if not created:
                raise hyperstack.SourceInvalid(f"duplicate_offer_in_capture: offer {offer_id}")
            hyperstack.ensure_spec_evidence(conn, context, target, product_id, row["gpu"], captured)
            db.add_evidence(conn, check_id, target["source_id"], "aggregator", view_url, version,
                            {**hit.excerpt, "primary_source": hyperstack.PRICEBOOK_URL}, captured)
            hyperstack.write_observation(conn, {**context, "target": target}, offer_id, check_id, hit, as_of, captured,
                                         alerts=False)
        counts["offers"] += len(found)
        if not found:
            counts["not_found"] += 1
        check_id, _ = db.add_check(conn, run_id, target["target_id"], "target", as_of,
                                   "public" if found else "unknown", "success" if found else "not_found", True, key,
                                   reason=None if found else f"저장본에 {target['model_name']} {target['contract_filter']} 없음")
        db.add_evidence(conn, check_id, target["source_id"], "aggregator", view_url, version,
                        {"scope": hyperstack.scope(target), "kind": "aggregator_history", "offers_found": len(found),
                         "parsed_from": sorted({r["format"] for r in found})}, captured)
    return counts


def _target_key(target, capture):
    return f"wayback:{target['target_id']}:{capture['timestamp']}"


def _line(outcome):
    if outcome["status"] != "success":
        return f"{outcome['timestamp']}: {outcome['status']} {outcome.get('reason', '')}"
    return (f"{outcome['timestamp']}: success 대상 {outcome['targets']} 오퍼 {outcome['offers']} "
            f"미발견 {outcome['not_found']} "
            f"건너뜀 {outcome['skipped']}")
