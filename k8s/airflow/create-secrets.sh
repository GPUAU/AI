#!/usr/bin/env bash
# Airflow Secret 2개를 만들거나 갱신한다(개인 kubeconfig로 실행, 캠퍼스망). 배포 workflow 역할은 Secret을 못 만든다.
#
#   export KUBECONFIG=~/.kube/u1-3.yaml
#   bash k8s/airflow/create-secrets.sh
#
# - airflow-secrets: 수집 키·Slack·JWT·Fernet (pod에 환경 변수로 들어감)
# - airflow-auth:    UI 비밀번호 파일 passwords.json (admin·team)
# 값은 화면·셸 기록에 남기지 않는다(입력 숨김, 임시 파일은 끝나면 지움). 다시 실행하면 새 값으로 덮어쓴다.
# 주의: 다시 실행하면 JWT·Fernet도 새로 만든다. Fernet이 바뀌면 UI에서 저장한 Connection·Variable 비밀값을 못 읽는다.
set -euo pipefail
NS=u1-3
tmp=$(mktemp -d)
chmod 700 "$tmp"
trap 'rm -rf "$tmp"' EXIT

ask() {   # ask 변수명 안내문 → 입력값(숨김)
  local value
  read -rs -p "$2: " value; echo >&2
  [ -n "$value" ] || { echo "$1 값이 비었다. 중단" >&2; exit 1; }
  printf '%s' "$value"
}

koreaexim=$(ask KOREAEXIM_API_KEY "수출입은행 환율 API 키")
hyperstack=$(ask HYPERSTACK_API_KEY "Hyperstack API 키")
aws_id=$(ask COLLECTOR_AWS_ACCESS_KEY_ID "AWS 가격 조회 전용 Access Key ID")
aws_secret=$(ask COLLECTOR_AWS_SECRET_ACCESS_KEY "AWS 가격 조회 전용 Secret Access Key")
slack=$(ask SLACK_WEBHOOK_URL "Slack 웹훅 URL")
admin_pw=$(ask ADMIN_PASSWORD "Airflow admin 비밀번호")
team_pw=$(ask TEAM_PASSWORD "Airflow team(읽기 전용) 비밀번호")

jwt=$(openssl rand -hex 32)
fernet=$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')

# --from-env-file은 '=' 뒤를 그대로 값으로 읽는다(따옴표 처리 없음)
cat > "$tmp/secrets.env" <<EOF
KOREAEXIM_API_KEY=$koreaexim
HYPERSTACK_API_KEY=$hyperstack
COLLECTOR_AWS_ACCESS_KEY_ID=$aws_id
COLLECTOR_AWS_SECRET_ACCESS_KEY=$aws_secret
AIRFLOW_CONN_SLACK_DEFAULT={"conn_type": "slackwebhook", "password": "$slack"}
AIRFLOW__API_AUTH__JWT_SECRET=$jwt
AIRFLOW__CORE__FERNET_KEY=$fernet
EOF
python3 - "$admin_pw" "$team_pw" > "$tmp/passwords.json" <<'EOF'
import json, sys
print(json.dumps({"admin": sys.argv[1], "team": sys.argv[2]}))
EOF

kubectl -n "$NS" create secret generic airflow-secrets --from-env-file="$tmp/secrets.env" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" create secret generic airflow-auth --from-file=passwords.json="$tmp/passwords.json" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" get secret airflow-secrets airflow-auth
echo "완료. 값은 출력하지 않았다. pod가 이미 떠 있으면 다시 시작해야 새 값을 읽는다:"
echo "  kubectl -n $NS rollout restart deployment/airflow-scheduler deployment/airflow-dag-processor deployment/airflow-api-server"
