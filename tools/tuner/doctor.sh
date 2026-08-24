#!/usr/bin/env bash
# doctor.sh — 트래픽 받기 전에 "조용히 망가진 것"을 찾는다.
#
# 왜 만들었나:
#   2026-08-21 회차를 통째로 날린 원인은 IAM 정책 하나가 안 붙은 것이었다.
#   증상은 엉뚱한 곳에 나왔다 — 파드는 전부 Running 이고 healthcheck 200 인데
#   ALB 는 502 였다. 로그를 파고들기 전까지는 앱이 멀쩡해 보인다.
#   이런 건 눈으로 못 찾는다. 25분짜리 회차를 태워서 알아내면 너무 비싸다.
#   여기서 걸리는 것들은 전부 실제로 한 번씩 당한 것들이다.
set -uo pipefail
cd "$(dirname "$0")" || exit 1; source ./common.sh
REGION=${AWS_DEFAULT_REGION:-ap-northeast-2}
ALB_NAME=${ALB_NAME:-apdev-alb}
fail=0; warn=0
ok()   { echo "   [O] $*"; }
bad()  { echo "   [X] $*"; fail=$((fail+1)); }
hmm()  { echo "   [!] $*"; warn=$((warn+1)); }

echo "== 1. 자격증명·클러스터"
ACC=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
  && ok "AWS 계정 $ACC (${AWS_PROFILE:-기본})" || { bad "AWS 자격증명이 안 먹는다"; exit 1; }
kubectl --request-timeout=10s get --raw /version >/dev/null 2>&1 \
  && ok "클러스터 연결" || { bad "클러스터에 못 붙는다"; exit 1; }

echo "== 2. ALB 컨트롤러 권한  ← 502 의 진짜 원인이 여기 있었다"
# 컨트롤러가 TargetGroupBinding 을 조정하려면 ELB 권한이 있어야 한다.
# 권한이 없으면 DescribeTargetHealth 에서 403 을 맞고 '조정 전체'를 포기한다.
# 그러면 파드가 노드를 옮겨도 타깃이 옛 IP 에 멈춘다 → 502. 파드는 멀쩡해 보인다.
R=${LBC_ROLE:-AmazonEKSLoadBalancerControllerRole}
POL=$(aws iam list-attached-role-policies --role-name "$R" \
      --query 'AttachedPolicies[].PolicyName' --output text 2>/dev/null)
if [ -z "${POL:-}" ] || [ "$POL" = None ]; then
  bad "$R 에 IAM 정책이 하나도 없다 — 타깃이 갱신되지 않는다"
  echo "       고치기: aws iam create-policy --policy-name AWSLoadBalancerControllerIAMPolicy \\"
  echo "                 --policy-document file://../../terraform/k8s/iam_policy.json"
  echo "               aws iam attach-role-policy --role-name $R --policy-arn <위 ARN>"
  echo "               kubectl -n kube-system rollout restart deploy aws-load-balancer-controller"
else
  ok "$R 정책: $POL"
fi
E=$(kubectl -n kube-system logs -l app.kubernetes.io/name=aws-load-balancer-controller \
    --tail=200 --since=10m 2>/dev/null | grep -c "AccessDenied" || true)
[ "${E:-0}" -gt 0 ] && bad "컨트롤러 로그에 최근 10분간 AccessDenied ${E}건" || ok "컨트롤러 권한 오류 없음"

echo "== 3. ALB 타깃이 실제로 살아 있나"
LB=$(aws elbv2 describe-load-balancers --region "$REGION" --names "$ALB_NAME" \
     --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null)
