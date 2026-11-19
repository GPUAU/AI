"""ERD 초안 스키마 읽기·쓰기. 관측·가격 요소는 추가만 한다(DDL 트리거가 수정·삭제를 막는다).

NULL은 '미확인'이다. 원천에서 확인한 값만 넣고 모르는 값을 만들어 내지 않는다.
"""
import json
from decimal import Decimal

# 상품의 같은 구성 계열을 찾는 키. AWS는 인스턴스 유형 + 리전(스팟은 가용영역 ID)이다.
PRODUCT_IDENTITY = ("provider_id", "gpu_model_id", "original_product_name", "region_code", "system_type",
                    "service_option")
# 수집기가 원천에서 읽어 채우는 구성 값. 하나라도 바뀌면 새 구성 버전을 만든다(DR-05).
# 상호연결 확인 상태는 사양 근거 절차가 채우므로 비교하지 않는다.
CONFIG_FIELDS = ("sku", "original_model_name", "gpu_count", "vram_gb_per_gpu", "vcpu_count", "ram_gb",
                 "storage_description", "spec_scope", "inter_node_network", "country_code")
OFFER_IDENTITY = ("contract_type", "contract_product_type", "commitment_months", "payment_option")


def provider_id(conn, name):
    row = conn.execute("SELECT provider_id FROM provider WHERE provider_name = %s", (name,)).fetchone()
    if row is None:
        raise LookupError(f"provider not seeded: {name}")
    return row[0]


def rule(conn, version):
    row = conn.execute("SELECT rule_id, monthly_hours FROM normalization_rule WHERE rule_version = %s",
                       (version,)).fetchone()
    if row is None:
        raise LookupError(f"normalization_rule not seeded: {version}")
    return {"rule_id": row[0], "monthly_hours": row[1], "version": version}


def enabled_targets(conn, provider):
    rows = conn.execute(
        """SELECT t.target_id, t.provider_id, t.gpu_model_id, m.model_name, t.source_id, s.source_name, s.base_url,
                  t.region_filter, t.contract_filter, t.scope_version
           FROM collection_target t
           JOIN provider p ON p.provider_id = t.provider_id
           JOIN gpu_model m ON m.gpu_model_id = t.gpu_model_id
           JOIN data_source s ON s.source_id = t.source_id
           WHERE p.provider_name = %s AND t.enabled
           ORDER BY t.target_id""", (provider,)).fetchall()
    keys = ("target_id", "provider_id", "gpu_model_id", "model_name", "source_id", "source_name", "base_url",
            "region_filter", "contract_filter", "scope_version")
    return [dict(zip(keys, row)) for row in rows]


def begin_run(conn, started_at, trigger_type, collector_version):
    return conn.execute(
        """INSERT INTO collection_run (started_at, run_status, trigger_type, collector_version)
           VALUES (%s, 'running', %s, %s) RETURNING run_id""",
        (started_at, trigger_type, collector_version)).fetchone()[0]


def finish_run(conn, run_id, finished_at, status, failure_reason=None):
    conn.execute("UPDATE collection_run SET finished_at = %s, run_status = %s, failure_reason = %s WHERE run_id = %s",
                 (finished_at, status, failure_reason, run_id))


# 상품·오퍼 ---------------------------------------------------------------------

def ensure_product(conn, spec):
    """반환: (product_id, 이전 구성 product_id 또는 None). 구성이 같으면 기존 행을 쓴다.

    구성이 바뀌면 기존 행을 덮지 않고 configuration_version을 올린 새 행을 만든다(DR-05).
    새 구성은 상호연결을 다시 확인해야 하므로 미확인(GPU 1장은 해당 없음)으로 시작한다.
    """
    where = " AND ".join(f"{name} IS NOT DISTINCT FROM %s" for name in PRODUCT_IDENTITY)
    row = conn.execute(
        f"""SELECT product_id, configuration_version, {", ".join(CONFIG_FIELDS)} FROM product WHERE {where}
            ORDER BY configuration_version DESC LIMIT 1""",
        tuple(spec[name] for name in PRODUCT_IDENTITY)).fetchone()
    if row is not None and all(_same(value, spec.get(name)) for name, value in zip(CONFIG_FIELDS, row[2:])):
        return row[0], None
    version = row[1] + 1 if row is not None else 1
    columns = PRODUCT_IDENTITY + CONFIG_FIELDS + ("interconnect_status", "configuration_version")
    values = [spec[name] for name in PRODUCT_IDENTITY] + [spec.get(name) for name in CONFIG_FIELDS]
    values += ["not_applicable" if spec.get("gpu_count") == 1 else "unconfirmed", version]
    product_id = conn.execute(
        f"""INSERT INTO product ({", ".join(columns)}) VALUES ({", ".join(["%s"] * len(columns))})
            RETURNING product_id""", values).fetchone()[0]
    return product_id, (row[0] if row is not None else None)


