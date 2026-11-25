"""Verda 과거 약정 추정 (집계 출처: Wayback에 저장된 verda.com/pricing의 약정 할인율).

| 출처(data_source.source_name) | 원천 | 시점 |
| verda_pricing_page_wayback | web.archive.org 저장본: verda.com/pricing 'Reserved' 표(기간별 % off on-demand) | 2026-07-01~ |

- 약정 할인율 API(long-term periods)는 저장본이 없다. 가격 페이지 저장본의 할인율을 과거 온디맨드 관측(verda_api_wayback)에
  적용해 매일 수집과 같은 가정 시나리오(관측 price_certainty=estimate, 환산 assumption)를 만든다.
- 각 온디맨드 관측에는 그 시각 이전 가장 가까운 가격 페이지 저장본의 할인율을 쓴다. 그런 저장본이 없으면 만들지 않는다.
  (2026-07-01~11-16 저장본 28개에서 할인율 1개월 2%·3개월 3%·6개월 4%·1년 8%·2년 25%가 한 번도 바뀌지 않았다.)
- 상품은 온디맨드 관측의 상품을 그대로 쓴다. 관측 시각은 온디맨드 관측 시각. 같은 온디맨드 관측은 다시 처리하지 않는다.
"""
import gzip
import html as html_lib
import re
from datetime import datetime, timezone
from decimal import Decimal

from .. import db, runs
from . import collect as verda

SOURCE_RATES = "verda_pricing_page_wayback"
SOURCE_BASE = "verda_api_wayback"
ARCHIVED_URL = "verda.com/pricing"
CDX_URL = ("https://web.archive.org/cdx/search/cdx?url={url}&output=json&filter=statuscode:200"
           "&fl=timestamp,original,digest&collapse=digest")
RAW_URL = "https://web.archive.org/web/{timestamp}id_/{original}"
VIEW_URL = "https://web.archive.org/web/{timestamp}/{original}"
REQUEST_CONFIG = {"connect_timeout_s": 20, "read_idle_timeout_s": 90}
SECONDS_BETWEEN_REQUESTS = 6
RATE_ROW = re.compile(r"(\d+) (month|months|year|years) \| (\d+(?:\.\d+)?)% off on-demand")