if [ -z "${LB:-}" ] || [ "$LB" = None ]; then bad "ALB 를 못 찾았다: $ALB_NAME"; else
  for tg in $(aws elbv2 describe-target-groups --region "$REGION" --load-balancer-arn "$LB" \
              --query 'TargetGroups[].[TargetGroupName,TargetGroupArn]' --output text 2>/dev/null | awk '{print $1":"$2}'); do
    n=${tg%%:*}; arn=${tg#*:}
    st=$(aws elbv2 describe-target-health --region "$REGION" --target-group-arn "$arn" \
         --query 'TargetHealthDescriptions[].TargetHealth.State' --output text 2>/dev/null)
    h=$(tr ' \t' '\n' <<<"$st" | grep -c '^healthy$' || true)
    u=$(tr ' \t' '\n' <<<"$st" | grep -c '^unhealthy$' || true)
    if [ "${h:-0}" = 0 ]; then bad "$n 타깃에 healthy 가 없다 (unhealthy ${u})"
    elif [ "${u:-0}" -gt 0 ]; then hmm "$n healthy ${h} / unhealthy ${u}"
    else ok "$n healthy ${h}"; fi
  done
fi

echo "== 4. 파드가 노드에 퍼져 있나  ← 노드 절반이 노는 사고가 있었다"
# topologySpread 는 '스케줄 시점'에만 강제된다. 노드가 흔들리는 중에 롤아웃이 돌면
# 전부 한 노드에 몰리고, 그 뒤로는 스스로 안 고쳐진다. 용량이 그냥 절반이 된다.
NODES=$(kubectl get nodes --no-headers 2>/dev/null | grep -c " Ready")
for app in user product; do
  d=$(kubectl -n "$NS" get pods -l app=$app -o jsonpath='{range .items[?(@.status.phase=="Running")]}{.spec.nodeName}{"\n"}{end}' 2>/dev/null | sort -u | grep -c . || true)
  p=$(kubectl -n "$NS" get pods -l app=$app --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l | tr -d ' ')
  if [ "${p:-0}" -ge 2 ] && [ "${d:-0}" -lt 2 ]; then
    bad "$app 파드 ${p}개가 노드 ${d}대에 몰려 있다 (노드 ${NODES}대)"
    echo "       고치기: kubectl -n $NS rollout restart deploy $app   (노드가 안정된 뒤에)"
  else ok "$app 파드 ${p}개 / 노드 ${d}대"; fi
done

echo "== 4b. 다른 튜너가 이 클러스터를 잡고 있지 않나  ← 둘이 싸우면 회차가 버려진다"
CUR=$(kubectl -n "$NS" get cm tuner-lock -o jsonpath='{.data.owner}{" "}{.data.ts}' 2>/dev/null)
if [ -n "${CUR:-}" ]; then
  set -- $CUR; AGE=$(( $(date +%s) - ${2:-0} ))
  if [ "$AGE" -lt 180 ]; then hmm "튜너가 이미 돌고 있다: $1 (${AGE}초 전 갱신)"
  else ok "옛 잠금만 남아 있다: $1 (${AGE}초 전) — 새로 잡으면 된다"; fi
else ok "잠금 없음"; fi

echo "== 4c. CPU 지분이 한쪽으로 쏠려 있지 않나  ← 노드를 늘려도 안 낫던 원인"
# CFS 는 경합할 때 cpu.requests 비율로 나눈다. 배포 기본값 stress 600m : user 70m 은
# 8.6:1 이라 경합 순간 user 가 굶는다. 실측: 그 상태 2대에서 p50 132ms,
# 3대로 늘리자 11ms. DB 는 내내 한가했다 — 순수 지분 문제다.
SR=$(kubectl -n "$NS" get deploy stress -o jsonpath='{.spec.template.spec.containers[0].resources.requests.cpu}' 2>/dev/null)
UR=$(kubectl -n "$NS" get deploy user -o jsonpath='{.spec.template.spec.containers[0].resources.requests.cpu}' 2>/dev/null)
num() { case "$1" in *m) echo "${1%m}";; "") echo 0;; *) echo $(( ${1%.*} * 1000 ));; esac; }
SN=$(num "${SR:-0}"); UN=$(num "${UR:-0}")
if [ "${UN:-0}" -gt 0 ] && [ "$SN" -gt $((UN*4)) ]; then
  bad "stress ${SR} : user ${UR} = $((SN/UN)):1 — 경합 시 user 가 굶는다"
  echo "       고치기: ./tune_requests.sh 100m"
else
  ok "cpu.requests stress ${SR:-?} / user ${UR:-?}"
fi

echo "== 4d. 노드를 만드는 컨트롤러가 살아 있나  ← 죽으면 증설이 통째로 무산된다"
# 실측(2026-08-21 ambush, 노드 CPU 103%): karpenter 가 0/1 Ready 로 7번 재시작하며
# 죽어 있었다. 튜너는 매 주기 "3대로 가자"고 결정했지만 노드는 하나도 안 생겼다.
# 판단은 옳은데 실행이 불가능한 상태 — 로그만 봐서는 절대 못 찾는다.
for D in karpenter aws-load-balancer-controller; do
  J=$(kubectl -n kube-system get deploy "$D" -o json 2>/dev/null)
  [ -z "$J" ] && { hmm "$D 를 못 찾았다"; continue; }
  echo "$J" | python3 -c "
