"""ERD 스키마 수집·적재 명령.

  python -m gpu_collector.erd init                     DDL(빈 DB일 때만) + 기준 정보(sql/seed.sql) 적용
  python -m gpu_collector.erd collect aws [--date 2026-11-20] [--only aws_ec2_pricing_api,aws_savings_plans_api,aws_ec2_spot_api]
                                          [--trigger manual|scheduled|retry]
  python -m gpu_collector.erd backfill aws --since 2023-07 [--until 2026-11] [--only ...] [--regions ...] [--spot-days 90]
                                          [--keep-files] [--workers 3]
                                                       공식 과거 자료 적재(가격 파일 과거 버전, 스팟 90일)
  python -m gpu_collector.erd collect azure [--date ...] [--trigger ...]   Azure Retail Prices API (대상 SKU 전 리전)
  python -m gpu_collector.erd backfill azure --since 2025-10 [--until 2026-11] [--every month|week|day] [--keep-files]
                                                       [--no-verify]
                                                       GitHub 일별 스냅샷(집계 출처)으로 Azure 과거 가격 적재
  python -m gpu_collector.erd collect verda [--date ...] [--trigger ...]   Verda 공개 API (지역별 온디맨드·스팟, 약정 할인율 가정)
  python -m gpu_collector.erd backfill verda --since 2026-08 [--until 2026-11] [--no-verify]
                                                       Wayback 저장본(집계 출처)으로 Verda 과거 가격 적재
  python -m gpu_collector.erd collect nebius [--date ...] [--trigger ...]  Nebius 문서 가격표(온디맨드·약정 상태) + 공개 스팟 API
  python -m gpu_collector.erd backfill nebius [--days 29] [--only spot,docs] [--no-verify]
                                                       스팟 30일 이력(공식 API) + 문서 Wayback 저장본(집계)
  python -m gpu_collector.erd collect oracle [--date ...] [--trigger ...]  OCI 공개 정가 API(GPU 1장·시간 온디맨드, 리전 단일가)
  python -m gpu_collector.erd backfill oracle --since 2026-05 [--until 2026-11] [--no-verify]
                                                       Wayback 정가 API 응답 저장본(2026-05~)·SkyPilot 카탈로그(2025-01~)로
                                                       Oracle 과거 정가 적재(집계 출처, --only로 출처 선택)
  python -m gpu_collector.erd collect scaleway [--date ...] [--trigger ...]  Scaleway 공개 카탈로그 API(B300·H100, EUR → 교차환율 USD)
  python -m gpu_collector.erd backfill scaleway --since 2024-05 [--until 2026-11]
                                                       Wayback GPU 가격 페이지 저장본(집계): 2025~ 내장 JSON 리전별 분 단가,
                                                       2024 HTML 표 시간 단가(리전 미상)
  python -m gpu_collector.erd collect hyperstack [--date ...] [--trigger ...]  Hyperstack pricebook·flavors API(B300·B200·H200·H100
                                                       온디맨드·스팟, GPU 장당, API 키)
  python -m gpu_collector.erd backfill hyperstack --since 2023-11 [--until 2026-11]
                                                       Wayback 가격 페이지 저장본(집계): 온디맨드·스팟 GPU 장당(리전 미상)
  python -m gpu_collector.erd collect fx [--date ...] [--trigger ...]      한국수출입은행 현재환율 API(매매기준율, 응답 통화 전부→KRW)
                                                       기준일부터 거슬러 올라가 가장 가까운 영업일 환율을 적재
  python -m gpu_collector.erd backfill fx --since 2023-07-01 [--until 2026-11-20] [--max-calls 900]
                                                       과거 영업일 환율(이미 적재한 날짜는 건너뜀, 하루 1,000회 제한)
  python -m gpu_collector.erd convert krw [--since 2023-07-01]
                                                       USD 환산값을 원화로(관측일 이하 최신 USD→KRW 매매기준율, 그날 환율이
                                                       확정된 관측만. 원화 환산이 없는 행만 적재)
  python -m gpu_collector.erd status record            랙스케일·출시 예정 모델 공개 상태 수기 확인 기록(erd/status.py, 월 1회)
  python -m gpu_collector.erd specs aws                 이미 적재한 AWS 상품에 모델 원문·사양 근거(상호연결)를 채운다(1회)
  python -m gpu_collector.erd specs oracle              이미 적재한 Oracle 상품에 vCPU(x86)·NVLink 근거를 채운다(1회, 구성 버전 유지)
  python -m gpu_collector.erd report [--model B300]    최신 가격과 대상별 마지막 점검 상태

--date: 매일 수집의 수집 기준일(KST, 생략하면 오늘). 같은 기준일에 이미 적재한 묶음은 건너뛴다.

실행 안전장치
  - 같은 사업자의 collect·backfill·specs는 동시에 하나만 실행한다(DB advisory lock). 이미 실행 중이면 종료 코드 75.
  - 잠금을 잡은 뒤 같은 사업자의 끝나지 않은(running) 실행을 실패로 닫는다.
  - SIGTERM을 받으면 지금 묶음을 되돌리고 실행을 실패로 기록한 뒤 끝낸다.

종료 코드: 0 성공, 1 부분 실패·예기치 않은 오류·중단, 2 실패(원천 장애 등, 다시 시도할 만함),
           3 설정 오류(다시 시도해도 같음), 75 같은 사업자 실행 중.

환경 변수: DATABASE_URL(필수, 적재 대상 DB), CACHE_DIR(선택, 원천 파일 캐시. 기본 ./var/cache).
AWS 가격 API는 AWS_ACCESS_KEY_ID·AWS_SECRET_ACCESS_KEY, 환율은 KOREAEXIM_API_KEY, Hyperstack은 HYPERSTACK_API_KEY
환경 변수가 필요하다.
"""
import argparse
import json
import os
import signal
import sys
from datetime import datetime, timezone
from importlib import resources

