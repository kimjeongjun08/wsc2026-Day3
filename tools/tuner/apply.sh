#!/usr/bin/env bash
# apply.sh <공유노드수> — 솔버가 고른 노드 수를 실제로 고정한다.
#
# 왜 minDomains 인가:
#   Karpenter 는 Pending 파드를 봐야 노드를 만든다. 그런데 스케줄 가능한 노드가
#   1대뿐이면 topologySpread 의 skew 가 항상 0 이라 파드가 Pending 되지 않고
#   한 노드에 쌓인다 → 노드가 안 늘어난다. minDomains=N 은 도메인이 N 개 될 때까지
#   파드를 Pending 시켜서 Karpenter 를 미리 깨운다.
#   즉 "부하가 오면 늘린다"(사후)가 아니라 "N대를 항상 유지한다"(사전)가 된다.
#   스파이크 시점·크기를 몰라도 동작한다.
#
# 총 노드 = 공유노드수 + stress 전용 1대
set -uo pipefail
cd "$(dirname "$0")"; source ./lib.sh
MD=${1:?사용법: apply.sh <공유노드수>}

VCPU=${VCPU:-2}
LIMIT=$((MD*VCPU))
echo "== 공유 노드 $MD 대로 고정 (총 $((MD+1))대, stress 전용 1대 포함)"
echo "-- 상한: NodePool limits.cpu=$LIMIT (노드 $MD 대분). minDomains 는 하한이라"
echo "   상한을 안 걸면 HPA 가 파드를 늘리는 만큼 Karpenter 가 노드를 계속 붙인다(실측 9대)."
bx "kubectl patch nodepool apdev-pool --type=merge -p '{\"spec\":{\"limits\":{\"cpu\":\"$LIMIT\"}}}' >/dev/null
kubectl patch nodepool apdev-stress-pool --type=merge -p '{\"spec\":{\"limits\":{\"cpu\":\"$VCPU\"}}}' >/dev/null
echo '   apdev-pool limits.cpu='\$(kubectl get nodepool apdev-pool -o jsonpath='{.spec.limits.cpu}')' / stress='\$(kubectl get nodepool apdev-stress-pool -o jsonpath='{.spec.limits.cpu}')"
bx "for d in user product; do
  kubectl -n $NS patch deploy \$d --type=json \
    -p='[{\"op\":\"replace\",\"path\":\"/spec/template/spec/topologySpreadConstraints/0/minDomains\",\"value\":$MD}]' >/dev/null \
    || kubectl -n $NS patch deploy \$d --type=json \
       -p='[{\"op\":\"add\",\"path\":\"/spec/template/spec/topologySpreadConstraints/0/minDomains\",\"value\":$MD}]' >/dev/null
done
kubectl -n $NS rollout status deploy/user --timeout=300s | tail -1
kubectl -n $NS rollout status deploy/product --timeout=300s | tail -1"

WANT=$((MD+1))
echo "-- 노드 $WANT 대로 수렴 대기 (증가/감소 양방향)"
for i in $(seq 1 40); do
  n=$(bx "kubectl get nodes --no-headers | wc -l" | tr -d ' \n')
  echo "   ${i}: ${n}대 (목표 $WANT)"
  [ "${n:-0}" = "$WANT" ] && break
  sleep 20
done
bx "kubectl get nodes -o custom-columns=N:.metadata.name,ROLE:.metadata.labels.role --no-headers
echo ---
kubectl get pods -n $NS -o wide --no-headers | awk '{print \$1, \$7}' | sed 's/-[a-z0-9]*-[a-z0-9]*  */ /' | sort | uniq -c | sort -rn | head"
