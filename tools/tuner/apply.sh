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
# ★롤아웃 대기는 짧게 끊는다.
#   과부하에서는 새 파드가 기동 검사를 통과하지 못해 rollout status 가 상한을
#   꽉 채운다. 실측(2026-08-21 ambush): 그 300초 동안 운영 루프가 한 줄도 못 찍었고
#   p50 이 5초를 넘었다. 롤아웃은 뒤늦게라도 끝나므로 기다릴 이유가 없다 —
#   다음 주기가 현재 상태를 다시 본다.
cd "$(dirname "$0")" || exit 1; source ./common.sh
T=${1:?사용법: apply.sh <총노드수> [iso|shared] [상한노드수]}
MODE=${2:-iso}
# ★상한을 하한과 분리한다.
#   트래픽 규모를 모르는 상태(대회 시작 시점)에서 상한까지 묶으면, 스파이크가 와도
#   Karpenter 가 노드를 못 만든다. 실측: 그 조건에서 stress 가 26% 로 무너져 22.5점.
#   하한(minDomains)은 "항상 이만큼은 유지", 상한(limits.cpu)은 "여기까지는 허용"이다.
#   트래픽을 재고 나면 상한을 하한까지 좁혀 비용을 확정한다.
CAP=${3:-$T}
[ "$CAP" -lt "$T" ] && CAP=$T
VCPU=${VCPU:-2}

# MODE: shared | iso | iso2 | iso3 ...  (iso 뒤 숫자 = stress 전용 노드 수)
#   stress 는 요청 하나가 235ms 라 파드 1개(2코어)가 4~5rps 에서 포화한다.
#   stress 트래픽이 크면 전용 노드가 2대 이상 필요하다.
if [ "$MODE" = "shared" ]; then
  ISO=0
  DOMAINS=$T                       # 참고값(로그용). 실제 minDomains 는 아래에서 2 로 고정한다
  KARP=$((T-1)); STRESS_LIMIT=0    # MNG 1대 제외한 나머지가 Karpenter 몫