def history_product(conn, spec, as_of):
    """과거 자료용 상품 찾기. 반환: product_id.

    as_of가 이 상품 계열의 마지막 관측 이후면 현재 수집과 같다(구성이 바뀌면 새 버전). 그보다 앞선 자료는 구성이
    같은 기존 버전에만 붙인다. 구성 버전 번호가 시간 순서를 따르게 하려고, 맞는 버전이 없으면 적재하지 않는다.
    """
    identity = tuple(spec[name] for name in PRODUCT_IDENTITY)
    where = " AND ".join(f"p.{name} IS NOT DISTINCT FROM %s" for name in PRODUCT_IDENTITY)
    last = conn.execute(
        f"""SELECT max(ob.collected_at) FROM price_observation ob JOIN offer o ON o.offer_id = ob.offer_id
            JOIN product p ON p.product_id = o.product_id WHERE {where}""", identity).fetchone()[0]
    if last is None or as_of >= last:
        return ensure_product(conn, spec)[0]
    rows = conn.execute(
        f"""SELECT product_id, {", ".join(CONFIG_FIELDS)} FROM product p WHERE {where}
            ORDER BY configuration_version DESC""", identity).fetchall()
    for row in rows:
        if all(_same(value, spec.get(name)) for name, value in zip(CONFIG_FIELDS, row[1:])):
            return row[0]
    raise HistoryOrderError(f"{spec['original_product_name']}@{spec['region_code']}: {as_of:%Y-%m-%d} 구성이 이후 "
                            "구성 버전과 달라 시간 순서대로 다시 적재해야 함")


class HistoryOrderError(ValueError):
    """과거 자료를 시간 순서와 어긋나게 적재하려 함."""


def product_as_of(conn, provider, gpu_model_id, instance_type, region_code, as_of):
    """as_of 시점에 쓰이던 EC2 상품 구성: as_of 이전 마지막 관측의 상품. 관측이 없으면 최신 구성."""
    row = conn.execute(
        f"""SELECT p.product_id, {", ".join("p." + name for name in CONFIG_FIELDS)}
            FROM price_observation ob JOIN offer o ON o.offer_id = ob.offer_id
            JOIN product p ON p.product_id = o.product_id
            WHERE p.provider_id = %s AND p.gpu_model_id = %s AND p.original_product_name = %s
              AND p.region_code = %s AND p.system_type = 'instance' AND p.service_option = 'standard'
              AND ob.collected_at <= %s
            ORDER BY ob.collected_at DESC, ob.observation_id DESC LIMIT 1""",
        (provider, gpu_model_id, instance_type, region_code, as_of)).fetchone()
    if row is None:
        return latest_product(conn, provider, gpu_model_id, instance_type, region_code)
    return {"product_id": row[0], **dict(zip(CONFIG_FIELDS, row[1:]))}


def latest_product(conn, provider, gpu_model_id, instance_type, region_code):
    """같은 리전 EC2 상품의 최신 구성. Savings Plans·스팟이 사양과 상품을 이어받을 때 쓴다."""
    row = conn.execute(
        f"""SELECT product_id, {", ".join(CONFIG_FIELDS)} FROM product
            WHERE provider_id = %s AND gpu_model_id = %s AND original_product_name = %s AND region_code = %s
              AND system_type = 'instance' AND service_option = 'standard'
            ORDER BY configuration_version DESC LIMIT 1""",
        (provider, gpu_model_id, instance_type, region_code)).fetchone()
    if row is None:
        return None
    return {"product_id": row[0], **dict(zip(CONFIG_FIELDS, row[1:]))}


