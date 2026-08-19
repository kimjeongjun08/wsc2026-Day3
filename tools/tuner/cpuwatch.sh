#!/usr/bin/env bash
# cpuwatch.sh <초> — 부하 회차 중 앱별 누적 CPU(커널 카운터)와 파드 수를 주기적으로 찍는다.
#   목적: 격리 프로파일의 d 와 "실제 부하 중 d" 를 같은 단위로 비교하기 위함.
#   출력: ts,app,cpu_sec_total,pods   (cpu_sec_total 은 앱의 모든 파드 합산 누적치)
set -uo pipefail
cd "$(dirname "$0")"; source ./lib.sh
INT=${1:-60}
while :; do
  bx '
NODES=$(kubectl get nodes -o jsonpath="{range .items[*]}{.metadata.name} {end}")
declare -A CPU POD
for N in $NODES; do
  kubectl get --raw "/api/v1/nodes/$N/proxy/metrics/resource" 2>/dev/null \
  | awk "/^container_cpu_usage_seconds_total/{print}" \
  | sed -n "s/.*pod=\"\([^\"]*\)\".*} \([0-9.e+-]*\) .*/\1 \2/p"
done | awk "{split(\$1,a,\"-\"); app=a[1]; if(app==\"user\"||app==\"product\"||app==\"stress\"){c[app]+=\$2; n[app]++}} END{for(k in c) printf \"%s %.3f %d\n\", k, c[k], n[k]}"
' | awk -v t="$(date +%s)" 'NF>=3{print t","$1","$2","$3}'
  sleep "$INT"
done