import psycopg

from .aws import collect as aws, history as aws_history
from .azure import collect as azure, history as azure_history
from .verda import collect as verda, history as verda_history
from .nebius import collect as nebius, history as nebius_history
from .fx import collect as fx, convert as krw
from .oracle import collect as oracle, history as oracle_history, skypilot as oracle_skypilot
from .scaleway import collect as scaleway, history as scaleway_history
from .hyperstack import collect as hyperstack, history as hyperstack_history
from . import runs, status

SQL = resources.files("gpu_collector") / "sql"
DDL = SQL / "ddl.sql"
SEED = SQL / "seed.sql"

EXIT_OK, EXIT_PARTIAL, EXIT_FAILED, EXIT_CONFIG, EXIT_LOCKED = 0, 1, 2, 3, 75
EXIT_BY_STATUS = {"success": EXIT_OK, "partial_failure": EXIT_PARTIAL, "failed": EXIT_FAILED}
# DB 시간 제한: 접속 10초, 문 하나 10분, 잠금 대기 30초. 네트워크 단절·잠금 대기로 무한히 멈추지 않게 한다.
CONNECT_TIMEOUT_S = 10
DB_OPTIONS = "-c statement_timeout=600000 -c lock_timeout=30000"
COLLECTORS = {"aws": aws, "azure": azure, "verda": verda, "nebius": nebius, "oracle": oracle, "scaleway": scaleway,
              "hyperstack": hyperstack, "fx": fx}
NO_BACKFILL = set()           # 과거 이력 적재가 없는 수집기


