#!/usr/bin/env bash
# 기존 ERD DB의 데이터를 현재 스키마(패키지 sql/ddl.sql)를 적용한 빈 DB로 옮긴다.
#
#   scripts/migrate_data.sh <원본 DB URL> <대상 DB URL>
#
# - 대상 DB는 미리 만들어 두고 비어 있어야 한다(테이블이 있으면 중단).
# - 대상에 DDL만 적용한다. 기준 정보(사업자·출처 등)도 원본에서 ID 그대로 옮기므로 seed는 적용하지 않는다.
# - 테이블은 의존 순서대로, 전체를 트랜잭션 하나로 적재한다.
#   · 점검 행 검사 트리거가 상품·수집 대상을 바로 조회하므로 상품·오퍼를 점검보다 먼저 넣는다.
#   · 상호연결 근거 검사는 커밋 시점에 실행되므로 상품과 근거가 모두 들어간 뒤 한 번에 커밋한다.
# - 끝나면 테이블 건수와 identity 시퀀스를 원본과 비교한다.
# 필요한 것: psql·pg_dump(PostgreSQL 17), gpu_collector 패키지가 설치된 python
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "사용법: $0 <원본 DB URL> <대상 DB URL>" >&2
  exit 2
fi
SOURCE=$1
TARGET=$2
PYTHON=${PYTHON:-python}

TABLES=(provider gpu_model data_source normalization_rule collection_target collection_run product offer
        collection_check source_evidence product_evidence price_observation price_component price_normalization
        collection_alert exchange_rate krw_conversion)

existing=$(psql "$TARGET" -Atc "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
if [ "$existing" != "0" ]; then
  echo "대상 DB에 테이블이 ${existing}개 있다. 빈 DB에만 옮긴다." >&2
  exit 1
fi

ddl=$("$PYTHON" -c 'from importlib import resources; print(resources.files("gpu_collector") / "sql" / "ddl.sql")')
echo "1/3 대상 DB에 DDL 적용: $ddl"
psql -q -v ON_ERROR_STOP=1 "$TARGET" -f "$ddl"

echo "2/3 데이터 적재(테이블 ${#TABLES[@]}개, 단일 트랜잭션)"
# price_observation은 정정 관측이 이전 관측을 가리키는 자기 참조가 있어 pg_dump가 '참조키 교차' 경고를 낸다.
# 행이 ID 순서로 들어가고 트랜잭션 하나로 적재하므로 문제가 되지 않는다.
{
  for table in "${TABLES[@]}"; do
    pg_dump --data-only --table="public.$table" "$SOURCE"
  done
} | psql -q -v ON_ERROR_STOP=1 --single-transaction "$TARGET" > /dev/null

echo "3/3 검증"
failed=0
for table in "${TABLES[@]}"; do
  a=$(psql "$SOURCE" -Atc "SELECT count(*) FROM $table")
  b=$(psql "$TARGET" -Atc "SELECT count(*) FROM $table")
  if [ "$a" = "$b" ]; then mark=OK; else mark=DIFF; failed=1; fi
  printf '  %-22s 원본 %8s  대상 %8s  %s\n' "$table" "$a" "$b" "$mark"
done

# identity 시퀀스가 테이블 최댓값 이상인지(이후 INSERT가 기존 ID와 겹치지 않는지)
behind=$(psql "$TARGET" -At <<'SQL'
SELECT string_agg(format('%s.%s', c.table_name, c.column_name), ', ')
FROM information_schema.columns c
WHERE c.table_schema = 'public' AND c.is_identity = 'YES'
  AND COALESCE((xpath('/row/m/text()', query_to_xml(
          format('SELECT max(%I) AS m FROM %I', c.column_name, c.table_name), false, false, '')))[1]::text::bigint, 0)
      > COALESCE((SELECT last_value FROM pg_sequences s
                  WHERE format('%I.%I', s.schemaname, s.sequencename)
                        = pg_get_serial_sequence(format('%I', c.table_name), c.column_name)), 0)
SQL
)
if [ -n "$behind" ]; then
  echo "  identity 시퀀스가 최댓값보다 뒤처짐: $behind"
  failed=1
else
  echo "  identity 시퀀스: 모두 최댓값 이상"
fi

if [ "$failed" -ne 0 ]; then
  echo "검증 실패" >&2
  exit 1
fi
echo "완료"