def ensure_offer(conn, product_id, offer):
    where = " AND ".join(f"{name} IS NOT DISTINCT FROM %s" for name in OFFER_IDENTITY)
    row = conn.execute(
        f"""SELECT offer_id FROM offer WHERE product_id = %s AND {where}
            ORDER BY terms_version DESC LIMIT 1""",
        (product_id, *(offer.get(name) for name in OFFER_IDENTITY))).fetchone()
    if row is not None:
        return row[0]
    return conn.execute(
        f"""INSERT INTO offer (product_id, {", ".join(OFFER_IDENTITY)}) VALUES (%s, {", ".join(["%s"] * len(OFFER_IDENTITY))})
            RETURNING offer_id""", (product_id, *(offer.get(name) for name in OFFER_IDENTITY))).fetchone()[0]


# 점검·근거 ---------------------------------------------------------------------

def add_check(conn, run_id, target_id, check_scope, checked_at, disclosure, processing, scope_completed,
              deduplication_key, *, product_id=None, offer_id=None, reason=None):
    """같은 중복 방지 키가 이미 있으면 그 행을 쓴다(재처리)."""
    row = conn.execute(
        """INSERT INTO collection_check (run_id, target_id, product_id, offer_id, check_scope, checked_at,
                                         disclosure_status, processing_status, scope_completed, reason,
                                         deduplication_key)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (deduplication_key) DO NOTHING RETURNING check_id""",
        (run_id, target_id, product_id, offer_id, check_scope, checked_at, disclosure, processing, scope_completed,
         reason, deduplication_key)).fetchone()
    if row is not None:
        return row[0], True
    existing = conn.execute("SELECT check_id FROM collection_check WHERE deduplication_key = %s",
                            (deduplication_key,)).fetchone()
    return existing[0], False


def check_exists(conn, deduplication_key):
    return conn.execute("SELECT 1 FROM collection_check WHERE deduplication_key = %s",
                        (deduplication_key,)).fetchone() is not None


def add_evidence(conn, check_id, source_id, role, source_url, source_version, excerpt, captured_at, *,
                 primary_verified=None, verified_at=None):
    """primary_verified: 집계 근거의 값을 공식 원천에서 확인했는지(모르면 None). 확인했으면 verified_at을 함께 준다."""
    body = excerpt if isinstance(excerpt, str) or excerpt is None else json.dumps(
        excerpt, ensure_ascii=False, sort_keys=True, default=str)
    return conn.execute(
        """INSERT INTO source_evidence (check_id, source_id, evidence_role, source_url, source_version, excerpt,
                                        primary_verified, verified_at, captured_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING evidence_id""",
        (check_id, source_id, role, source_url, source_version, body, primary_verified, verified_at,
         captured_at)).fetchone()[0]


def has_product_evidence(conn, product_id):
    return conn.execute("SELECT 1 FROM product_evidence WHERE product_id = %s LIMIT 1",
                        (product_id,)).fetchone() is not None


def add_product_evidence(conn, product_id, evidence_id, attributes, intra_node_interconnect=None):
    """상품 속성을 사양 근거에 연결한다. 상호연결 근거가 있으면 확인(confirmed)으로 바꾼다(DR-07).

    상호연결은 구성 비교 대상이 아니므로(CONFIG_FIELDS 밖) 같은 구성 행에 바로 기록한다."""
    names = list(attributes) + (["intra_node_interconnect"] if intra_node_interconnect else [])
    for name in names:
        conn.execute("""INSERT INTO product_evidence (product_id, evidence_id, attribute_name) VALUES (%s, %s, %s)
                        ON CONFLICT DO NOTHING""", (product_id, evidence_id, name))
    if intra_node_interconnect:
        conn.execute("""UPDATE product SET intra_node_interconnect = %s, interconnect_status = 'confirmed'
                        WHERE product_id = %s""", (intra_node_interconnect, product_id))


