"""한국수출입은행 현재환율 API → exchange_rate 적재.

| 출처(data_source.source_name) | 원천 |
| koreaexim_exchange_api | GET oapi.koreaexim.go.kr/site/program/financial/exchangeJSON?data=AP01&searchdate=YYYYMMDD (인증키 authkey) |

- 매매기준율(deal_bas_r)을 원천 그대로 '{통화}→KRW'로 저장한다. 응답의 통화 전부(KRW 자신 제외)를 적재하고,
  교차환율(EUR→USD 등)은 저장하지 않고 환산할 때 계산한다.
- JPY(100)·IDR(100)처럼 단위가 붙은 통화는 1단위 환율로 나눠 저장한다.
- 영업일 11시 전후에 그날 환율이 나온다. 비영업일이나 갱신 전 날짜는 빈 배열이 온다. 매일 수집은 수집 기준일부터
  거슬러 올라가 가장 가까운 영업일 환율을 적재한다(reference_date = 그 영업일). 주말은 환율이 없어 호출하지 않는다.
- 기준일이 수집 기준일보다 4일 넘게 지나면 fx_stale(DR-04), 수집에 실패하면 fx_failed 알림을 남긴다.
- 인증키는 KOREAEXIM_API_KEY 환경 변수로만 읽는다. URL·근거·실패 사유·출력·예외 연결에 남기지 않는다.
- 하루 1,000회 제한이다. 백필은 이미 적재한 날짜를 건너뛰고 호출 상한(max_calls)에서 멈춘다. 다시 실행하면 이어서 적재한다.
"""
import re
from datetime import date as date_type, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests

from ... import fetch
from ...env import MissingSecret, secret
from .. import db, runs

SOURCE = "koreaexim_exchange_api"
API_URL = "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON"
COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-fx 1"
HISTORY_VERSION = "gpu-collector 0.1.0 / erd-fx-history 1"
KEY_ENV = "KOREAEXIM_API_KEY"
QUOTE = "KRW"
LOOKBACK_DAYS = 10      # 설·추석 연휴(주말 포함)를 넘겨 직전 영업일을 찾는다
STALE_DAYS = 4          # DR-04: 기준일이 4일 넘게 지나면 fx_stale
MAX_CALLS = 900         # 백필 1회 호출 상한. 하루 1,000회 중 매일 수집 몫을 남긴다
REQUEST_TIMEOUT = (15, 60)
UNIT = re.compile(r"^([A-Z]{3})(?:\((\d+)\))?$")
RESULT_OK = 1
RESULT_CONFIG = {2: "data 코드 오류", 3: "인증키 오류(발급 2년 뒤 파기됐을 수 있음)"}
RESULT_LIMIT = 4


class ConfigError(LookupError):
    """인증키 없음·인증키 오류·요청 코드 오류. 다시 시도해도 같다(종료 코드 3)."""


class SourceInvalid(ValueError):
    """응답은 받았으나 환율로 읽을 수 없다."""


class LimitExceeded(fetch.FetchError):
    """일일 호출 제한(1,000회) 초과. 다음 날 다시 실행한다."""


EXPECTED_ERRORS = (fetch.FetchError, SourceInvalid, OSError)


# 원천 ---------------------------------------------------------------------------

def api_fetcher():
    """searchdate 하루 응답을 돌려주는 함수. 인증키가 없으면 ConfigError."""
    try:
        key = secret(KEY_ENV)
    except MissingSecret as error:
        raise ConfigError(str(error)) from None
    return lambda day: request_day(day, key)