import json,sys,os
d=json.load(sys.stdin); n=d['metadata']['name']
st=d.get('status',{}); want=d['spec'].get('replicas',1)
rdy=st.get('readyReplicas',0)
c=d['spec']['template']['spec']['containers'][0].get('resources',{})
req=(c.get('requests') or {}).get('cpu'); lim=(c.get('limits') or {}).get('cpu')
bad=[]
if rdy<want: bad.append('준비 %d/%d'%(rdy,want))
if not req:  bad.append('CPU 요청 없음(과부하에 굶는다)')
if lim:      bad.append('CPU 상한 %s(스로틀된다)'%lim)
print(('   [X] ' if bad else '   [O] ')+n+' '+(', '.join(bad) if bad else 'ready %d/%d, cpu요청 %s, 상한 없음'%(rdy,want,req)))
sys.exit(1 if bad else 0)" || fail=$((fail+1))
  R=$(kubectl -n kube-system get pods -l app.kubernetes.io/name="$D" --no-headers 2>/dev/null | awk '{s+=$4} END{print s+0}')
  [ "${R:-0}" -gt 3 ] && hmm "$D 재시작 ${R}회 — 자원 부족을 의심해라"
done

echo "== 4e. 튜너가 '지금' 판단하고 있나  ← 잠금만 보면 속는다"
# 실측 사고(2026-08-21 공식 120분): watch 를 두 번 불러 두 개가 떴다.
# 잠금이 둘째를 막았고(정상), 첫째는 이미 죽어 있었다. 아무도 몰랐다.
# 120분 회차 내내 튜너가 판단을 한 번도 안 했고, 노드 변화는 HPA+Karpenter
# 기본 동작이었다. 로그에는 "다른 튜너가 잡고 있다"만 1437번 찍혀 정상처럼 보였다.
# 잠금이 있는지가 아니라 '원장이 갱신되는지'를 봐야 한다.
NPROC=$(pgrep -c -f 'autotune[.]sh run' 2>/dev/null | head -1)
case "$NPROC" in (''|*[!0-9]*) NPROC=0;; esac
if [ "${NPROC:-0}" -gt 2 ]; then
  bad "운영 루프가 ${NPROC}개 돈다 — 중복 실행이다"
  echo "       고치기: pkill -f '[.]supervise[.]sh'; pkill -f 'autotune[.]sh run'  뒤 ./GO.sh watch"
elif [ "${NPROC:-0}" = 0 ]; then
  hmm "운영 루프가 안 돈다 (트래픽 전이면 정상 — ./GO.sh watch 로 켠다)"
else
  LED=${LEDGER:-.round-ledger.json}
  if [ -f "$LED" ]; then
    AGE=$(( $(date +%s) - $(mtime "$LED") ))
    if [ "$AGE" -gt 180 ]; then
      bad "원장이 ${AGE}초째 그대로다 — 루프가 살아만 있고 판단은 안 한다"
      echo "       autotune.log 를 봐라. 잠금 충돌이면 위 방법으로 다시 켜라."
    else ok "운영 루프 ${NPROC}개, 원장 ${AGE}초 전 갱신 (정상 판단 중)"; fi
  else hmm "원장이 아직 없다 (트래픽이 시작되면 생긴다)"; fi
fi
# 로그에 잠금 충돌이 쌓여 있으면 그것도 잡는다
LC=$(grep -c "다른 튜너가 이미" autotune.log 2>/dev/null | head -1)
case "$LC" in (''|*[!0-9]*) LC=0;; esac
[ "$LC" -gt 5 ] && bad "로그에 잠금 충돌 ${LC}건 — 중복 실행이 있었다"

echo "== 4f. 채점에 세어지는 EC2 가 워커뿐인가  ← 가만히 앉아 4점을 잃던 자리"
# 채점기는 계정에서 도는 EC2 를 전부 센다(running + pending). bastion 처럼
# 트래픽과 무관한 인스턴스가 떠 있으면 워커 2대로 완벽하게 돌아도 3대로 잡혀
# cost_ratio 가 1.5 가 되고 비용 12점 중 4점이 날아간다.
# 과제지도 "EC2 인스턴스는 t3.medium 타입만" 을 요구한다.
_extra=$(aws ec2 describe-instances \
  --filters Name=instance-state-name,Values=running,pending \
  --query "Reservations[].Instances[?InstanceType!='t3.medium'].[InstanceId,InstanceType,Tags[?Key=='Name']|[0].Value]" \
  --output text 2>/dev/null)
