"""제3자 저장본(aggregator) 과거 관측의 원천 확인(source_evidence.primary_verified) 판정.

과거 값을 오늘 값과 비교하지 않는다. 그 무렵의 공식 값이 있을 때만 판정한다.

- true:  저장 시각 ±7일 안의 매일 수집(공식) 관측과 값이 같다. 또는 매일 수집 관측의 적용 시작일(effective_from)이
         저장 시각 이전이고 값이 같다(공식 원천이 그때부터 같은 가격이라고 밝힘).
- false: ±1일 안(사실상 같은 때)의 매일 수집 관측과 값이 다르다. 저장본을 의심할 근거다.
         1~7일 차이에서 값이 다르면 그 사이 가격이 바뀌었을 수 있어(예: Verda는 며칠 새 조금씩 바뀜) 판정하지 않는다.
- NULL:  비교할 공식 관측이 없다(대조 불가).

비교 값은 가격 요소를 GPU 1장·시간 단위(원래 통화)로 맞춘 것이다. 분 단가는 × 60, 인스턴스·노드 가격은 ÷ GPU 수.
약정 총액·선납금은 그대로 비교한다. 비교 대상은 같은 오퍼의 매일 수집이고, Hyperstack 과거 상품(리전 미상 GPU 단가)은
같은 GPU 종류·계약의 매일 수집 오퍼다(pricebook 단가가 flavor·리전과 무관).
"""
import json
from datetime import timedelta

WINDOW = timedelta(days=7)          # 같음 판정 범위
SAME_TIME = timedelta(days=1)       # 다름 판정 범위
RULE = "같음: ±7일 안 매일 수집 또는 적용 시작일이 저장 시각 이전인 매일 수집과 같음 / 다름: ±1일 안 매일 수집과 다름"
BY_MODEL = {"Hyperstack"}       # 과거 상품을 GPU 종류로 매일 수집 오퍼와 짝짓는 사업자


def signature(conn, observation_id):
    """관측 가격을 비교 가능한 형태로. 맞출 수 없으면(GPU 수 미상 등) None."""
    rows = conn.execute(
        """SELECT pc.component_type, pc.amount, pc.currency, pc.time_unit, pc.applies_to, p.gpu_count
           FROM price_component pc JOIN price_observation ob USING (observation_id)
           JOIN offer o ON o.offer_id = ob.offer_id JOIN product p ON p.product_id = o.product_id
           WHERE pc.observation_id = %s""", (observation_id,)).fetchall()
    if not rows:
        return None
    items = []
    for kind, amount, currency, unit, applies_to, gpu_count in rows:
        if amount is None:
            items.append((kind, None, currency, unit))
        elif kind in ("hourly_rate", "list_price") and unit in ("hour", "minute"):
            value = amount * 60 if unit == "minute" else amount
            if applies_to != "gpu":
                if not gpu_count:
                    return None
                value = value / gpu_count
            items.append(("per_gpu_hour", value.normalize(), currency, None))
        else:
            items.append((kind, amount.normalize(), currency, unit))
    return tuple(sorted(items, key=str))


def equivalent_offers(conn, observation_id):
    """비교할 매일 수집 오퍼 ID 목록."""
    provider, region, model, contract, offer_id, provider_id = conn.execute(
        """SELECT pv.provider_name, p.region_code, p.original_model_name, o.contract_type, o.offer_id, p.provider_id
           FROM price_observation ob JOIN offer o USING (offer_id) JOIN product p USING (product_id)
           JOIN provider pv ON pv.provider_id = p.provider_id WHERE ob.observation_id = %s""",
        (observation_id,)).fetchone()
    if provider in BY_MODEL and region is None:
        rows = conn.execute(
            """SELECT o.offer_id FROM offer o JOIN product p USING (product_id)
               WHERE p.provider_id = %s AND p.original_model_name = %s AND o.contract_type = %s""",
            (provider_id, model, contract)).fetchall()
        return [r[0] for r in rows]
    return [offer_id]


def verdict(conn, observation_id):
    """반환: (primary_verified, verified_at, 근거에 남길 비교 정보)."""
    as_of = conn.execute("SELECT collected_at FROM price_observation WHERE observation_id = %s",
                         (observation_id,)).fetchone()[0]
    mine = signature(conn, observation_id)
    if mine is None:
        return None, None, {"rule": RULE, "result": "대조 불가(가격 단위를 맞출 수 없음)"}
    daily = conn.execute(
        """SELECT ob.observation_id, ob.collected_at, ob.effective_from FROM price_observation ob
           JOIN collection_check c ON c.check_id = ob.check_id AND c.deduplication_key LIKE 'daily:%%'
           WHERE ob.offer_id = ANY(%s)
             AND (ob.collected_at BETWEEN %s AND %s OR (ob.effective_from IS NOT NULL AND ob.effective_from <= %s))
           ORDER BY abs(extract(epoch FROM ob.collected_at - %s))""",
        (equivalent_offers(conn, observation_id), as_of - WINDOW, as_of + WINDOW, as_of, as_of)).fetchall()
    near = [d for d in daily if abs(d[1] - as_of) <= WINDOW]
    for daily_id, at, _ in near:
        if signature(conn, daily_id) == mine:
            return True, at, _info("같음(±7일)", daily_id, at)
    if near and abs(near[0][1] - as_of) <= SAME_TIME:
        return False, None, _info("다름(±1일)", near[0][0], near[0][1])
    for daily_id, at, effective in daily:
        if signature(conn, daily_id) == mine:
            return True, at, {**_info("같음(적용 시작일 이전부터 같은 가격)", daily_id, at),
                              "effective_from": effective.isoformat()}
    if near:
        return None, None, {**_info("대조 불가(1~7일 차이, 그 사이 가격이 바뀌었을 수 있음)", near[0][0], near[0][1])}
    return None, None, {"rule": RULE, "result": "대조 불가(그 무렵 공식 관측 없음)"}


def _info(result, daily_id, at):
    return {"rule": RULE, "result": result, "compared_observation_id": daily_id, "compared_daily_at": at.isoformat()}


def reverify(conn, *, sources=None):
    """집계 근거의 원천 확인을 다시 판정해 고친다. sources: data_source.source_name 모음(생략하면 전체).
    반환: {same, different, unknown, changed}. 커밋은 호출한 쪽이 한다."""
    rows = conn.execute(
        """SELECT e.evidence_id, ob.observation_id, e.primary_verified, e.excerpt
           FROM source_evidence e JOIN data_source ds ON ds.source_id = e.source_id
           JOIN price_observation ob ON ob.check_id = e.check_id
           WHERE e.evidence_role = 'aggregator' AND (%s::text[] IS NULL OR ds.source_name = ANY(%s))
           ORDER BY e.evidence_id""", (sorted(sources) if sources else None,) * 2).fetchall()
    counts = {"same": 0, "different": 0, "unknown": 0, "changed": 0}
    for evidence_id, observation_id, before, excerpt in rows:
        primary, verified_at, info = verdict(conn, observation_id)
        counts["same" if primary else "different" if primary is False else "unknown"] += 1
        counts["changed"] += primary is not before
        conn.execute("UPDATE source_evidence SET primary_verified = %s, verified_at = %s, excerpt = %s "
                     "WHERE evidence_id = %s", (primary, verified_at, _merge(excerpt, info), evidence_id))
    return counts


def _merge(excerpt, info):
    try:
        body = json.loads(excerpt) if excerpt else {}
    except ValueError:
        return excerpt
    if not isinstance(body, dict):
        return excerpt
    body["verification"] = info
    return json.dumps(body, ensure_ascii=False, default=str)