def previously_seen_offers(conn, target_id):
    """이 대상에서 가격을 확인한 적 있는 오퍼(최신 구성 상품의 것만). 이번에 없으면 오퍼 미발견으로 남긴다."""
    rows = conn.execute(
        """SELECT DISTINCT c.offer_id, c.product_id
           FROM collection_check c JOIN product p ON p.product_id = c.product_id
           WHERE c.target_id = %s AND c.check_scope = 'offer' AND c.processing_status = 'success'
             AND NOT EXISTS (
                 SELECT 1 FROM product newer
                 WHERE newer.provider_id = p.provider_id AND newer.gpu_model_id = p.gpu_model_id
                   AND newer.original_product_name IS NOT DISTINCT FROM p.original_product_name
                   AND newer.region_code IS NOT DISTINCT FROM p.region_code
                   AND newer.system_type = p.system_type AND newer.service_option = p.service_option
                   AND newer.configuration_version > p.configuration_version)""", (target_id,)).fetchall()
    return {offer_id: product_id for offer_id, product_id in rows}


# 관측·환산 ---------------------------------------------------------------------

def add_observation(conn, offer_id, check_id, observation):
    return conn.execute(
        """INSERT INTO price_observation (offer_id, check_id, collected_at, effective_from, price_certainty,
                                          raw_price_text, original_currency, original_time_unit, price_applies_to,
                                          raw_terms)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING observation_id""",
        (offer_id, check_id, observation["collected_at"], observation.get("effective_from"),
         observation["price_certainty"], observation.get("raw_price_text"), observation.get("original_currency"),
         observation.get("original_time_unit"), observation.get("price_applies_to"),
         observation.get("raw_terms"))).fetchone()[0]


def add_component(conn, observation_id, component):
    return conn.execute(
        """INSERT INTO price_component (observation_id, component_type, amount, currency, time_unit, applies_to,
                                        discount_rate, raw_value)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING component_id""",
        (observation_id, component["component_type"], component.get("amount"), component.get("currency"),
         component.get("time_unit"), component.get("applies_to"), component.get("discount_rate"),
         component["raw_value"])).fetchone()[0]


def add_normalization(conn, observation_id, rule_id, result, calculated_at):
    return conn.execute(
        """INSERT INTO price_normalization (observation_id, rule_id, nominal_usd_gpu_hour, effective_usd_gpu_hour,
                                            result_status, reason, calculation_inputs, calculated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING normalization_id""",
        (observation_id, rule_id, result["nominal"], result["effective"], result["status"], result.get("reason"),
         json.dumps(result["inputs"], default=str), calculated_at)).fetchone()[0]


def previous_effective(conn, offer_id, rule_id, before_observation_id):
    """같은 오퍼·같은 규칙의 직전 실효 단가. 급변 점검(OR-06)에 쓴다."""
    return conn.execute(
        """SELECT o.observation_id, n.effective_usd_gpu_hour
           FROM price_observation o JOIN price_normalization n ON n.observation_id = o.observation_id
           WHERE o.offer_id = %s AND n.rule_id = %s AND o.observation_id < %s
             AND n.effective_usd_gpu_hour IS NOT NULL
           ORDER BY o.collected_at DESC, o.observation_id DESC LIMIT 1""",
        (offer_id, rule_id, before_observation_id)).fetchone()


def add_alert(conn, alert_type, detected_at, deduplication_key, *, run_id=None, target_id=None, check_id=None,
              source_id=None, previous_observation_id=None, current_observation_id=None, rule_version=None,
              reason=None):
    conn.execute(
        """INSERT INTO collection_alert (run_id, target_id, check_id, source_id, previous_observation_id,
                                         current_observation_id, alert_type, detected_at, rule_version, reason,
                                         deduplication_key)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (deduplication_key) DO NOTHING""",
        (run_id, target_id, check_id, source_id, previous_observation_id, current_observation_id, alert_type,
         detected_at, rule_version, reason, deduplication_key))


def _same(stored, new):
    if isinstance(stored, Decimal) or isinstance(new, Decimal):
        if stored is None or new is None:
            return stored is None and new is None
        return Decimal(stored) == Decimal(new)
    return stored == new
