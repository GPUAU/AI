"""USD 환산값 → 원화 환산(krw_conversion) 적재.

price_normalization의 USD/GPU·시간 값(명목·실효)에 한국수출입은행 USD→KRW 매매기준율을 곱해 krw_conversion에 넣는다.
사용자 입력 환율의 결과는 저장하지 않는다(FR-16). 곱셈은 DB numeric으로 해 반올림하지 않는다.

환율 고르는 규칙(#20 안 A): 관측일(관측 시각의 KST 날짜) 이하의 가장 최근 기준일 환율.
- 관측일의 환율이 확정됐을 때만 환산한다. 적재된 USD→KRW 환율의 최신 기준일이 관측일 이상이면 확정으로 본다.
  아직 확정되지 않은 관측(오늘 환율 고시 전, 주말 뒤 다음 영업일 환율 미적재)은 보류하고 다음 실행에서 환산한다.
- 나중에 그 관측에 맞는 환율이 바뀌면(빠졌던 영업일 환율을 뒤늦게 적재) 정정 행을 추가한다. 기존 행은 지우지 않는다.
  한 환산 행에 원화 환산이 여러 개면 쓴 환율의 기준일이 가장 늦은 것이 현재 값이다(조회할 때 그 행을 고른다).
- 첫 환율보다 이른 관측은 쓸 환율이 없어 건너뛰고 fx_failed로 남긴다(환율 백필 필요).
- 쓴 환율의 기준일이 관측일보다 4일 넘게 지났으면 fx_stale 알림(DR-04).
"""
from datetime import date as date_type, datetime, timezone

from .. import db
from . import collect as fx

COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-krw 1"
LOCK = "krw"
BASE, QUOTE = "USD", "KRW"

# 관측일과 그날 쓸 환율(기준일 ≤ 관측일 중 최신). latest_date보다 늦은 관측은 확정 전이라 고르지 않는다.
PICK = """
    WITH latest AS (
        SELECT max(reference_date) AS d FROM exchange_rate
        WHERE source_id = %(source)s AND base_currency = %(base)s AND quote_currency = %(quote)s
    ), cand AS (
        SELECT n.normalization_id, (ob.collected_at AT TIME ZONE 'Asia/Seoul')::date AS observed_on
        FROM price_normalization n
        JOIN price_observation ob ON ob.observation_id = n.observation_id
        WHERE (n.nominal_usd_gpu_hour IS NOT NULL OR n.effective_usd_gpu_hour IS NOT NULL)
          AND (%(since)s::date IS NULL OR (ob.collected_at AT TIME ZONE 'Asia/Seoul')::date >= %(since)s::date)
    ), pick AS (
        SELECT c.normalization_id, c.observed_on, r.exchange_rate_id, r.reference_date,
               c.observed_on > coalesce((SELECT d FROM latest), '-infinity'::date) AS pending
        FROM cand c
        LEFT JOIN LATERAL (
            SELECT e.exchange_rate_id, e.reference_date FROM exchange_rate e
            WHERE e.source_id = %(source)s AND e.base_currency = %(base)s AND e.quote_currency = %(quote)s
              AND e.reference_date <= c.observed_on
            ORDER BY e.reference_date DESC LIMIT 1
        ) r ON true
    )
"""

COUNTS = PICK + """
    SELECT count(*) FILTER (WHERE pending), count(*) FILTER (WHERE NOT pending AND exchange_rate_id IS NULL),
           min(observed_on) FILTER (WHERE NOT pending AND exchange_rate_id IS NULL),
           max(observed_on) FILTER (WHERE NOT pending AND exchange_rate_id IS NULL),
           (SELECT d FROM latest)
    FROM pick
"""

# 맞는 환율로 된 원화 환산이 없는 행만 넣는다. 같은 문장 안의 EXISTS는 넣기 전 상태를 보므로 정정 여부를 알 수 있다.
INSERT = PICK + """
    , ins AS (
        INSERT INTO krw_conversion (normalization_id, exchange_rate_id, nominal_krw_gpu_hour, effective_krw_gpu_hour,
                                    calculated_at)
        SELECT p.normalization_id, p.exchange_rate_id, n.nominal_usd_gpu_hour * e.rate,
               n.effective_usd_gpu_hour * e.rate, %(calculated_at)s
        FROM pick p
        JOIN price_normalization n ON n.normalization_id = p.normalization_id
        JOIN exchange_rate e ON e.exchange_rate_id = p.exchange_rate_id
        WHERE NOT p.pending
        ON CONFLICT (normalization_id, exchange_rate_id) DO NOTHING
        RETURNING normalization_id
    )
    SELECT count(*),
           count(*) FILTER (WHERE EXISTS (SELECT 1 FROM krw_conversion k WHERE k.normalization_id = i.normalization_id)),
           array_agg(DISTINCT p.observed_on::text || '|' || p.reference_date::text)
               FILTER (WHERE p.observed_on - p.reference_date > %(stale_days)s)
    FROM ins i JOIN pick p ON p.normalization_id = i.normalization_id
"""


def convert(conn, *, since=None, now=None, trigger_type="manual"):
    """원화 환산이 없거나 맞는 환율이 바뀐 환산 행을 환산한다. since: 관측일(KST) 'YYYY-MM-DD'부터만. 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    params = {"since": _day(since) if since else None, "source": fx.source_id(conn), "base": BASE, "quote": QUOTE,
              "stale_days": fx.STALE_DAYS}
    run_id = db.begin_run(conn, now(), trigger_type, COLLECTOR_VERSION)
    conn.commit()
    try:
        pending, no_rate, first_missing, last_missing, latest = conn.execute(COUNTS, params).fetchone()
        calculated_at = now()
        converted, corrected, stale = conn.execute(INSERT, {**params, "calculated_at": calculated_at}).fetchone()
        stale = sorted(stale or [])
        for pair in stale:
            observed_on, reference = pair.split("|")
            days = (date_type.fromisoformat(observed_on) - date_type.fromisoformat(reference)).days
            db.add_alert(conn, "fx_stale", calculated_at, f"fx_stale:krw:{observed_on}:{reference}", run_id=run_id,
                         source_id=params["source"],
                         reason=f"원화 환산: 관측일 {observed_on}에 기준일 {reference} 환율 사용({days}일 지남)")
        if no_rate:
            db.add_alert(conn, "fx_failed", calculated_at, f"fx_failed:krw:{first_missing}:{last_missing}",
                         run_id=run_id, source_id=params["source"],
                         reason=(f"원화 환산: 관측일 {first_missing}~{last_missing}에 쓸 USD→KRW 환율이 없음"
                                 f"({no_rate}건). backfill fx로 그 이전 환율을 적재해야 함"))
        status = "partial_failure" if no_rate else "success"
        db.finish_run(conn, run_id, now(), status, f"환율 없는 관측 {no_rate}건" if no_rate else None)
        conn.commit()
    except Exception as error:
        conn.rollback()
        db.finish_run(conn, run_id, now(), "failed", f"unexpected: {type(error).__name__}")
        conn.commit()
        raise
    return {"run_id": run_id, "status": status, "latest_rate_date": latest.isoformat() if latest else None,
            "converted": converted, "corrected": corrected, "pending": pending, "no_rate": no_rate,
            "stale_alerts": len(stale)}


def _day(value):
    try:
        return date_type.fromisoformat(value)
    except (TypeError, ValueError):
        raise fx.ConfigError(f"--since는 YYYY-MM-DD 형식: {value}") from None
