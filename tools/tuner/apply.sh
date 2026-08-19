#!/usr/bin/env bash
# apply.sh <총노드수> [iso|shared] — 솔버가 고른 구성을 실제로 고정한다.
#
#   총노드수 T    : 채점의 avg_ec2 와 같은 단위. MNG 1대 + Karpenter 노드들.
#   iso           : stress 를 taint 된 전용 노드에 격리 (기본)
#   shared        : stress 를 user/product 와 같은 노드에 태운다 (노드 1대 절약)
#
# 왜 minDomains 인가:
#   Karpenter 는 Pending 파드를 봐야 노드를 만든다. 그런데 스케줄 가능한 노드가
#   1대뿐이면 topologySpread 의 skew 가 항상 0 이라 파드가 Pending 되지 않고
#   한 노드에 쌓인다 → 노드가 안 늘어난다. minDomains=N 은 도메인이 N 개 될 때까지
#   파드를 Pending 시켜서 Karpenter 를 미리 깨운다.
#   즉 "부하가 오면 늘린다"(사후)가 아니라 "N대를 항상 유지한다"(사전)가 된다.
#   스파이크 시점·크기를 몰라도 동작한다.
#
# 왜 상한(limits.cpu)도 같이 거는가:
#   minDomains 는 하한만 정한다. 상한이 없으면 HPA 가 파드를 늘리는 만큼
#   Karpenter 가 노드를 계속 붙인다 (실측: x1.0 에서 9대 → 비용 0/12).
set -uo pipefail
cd "$(dirname "$0")"; source ./lib.sh
T=${1:?사용법: apply.sh <총노드수> [iso|shared]}
MODE=${2:-iso}
VCPU=${VCPU:-2}

# MODE: shared | iso | iso2 | iso3 ...  (iso 뒤 숫자 = stress 전용 노드 수)
#   stress 는 요청 하나가 235ms 라 파드 1개(2코어)가 4~5rps 에서 포화한다.
#   stress 트래픽이 크면 전용 노드가 2대 이상 필요하다.
if [ "$MODE" = "shared" ]; then
  ISO=0
  DOMAINS=$T                       # 모든 노드가 user/product 의 도메인
  KARP=$((T-1)); STRESS_LIMIT=0    # MNG 1대 제외한 나머지가 Karpenter 몫
