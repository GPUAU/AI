"""비USD 가격을 USD로 바꿀 때 쓰는 환율(한국수출입은행 매매기준율의 교차환율).

수출입은행은 원화 기준 환율만 주므로 1 {통화} = ({통화}→KRW) ÷ (USD→KRW) USD로 계산한다.
규칙은 원화 환산(convert krw)과 같다: 관측일(KST) 이하의 가장 최근 기준일. 매일 12시에 collect fx를 먼저 실행하므로
보통 관측 당일 환율이다. 두 통화의 기준일이 다르면(한쪽만 비어 있는 날) 쓰지 않고 None.
"""
from datetime import date as date_type

from . import collect as fx

QUOTE = "KRW"


def usd_rate(conn, currency, observed_on):
    """1 {currency}의 USD 값과 쓴 환율. 반환: {rate, reference_date, currency_rate_id, usd_rate_id} 또는 None."""
    if currency == "USD":
        return {"rate": 1, "reference_date": None, "currency_rate_id": None, "usd_rate_id": None}
    source = fx.source_id(conn)
    rows = {}
    for code in (currency, "USD"):
        row = conn.execute(
            """SELECT exchange_rate_id, rate, reference_date FROM exchange_rate
               WHERE source_id = %s AND base_currency = %s AND quote_currency = %s AND reference_date <= %s
               ORDER BY reference_date DESC LIMIT 1""", (source, code, QUOTE, observed_on)).fetchone()
        if row is None:
            return None
        rows[code] = row
    (cid, crate, cref), (uid, urate, uref) = rows[currency], rows["USD"]
    if cref != uref:
        return None
    return {"rate": crate / urate, "reference_date": cref, "currency_rate_id": cid, "usd_rate_id": uid,
            "currency_krw": crate, "usd_krw": urate}


def stale(observed_on, reference_date):
    return isinstance(reference_date, date_type) and (observed_on - reference_date).days > fx.STALE_DAYS