def read_rates(body):
    """가격 페이지 저장본 → [(code, months, 할인율 %, 원문 행)] (기간 순). 표가 없으면 검증 실패."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    text = re.sub(r"(?s)<script\b.*?</script\s*>|<style\b.*?</style\s*>", "", body.decode("utf8", "replace"))
    text = re.sub(r"(?:\s*\|\s*)+", " | ", re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " | ", text))))
    periods, seen = [], set()
    for value, unit, percent in RATE_ROW.findall(text):
        months = int(value) * (12 if unit.startswith("year") else 1)
        if months in seen:
            continue
        seen.add(months)
        rate = Decimal(percent)
        if not 0 <= rate < 100:
            raise verda.SourceInvalid(f"unexpected_rate: {value} {unit} {percent}")
        row = {"code": f"{months}_MONTHS", "name": f"{value} {unit}", "unit_name": "month", "unit_value": months,
               "discount_percentage": percent, "is_enabled": True}
        periods.append((row["code"], months, rate, row))
    if not periods:
        raise verda.SourceInvalid("no_reserved_rates_in_page")
    return sorted(periods, key=lambda p: p[1])


def list_captures(get_json):
    body = get_json(CDX_URL.format(url=ARCHIVED_URL), REQUEST_CONFIG)
    if not isinstance(body, list):
        raise verda.SourceInvalid("unexpected_cdx_format")
    header, rows = (body[0], body[1:]) if body else ([], [])
    found = {}
    for row in rows:
        record = dict(zip(header, row))
        stamp = record.get("timestamp") or ""
        if len(stamp) == 14 and stamp.isdigit():
            found.setdefault(stamp, {"timestamp": stamp, "original": record.get("original"),
                                     "digest": record.get("digest"),
                                     "at": datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)})
    return sorted(found.values(), key=lambda c: c["timestamp"])


def pending_bases(conn, provider_id, targets):
    """아직 약정을 만들지 않은 과거 온디맨드 관측. 반환: [base dict] (verda.write_group이 모으는 형태 + product_id·at)."""
    columns = db.PRODUCT_IDENTITY + db.CONFIG_FIELDS
    rows = conn.execute(
        f"""SELECT p.product_id, ob.observation_id, ob.collected_at, pc.amount, m.model_name,
                   {", ".join("p." + c for c in columns)}
            FROM price_observation ob
            JOIN collection_check c ON c.check_id = ob.check_id
            JOIN collection_target t ON t.target_id = c.target_id
            JOIN data_source s ON s.source_id = t.source_id AND s.source_name = %s
            JOIN offer o ON o.offer_id = ob.offer_id AND o.contract_type = 'on_demand'
            JOIN product p ON p.product_id = o.product_id AND p.provider_id = %s
            JOIN gpu_model m ON m.gpu_model_id = p.gpu_model_id
            JOIN price_component pc ON pc.observation_id = ob.observation_id AND pc.component_type = 'hourly_rate'
            ORDER BY ob.collected_at, ob.observation_id""", (SOURCE_BASE, provider_id)).fetchall()
    by_model = {t["model_name"]: t for t in targets}
    bases = []
    for product_id, observation_id, at, hourly, model, *values in rows:
        target = by_model.get(model)
        if target is None or db.check_exists(conn, _base_key(target, observation_id)):
            continue
        product = dict(zip(columns, values))
        bases.append({"model": model, "product": product, "product_id": product_id, "observation_id": observation_id,
                      "hourly": hourly, "instance_type": product["original_product_name"],
                      "location": product["region_code"], "at": at, "target": target})
    return bases


def backfill(conn, context, *, get_json, get_bytes, pause, log):
    """반환: 결과 묶음 목록. context: run_id·rule·now(verda history와 같음)."""
    targets = [t for t in db.enabled_targets(conn, verda.PROVIDER) if t["source_name"] == SOURCE_RATES]
    if not targets:
        return []
    bases = pending_bases(conn, context["provider"], targets)
    if not bases:
        log("약정 추정: 처리할 과거 온디맨드 관측 없음")
        return []
    try:
        captures = list_captures(get_json)
    except verda.EXPECTED_ERRORS as error:
        reason = f"rates cdx: {type(error).__name__}: {error}"
        runs.record_failure(conn, context, targets, "collection_failed", reason,
                            key=lambda t: f"rates-failed:{context['run_id']}:{t['target_id']}:cdx")
        return [{"group": "rates cdx", "timestamp": "rates cdx", "status": "collection_failed", "reason": reason}]
    chosen = {}
    for base in bases:
        before = [c for c in captures if c["at"] <= base["at"]]
        if before:
            chosen.setdefault(before[-1]["timestamp"], (before[-1], []))[1].append(base)
    skipped = len(bases) - sum(len(v[1]) for v in chosen.values())
    log(f"약정 추정: 온디맨드 관측 {len(bases)}개, 할인율 저장본 {len(chosen)}개 사용, 그 이전 저장본 없음 {skipped}개")
    outcomes = []
    for index, (stamp, (capture, group)) in enumerate(sorted(chosen.items())):
        if index:
            pause(SECONDS_BETWEEN_REQUESTS)
        outcome = {"group": f"rates {stamp}", "timestamp": f"rates {stamp}", "bases": len(group)}
        try:
            periods = read_rates(get_bytes(RAW_URL.format(**capture), REQUEST_CONFIG))
            outcome.update(status="success", offers=_write(conn, context, capture, periods, group))
            conn.commit()
        except verda.EXPECTED_ERRORS as error:
            processing = "validation_failed" if isinstance(error, verda.SourceInvalid) else "collection_failed"
            runs.record_failure(conn, context, targets, processing, f"{type(error).__name__}: {error}",
                                key=lambda t: f"rates-failed:{context['run_id']}:{t['target_id']}:{stamp}")
            outcome.update(status=processing, reason=f"{type(error).__name__}: {error}")
        log(f"{outcome['group']}: {outcome['status']} 온디맨드 {outcome['bases']} 약정 {outcome.get('offers', 0)} "
            f"{outcome.get('reason', '')}")
        outcomes.append(outcome)
    return outcomes


def _write(conn, context, capture, periods, group):
    run_id, captured = context["run_id"], context["now"]()
    view_url = VIEW_URL.format(**capture)
    version = f"wayback {capture['timestamp']} digest:{capture['digest']}"
    rates = {code: str(percent) for code, _months, percent, _row in periods}
    count = 0
    for base in group:
        target = base["target"]
        for code, months, percent, row in periods:
            hit = verda.make_commitment_hit(base, code, months, percent, row, view_url)
            offer_id = db.ensure_offer(conn, base["product_id"], hit.offer)
            check_id, created = db.add_check(
                conn, run_id, target["target_id"], "offer", base["at"], "public", "success", True,
                f"{_base_key(target, base['observation_id'])}:offer:{offer_id}",
                product_id=base["product_id"], offer_id=offer_id)
            if not created:
                raise verda.SourceInvalid(f"duplicate_commitment: base {base['observation_id']} offer {offer_id}")
            db.add_evidence(conn, check_id, target["source_id"], "aggregator", view_url, version,
                            {**hit.excerpt, "rates_capture": capture["timestamp"], "primary_source": verda.LONG_TERM_URL},
                            captured)
            verda.write_observation(conn, context, offer_id, check_id, hit, base["at"], captured)
            count += 1
        check_id, _ = db.add_check(conn, run_id, target["target_id"], "target", base["at"], "public", "success", True,
                                   _base_key(target, base["observation_id"]))
        db.add_evidence(conn, check_id, target["source_id"], "aggregator", view_url, version,
                        {"scope": verda.scope(target), "kind": "aggregator_history", "base_observation_id":
                         base["observation_id"], "rates": rates, "offers_found": len(periods)}, captured)
    return count


def _base_key(target, observation_id):
    return f"wayback-rates:{target['target_id']}:base:{observation_id}"