else
  ISO=${MODE#iso}; ISO=${ISO:-1}
  DOMAINS=$((T-ISO))               # stress 노드는 taint → 도메인에서 제외됨
  KARP=$((T-1-ISO)); STRESS_LIMIT=$((ISO*VCPU))
fi
[ "$DOMAINS" -lt 1 ] && { echo "공유 노드가 0 이하다 — 총노드수를 늘려라" >&2; exit 1; }
LIMIT=$((KARP*VCPU))
echo "== 총 $T 대 / stress=$MODE (전용 ${ISO}대)  (도메인 $DOMAINS, Karpenter 공유 상한 ${LIMIT}vCPU, stress 상한 ${STRESS_LIMIT}vCPU)"

# stress 배치 모드 전환
if [ "$ISO" = "0" ]; then
  bx "kubectl -n $NS patch deploy stress --type=json -p='[
        {\"op\":\"remove\",\"path\":\"/spec/template/spec/nodeSelector\"},
        {\"op\":\"remove\",\"path\":\"/spec/template/spec/tolerations\"}]' >/dev/null 2>&1 || true"
else
  # stress 전용 노드가 2대 이상 필요하면 stress 파드에도 분산 제약을 걸어야 한다.
  #   user/product 와 같은 이유다 — 도메인이 1개뿐이면 skew 가 항상 0 이라
  #   파드가 Pending 되지 않고, Karpenter 는 Pending 을 봐야 노드를 만든다.
  #   (실측: 제약 없이 iso2 를 걸었더니 stress 노드가 1대만 떴다)
  SPREAD=""
  if [ "$ISO" -gt 1 ]; then
    SPREAD=",\"topologySpreadConstraints\":[{\"maxSkew\":1,\"topologyKey\":\"kubernetes.io/hostname\",\"whenUnsatisfiable\":\"DoNotSchedule\",\"minDomains\":$ISO,\"labelSelector\":{\"matchLabels\":{\"app\":\"stress\"}}}]"
  fi
  bx "kubectl -n $NS patch deploy stress -p '{\"spec\":{\"template\":{\"spec\":{
        \"nodeSelector\":{\"role\":\"stress\"},
        \"tolerations\":[{\"key\":\"workload\",\"value\":\"stress\",\"effect\":\"NoSchedule\",\"operator\":\"Equal\"}]$SPREAD}}}}' >/dev/null"
  # 파드가 노드 수만큼은 있어야 분산이 의미가 있다
  # 전용 노드 수만큼은 파드가 있어야 분산이 의미가 있다. 줄일 때도 되돌린다.
  bx "kubectl -n $NS patch hpa stress-hpa -p '{\"spec\":{\"minReplicas\":$ISO}}' >/dev/null 2>&1 || true"
  # 전용 노드가 1대면 분산 제약을 없앤다 (도메인 1개짜리 제약은 무의미하고 축소를 막는다)
  [ "$ISO" -le 1 ] && bx "kubectl -n $NS patch deploy stress --type=json -p='[{\"op\":\"remove\",\"path\":\"/spec/template/spec/topologySpreadConstraints\"}]' >/dev/null 2>&1 || true"
fi

bx "kubectl patch nodepool apdev-pool --type=merge -p '{\"spec\":{\"limits\":{\"cpu\":\"$LIMIT\"}}}' >/dev/null
kubectl patch nodepool apdev-stress-pool --type=merge -p '{\"spec\":{\"limits\":{\"cpu\":\"$STRESS_LIMIT\"}}}' >/dev/null
echo '   apdev-pool='\$(kubectl get nodepool apdev-pool -o jsonpath='{.spec.limits.cpu}')' stress-pool='\$(kubectl get nodepool apdev-stress-pool -o jsonpath='{.spec.limits.cpu}')"

bx "for d in user product; do
  kubectl -n $NS patch deploy \$d --type=json \
    -p='[{\"op\":\"replace\",\"path\":\"/spec/template/spec/topologySpreadConstraints/0/minDomains\",\"value\":$DOMAINS}]' >/dev/null \
    || kubectl -n $NS patch deploy \$d --type=json \
       -p='[{\"op\":\"add\",\"path\":\"/spec/template/spec/topologySpreadConstraints/0/minDomains\",\"value\":$DOMAINS}]' >/dev/null
done
kubectl -n $NS rollout status deploy/user --timeout=300s | tail -1
kubectl -n $NS rollout status deploy/product --timeout=300s | tail -1
kubectl -n $NS rollout status deploy/stress --timeout=300s | tail -1"

# 축소는 Karpenter 의 자발적 통합에 맡기지 않는다.
#   limits.cpu 를 낮춰도 '이미 떠 있는' 노드는 안 지운다. 게다가 user/product 의
#   topologySpread 때문에 통합 시뮬레이션이 막혀 몇 분을 기다려도 안 줄었다(실측).
#   목표 초과분만큼 NodeClaim 을 직접 회수한다 — Karpenter 가 drain 까지 해준다.
reclaim() {   # $1=풀이름 $2=목표 노드수
  local cur drop
  cur=$(bx "kubectl get nodeclaim -l karpenter.sh/nodepool=$1 --no-headers 2>/dev/null | wc -l" | tr -d ' \n')
  if [ "${cur:-0}" -gt "$2" ]; then
    drop=$((cur-$2))
    echo "-- $1: $cur → $2 대, NodeClaim $drop 개 회수"
    bx "kubectl get nodeclaim -l karpenter.sh/nodepool=$1 --no-headers | awk '{print \$1}' | head -$drop | xargs -r kubectl delete nodeclaim"
  fi
}
reclaim apdev-pool "$KARP"
reclaim apdev-stress-pool "$ISO"

echo "-- 노드 $T 대로 수렴 대기 (증가/감소 양방향)"
for i in $(seq 1 40); do
  n=$(bx "kubectl get nodes --no-headers | wc -l" | tr -d ' \n')
  echo "   ${i}: ${n}대 (목표 $T)"
  [ "${n:-0}" = "$T" ] && break
  sleep 20
done
bx "kubectl get nodes -L role --no-headers | awk '{print \$1, \$6}'
echo ---
kubectl get pods -n $NS -o wide --no-headers | awk '{split(\$1,a,\"-\"); print a[1], \$7}' | sort | uniq -c"