if [ -n "$_extra" ]; then
  echo "   [X] 워커가 아닌 EC2 가 떠 있다 — 비용 지표에 그대로 잡힌다"
  echo "$_extra" | while read -r _i _t _n; do
    [ -n "$_i" ] && echo "       $_i ($_t, ${_n:-이름없음})"
  done
  echo "       고치기: aws ec2 stop-instances --instance-ids <ID>"
  BAD=$((BAD+1))
else
  echo "   [O] t3.medium 워커만 떠 있다"
fi

echo "== 5. 노드 수와 상한이 도구 상태와 맞나"
ST=$(cat "${STATE:-.autotune-state}" 2>/dev/null || echo "")
T=$(awk '{print $1}' <<<"$ST"); M=$(awk '{print $2}' <<<"$ST"); C=$(awk '{print $3}' <<<"$ST")
if [ -z "${T:-}" ]; then hmm "상태 파일이 없다 — apply.sh 를 한 번 돌려라"; else
  ok "상태: ${T}대 / ${M} / 상한 ${C}"
  [ "${NODES:-0}" -gt "${C:-$T}" ] && bad "실제 노드 ${NODES}대 > 상한 ${C} — Karpenter 가 마음대로 늘렸다"
  CPU=$(kubectl get nodepool apdev-pool -o jsonpath='{.spec.limits.cpu}' 2>/dev/null)
  ok "NodePool 상한 ${CPU} vCPU (= Karpenter 노드 $((${CPU:-0}/2))대 + MNG 1대)"
fi

echo "== 6. 실측 방아쇠가 도나  ← 잘못 만들면 '전부 나쁨'이라 거짓말한다"
S=$(date +%s%N); PR=$(./probe.sh 2>/dev/null); E=$(( ($(date +%s%N)-S)/1000000 ))
if [ -z "${PR:-}" ] || [ "$PR" = '{}' ]; then bad "probe.sh 가 아무것도 못 냈다"; else
  echo "$PR" | python3 -c "
import json,sys
d=json.load(sys.stdin); bad=0
for a in ('user','product'):
    v=d.get(a)
    if not v: print('   [X] probe %s 결과 없음' % a); bad=1; continue
    print('   [%s] probe %-8s 통과 %3d%%  p50 %.0fms  p90 %.0fms' %
          ('O' if v['pass']>=90 else '!', a, v['pass'], v['p50']*1000, v['p90']*1000))
s=d.get('stress')
if s: print('   [O] stress CPU %d%%' % s['cpu_pct'])
sys.exit(bad)" || fail=$((fail+1))
  if [ "$E" -gt 10000 ]; then bad "probe 가 ${E}ms 걸렸다 — 주기를 잡아먹는다 (정상 2~3초)"
  else ok "probe 소요 ${E}ms"; fi
  # ★probe 가 앱의 정상 응답을 받고 있나. 경로가 앱과 안 맞으면 404 를 빨리 받고
  #   "전부 통과"라고 거짓 보고한다. 생성(POST)이 2xx 로 오는지로 판별한다.
  O=$(echo "$PR" | python3 -c "import json,sys; print((json.load(sys.stdin).get('user') or {}).get('ok2xx','?'))" 2>/dev/null)
  if [ "${O:-0}" = 0 ]; then
    bad "probe 가 앱의 정상 응답(2xx)을 한 건도 못 받았다 — 경로가 앱과 안 맞는다"
    echo "       probe.sh 의 /v1/user, /v1/product 경로가 이번 앱과 같은지 확인해라"
  else ok "probe 가 앱 정상 응답 ${O}건 확인 (경로가 맞다)"; fi
fi

echo "== 7. HPA 상한이 노드에 비해 터무니없지 않나"
kubectl -n "$NS" get hpa -o json 2>/dev/null | python3 -c "
import json,sys,os
nodes=int(os.environ.get('NODES','2') or 2)
for h in json.load(sys.stdin)['items']:
    mx=h['spec']['maxReplicas']; nm=h['metadata']['name']
    tag='O' if mx <= nodes*6 else '!'
    print('   [%s] %-14s max=%d (노드 %d대)' % (tag, nm, mx, nodes))
" NODES="$NODES" 2>/dev/null || hmm "HPA 를 못 읽었다"

echo
if [ "$fail" = 0 ]; then echo "== 이상 없음 (경고 ${warn}건). 트래픽 받아도 된다."
else echo "== 문제 ${fail}건 — 이대로 회차를 돌리면 낭비다."; fi
exit "$fail"
