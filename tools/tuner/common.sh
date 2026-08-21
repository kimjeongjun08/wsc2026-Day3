#!/usr/bin/env bash
# common.sh — 클러스터/AWS 공통 헬퍼. 채점 서버와 무관하며 대회 환경에서 그대로 돈다.
set -uo pipefail
# ★프로파일을 강제하지 않는다.
#   예전엔 ${AWS_PROFILE:-lee} 였다. 'lee' 는 내 연습 환경 이름이고, 대회장이나
#   다른 사람 PC 에서는 존재하지 않는 프로파일을 가리키게 된다. 안 정해져 있으면
#   AWS CLI 의 기본 동작(default 프로파일 / 환경변수 / 인스턴스 역할)에 맡긴다.
[ -n "${AWS_PROFILE:-}" ] && export AWS_PROFILE
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-ap-northeast-2}
# 숫자 포맷만 C 로 고정한다. 로케일에 따라 awk/printf 가 소수점을 쉼표로 찍으면
# (예: de_DE) 파이썬 float 파싱이 깨진다. 문자 인코딩은 건드리지 않는다.
export LC_NUMERIC=C
# ★AWS 호출이 영원히 매달리지 않게 한다.
#   운영 루프는 한 주기가 60초다. 그 안에서 aws 호출 하나가 기본값(연결 60초,
#   읽기 60초, 재시도 최대)으로 물리면 주기가 통째로 밀리고, 그동안 트래픽은
#   대응 없이 흐른다. 대회에서 이건 회차를 버리는 것과 같다.
#   짧게 끊고 다음 주기에 다시 시도하는 쪽이 항상 낫다 — 지표는 1분마다 갱신되니까.
export AWS_MAX_ATTEMPTS=${AWS_MAX_ATTEMPTS:-2}
export AWS_RETRY_MODE=${AWS_RETRY_MODE:-standard}
export AWS_CLI_CONNECT_TIMEOUT=${AWS_CLI_CONNECT_TIMEOUT:-5}
export AWS_CLI_READ_TIMEOUT=${AWS_CLI_READ_TIMEOUT:-15}

# 겉옷: 어떤 명령이든 시간 상한을 씌운다. timeout 이 없는 환경도 있으므로 확인한다.
if command -v timeout >/dev/null 2>&1; then
  cap() { timeout "${1}s" "${@:2}"; }
else
  cap() { shift; "$@"; }
fi
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

# 파일 수정시각(epoch). GNU stat 과 BSD(macOS) stat 은 옵션이 다르고,
# 리눅스에서 `stat -f` 는 에러가 아니라 파일시스템 정보를 성공적으로 뱉는다.
# 그래서 `stat -f ... || stat -c ...` 식 fallback 은 리눅스에서 동작하지 않는다.
# 숫자가 나왔는지까지 확인해야 안전하다.
mtime() {
  local v
  v=$(stat -c %Y "$1" 2>/dev/null); case "$v" in ''|*[!0-9]*) ;; *) echo "$v"; return 0;; esac
  v=$(stat -f %m "$1" 2>/dev/null); case "$v" in ''|*[!0-9]*) ;; *) echo "$v"; return 0;; esac
  echo 0; return 1
}
