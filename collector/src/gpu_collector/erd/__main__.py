"""ERD 초안 스키마 명령.

  python -m gpu_collector.erd init                     DDL(빈 DB일 때만) + 기준 정보(sql/seed.sql) 적용
  python -m gpu_collector.erd collect aws [--only aws_ec2_price_list,aws_savings_plans_price_list,aws_ec2_spot_api]
                                          [--trigger manual|scheduled|retry]
  python -m gpu_collector.erd backfill aws --since 2023-07 [--until 2026-11] [--only ...] [--regions ...] [--spot-days 90]
                                          [--keep-files] [--workers 3]
                                                       공식 과거 자료 적재(가격 파일 과거 버전, 스팟 90일)
  python -m gpu_collector.erd collect azure [--trigger ...]   Azure Retail Prices API (대상 SKU 전 리전)
  python -m gpu_collector.erd backfill azure --since 2025-10 [--until 2026-11] [--every month|week|day] [--keep-files]
                                                       [--no-verify]
                                                       GitHub 일별 스냅샷(집계 출처)으로 Azure 과거 가격 적재
  python -m gpu_collector.erd collect verda [--trigger ...]   Verda 공개 API (지역별 온디맨드·스팟, 약정 할인율 가정)
  python -m gpu_collector.erd backfill verda --since 2026-08 [--until 2026-11] [--no-verify]
                                                       Wayback 저장본(집계 출처)으로 Verda 과거 가격 적재
  python -m gpu_collector.erd collect nebius [--trigger ...]  Nebius 문서 가격표(온디맨드·약정 상태) + 공개 스팟 API
  python -m gpu_collector.erd backfill nebius [--days 29] [--only spot,docs] [--no-verify]
                                                       스팟 30일 이력(공식 API) + 문서 Wayback 저장본(집계)
  python -m gpu_collector.erd status record            랙스케일·출시 예정 모델 공개 상태 수기 확인 기록(erd/status.py, 월 1회)
  python -m gpu_collector.erd specs aws                 이미 적재한 AWS 상품에 모델 원문·사양 근거(상호연결)를 채운다(1회)
  python -m gpu_collector.erd report [--model B300]    최신 가격과 대상별 마지막 점검 상태

환경 변수: DATABASE_URL(필수, 적재 대상 DB), CACHE_DIR(선택, 원천 파일 캐시. 기본 ./var/cache).
AWS 가격 API는 AWS_ACCESS_KEY_ID·AWS_SECRET_ACCESS_KEY 환경 변수가 필요하다.
"""
import argparse
import json
import os
import sys
from importlib import resources

import psycopg

from .aws import collect as aws, history as aws_history
from .azure import collect as azure, history as azure_history
from .verda import collect as verda, history as verda_history
from .nebius import collect as nebius, history as nebius_history
from . import status

SQL = resources.files("gpu_collector") / "sql"
DDL = SQL / "ddl.sql"
SEED = SQL / "seed.sql"


def database_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL 환경 변수가 필요하다")
    return url