def request_day(day, authkey, *, get=requests.get):
    """하루치 응답(JSON 배열). 실패 사유에는 예외 이름만 남긴다(requests 예외 문구에 인증키가 든 URL이 있다)."""
    params = {"authkey": authkey, "searchdate": f"{day:%Y%m%d}", "data": "AP01"}
    try:
        response = fetch.send(lambda: get(API_URL, params=params, headers={"User-Agent": fetch.USER_AGENT},
                                          timeout=REQUEST_TIMEOUT))
    except requests.RequestException as error:
        raise fetch.FetchError(f"request_failed: {type(error).__name__}") from None
    if response.status_code != 200:
        raise fetch.FetchError(f"http_{response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise fetch.FetchError("invalid_json") from None


def source_url(day):
    """근거로 남기는 요청 주소. 인증키는 넣지 않는다."""
    return f"{API_URL}?searchdate={day:%Y%m%d}&data=AP01"


def read_rates(body):
    """응답 → {통화: 1단위 원화 환율}. 빈 배열(비영업일·갱신 전)이면 None."""
    if not isinstance(body, list):
        raise SourceInvalid("응답이 배열이 아님")
    if not body:
        return None
    results = {item.get("result") for item in body if isinstance(item, dict)}
    for code, reason in RESULT_CONFIG.items():
        if code in results:
            raise ConfigError(f"result_{code}: {reason}")
    if RESULT_LIMIT in results:
        raise LimitExceeded(f"result_{RESULT_LIMIT}: 일일 호출 제한(1,000회) 초과")
    rates = {}
    for item in body:
        if not isinstance(item, dict) or item.get("result") != RESULT_OK:
            raise SourceInvalid(f"알 수 없는 result: {item.get('result') if isinstance(item, dict) else '형식 오류'}")
        match = UNIT.match(item.get("cur_unit") or "")
        if not match:
            raise SourceInvalid(f"통화 단위를 읽을 수 없음: {item.get('cur_unit')!r}"[:200])
        currency, per = match.group(1), int(match.group(2) or 1)
        if currency == QUOTE:
            continue
        try:
            value = Decimal(str(item.get("deal_bas_r")).replace(",", ""))
        except InvalidOperation:
            raise SourceInvalid(f"{currency} 매매기준율을 읽을 수 없음: {item.get('deal_bas_r')!r}"[:200]) from None
        if not value.is_finite() or value <= 0 or per <= 0:
            raise SourceInvalid(f"{currency} 매매기준율이 양수가 아님: {item.get('deal_bas_r')!r}"[:200])
        if currency in rates:
            raise SourceInvalid(f"{currency}가 두 번 나옴")
        rates[currency] = value / per
    if "USD" not in rates:
        raise SourceInvalid("USD 환율이 없음")
    return rates


# 적재 ---------------------------------------------------------------------------

def source_id(conn):
    row = conn.execute("SELECT source_id FROM data_source WHERE source_name = %s", (SOURCE,)).fetchone()
    if row is None:
        raise LookupError(f"data_source not seeded: {SOURCE}")
    return row[0]


def loaded_days(conn, source, start, end):
    rows = conn.execute(
        """SELECT DISTINCT reference_date FROM exchange_rate
           WHERE source_id = %s AND quote_currency = %s AND reference_date BETWEEN %s AND %s""",
        (source, QUOTE, start, end)).fetchall()
    return {row[0] for row in rows}


def write_rates(conn, source, day, rates, collected_at):
    """하루치 환율을 넣는다. 이미 있는 통화는 건너뛴다. 반환: 새로 넣은 행 수."""
    inserted = 0
    for currency, rate in sorted(rates.items()):
        cursor = conn.execute(
            """INSERT INTO exchange_rate (source_id, base_currency, quote_currency, rate, reference_date, collected_at,
                                          source_url)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (source_id, base_currency, quote_currency, reference_date) DO NOTHING""",
            (source, currency, QUOTE, rate, day, collected_at, source_url(day)))
        inserted += cursor.rowcount
    return inserted


def business_day(day):
    return day.weekday() < 5


# 매일 수집 ----------------------------------------------------------------------

def collect(conn, *, trigger_type="manual", now=None, date=None, fetch_day=None):
    """환율 수집 1회. 수집 기준일(KST, 생략하면 오늘)에서 가장 가까운 영업일 환율을 적재한다. 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    day = date_type.fromisoformat(runs.collection_date(date, now))
    source = source_id(conn)
    fetch_day = fetch_day or api_fetcher()
    run_id = db.begin_run(conn, now(), trigger_type, COLLECTOR_VERSION)
    conn.commit()
    try:
        outcome = _collect_day(conn, source, run_id, day, fetch_day, now)
    except ConfigError as error:
        _fail(conn, run_id, source, day, now, str(error))
        db.finish_run(conn, run_id, now(), "failed", f"{SOURCE}: {error}"[:2000])
        conn.commit()
        raise
    except EXPECTED_ERRORS as error:
        outcome = _fail(conn, run_id, source, day, now, _reason(error))
    except Exception as error:
        conn.rollback()
        db.finish_run(conn, run_id, now(), "failed", f"unexpected: {type(error).__name__}")
        conn.commit()
        raise
    status = runs.run_status([outcome])
    reason = None if status == "success" else f"{SOURCE}: {outcome['reason']}"[:2000]
    db.finish_run(conn, run_id, now(), status, reason)
    conn.commit()
    return {"run_id": run_id, "date": day.isoformat(), "status": status, "groups": [outcome]}


def last_loaded(conn, source, day):
    return conn.execute(
        """SELECT max(reference_date) FROM exchange_rate
           WHERE source_id = %s AND quote_currency = %s AND reference_date <= %s""", (source, QUOTE, day)).fetchone()[0]


def _collect_day(conn, source, run_id, day, fetch_day, now):
    """기준일 환율을 적재한다. 마지막 적재일 다음날부터 기준일까지 빠진 영업일도 함께 채운다(최대 LOOKBACK_DAYS).

    며칠 실패한 뒤 성공해도 그 사이 영업일 환율이 비지 않게 한다. 비어 있으면 원화 환산이 그 날짜에 직전 환율을
    쓰게 된다. 적재한 날이 하나도 없던 DB면 기준일에서 가장 가까운 영업일 하나만 찾는다(그 이전은 backfill fx)."""
    last = last_loaded(conn, source, day)
    if last == day:
        return _stale_check(conn, source, run_id, day, now, {
            "source": SOURCE, "status": "skipped", "reference_date": day.isoformat(), "calls": 0,
            "reason": "이 기준일 환율을 이미 적재함"})
    floor = day - timedelta(days=LOOKBACK_DAYS)
    lower = max(last + timedelta(days=1), floor) if last else floor
    calls, inserted, loaded = 0, 0, []
    candidate = day
    while candidate >= lower:
        if business_day(candidate):
            rates = read_rates(fetch_day(candidate))
            calls += 1
            if rates is not None:
                inserted += write_rates(conn, source, candidate, rates, now())
                conn.commit()
                loaded.append(candidate)
                if last is None:
                    break                 # 첫 적재: 가장 가까운 영업일 하나만
        candidate -= timedelta(days=1)
    reference = loaded[0] if loaded else last
    if reference is None:
        raise SourceInvalid(f"{day} 이전 {LOOKBACK_DAYS}일 안에 환율이 없음")
    outcome = {"source": SOURCE, "status": "success" if loaded else "skipped", "reference_date": reference.isoformat(),
               "calls": calls, "inserted": inserted}
    if len(loaded) > 1 or (loaded and loaded[0] != day):
        outcome["loaded_dates"] = [d.isoformat() for d in sorted(loaded)]
    if not loaded:
        outcome["reason"] = "기준일 환율이 아직 없음(비영업일·고시 전). 직전 적재일 환율 사용"
    if last is not None and last < floor - timedelta(days=1):
        outcome["gap_before"] = f"{last + timedelta(days=1)}~{floor - timedelta(days=1)}: backfill fx로 채워야 함"
    return _stale_check(conn, source, run_id, day, now, outcome)


def _stale_check(conn, source, run_id, day, now, outcome):
    reference = date_type.fromisoformat(outcome["reference_date"])
    if (day - reference).days > STALE_DAYS:
        db.add_alert(conn, "fx_stale", now(), f"fx_stale:{day}:{reference}", run_id=run_id, source_id=source,
                     reason=f"{SOURCE}: 기준일 {reference}가 수집 기준일 {day}보다 {(day - reference).days}일 지남")
        conn.commit()
        outcome["stale"] = True
    return outcome


def _fail(conn, run_id, source, day, now, reason):
    conn.rollback()
    db.add_alert(conn, "fx_failed", now(), f"fx_failed:{run_id}:{day}", run_id=run_id, source_id=source,
                 reason=f"{SOURCE} {day}: {reason}"[:2000])
    conn.commit()
    return {"source": SOURCE, "status": "failed", "reason": reason}


def _reason(error):
    """실패 사유. 우리가 만든 예외는 문구를, 그 밖은 이름만 남긴다(인증키가 든 문구를 남기지 않는다)."""
    if isinstance(error, (fetch.FetchError, SourceInvalid)):
        return str(error)[:1000]
    return type(error).__name__


# 과거 이력 ----------------------------------------------------------------------

def backfill(conn, *, since, until=None, now=None, fetch_day=None, max_calls=MAX_CALLS, trigger_type="manual"):
    """since~until(YYYY-MM-DD, until 생략하면 오늘 KST)의 영업일 환율을 적재한다.

    이미 적재한 날짜와 주말은 호출하지 않는다. 호출 상한이나 일일 제한에 걸리면 멈추고 partial_failure로 끝낸다
    (다시 실행하면 이어서 적재한다). 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    start = _day(since, "since")
    end = _day(until, "until") if until else date_type.fromisoformat(runs.collection_date(None, now))
    if start > end:
        raise ConfigError(f"since({start})가 until({end})보다 늦다")
    source = source_id(conn)
    fetch_day = fetch_day or api_fetcher()
    run_id = db.begin_run(conn, now(), trigger_type, HISTORY_VERSION)
    conn.commit()
    done = loaded_days(conn, source, start, end)
    summary = {"since": start.isoformat(), "until": end.isoformat(), "calls": 0, "loaded_days": 0, "empty_days": 0,
               "already_loaded_days": len(done), "inserted": 0}
    groups = []
    try:
        day = start
        while day <= end:
            if business_day(day) and day not in done:
                if summary["calls"] >= max_calls:
                    groups.append({"date": day.isoformat(), "status": "incomplete",
                                   "reason": f"호출 상한 {max_calls}회에 도달. 다시 실행하면 이 날짜부터 이어서 적재"})
                    break
                try:
                    body = fetch_day(day)
                    summary["calls"] += 1
                    rates = read_rates(body)
                except LimitExceeded as error:
                    groups.append({"date": day.isoformat(), "status": "failed", "reason": str(error)})
                    break
                except EXPECTED_ERRORS as error:
                    conn.rollback()
                    groups.append({"date": day.isoformat(), "status": "failed", "reason": _reason(error)})
                else:
                    if rates is None:
                        summary["empty_days"] += 1
                    else:
                        summary["inserted"] += write_rates(conn, source, day, rates, now())
                        conn.commit()
                        summary["loaded_days"] += 1
                        groups.append({"date": day.isoformat(), "status": "success"})
            day += timedelta(days=1)
    except ConfigError as error:
        _fail(conn, run_id, source, day, now, str(error))
        db.finish_run(conn, run_id, now(), "failed", f"{SOURCE}: {error}"[:2000])
        conn.commit()
        raise
    except Exception as error:
        conn.rollback()
        db.finish_run(conn, run_id, now(), "failed", f"unexpected: {type(error).__name__}")
        conn.commit()
        raise
    problems = [g for g in groups if g["status"] != "success"]
    if not problems:
        status = "success"
    else:
        status = "partial_failure" if summary["loaded_days"] or done else "failed"
        reason = "; ".join(f"{g['date']}: {g['reason']}" for g in problems)
        db.add_alert(conn, "fx_failed", now(), f"fx_failed:{run_id}", run_id=run_id, source_id=source,
                     reason=f"{SOURCE} 백필: {reason}"[:2000])
    db.finish_run(conn, run_id, now(), status,
                  None if not problems else f"{SOURCE}: {problems[0]['date']}: {problems[0]['reason']}"[:2000])
    conn.commit()
    return {"run_id": run_id, "status": status, **summary, "groups": groups}


def _day(value, name):
    try:
        return date_type.fromisoformat(value)
    except (TypeError, ValueError):
        raise ConfigError(f"--{name}는 YYYY-MM-DD 형식: {value}") from None
