"""수집 실행 공통: 수집 기준일·중복 방지 키, 묶음 건너뛰기, 실패 기록, 실행 상태, 사업자 잠금, 중단된 실행 정리.

매일 수집의 성공 기록은 수집 기준일 키(daily:{date}:…)를 쓴다. 같은 날 다시 실행하면 이미 적재한 묶음은 건너뛰고
실패한 묶음만 다시 적재한다. 실패 기록은 실행 번호 키를 써서 다음 실행에서 다시 시도할 수 있게 한다.
"""
from datetime import date as date_type, datetime
from zoneinfo import ZoneInfo

from . import db

KST = ZoneInfo("Asia/Seoul")
LOCK_NAMESPACE = "gpu_collector:"


# 수집 기준일·중복 방지 키 --------------------------------------------------------------

def collection_date(value, now):
    """수집 기준일(KST). value가 없으면 now()의 KST 날짜. 반환: 'YYYY-MM-DD'."""
    if value is None:
        return now().astimezone(KST).date().isoformat()
    if isinstance(value, date_type):
        return value.isoformat()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as error:
        raise ValueError(f"수집 기준일 형식은 YYYY-MM-DD: {value}") from error


def target_key(context, target):
    """대상 범위 점검(성공·미발견)의 키."""
    return f"daily:{context['date']}:{target['target_id']}:target"


def offer_key(context, target, offer_id):
    """오퍼 점검(가격 확인·오퍼 미발견)의 키."""
    return f"daily:{context['date']}:{target['target_id']}:offer:{offer_id}"


def offer_key_prefix(context, target):
    return f"daily:{context['date']}:{target['target_id']}:offer:"


def failure_key(context, target):
    """실패 점검의 키. 실행 번호를 넣어 다음 실행에서 다시 시도할 수 있게 한다."""
    return f"{context['run_id']}:{target['target_id']}:target"


def group_done(conn, context, targets):
    """묶음의 대상이 모두 이 수집 기준일에 기록돼 있는지(성공·미발견). 실패만 있으면 다시 처리한다."""
    return bool(targets) and all(db.check_exists(conn, target_key(context, target)) for target in targets)


def skipped(outcome):
    return {**outcome, "status": "skipped", "reason": "이 수집 기준일에 이미 적재함"}


# 실패 기록·실행 상태 -------------------------------------------------------------------

def record_failure(conn, context, targets, processing, reason, *, key):
    """작업을 되돌리고 대상별 실패 점검과 알림을 남긴다. 실패를 공개 상태로 바꾸지 않는다."""
    conn.rollback()
    run_id, checked_at = context["run_id"], context["now"]()
    for target in targets:
        db.add_check(conn, run_id, target["target_id"], "target", checked_at, "unknown", processing, False,
                     key(target), reason=reason[:1000])
    first = targets[0]
    db.add_alert(conn, "collection_failed", checked_at, f"collection_failed:{key(first)}", run_id=run_id,
                 source_id=first["source_id"], reason=f"{first['source_name']}: {reason}"[:2000])
    conn.commit()


def run_status(outcomes):
    """묶음 결과로 실행 상태를 정한다. 건너뛴 묶음은 성공으로 본다."""
    ok = sum(1 for outcome in outcomes if outcome["status"] in ("success", "skipped"))
    if ok == len(outcomes):
        return "success"
    return "failed" if ok == 0 else "partial_failure"


# 사업자 잠금·중단된 실행 정리 ---------------------------------------------------------

def try_lock(conn, name):
    """세션 단위 advisory lock. 같은 이름의 실행이 이미 잡고 있으면 False. 연결이 끊기면 자동으로 풀린다."""
    locked = conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (LOCK_NAMESPACE + name,)).fetchone()[0]
    conn.commit()
    return locked


def unlock(conn, name):
    conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (LOCK_NAMESPACE + name,))
    conn.commit()


def close_abandoned(conn, name, finished_at):
    """잠금을 잡은 뒤 같은 이름의 끝나지 않은 실행을 실패로 닫는다. 반환: 닫은 실행 번호 목록.

    살아 있는 실행은 잠금을 쥐고 있으므로, 잠금을 잡은 시점에 남아 있는 running 실행은 중단된 것이다.
    실행 행에 사업자 칸이 없어 collector_version(예: 'gpu-collector 0.1.0 / erd-aws-history 1')으로 구분한다.
    """
    rows = conn.execute(
        """UPDATE collection_run SET run_status = 'failed', finished_at = %s,
                  failure_reason = 'abandoned: 끝나지 않은 채 중단된 실행(다음 실행이 정리함)'
           WHERE run_status = 'running' AND collector_version LIKE %s
           RETURNING run_id""", (finished_at, f"% / erd-{name}%")).fetchall()
    conn.commit()
    return sorted(row[0] for row in rows)
