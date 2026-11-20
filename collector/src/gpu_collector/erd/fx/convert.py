"""USD 환산값 → 원화 환산(krw_conversion) 적재.

price_normalization의 USD/GPU·시간 값(명목·실효)에 한국수출입은행 USD→KRW 매매기준율을 곱해 krw_conversion에 넣는다.
사용자 입력 환율의 결과는 저장하지 않는다(FR-16). 반올림하지 않는다.

환율 고르는 규칙(#20 안 A): 관측일(관측 시각의 KST 날짜) 이하의 가장 최근 기준일 환율.
- 관측일의 환율이 확정됐을 때만 환산한다. 적재된 USD→KRW 환율의 최신 기준일이 관측일 이상이면 확정으로 본다.
  아직 확정되지 않은 관측(오늘 환율 고시 전, 주말 뒤 다음 영업일 환율 미적재)은 보류하고 다음 실행에서 환산한다.
  그래서 언제 다시 실행해도 같은 관측에는 같은 환율이 붙는다.
- 첫 환율보다 이른 관측은 쓸 환율이 없어 건너뛰고 실패로 남긴다(환율 백필 필요).
- 쓴 환율의 기준일이 관측일보다 4일 넘게 지났으면 fx_stale 알림(DR-04).
- 원화 환산이 이미 있는 환산 행은 다시 만들지 않는다. 곱셈은 DB numeric으로 해 반올림하지 않는다.
"""
from datetime import date as date_type, datetime, timezone

from .. import db
from . import collect as fx

COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-krw 1"
LOCK = "krw"
BASE, QUOTE = "USD", "KRW"

# 환산할 행: USD 값이 있고 원화 환산이 없는 환산. 관측일(KST)과 그날 쓸 환율(기준일 ≤ 관측일 중 최신)을 함께 고른다.
CANDIDATES = """
    WITH pending AS (
        SELECT n.normalization_id, n.nominal_usd_gpu_hour, n.effective_usd_gpu_hour,
               (ob.collected_at AT TIME ZONE 'Asia/Seoul')::date AS observed_on
        FROM price_normalization n
        JOIN price_observation ob ON ob.observation_id = n.observation_id
        WHERE (n.nominal_usd_gpu_hour IS NOT NULL OR n.effective_usd_gpu_hour IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM krw_conversion k WHERE k.normalization_id = n.normalization_id)
          AND (%(since)s::date IS NULL OR (ob.collected_at AT TIME ZONE 'Asia/Seoul')::date >= %(since)s::date)
    )
    SELECT p.normalization_id, p.nominal_usd_gpu_hour, p.effective_usd_gpu_hour, p.observed_on,
           r.exchange_rate_id, r.rate, r.reference_date
    FROM pending p
    LEFT JOIN LATERAL (
        SELECT e.exchange_rate_id, e.rate, e.reference_date FROM exchange_rate e
        WHERE e.source_id = %(source)s AND e.base_currency = %(base)s AND e.quote_currency = %(quote)s
          AND e.reference_date <= p.observed_on
        ORDER BY e.reference_date DESC LIMIT 1
    ) r ON true
"""


def convert(conn, *, since=None, now=None, trigger_type="manual"):
    """원화 환산이 없는 환산 행을 환산한다. since: 관측일(KST) 'YYYY-MM-DD'부터만. 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    start = _day(since) if since else None
    source = fx.source_id(conn)
    latest = conn.execute(
        """SELECT max(reference_date) FROM exchange_rate
           WHERE source_id = %s AND base_currency = %s AND quote_currency = %s""",
        (source, BASE, QUOTE)).fetchone()[0]
    run_id = db.begin_run(conn, now(), trigger_type, COLLECTOR_VERSION)
    conn.commit()
    try:
        rows = conn.execute(CANDIDATES, {"since": start, "source": source, "base": BASE,
                                         "quote": QUOTE}).fetchall()
        calculated_at = now()
        summary = {"latest_rate_date": latest.isoformat() if latest else None, "converted": 0, "pending": 0,
                   "no_rate": 0, "stale_alerts": 0}
        stale = set()
        no_rate_days = set()
        for normalization_id, _nominal, _effective, observed_on, rate_id, _rate, reference in rows:
            if latest is None or observed_on > latest:
                summary["pending"] += 1          # 그날 환율이 아직 확정되지 않음
                continue
            if rate_id is None:
                summary["no_rate"] += 1          # 첫 환율보다 이른 관측
                no_rate_days.add(observed_on)
                continue
            # 곱셈은 DB numeric으로 한다(정확한 값). Python Decimal은 기본 28자리라 긴 USD 값에서 끝자리가 반올림된다.
            inserted = conn.execute(
                """INSERT INTO krw_conversion (normalization_id, exchange_rate_id, nominal_krw_gpu_hour,
                                               effective_krw_gpu_hour, calculated_at)
                   SELECT n.normalization_id, e.exchange_rate_id, n.nominal_usd_gpu_hour * e.rate,
                          n.effective_usd_gpu_hour * e.rate, %s
                   FROM price_normalization n, exchange_rate e
                   WHERE n.normalization_id = %s AND e.exchange_rate_id = %s
                   ON CONFLICT (normalization_id, exchange_rate_id) DO NOTHING""",
                (calculated_at, normalization_id, rate_id)).rowcount
            summary["converted"] += inserted
            if (observed_on - reference).days > fx.STALE_DAYS:
                stale.add((observed_on, reference))
        for observed_on, reference in sorted(stale):
            db.add_alert(conn, "fx_stale", calculated_at, f"fx_stale:krw:{observed_on}:{reference}", run_id=run_id,
                         source_id=source, reason=(f"원화 환산: 관측일 {observed_on}에 기준일 {reference} 환율 사용 "
                                                   f"({(observed_on - reference).days}일 지남)"))
        summary["stale_alerts"] = len(stale)
        if no_rate_days:
            first, last = min(no_rate_days), max(no_rate_days)
            db.add_alert(conn, "fx_failed", calculated_at, f"fx_failed:krw:{run_id}", run_id=run_id, source_id=source,
                         reason=(f"원화 환산: 관측일 {first}~{last}에 쓸 USD→KRW 환율이 없음 "
                                 f"({summary['no_rate']}건). backfill fx로 그 이전 환율을 적재해야 함"))
        status = "partial_failure" if summary["no_rate"] else "success"
        reason = None if status == "success" else f"환율 없는 관측 {summary['no_rate']}건"
        db.finish_run(conn, run_id, now(), status, reason)
        conn.commit()
    except Exception as error:
        conn.rollback()
        db.finish_run(conn, run_id, now(), "failed", f"unexpected: {type(error).__name__}")
        conn.commit()
        raise
    return {"run_id": run_id, "status": status, **summary, "groups": []}


def _day(value):
    try:
        return date_type.fromisoformat(value)
    except (TypeError, ValueError):
        raise fx.ConfigError(f"--since는 YYYY-MM-DD 형식: {value}") from None
