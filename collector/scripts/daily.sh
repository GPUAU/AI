#!/usr/bin/env bash
# 매일 수집 순서 (collector README): 환율 → 사업자별 수집 → 원화 환산.
# - Scaleway는 EUR→USD 환산에 그날 환율을 쓰므로 fx 다음에 실행한다.
# - 키가 없는 수집기는 건너뛴다(Secret 추가 전에도 나머지는 수집).
# - 한 사업자가 실패해도 나머지는 계속하고, 끝에 실패가 있으면 1로 끝낸다(Job 재시도는 하지 않음).
set -uo pipefail
TRIGGER=${COLLECT_TRIGGER:-scheduled}
failed=()

run() {
  echo "::: $*"
  python -m gpu_collector.erd "$@" --trigger "$TRIGGER"
  local code=$?
  echo "::: $* -> exit $code"
  [ "$code" -eq 0 ] || failed+=("$*($code)")
}

has() { for name in "$@"; do [ -n "${!name:-}" ] || return 1; done; }

if has KOREAEXIM_API_KEY; then run collect fx; else echo "::: skip collect fx (KOREAEXIM_API_KEY 없음)"; fi
if has AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; then run collect aws; else echo "::: skip collect aws (AWS 키 없음)"; fi
for provider in azure verda nebius oracle scaleway; do run collect "$provider"; done
if has HYPERSTACK_API_KEY; then run collect hyperstack; else echo "::: skip collect hyperstack (HYPERSTACK_API_KEY 없음)"; fi
run convert krw

if [ ${#failed[@]} -gt 0 ]; then
  echo "::: 실패: ${failed[*]}"
  exit 1
fi
echo "::: 완료"
