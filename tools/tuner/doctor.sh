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
