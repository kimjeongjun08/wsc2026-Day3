#!/usr/bin/env bash
# common.sh — 클러스터/AWS 공통 헬퍼. 채점 서버와 무관하며 대회 환경에서 그대로 돈다.
set -uo pipefail
export AWS_PROFILE=${AWS_PROFILE:-lee}
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-ap-northeast-2}
BASTION=${BASTION:-}
NS=${NS:-apdev}

# 클러스터에 대고 셸 스크립트를 실행하고 stdout 을 돌려준다.
#   기본은 로컬 kubectl 이다. 과제 스펙이 t3.medium 외 인스턴스를 금지하고
#   미사용 EC2 를 감점하므로 bastion 없이 도는 게 정상 구성이어야 한다.
#   EKS API 에 로컬로 못 붙는 환경에서만 USE_SSM=1 로 bastion 을 경유한다.
bx() {
  local script="$1" tmp cid st
  if [ "${USE_SSM:-0}" != "1" ]; then
    bash -c "$script"
    return $?
  fi
  tmp=$(mktemp)
  python3 -c 'import json,sys; print(json.dumps({"commands":["export KUBECONFIG=/root/.kube/config", sys.argv[1]]}))' "$script" > "$tmp"
  cid=$(aws ssm send-command --instance-ids "$BASTION" --document-name AWS-RunShellScript \
        --parameters file://"$tmp" --query 'Command.CommandId' --output text 2>/dev/null)
  rm -f "$tmp"
  [ -z "$cid" ] && { echo "SSM_SEND_FAILED" >&2; return 1; }
  while :; do
    st=$(aws ssm get-command-invocation --command-id "$cid" --instance-id "$BASTION" --query Status --output text 2>/dev/null)
    [ "$st" = "InProgress" ] || [ "$st" = "Pending" ] || break
    sleep 5
  done
  aws ssm get-command-invocation --command-id "$cid" --instance-id "$BASTION" \
     --query 'StandardOutputContent' --output text 2>/dev/null
}