def cache_dir():
    return os.environ.get("CACHE_DIR", "var/cache")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="gpu_collector.erd")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    c = sub.add_parser("collect")
    c.add_argument("provider", choices=["aws", "azure", "verda", "nebius"])
    c.add_argument("--only", help="쉼표로 구분한 data_source.source_name")
    c.add_argument("--trigger", default="manual", choices=["manual", "scheduled", "retry"])
    b = sub.add_parser("backfill")
    b.add_argument("provider", choices=["aws", "azure", "verda", "nebius"])
    b.add_argument("--since", help="YYYY-MM부터 (aws·azure·verda 필수)")
    b.add_argument("--days", type=int, default=29, help="Nebius 스팟 이력 일수(최대 29)")
    b.add_argument("--until", help="YYYY-MM까지(생략하면 이번 달)")
    b.add_argument("--only", help="쉼표로 구분한 과거 자료 data_source.source_name")
    b.add_argument("--regions", help="쉼표로 구분한 리전(생략하면 대상 전체)")
    b.add_argument("--spot-days", type=int, default=aws_history.SPOT_DAYS)
    b.add_argument("--keep-files", action="store_true", help="받은 과거 가격 파일을 지우지 않는다")
    b.add_argument("--workers", type=int, default=3, help="미리 받을 파일 수")
    b.add_argument("--every", default="month", choices=["month", "week", "day"], help="Azure 스냅샷 간격")
    b.add_argument("--no-verify", action="store_true", help="Azure·Verda: 공식 API와 대조하지 않는다")
    st = sub.add_parser("status")
    st.add_argument("action", choices=["record"])
    sp = sub.add_parser("specs")
    sp.add_argument("provider", choices=["aws"])
    r = sub.add_parser("report")
    r.add_argument("--model", default="B300")
    args = parser.parse_args(argv)

    with psycopg.connect(database_url()) as conn:
        if args.command == "init":
            print(json.dumps(init(conn), ensure_ascii=False))
        elif args.command == "status":
            print(json.dumps(status.record(conn), ensure_ascii=False))
        elif args.command == "specs":
            print(json.dumps(aws.attach_specs(conn), ensure_ascii=False))
        elif args.command == "collect":
            only = set(args.only.split(",")) if args.only else None
            if args.provider == "azure":
                summary = azure.collect(conn, trigger_type=args.trigger)
            elif args.provider == "verda":
                summary = verda.collect(conn, trigger_type=args.trigger)
            elif args.provider == "nebius":
                summary = nebius.collect(conn, trigger_type=args.trigger)
            else:
                summary = aws.collect(conn, trigger_type=args.trigger, only=only,
                                      cache_dir=cache_dir())
            print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
            return 0 if summary["status"] == "success" else 1
        elif args.command == "backfill" and args.provider == "nebius":
            summary = nebius_history.backfill(conn, days=args.days, verify=not args.no_verify,
                                              only=set(args.only.split(",")) if args.only else None)
            failed = [g for g in summary["groups"] if g["status"] != "success"]
            print(json.dumps({"run_id": summary["run_id"], "status": summary["status"],
                              "groups": len(summary["groups"]), "failed": failed}, ensure_ascii=False, indent=2,
                             default=str))
            return 0 if summary["status"] == "success" else 1
        elif args.command == "backfill" and not args.since:
            parser.error("--since가 필요하다")
        elif args.command == "backfill" and args.provider == "verda":
            summary = verda_history.backfill(conn, since=args.since, until=args.until, verify=not args.no_verify)
            failed = [g for g in summary["groups"] if g["status"] != "success"]
            print(json.dumps({"run_id": summary["run_id"], "status": summary["status"],
                              "groups": len(summary["groups"]), "failed": failed}, ensure_ascii=False, indent=2,
                             default=str))
            return 0 if summary["status"] == "success" else 1
        elif args.command == "backfill" and args.provider == "azure":
            summary = azure_history.backfill(
                conn, since=args.since, until=args.until, every=args.every,
                cache_dir=cache_dir(),
                keep_files=args.keep_files, verify=not args.no_verify)
            failed = [g for g in summary["groups"] if g["status"] != "success"]
            print(json.dumps({"run_id": summary["run_id"], "status": summary["status"],
                              "groups": len(summary["groups"]), "failed": failed}, ensure_ascii=False, indent=2,
                             default=str))
            return 0 if summary["status"] == "success" else 1
        elif args.command == "backfill":
            summary = aws_history.backfill(
                conn, since=args.since, until=args.until,
                only=set(args.only.split(",")) if args.only else None,
                regions=set(args.regions.split(",")) if args.regions else None, spot_days=args.spot_days,
                cache_dir=cache_dir(),
                keep_files=args.keep_files, workers=args.workers)
            failed = [g for g in summary["groups"] if g["status"] != "success"]
            print(json.dumps({"run_id": summary["run_id"], "status": summary["status"],
                              "groups": len(summary["groups"]), "failed": failed}, ensure_ascii=False, indent=2,
                             default=str))
            return 0 if summary["status"] == "success" else 1
        else:
            report(conn, args.model)
    return 0


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