class Terminated(Exception):
    """SIGTERM을 받음. 수집기의 예외 처리가 묶음을 되돌리고 실행을 실패로 기록한다."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_CONFIG, f"{self.prog}: 오류: {message}\n")


def cache_dir():
    return os.environ.get("CACHE_DIR", "var/cache")


def date_arg(value):
    try:
        return runs.collection_date(value, None)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def parse(argv):
    parser = Parser(prog="gpu_collector.erd")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    sub.add_parser("init")
    c = sub.add_parser("collect")
    c.add_argument("provider", choices=sorted(COLLECTORS))
    c.add_argument("--date", type=date_arg, help="수집 기준일 YYYY-MM-DD(KST, 생략하면 오늘)")
    c.add_argument("--only", help="쉼표로 구분한 data_source.source_name (aws만)")
    c.add_argument("--trigger", default="manual", choices=["manual", "scheduled", "retry"])
    b = sub.add_parser("backfill")
    b.add_argument("provider", choices=sorted(set(COLLECTORS) - NO_BACKFILL))
    b.add_argument("--since", help="YYYY-MM부터 (nebius 외 필수). fx는 YYYY-MM-DD")
    b.add_argument("--days", type=int, default=29, help="Nebius 스팟 이력 일수(최대 29)")
    b.add_argument("--until", help="YYYY-MM까지(생략하면 이번 달). fx는 YYYY-MM-DD(생략하면 오늘)")
    b.add_argument("--max-calls", type=int, default=fx.MAX_CALLS, help="fx: 이번 실행의 API 호출 상한")
    b.add_argument("--only", help="쉼표로 구분한 과거 자료 data_source.source_name")
    b.add_argument("--regions", help="쉼표로 구분한 리전(생략하면 대상 전체)")
    b.add_argument("--spot-days", type=int, default=aws_history.SPOT_DAYS)
    b.add_argument("--keep-files", action="store_true", help="받은 과거 가격 파일을 지우지 않는다")
    b.add_argument("--workers", type=int, default=3, help="미리 받을 파일 수")
    b.add_argument("--every", default="month", choices=["month", "week", "day"], help="Azure 스냅샷 간격")
    b.add_argument("--no-verify", action="store_true", help="Azure·Verda·Oracle: 공식 API와 대조하지 않는다")
    cv = sub.add_parser("convert")
    cv.add_argument("target", choices=["krw"])
    cv.add_argument("--since", help="관측일(KST) YYYY-MM-DD부터만 (생략하면 전체)")
    cv.add_argument("--trigger", default="manual", choices=["manual", "scheduled", "retry"])
    st = sub.add_parser("status")
    st.add_argument("action", choices=["record"])
    sp = sub.add_parser("specs")
    sp.add_argument("provider", choices=["aws", "oracle"])
    r = sub.add_parser("report")
    r.add_argument("--model", default="B300")
    args = parser.parse_args(argv)
    if args.command == "backfill" and args.provider != "nebius" and not args.since:
        parser.error("--since가 필요하다")
    return args


def lock_name(args):
    """같은 이름끼리 동시에 실행하지 않는다. 사업자의 collect·backfill·specs는 같은 이름을 쓴다."""
    if args.command in ("collect", "backfill", "specs"):
        return args.provider
    if args.command == "convert":
        return krw.LOCK
    if args.command == "status":
        return "status"
    return None


def main(argv=None):
    args = parse(argv)
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL 환경 변수가 필요하다", file=sys.stderr)
        return EXIT_CONFIG
    signal.signal(signal.SIGTERM, _terminate)
    try:
        conn = psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_S, options=DB_OPTIONS)
    except psycopg.OperationalError as error:
        print(f"DB 접속 실패: {type(error).__name__}", file=sys.stderr)
        return EXIT_FAILED
    with conn:
        name = lock_name(args)
        abandoned = []
        if name:
            if not runs.try_lock(conn, name):
                print(json.dumps({"status": "locked", "lock": name, "reason": "같은 이름의 실행이 진행 중"},
                                 ensure_ascii=False))
                return EXIT_LOCKED
            abandoned = runs.close_abandoned(conn, name, datetime.now(timezone.utc))
        try:
            return run(conn, args, abandoned)
        except LookupError as error:
            # 수집 대상·기준 정보가 없음(seed 미적용 등). 다시 시도해도 같다
            print(f"설정 오류: {error}", file=sys.stderr)
            return EXIT_CONFIG
        except Terminated:
            print("SIGTERM: 실행을 실패로 기록하고 끝냄", file=sys.stderr)
            return EXIT_PARTIAL


def run(conn, args, abandoned):
    if args.command == "init":
        print(json.dumps(init(conn), ensure_ascii=False))
        return EXIT_OK
    if args.command == "report":
        report(conn, args.model)
        return EXIT_OK
    if args.command == "status":
        summary = status.record(conn)
    elif args.command == "convert":
        summary = krw.convert(conn, since=args.since, trigger_type=args.trigger)
    elif args.command == "specs":
        summary = (oracle if args.provider == "oracle" else aws).attach_specs(conn)
    elif args.command == "collect":
        kwargs = {"trigger_type": args.trigger, "date": args.date}
        if args.provider == "aws":
            kwargs.update(only=set(args.only.split(",")) if args.only else None, cache_dir=cache_dir())
        summary = COLLECTORS[args.provider].collect(conn, **kwargs)
    else:
        summary = backfill(conn, args)
        failed = [g for g in summary["groups"] if g["status"] != "success"]
        extra = {k: v for k, v in summary.items() if k not in ("run_id", "status", "groups")}
        summary = {"run_id": summary["run_id"], "status": summary["status"], **extra,
                   "groups": len(summary["groups"]), "failed": failed}
    if abandoned:
        summary = {**summary, "closed_abandoned_runs": abandoned}
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return EXIT_BY_STATUS.get(summary["status"], EXIT_PARTIAL)


def backfill(conn, args):
    only = set(args.only.split(",")) if args.only else None
    if args.provider == "fx":
        return fx.backfill(conn, since=args.since, until=args.until, max_calls=args.max_calls)
    if args.provider == "oracle":
        return oracle_backfill(conn, args, only)
    if args.provider == "scaleway":
        return scaleway_history.backfill(conn, since=args.since, until=args.until)
    if args.provider == "hyperstack":
        return hyperstack_history.backfill(conn, since=args.since, until=args.until)
    if args.provider == "nebius":
        return nebius_history.backfill(conn, days=args.days, verify=not args.no_verify, only=only)
    if args.provider == "verda":
        return verda_history.backfill(conn, since=args.since, until=args.until, verify=not args.no_verify)
    if args.provider == "azure":
        return azure_history.backfill(conn, since=args.since, until=args.until, every=args.every,
                                      cache_dir=cache_dir(), keep_files=args.keep_files, verify=not args.no_verify)
    return aws_history.backfill(conn, since=args.since, until=args.until, only=only,
                                regions=set(args.regions.split(",")) if args.regions else None,
                                spot_days=args.spot_days, cache_dir=cache_dir(), keep_files=args.keep_files,
                                workers=args.workers)


def oracle_backfill(conn, args, only):
    """Oracle 과거 이력: Wayback 정가 API 저장본, SkyPilot 카탈로그를 차례로 적재한다(--only로 고를 수 있다)."""
    sources = [(oracle_history.SOURCE_WAYBACK, oracle_history.backfill), (oracle_skypilot.SOURCE_SKYPILOT, oracle_skypilot.backfill)]
    summaries = [load(conn, since=args.since, until=args.until, verify=not args.no_verify)
                 for name, load in sources if only is None or name in only]
    if not summaries:
        raise LookupError(f"--only에 Oracle 과거 자료 출처가 없음: {', '.join(name for name, _ in sources)}")
    statuses = {s["status"] for s in summaries}
    status = "success" if statuses == {"success"} else ("failed" if statuses == {"failed"} else "partial_failure")
    return {"run_id": [s["run_id"] for s in summaries], "status": status,
            "groups": [g for s in summaries for g in s["groups"]]}


def _terminate(signum, frame):
    raise Terminated()


def init(conn):
    exists = conn.execute("SELECT to_regclass('public.provider') IS NOT NULL").fetchone()[0]
    if not exists:
        conn.execute(DDL.read_text(encoding="utf8"))
    conn.execute(SEED.read_text(encoding="utf8"))
    conn.commit()
    targets = conn.execute("SELECT count(*) FROM collection_target WHERE enabled").fetchone()[0]
    return {"ddl_applied": not exists, "enabled_targets": targets}


def report(conn, model):
    print(f"## {model} 최신 가격 (오퍼별 마지막 관측, USD/GPU·시간)")
    rows = conn.execute(
        """SELECT DISTINCT ON (o.offer_id) pv.provider_name, p.region_code, o.contract_type, o.contract_product_type,
                  o.commitment_months, o.payment_option, n.nominal_usd_gpu_hour, n.effective_usd_gpu_hour,
                  n.result_status, ob.collected_at, ob.effective_from
           FROM price_observation ob
           JOIN offer o ON o.offer_id = ob.offer_id
           JOIN product p ON p.product_id = o.product_id
           JOIN provider pv ON pv.provider_id = p.provider_id
           JOIN gpu_model m ON m.gpu_model_id = p.gpu_model_id
           LEFT JOIN price_normalization n ON n.observation_id = ob.observation_id
           WHERE m.model_name = %s
           ORDER BY o.offer_id, ob.collected_at DESC, ob.observation_id DESC""", (model,)).fetchall()
    rows.sort(key=lambda r: (r[0], r[1] or "", r[2], r[3] or "", r[4] or 0, r[5] or ""))
    for provider, region, contract, product_type, months, payment, nominal, effective, status, collected, _eff in rows:
        fmt = lambda v: f"{v:.4f}" if v is not None else "-"
        print(f"{provider:6} {region or '?':15} {contract:10} {product_type or '':28} {months or '':>3} "
              f"{payment or '':16} 명목 {fmt(nominal):>9} 실효 {fmt(effective):>9} {status} {collected:%Y-%m-%d %H:%M}")
    print(f"\n## {model} 대상별 마지막 점검")
    rows = conn.execute(
        """SELECT DISTINCT ON (t.target_id) s.source_name, t.region_filter, t.contract_filter, c.disclosure_status,
                  c.processing_status, c.checked_at, c.reason
           FROM collection_target t
           JOIN gpu_model m ON m.gpu_model_id = t.gpu_model_id
           JOIN data_source s ON s.source_id = t.source_id
           JOIN collection_check c ON c.target_id = t.target_id AND c.check_scope = 'target'
           WHERE m.model_name = %s
           ORDER BY t.target_id, c.checked_at DESC, c.check_id DESC""", (model,)).fetchall()
    for source, region, contract, disclosure, processing, checked, reason in rows:
        print(f"{source:30} {region or '-':15} {contract or '-':20} {disclosure:14} {processing:18} "
              f"{checked:%Y-%m-%d %H:%M} {reason or ''}")


if __name__ == "__main__":
    sys.exit(main())
