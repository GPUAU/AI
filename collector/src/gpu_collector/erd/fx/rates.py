"""비USD 가격을 USD로 바꿀 때 쓰는 환율(한국수출입은행 매매기준율의 교차환율).

수출입은행은 원화 기준 환율만 주므로 1 {통화} = ({통화}→KRW) ÷ (USD→KRW) USD로 계산한다.
규칙은 원화 환산(convert krw)과 같다: 관측일(KST) 이하의 가장 최근 기준일, 단 관측일의 환율이 확정됐을 때만
(적재된 USD→KRW 최신 기준일 ≥ 관측일). 확정 전이면 None을 돌려주고 환산은 not_computable(환율 미확정)으로 남긴다.
convert krw가 시작할 때 fill_pending으로 확정된 것부터 USD 환산을 채운 뒤 원화로 바꾸므로, USD 환산과 원화 환산이
항상 같은 기준일 환율을 쓴다(원화 = 원래 통화 시간 단가 × {통화}→KRW ÷ GPU 수).
두 통화의 기준일이 다르면(한쪽만 비어 있는 날) 쓰지 않는다.
"""
import json
from datetime import date as date_type

from . import collect as fx

QUOTE = "KRW"
PENDING = "환율 미확정"


def latest_usd_date(conn, source):
    return conn.execute(
        """SELECT max(reference_date) FROM exchange_rate
           WHERE source_id = %s AND base_currency = 'USD' AND quote_currency = %s""", (source, QUOTE)).fetchone()[0]


def usd_rate(conn, currency, observed_on):
    """1 {currency}의 USD 값과 쓴 환율. 반환: {rate, reference_date, currency_rate_id, usd_rate_id} 또는
    (None, 사유). 관측일 환율이 아직 확정되지 않았으면 사유 PENDING."""
    if currency == "USD":
        return {"rate": 1, "reference_date": None, "currency_rate_id": None, "usd_rate_id": None}, None
    source = fx.source_id(conn)
    latest = latest_usd_date(conn, source)
    if latest is None or latest < observed_on:
        return None, PENDING
    rows = {}
    for code in (currency, "USD"):
        row = conn.execute(
            """SELECT exchange_rate_id, rate, reference_date FROM exchange_rate
               WHERE source_id = %s AND base_currency = %s AND quote_currency = %s AND reference_date <= %s
               ORDER BY reference_date DESC LIMIT 1""", (source, code, QUOTE, observed_on)).fetchone()
        if row is None:
            return None, f"{observed_on} 이하 {code}→KRW 환율 없음"
        rows[code] = row
    (cid, crate, cref), (uid, urate, uref) = rows[currency], rows["USD"]
    if cref != uref:
        return None, f"{currency}·USD 기준일이 다름({cref}, {uref})"
    return {"rate": crate / urate, "reference_date": cref, "currency_rate_id": cid, "usd_rate_id": uid,
            "currency_krw": crate, "usd_krw": urate}, None


def stale(observed_on, reference_date):
    return isinstance(reference_date, date_type) and (observed_on - reference_date).days > fx.STALE_DAYS


def apply(inputs, per_hour, gpu_count, fx_rate):
    """USD/GPU·시간과 기록할 입력값."""
    value = per_hour * fx_rate["rate"] / gpu_count
    inputs = {**inputs, "currency_rate_id": fx_rate["currency_rate_id"], "usd_rate_id": fx_rate["usd_rate_id"],
              "reference_date": fx_rate["reference_date"].isoformat() if fx_rate["reference_date"] else None,
              "currency_krw": str(fx_rate.get("currency_krw")), "usd_krw": str(fx_rate.get("usd_krw"))}
    return value, inputs


def fill_pending(conn):
    """환율 미확정으로 남긴 비USD 환산을 환율이 확정된 것부터 채운다(같은 행을 갱신). 반환: 채운 행 수.

    값이 바뀌는 것이 아니라 미확인(NULL)을 확정된 환율로 채우는 것이라 새 행을 만들지 않는다."""
    rows = conn.execute(
        """SELECT n.normalization_id, n.calculation_inputs, pc.amount, pc.time_unit
           FROM price_normalization n
           JOIN price_component pc ON pc.component_id = ((n.calculation_inputs -> 'component_ids') ->> 0)::bigint
           WHERE n.result_status = 'not_computable' AND n.reason = %s
             AND n.calculation_inputs ? 'currency' AND n.calculation_inputs ? 'observed_on'""", (PENDING,)).fetchall()
    filled = 0
    for normalization_id, inputs, amount, time_unit in rows:
        observed_on = date_type.fromisoformat(inputs["observed_on"])
        fx_rate, _reason = usd_rate(conn, inputs["currency"], observed_on)
        if fx_rate is None:
            continue
        per_hour = amount * 60 if time_unit == "minute" else amount
        value, inputs = apply(inputs, per_hour, inputs["gpu_count"], fx_rate)
        conn.execute(
            """UPDATE price_normalization SET nominal_usd_gpu_hour = %s, effective_usd_gpu_hour = %s,
                      result_status = 'success', reason = NULL, calculation_inputs = %s
               WHERE normalization_id = %s AND result_status = 'not_computable'""",
            (value, value, json.dumps(inputs, ensure_ascii=False, default=str), normalization_id))
        filled += 1
    return filled