else
  ISO=${MODE#iso}; ISO=${ISO:-1}
  # 배치 이름이 iso/iso2/... 가 아니면 여기서 멈춘다.
  # 예전에 STATE 파싱이 깨져 MODE 에 "shared 6" 이 들어왔고,
  # 산술식에서 "shared: unbound variable" 로 죽어 과부하 증설이 실패했다.
  case "$ISO" in (*[!0-9]*|"") echo "배치 값이 잘못됐다: '$MODE' (shared|iso|iso2|iso3)" >&2; exit 1 ;; esac
  DOMAINS=$((T-ISO))               # stress 노드는 taint → 도메인에서 제외됨
  KARP=$((T-1-ISO)); STRESS_LIMIT=$((ISO*VCPU))
fi
[ "$DOMAINS" -lt 1 ] && { echo "공유 노드가 0 이하다 — 총노드수를 늘려라" >&2; exit 1; }
# ★바닥 도메인 수. 회차 중에 안 바꾼다(바꾸면 롤아웃이 돈다).
#   자리표시 파드 계산과 minDomains 패치가 같은 값을 봐야 하므로 여기서 한 번만 정한다.
MIN_DOMAINS_FLOOR=${MIN_DOMAINS:-2}
[ "$DOMAINS" -lt "$MIN_DOMAINS_FLOOR" ] && MIN_DOMAINS_FLOOR=$DOMAINS
LIMIT=$(( (KARP + (CAP-T)) * VCPU ))
echo "== 하한 $T 대 / 상한 $CAP 대 / stress=$MODE (전용 ${ISO}대)"
echo "   도메인 $DOMAINS, Karpenter 공유 상한 ${LIMIT}vCPU, stress 상한 ${STRESS_LIMIT}vCPU"

# ★트래픽 중에는 Deployment 를 건드리지 않는다.
#   Deployment 패치는 롤링 재시작을 일으킨다. maxUnavailable:0 이라 용량이 줄지는 않지만,
#   파드 교체 때 ALB 등록/해제가 오가면서 흘리는 요청이 생기고 그건 가용성 12점에 직결된다.
#   그래서 '값이 실제로 달라질 때만' 패치한다.
#   노드 상한(NodePool.limits.cpu)은 파드를 건드리지 않으므로 언제든 바꿔도 안전하다.
CUR_MD=$(bx "kubectl -n $NS get deploy user -o jsonpath='{.spec.template.spec.topologySpreadConstraints[0].minDomains}'" 2>/dev/null | tr -d ' \n')
CUR_SEL=$(bx "kubectl -n $NS get deploy stress -o jsonpath='{.spec.template.spec.nodeSelector.role}'" 2>/dev/null | tr -d ' \n')
WANT_SEL=""; [ "$ISO" != 0 ] && WANT_SEL=stress

# stress 배치 모드 전환
if [ "$CUR_SEL" = "$WANT_SEL" ] && [ "$ISO" -le 1 ]; then
  echo "-- stress 배치 변경 없음 — 롤아웃 생략"
elif [ "$ISO" = "0" ]; then
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

# ★HPA 상한을 노드 수에 맞춘다.
#   maxReplicas 20 은 노드가 몇 대든 파드를 20개까지 늘린다. 노드 2대(4 vCPU)에
#   파드 20개를 얹으면 용량이 느는 게 아니라 큐와 문맥전환만 는다.
#   실측: 세 앱 HPA 가 전부 20 에 붙어 파드 60개가 같은 코어를 나눠 썼다.
#   노드당 3개면 t3.medium 2코어에 맞는다. HPA 패치는 롤아웃을 안 부른다(무료).
# ★상한 노드 수 기준으로 잡는다.
#   노드를 늘리는 방아쇠가 'Pending 파드'이므로, HPA 가 허용 노드를 다 채우고도
#   조금 더 원해야 Karpenter 가 새 노드를 만든다. 반대로 무한정(20)이면
#   4 vCPU 에 파드 20개가 얹혀 용량이 아니라 큐만 는다(실측: 세 앱 모두 20 에 붙음).
HPA_MAX=$(( (CAP-ISO) * ${HPA_PER_NODE:-3} + 2 ))
[ "$HPA_MAX" -lt 2 ] && HPA_MAX=2
bx "for h in user-hpa product-hpa; do
  kubectl -n $NS patch hpa \$h -p '{\"spec\":{\"maxReplicas\":$HPA_MAX}}' >/dev/null 2>&1
done"
echo "   HPA 상한 user/product = $HPA_MAX (공유 ${DOMAINS}대 x ${HPA_PER_NODE:-3})"

# ★노드 수를 확실히 만든다 — 자리표시 파드로.
#   minDomains 를 2 로 고정한 뒤로 상한만 올려서는 노드가 안 생긴다.
#   Karpenter 는 Pending 파드를 봐야 노드를 만드는데, 부하가 없으면 Pending 이 없다.
#   실측(2026-08-21): pretune 이 "3대로 만들고 재겠다"며 800초를 기다렸다.
#   overprovisioning 파드(1900m, priority -10)를 띄우면 노드 하나를 통째로 차지해
#   Karpenter 가 노드를 만든다. 진짜 파드(priority 500~1000)가 자리를 원하면
#   즉시 쫓겨나므로(preempt) 용량을 뺏지 않는다 — 예열된 노드가 남는다.
#   ★앱 Deployment 를 안 건드리므로 롤아웃이 없다 = 가용성을 안 깎는다.
WANT_WARM=$((DOMAINS - MIN_DOMAINS_FLOOR))
[ "$WANT_WARM" -lt 0 ] && WANT_WARM=0
CUR_WARM=$(bx "kubectl -n $NS get deploy overprovisioning -o jsonpath='{.spec.replicas}'" 2>/dev/null | tr -d ' \n')
if [ "${CUR_WARM:-x}" != "$WANT_WARM" ]; then
  echo "   자리표시 파드 ${CUR_WARM:-?} → $WANT_WARM (노드를 확실히 만든다, 롤아웃 없음)"
  # ★실패를 삼키지 않는다. 예전엔 || true 로 넘겨서, Deployment 가 아예 없는데도
  #   조용히 지나갔다(실측: overprovisioning 이 배포된 적이 없었다).
  if ! bx "kubectl -n $NS scale deploy overprovisioning --replicas=$WANT_WARM >/dev/null 2>&1"; then
    echo "   !! 자리표시 파드를 못 만들었다 — overprovisioning 이 배포됐는지 확인해라"
    echo "      kubectl -n $NS get deploy overprovisioning"
  fi
fi

bx "kubectl patch nodepool apdev-pool --type=merge -p '{\"spec\":{\"limits\":{\"cpu\":\"$LIMIT\"}}}' >/dev/null
kubectl patch nodepool apdev-stress-pool --type=merge -p '{\"spec\":{\"limits\":{\"cpu\":\"$STRESS_LIMIT\"}}}' >/dev/null
echo '   apdev-pool='\$(kubectl get nodepool apdev-pool -o jsonpath='{.spec.limits.cpu}')' stress-pool='\$(kubectl get nodepool apdev-stress-pool -o jsonpath='{.spec.limits.cpu}')"

# ★minDomains 는 2 로 고정하고 회차 중에 절대 안 바꾼다.
#
#   왜 바꿨나 — 실측(2026-08-21 회차):
#     증설할 때마다 minDomains 를 올렸고, 그건 파드 템플릿 변경이라 user/product 가
#     롤링 재시작했다. peak2 부하 중 롤아웃이 돌면 ALB 등록/해제 사이에 요청이 흐른다.
#     노드를 4번 늘리는 동안 5xx 가 1005, 1030, 168건 터졌다. 가용성은 12점짜리이고
#     흘린 요청은 회차 끝까지 분모에 남는다. 증설 한 번이 가용성을 깎는 구조였다.
#
#   그럼 노드는 어떻게 늘리나:
#     minDomains 2 는 '바닥 2대'만 보장한다(고가용성 최소선이자 비용 만점 지점).
#     그 위로는 HPA 가 파드를 늘리면 갈 곳 없는 파드가 Pending 이 되고,
#     Karpenter 가 그걸 보고 노드를 만든다. 우리는 NodePool 의 limits.cpu 로
#     '어디까지 허용할지'만 정한다. limits 패치는 파드를 안 건드린다 = 롤아웃 없음.
#     즉 바닥은 minDomains 가, 천장은 도구가, 그 사이는 실제 수요가 정한다.
MIN_DOMAINS=$MIN_DOMAINS_FLOOR
if [ "$CUR_MD" = "$MIN_DOMAINS" ]; then
  echo "-- minDomains $MIN_DOMAINS 유지 — 롤아웃 없음"
else
  echo "-- minDomains $CUR_MD → $MIN_DOMAINS (이번 한 번만 롤아웃)"
bx "for d in user product; do
  kubectl -n $NS patch deploy \$d --type=json \
    -p='[{\"op\":\"replace\",\"path\":\"/spec/template/spec/topologySpreadConstraints/0/minDomains\",\"value\":$MIN_DOMAINS}]' >/dev/null \
    || kubectl -n $NS patch deploy \$d --type=json \
       -p='[{\"op\":\"add\",\"path\":\"/spec/template/spec/topologySpreadConstraints/0/minDomains\",\"value\":$MIN_DOMAINS}]' >/dev/null
done
kubectl -n $NS rollout status deploy/user --timeout=${ROLLOUT_TIMEOUT:-90}s | tail -1
kubectl -n $NS rollout status deploy/product --timeout=${ROLLOUT_TIMEOUT:-90}s | tail -1"
fi
bx "kubectl -n $NS rollout status deploy/stress --timeout=${ROLLOUT_TIMEOUT:-90}s | tail -1"

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
if [ "$CAP" = "$T" ]; then
  reclaim apdev-pool "$KARP"
else
  echo "-- 상한이 열려 있다($CAP 대) — 자동 증설분은 회수하지 않는다"
fi
reclaim apdev-stress-pool "$ISO"

echo "-- 노드 $T 대 이상으로 수렴 대기 (상한 $CAP)"
for i in $(seq 1 "${CONVERGE_TRIES:-12}"); do
  n=$(bx "kubectl get nodes --no-headers | wc -l" | tr -d ' \n')
  echo "   ${i}: ${n}대 (하한 $T / 상한 $CAP)"
  [ "${n:-0}" -ge "$T" ] && [ "${n:-0}" -le "$CAP" ] && break
  sleep 20
done
# ★영원히 기다리지 않는다. 못 만들었으면 그 사실을 말하고 넘어간다.
#   예전엔 40회 × 20초 = 800초를 조용히 기다렸다. 부르는 쪽(운영 루프·pretune)이
#   그동안 아무 판단도 못 한다. 노드는 뒤늦게라도 뜨므로 다음 주기가 알아서 본다.
if [ "${n:-0}" -lt "$T" ]; then
  echo "   !! ${CONVERGE_TRIES:-12}회 확인 동안 $T 대에 못 미쳤다(현재 ${n}대) — 넘어간다"
fi
# ★상태 파일은 여기서 쓴다.
#   예전엔 호출자(autotune/GO)가 각자 썼다. 그래서 apply.sh 를 직접 부르면
#   클러스터는 바뀌는데 STATE 는 옛 값이 남았고, 다음 판단이 그 옛 값을 믿었다.
#   실측 사고: STATE 가 "6 shared 8" 로 남은 채 stress 전용 전환이 걸려
#   2+2=4 대여야 할 것이 6+2=8 대가 됐다. 진실은 한 곳에서만 만든다.
echo "$T $MODE $CAP" > .autotune-state

bx "kubectl get nodes -L role --no-headers | awk '{print \$1, \$6}'
echo ---
kubectl get pods -n $NS -o wide --no-headers | awk '{split(\$1,a,\"-\"); print a[1], \$7}' | sort | uniq -c"
