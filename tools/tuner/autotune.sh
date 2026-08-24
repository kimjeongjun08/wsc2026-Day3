#!/usr/bin/env bash
# autotune.sh — 인프라 최적값을 스스로 찾아 적용한다. 대회 환경에서 그대로 돌아간다.
#
# 왜 이렇게 만들었나:
#   · 대회는 인프라 구축에 1시간만 준다. 트래픽이 오기 전에는 보정 회차를 못 돌린다.
#   · 채점 서버에는 접근할 수 없다. 그래서 트래픽을 ALB 지표에서 직접 읽는다.
#       ALB RequestCount(TargetGroup 차원) → 앱별 실제 rps
#     이 값이면 채점 서버 없이도 "지금 트래픽에 맞는 최적 구성"을 계산할 수 있다.
#
# 두 단계로 돈다:
#   1) 준비 단계 (트래픽 오기 전)
#      - 동시성 곡선이 없으면 측정한다 (앱당 약 1분).
#        곡선은 "이 앱이 2코어로 초당 몇 개까지 처리하는가"를 준다.
#        ※ 파드를 1개로 줄여서 재므로 트래픽이 흐르는 중에는 절대 하지 않는다.
#      - 콜드 스타트 구성을 적용한다.
#   2) 운영 단계 (트래픽 시작 후)
#      - 주기적으로 ALB 에서 rps 를 읽고 → solve.py 로 최적 구성을 계산 →
#        지금 구성보다 충분히 좋으면 적용한다.
#
# 사용:
#   ./autotune.sh prepare          # 곡선 측정 + 콜드 스타트 구성
#   ./autotune.sh run              # 운영 루프 (기본 5분 주기)
#   ./autotune.sh once             # 한 번만 읽고 계산·적용
#   ./autotune.sh show             # 지금 트래픽과 추천 구성만 출력 (적용 안 함)
set -uo pipefail
cd "$(dirname "$0")" || exit 1; source ./common.sh

REGION=${AWS_DEFAULT_REGION:-ap-northeast-2}
ALB_NAME=${ALB_NAME:-apdev-alb}
# ★주기를 둘로 나눈다.
#   과부하 방어는 빨라야 한다 — SLA 를 넘고 있는데 5분을 기다리면 그 구간을 통째로 잃는다.
#   반면 구조 변경(minDomains·stress 배치)은 Deployment 롤아웃을 부르므로 드물어야 한다.
#   파드 교체 때 흘리는 요청은 가용성 12점에 직결된다.
#   노드 상한(NodePool.limits.cpu)만 바꾸는 건 파드를 안 건드리므로 자주 해도 안전하다.
INTERVAL=${INTERVAL:-60}          # 감시 주기 (과부하 확인)
STRUCT_COOLDOWN=${STRUCT_COOLDOWN:-600}   # 구조 변경 최소 간격(초)
MIN_GAIN=${MIN_GAIN:-1.0}      # 이 점수 이상 좋아질 때만 구성을 바꾼다 (잦은 교체 방지)
STATE=${STATE:-.autotune-state}
MAX_NODES=${MAX_NODES:-8}
# ★상한을 하한과 같이 둔다 (기본 0).
#   예전엔 +2 를 열어놨다. "예상 못 한 스파이크를 Karpenter 가 흡수하게" 라는 이유였는데,
#   실측에서 이게 비용을 그냥 흘리는 구멍이었다: 튜너는 2대를 원하는데 HPA 가 파드를
#   늘리면 Karpenter 가 상한까지 알아서 붙여 4대가 됐다. 노드 2대 = 비용 4점이다.
#   게다가 그 4대의 CPU 는 5% / 72% / 1% / 43% 로 놀고 있었다 — 늘어난 게 일도 안 했다.
#   지금은 probe.sh 가 실시간으로 재서 20~60초 안에 대응하므로 열어둘 이유가 없다.
#   노드 수는 도구가 정한다. Karpenter 가 마음대로 못 늘린다.
CAP_MARGIN=${CAP_MARGIN:-0}

# ── 지금 트래픽을 ALB 에서 읽는다 (앱별 rps) ───────────────────────────────
read_traffic() {
  local lb lbdim tg name rpm out="{" first=1
  lb=$(aws elbv2 describe-load-balancers --region "$REGION" --names "$ALB_NAME" \
       --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null) || return 1
  [ -z "$lb" ] || [ "$lb" = "None" ] && { echo "ALB 를 못 찾았다: $ALB_NAME" >&2; return 1; }
  lbdim=${lb##*loadbalancer/}
  for tg in $(aws elbv2 describe-target-groups --region "$REGION" --load-balancer-arn "$lb" \
              --query 'TargetGroups[].TargetGroupArn' --output text); do
    name=$(aws elbv2 describe-target-groups --region "$REGION" --target-group-arns "$tg" \
           --query 'TargetGroups[0].TargetGroupName' --output text)
    name=${name#apdev-}
    # 최근 5분 중 '가장 바쁜 1분'을 쓴다. 평균을 쓰면 스파이크를 놓쳐 노드가 모자란다.
    rpm=$(aws cloudwatch get-metric-statistics --region "$REGION" \
          --namespace AWS/ApplicationELB --metric-name RequestCount \
          --dimensions Name=LoadBalancer,Value="$lbdim" Name=TargetGroup,Value="${tg##*:}" \
          --start-time "$(date -u -v-6M +%Y-%m-%dT%H:%M:%S 2>/dev/null || date -u -d '6 minutes ago' +%Y-%m-%dT%H:%M:%S)" \
          --end-time "$(date -u +%Y-%m-%dT%H:%M:%S)" \
          --period 60 --statistics Sum --query 'max(Datapoints[].Sum)' --output text 2>/dev/null)
    [ -z "$rpm" ] || [ "$rpm" = "None" ] && rpm=0
    [ $first -eq 0 ] && out="$out,"
    out="$out\"$name\":$(python3 -c "print(round($rpm/60.0,2))")"
    first=0
  done
  echo "$out}"
}

# ── 옛 판단 경로는 걷어냈다 ──────────────────────────────────────────────
#   recalibrate_curves / ask_solver / overload_nodes 는 "곡선 모델로 최적 노드 수를
#   계산하고, ALB 평균 지연이 SLA 를 넘으면 늘린다"는 설계였다. 둘 다 실측에서 졌다.
#     · 모델: 동거 모드에서 무거운 앱 지연을 크게 과대평가해 40점 구성을 탈락시켰다.
#     · 평균 지연: 채점되는 값이 아니다. 실측 user 는 평균이 SLA 를 넘었는데
#       통과율은 48.6% 였다 — "넘었다"만 알면 tier 를 겨냥할 수 없다.
#   지금은 alb_snapshot.sh(백분위) + decide.py(점수 산수) 가 그 자리를 대신한다.
#   곡선 측정(concurrency.sh)은 여전히 prepare 에서 콜드 스타트 크기를 잡는 데 쓴다.

# ── 한 번 돌기 ────────────────────────────────────────────────────────────
# ★판단 기준을 "SLA 를 넘었나"에서 "점수가 오르나"로 바꿨다.
#   예전 규칙은 ALB 평균 지연이 SLA 를 넘으면 노드를 늘렸다. 그건 증상 대응이다.
#   실측 대조군(공식 120분)에서 그 규칙은 노드를 6대까지 밀어올렸는데
#   성능은 tier 를 하나도 못 넘겼고 비용만 12 → 8 로 깎였다 (총 30.0/40).
#   채점표 산수로는 노드 1대가 비용 2점이고 성능은 앱당 최대 4점이다.
#   그래서 "늘려서 얻을 수 있는 최대치"가 "비용으로 확실히 잃는 값"보다 작으면
#   그 증설은 계산할 것도 없이 손해다. decide.py 가 그 산수를 한다.
#
#   그리고 비용은 '분' 평균, 성능은 '요청' 비율이다. 트래픽이 빠진 구간의 노드는
#   순손실이다 — 대조군은 하강 구간 40분을 평균 4.93대로 버텨 2점을 그냥 버렸다.
#   그래서 축소를 증설만큼 중요하게 다룬다.
once() {
  local apply=${1:-yes} snap nodes cur_n cur_m cur_iso cap
  local delta bad worst out
  snap=$(WIN_MIN=${WIN_MIN:-3} ./alb_snapshot.sh 2>/dev/null) || {
    echo "[$(date +%H:%M:%S)] ALB 지표를 못 읽었다 — 이번 주기는 건너뛴다"; return 0; }
  echo "[$(date +%H:%M:%S)] $(SNAP="$snap" python3 rpsline.py)"

  nodes=$(cap 15 kubectl get nodes --no-headers 2>/dev/null | grep -c " Ready")
  [ "${nodes:-0}" = 0 ] && { echo "   노드를 못 읽었다"; return 0; }

  # ★지금 파드가 어떤지는 직접 잰다. CloudWatch 는 1~3분 늦어서 방아쇠로 못 쓴다.
  #   ALB 로 직접 GET 몇 개(앱당 15개). 피크 312rps 옆에서 무시할 양이고,
  #   채점기는 자기 주입 요청만 세므로 점수에 안 들어간다.
  local prb=""
  if [ "${USE_PROBE:-1}" = 1 ]; then
    prb=$(cap "${PROBE_TIMEOUT_ALL:-20}" ./probe.sh 2>/dev/null)
    case "${prb:-}" in ''|'{}') prb="";; esac
  fi

  out=$(MAX_NODES=$MAX_NODES \
        python3 decide.py --snapshot "$snap" --nodes "$nodes" \
          ${prb:+--probe "$prb"} \
          $([ "$apply" = yes ] || echo --no-commit) 2>&1) || {
    echo "   판단 실패:"; echo "$out" | tail -3; return 0; }
  echo "$out" | grep -v '^[A-Z][A-Z]*='
  delta=$(sed -n 's/^DELTA=//p' <<<"$out")
  bad=$(sed -n 's/^BAD=//p' <<<"$out")
  scpu=$(sed -n 's/^STRESS_CPU=//p' <<<"$out"); scpu=${scpu:--1}
  step=$(sed -n 's/^STEP=//p' <<<"$out"); step=${step:-0}
  worst=$(sed -n 's/^WORST=//p' <<<"$out")
  [ -z "${delta:-}" ] && return 0
  [ "$delta" = 0 ] && return 0
  [ "$apply" != yes ] && { echo "   show 모드라 적용 안 함 (delta=$delta)"; return 0; }

  cur_n=$(awk '{print $1}' "$STATE" 2>/dev/null); cur_n=${cur_n:-$nodes}
  cur_m=$(awk '{print $2}' "$STATE" 2>/dev/null); cur_m=${cur_m:-shared}
  case "$cur_m" in shared) cur_iso=0;; iso) cur_iso=1;; iso*) cur_iso=${cur_m#iso};; *) cur_iso=0;; esac
  local want_iso=$cur_iso want_shared=$((cur_n-cur_iso)) new_n new_mode
  [ "$want_shared" -lt 2 ] && want_shared=2

  if [ "$delta" -gt 0 ]; then
    # ★stress 의 cpu.requests 를 올리는 단계는 없앴다. 두 가지가 다 나빴다.
    #
    #   (1) user 것을 뺏는다.
    #       600m 으로 올리면 stress : user = 8.6 : 1 이 된다. CFS 는 경합할 때
    #       requests 비율로 나누므로 user 가 그만큼 굶는다.
    #       실측(2026-08-21 ambush): 이 조치 후 user 통과율이 1.62% 였다.
    #       같은 날 100m 으로 유지한 회차는 채점기 40.0/40 을 받았다.
    #
    #   (2) 과부하 한복판에 롤아웃을 부른다.
    #       Deployment 패치는 롤링 재시작이고, 과부하에서는 새 파드가 기동 검사를
    #       통과하지 못해 rollout status 가 300초를 꽉 채운다. 그동안 운영 루프는
    #       아무 판단도 못 한다. 실측: 계단 진입 후 5분간 로그가 한 줄도 안 늘었고,
    #       그 사이 p50 이 5초를 넘고 5xx 가 239건 났다. 첫 증설은 7분 뒤였다.
    #
    #   stress 에 용량이 더 필요하면 전용 노드로 준다. 남의 몫을 뺏지 않는다.
    # ★격리 방아쇠는 둘이다.
    #   (1) stress 자신이 SLA 를 못 지킬 때
    #   (2) user/product 가 무너지는데 stress 가 CPU 를 크게 먹고 있을 때
    #       stress 는 SLA 가 1000ms 라 느슨해서 (1) 에 안 걸리면서도 CPU 는 계속 먹는다.
    #       그 상태에서 공유 노드를 붙이면 새 노드의 빈 CPU 를 stress 가 먼저 채우고
    #       user 에게는 거의 안 돌아간다. 노드를 사도 지연이 안 내려간다.
    #       실측(2026-08-21 ambush): 2→6대까지 늘렸는데 user p50 이 435ms 에서 멈췄다.
    local victim=0
    [[ ",$bad," == *,user,* ]] || [[ ",$bad," == *,product,* ]] && victim=1
    if [[ ",$bad," == *,stress,* ]]; then
      want_iso=$((cur_iso+1))
    elif [ "$victim" = 1 ] && [ "${scpu:--1}" -ge "${ISO_CPU_TRIGGER:-50}" ]; then
      want_iso=$((cur_iso+1))
      echo "   stress 가 CPU ${scpu}% 를 먹는 중 — 공유 노드 대신 전용 노드로 뺀다"
    fi
    if [[ ",$bad," == *,user,* ]] || [[ ",$bad," == *,product,* ]]; then
      want_shared=$((want_shared+1))
    fi
    # 어느 쪽으로도 안 늘었으면(위반 앱이 애매하면) 공유를 늘린다
    if [ "$want_iso" = "$cur_iso" ] && [ "$want_shared" = "$((cur_n-cur_iso))" ]; then
      want_shared=$((want_shared+1))
    fi
    # ★한 주기에 순증은 1대까지.
    #   stress 와 user/product 가 동시에 밀리면 예전엔 둘 다 처리해 4대 → 6대로
    #   두 칸을 한 번에 뛰었다(실측 3회차). 노드 1대는 비용 2점이라 두 칸이면 4점이고,
    #   효과를 재보기도 전에 지불하는 셈이다. 한 칸씩 사고 3분 뒤 채점한다.
    #   둘 다 밀리면 게이트에 가까운 쪽(stress)을 먼저 산다 — 비용 12점이 걸려 있다.
    # 게이트 방어(delta=2)는 두 칸까지 허용한다. 그 외는 한 칸.
    # ★계단(앞먹임)으로 나온 증설은 깎지 않는다.
    #   실측(2026-08-24 D회차): 계단이 +3 을 요청했는데 상한 2에 걸려 2→4 로만 갔고
    #   8대까지 세 주기가 걸렸다. 그 6분 30초 동안 user p50 이 1.5~5초였다.
    #   계단은 '지표가 나빠지기 전에' 미리 사는 것이라 한 칸씩 살 이유가 없다.
    local maxstep=1; [ "$delta" -ge 2 ] && maxstep=2
    [ "${step:-0}" = 1 ] && maxstep=$delta
    if [ "$delta" -ge 2 ]; then
      # 둘 다 밀리면 이미 두 칸이 잡혀 있다. 한쪽만 밀리면 그쪽으로 한 칸 더 준다.
      if [ "$((want_shared+want_iso))" -le "$((cur_n+1))" ]; then
        if [[ ",$bad," == *,stress,* ]] && [ "$want_iso" -gt "$cur_iso" ]; then
          want_shared=$((want_shared+1))
        else
          want_shared=$((want_shared+1))
        fi
      fi
    fi
    # ★계단이면 요청한 대수를 반드시 채운다.
    #   구성 로직은 bad 목록을 보고 '공유 +1 / 전용 +1' 식으로만 더하므로,
    #   판단이 +3 을 요청해도 합이 +2 에서 멈추는 일이 생긴다.
    #   실측(2026-08-24 E회차): 계단이 3→6대를 요청했는데 5대만 적용됐다.
    #   모자란 만큼은 공유로 채운다 — 전용은 stress 수요에 맞춰 이미 정해졌다.
    if [ "${step:-0}" = 1 ]; then
      local _need=$((cur_n+delta)) _have=$((want_shared+want_iso))
      if [ "$_have" -lt "$_need" ]; then
        want_shared=$((want_shared + _need - _have))
        echo "   계단 요청분을 채운다 — 공유를 ${_need} 대 구성에 맞춘다"
      fi
    fi
    if [ "$((want_shared+want_iso))" -gt "$((cur_n+maxstep))" ]; then
      if [ "$want_iso" -gt "$cur_iso" ]; then
        want_shared=$((cur_n-cur_iso))          # stress 전용만 +1
      else
        want_iso=$cur_iso                        # 공유만 +1
        want_shared=$((cur_n-cur_iso+1))
      fi
      echo "   (한 주기 순증은 ${maxstep}대까지 — 나머지는 다음 주기에)"
    fi
    new_n=$((want_shared+want_iso))
    [ "$new_n" -gt "$MAX_NODES" ] && { echo "   상한 $MAX_NODES 대 — 더 못 늘린다"; return 0; }
    cap=$((new_n+CAP_MARGIN))
  else
    # ★축소는 상한도 같이 내려야 실제로 줄어든다.
    #   apply.sh 는 상한이 열려 있으면(CAP>T) NodeClaim 을 회수하지 않는다.
    #   예전엔 축소 결정을 내려도 상한이 열린 채라 노드가 그대로 남았고,
    #   그래서 계곡 40분이 평균 4.93대로 유지됐다. 여기서 CAP=T 로 닫는다.
    if [ "$want_iso" -gt 0 ] && [[ ",$bad," != *,stress,* ]]; then
      want_iso=$((want_iso-1))
    else
      want_shared=$((want_shared-1)); [ "$want_shared" -lt 2 ] && want_shared=2
    fi
    new_n=$((want_shared+want_iso))
    [ "$new_n" -lt 2 ] && new_n=2
    [ "$new_n" = "$cur_n" ] && { echo "   더 내릴 곳이 없다"; return 0; }
    cap=$new_n
  fi
  new_mode=shared
  [ "$want_iso" = 1 ] && new_mode=iso
  [ "$want_iso" -gt 1 ] && new_mode="iso$want_iso"
  echo "   $cur_n/$cur_m → $new_n/$new_mode (공유 $want_shared + 전용 $want_iso, 상한 $cap)"
  # ★수렴 대기가 운영 루프를 막지 않게 상한을 씌운다.
  #   apply.sh 는 노드가 뜰 때까지 최대 800초를 기다린다. 그동안 루프는 아무 판단도
  #   못 한다 — 피크 한복판에서 13분을 눈감는 셈이다. 실측 3회차에서 실제로 그랬다.
  #   시간이 넘으면 그냥 다음 주기로 간다. 노드는 알아서 계속 뜬다.
  cap "${APPLY_TIMEOUT:-150}" ./apply.sh "$new_n" "$new_mode" "$cap" | tail -2
  return 2
}

# ── 안정화 확인 ───────────────────────────────────────────────────────────
# 트래픽은 정각에 들어온다. 그 순간에 롤아웃이 돌거나 노드가 뜨는 중이면
# 첫 분부터 5xx 가 나고, 가용성 12점이 깎인다. "준비 끝"을 객관적으로 판정한다.
ready() {
  local fail=0 n want tgt out
  echo "== 안정화 확인"

  # 1) 모든 Deployment 가 롤아웃 완료 상태인가 (원하는 수 == 준비된 수)
  out=$(kubectl -n "$NS" get deploy -o json 2>/dev/null | python3 -c "
import json,sys
bad=[]
for d in json.load(sys.stdin)['items']:
    st=d.get('status',{}); want=d['spec'].get('replicas',0)
    if st.get('readyReplicas',0)!=want or st.get('updatedReplicas',0)!=want:
        bad.append(d['metadata']['name'] + '(' + str(st.get('readyReplicas',0)) + '/' + str(want) + ')')
print(' '.join(bad))")
  if [ -n "$out" ]; then echo "   [X] 롤아웃 미완료: $out"; fail=1; else echo "   [O] 모든 Deployment 준비됨"; fi

  # 2) Pending 파드
  #    ★상한을 닫아두면 부하 중에는 Pending 이 정상이다.
  #      HPA 는 노드 사정을 모르고 파드를 늘리고, 노드 수는 도구가 정한다.
  #      갈 곳 없는 파드가 생기는 건 설계대로다 — 그걸 불안정으로 세면 매 주기
  #      60초씩 헛기다린다. 트래픽 전(prepare)에만 엄격히 본다.
  n=$(kubectl -n "$NS" get pods --field-selector=status.phase=Pending --no-headers 2>/dev/null | wc -l | tr -d ' ')
  if [ "${n:-0}" != 0 ] && [ "${STRICT_PENDING:-0}" = 1 ]; then
    echo "   [X] Pending 파드 ${n}개"; fail=1
  elif [ "${n:-0}" != 0 ]; then
    echo "   [O] Pending 파드 ${n}개 (상한이 닫혀 있으니 정상)"
  else echo "   [O] Pending 파드 없음"; fi

  # 3) 노드 수가 하한 이상이고 상한 이하인가
  #    ★상한이 열려 있으면 노드 수는 하한~상한 사이에서 정상적으로 오르내린다.
  #      "목표와 정확히 일치"를 요구하면 트래픽 중에는 항상 실패한다.
  want=$(awk '{print $1}' "$STATE" 2>/dev/null)
  cap=$(awk '{print ($3==""?$1:$3)}' "$STATE" 2>/dev/null)
  n=$(kubectl get nodes --no-headers 2>/dev/null | grep -c " Ready")
  if [ -n "$want" ] && { [ "${n:-0}" -lt "$want" ] || [ "${n:-0}" -gt "${cap:-$want}" ]; }; then
    echo "   [X] 노드 ${n}대 (하한 ${want} / 상한 ${cap:-$want}) — 수렴 중"; fail=1
  else echo "   [O] 노드 ${n}대 (하한 ${want:-?} / 상한 ${cap:-?})"; fi

  # 4) NotReady 노드가 없는가
  n=$(kubectl get nodes --no-headers 2>/dev/null | grep -c "NotReady")
  if [ "${n:-0}" != 0 ]; then echo "   [X] NotReady 노드 ${n}대"; fail=1; else echo "   [O] 모든 노드 Ready"; fi

  # 5) ALB 타깃이 전부 healthy 인가 — 실제로 트래픽을 받을 수 있는지의 최종 판정
  local lb tg bad=0 tot=0 hs
  lb=$(aws elbv2 describe-load-balancers --region "$REGION" --names "$ALB_NAME" \
       --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null)
  for tg in $(aws elbv2 describe-target-groups --region "$REGION" --load-balancer-arn "$lb" \
              --query 'TargetGroups[].TargetGroupArn' --output text 2>/dev/null); do
    hs=$(aws elbv2 describe-target-health --region "$REGION" --target-group-arn "$tg" \
         --query 'TargetHealthDescriptions[].TargetHealth.State' --output text 2>/dev/null)
    for h in $hs; do tot=$((tot+1)); [ "$h" = healthy ] || bad=$((bad+1)); done
  done
  if [ "$tot" = 0 ]; then echo "   [X] ALB 타깃이 하나도 없다"; fail=1
  elif [ "$bad" != 0 ]; then echo "   [X] ALB 타깃 ${bad}/${tot} 비정상"; fail=1
  else echo "   [O] ALB 타깃 ${tot}개 전부 healthy"; fi

  # 6) 최근에 구성을 바꿨다면 아직 흔들리는 중일 수 있다
  if [ -f "$STATE" ]; then
    local mt age
    mt=$(mtime "$STATE"); age=$(( $(date +%s) - ${mt:-0} ))
    if [ "$age" -lt 120 ]; then echo "   [X] 구성 변경 ${age}초 전 — 2분은 지켜봐라"; fail=1
    else echo "   [O] 마지막 구성 변경 ${age}초 전"; fi
  fi

  [ "$fail" = 0 ] && echo "== 준비 완료 — 트래픽 받아도 된다" || echo "== 아직 불안정하다"
  return $fail
}

# ── 준비 단계 ─────────────────────────────────────────────────────────────
prepare() {
  # ★회차 원장도 여기서만 지운다.
  #   점수는 회차 '전체'의 누적으로 매겨진다. 그래서 도구도 누적을 들고 있어야
  #   "이미 벌어둔 비용 여유"와 "남은 구간"을 구분할 수 있다.
  #   run 루프가 중간에 죽었다 살아나도 원장은 이어져야 하므로 거기서는 절대 안 지운다.
  rm -f .round-ledger.json
  local traffic
  traffic=$(read_traffic) || true
  if [ -n "${traffic:-}" ] && \
     [ "$(python3 -c "import json;print(1 if sum(json.loads('$traffic').values())>2 else 0)" 2>/dev/null)" = 1 ]; then
    echo "!! 트래픽이 흐르는 중이다 ($traffic). 곡선 측정은 파드를 1개로 줄이므로 위험하다."
    echo "   측정을 건너뛰고 콜드 스타트 구성만 적용한다."
  elif [ "${MEASURE_CURVES:-0}" != 1 ]; then
    # ★기본은 안 잰다.
    #   곡선은 solve.py 전용이고, solve.py 는 이제 운영 루프에서 안 쓴다
    #   (판단은 alb_snapshot.sh + decide.py 가 한다). 남은 용도는 pretune 의
    #   후보 하나 추천뿐인데, 측정에 대회 예산 6~10분이 든다. 값에 비해 비싸다.
    #   굳이 재려면: MEASURE_CURVES=1 ./autotune.sh prepare
    echo "== 동시성 곡선 측정은 건너뛴다 (MEASURE_CURVES=1 이면 잰다)"
    echo "   판단은 ALB 실측 백분위로 한다 — 곡선이 없어도 도구는 완전히 동작한다."
  else
    for spec in "user post" "user get" "product get" "stress post"; do
      set -- $spec
      # 저장소에 딸려온 곡선은 "내 연습 환경에서 잰 값"이다. 다른 계정·다른 대회장에서는
      # 그대로 쓰면 안 된다. 여기서 반드시 다시 잰다. (REUSE_CURVES=1 이면 재사용)
      if [ -f "concurrency-$1-$2.json" ]; then
        if [ "${REUSE_CURVES:-0}" = 1 ]; then echo "곡선 재사용: $1-$2 (REUSE_CURVES=1)"; continue; fi
        mv "concurrency-$1-$2.json" "seed-concurrency-$1-$2.json" 2>/dev/null
      fi
      DUR=${DUR:-8} ./concurrency.sh "$1" "$2" | tail -8
      if [ ! -f "concurrency-$1-$2.json" ] && [ -f "seed-concurrency-$1-$2.json" ]; then
        echo "   !! $1-$2 측정 실패 — 저장소에 딸려온 곡선으로 대체한다(다른 환경에서 잰 값이라 정확도 낮음)"
        cp "seed-concurrency-$1-$2.json" "concurrency-$1-$2.json"
      fi
    done
  fi

  # 콜드 스타트: 트래픽 규모를 모르는 상태에서의 시작 구성.
  #   ★하한과 상한을 분리한다.
  #     하한 2대  — 비용 만점 지점이자 고가용성 최소선. 트래픽이 가벼우면 여기 머문다.
  #     상한 6대  — 스파이크가 오면 Karpenter 가 알아서 늘릴 수 있게 열어둔다.
  #   상한까지 묶으면 예상 못 한 스파이크에 노드를 못 만들고 무너진다(실측 22.5점).
  #   점수는 트래픽 구간 '평균'이라, 초반 몇 분 노드가 많은 것보다 성능이 무너지는 게 훨씬 비싸다.
  #   트래픽을 재고 나면 run 루프가 상한을 하한까지 좁혀 비용을 확정한다.
  echo "== 콜드 스타트 구성 적용 (${COLD_NODES:-2}대 고정)"
  ./apply.sh "${COLD_NODES:-2}" "${COLD_MODE:-shared}" "${COLD_CAP:-${COLD_NODES:-2}}" | tail -3
  cap "${REQ_TIMEOUT:-120}" ./tune_requests.sh "${COLD_STRESS_REQ:-100m}" | tail -1


  # 트래픽이 들어오는 순간에 이미 안정 상태여야 한다. 될 때까지 확인한다.
  echo
  local i
  for i in $(seq 1 30); do
    STRICT_PENDING=1 ready && break
    echo "   ... 30초 후 재확인 ($i/30)"
    sleep 30
  done
}

case "${1:-run}" in
  prepare) prepare ;;
  once)    once yes ;;
  show)    once no ;;
  ready)   ready ;;
  run)
    # ★같은 클러스터를 두 튜너가 조작하면 서로의 결정을 덮어쓴다.
    #   실측(2026-08-21 3회차): 다른 PC 의 감독 루프가 살아 있는 줄 모르고 회차를
    #   시작했다. 한쪽은 3대를 지시했는데 노드는 6대가 됐고, 상태 파일과 클러스터가
    #   따로 놀아 로그만 봐서는 원인을 알 수 없었다. 회차 하나를 통째로 버렸다.
    #   클러스터 안에 임차권(lease)을 두고, 남이 살아 있으면 시작하지 않는다.
    LOCK_ID="${LOCK_ID:-$(hostname)-$$}"
    lock_take() {
      local cur age
      cur=$(kubectl -n "$NS" get cm tuner-lock -o jsonpath='{.data.owner}{" "}{.data.ts}' 2>/dev/null)
      if [ -n "${cur:-}" ]; then
        set -- $cur
        age=$(( $(date +%s) - ${2:-0} ))
        if [ "$1" != "$LOCK_ID" ] && [ "$age" -lt "${LOCK_TTL:-180}" ]; then
          echo "!! 다른 튜너가 이미 이 클러스터를 잡고 있다: $1 (${age}초 전 갱신)" >&2
          echo "   그쪽을 먼저 끄거나, 정말 넘겨받으려면:" >&2
          echo "     kubectl -n $NS delete cm tuner-lock" >&2
          return 1
        fi
      fi
      kubectl -n "$NS" create cm tuner-lock         --from-literal=owner="$LOCK_ID" --from-literal=ts="$(date +%s)"         --dry-run=client -o yaml 2>/dev/null | kubectl apply -f - >/dev/null 2>&1
      return 0
    }
    lock_beat() {
      kubectl -n "$NS" create cm tuner-lock         --from-literal=owner="$LOCK_ID" --from-literal=ts="$(date +%s)"         --dry-run=client -o yaml 2>/dev/null | kubectl apply -f - >/dev/null 2>&1
    }
    # ★잠금을 못 잡으면 바로 포기한다(exit 3). 감독 루프도 재기동하지 않는다.
    #   실측: 거부당한 쪽이 5초마다 무한 재시도해 1437번 헛돌았다.
    #   로그가 그 메시지로 가득 차서 진짜 상태를 못 보게 만든다.
    if ! lock_take; then
      echo "!! 시작하지 않는다. 먼저 그쪽을 끄고 다시 실행해라." >&2
      : > .no-restart      # 감독 루프에게 재기동하지 말라고 알린다
      exit 3
    fi
    rm -f .no-restart
    # ★죽으면 왜 죽었는지 남긴다.
    #   실측: 긴 회차에서 이 루프가 76분 만에 조용히 끝났다. 로그 마지막 줄은
    #   정상적인 트래픽 라인이었고 에러가 없었다. 원인을 못 찾은 채 감독 루프로
    #   덮어놨었는데, 덮기만 하면 다음에도 똑같이 모른다. 신호와 종료를 기록한다.
    trap 'echo "[$(date +%H:%M:%S)] 종료 신호 받음: $s" >&2' HUP INT TERM
    for s in HUP INT TERM; do trap "echo \"[\$(date +%H:%M:%S)] 신호 $s 받고 종료한다\" >&2; exit 129" $s; done
    trap 'rc=$?; echo "[$(date +%H:%M:%S)] 루프 종료 rc=$rc (line $LINENO)" >&2' EXIT
    echo "운영 루프 시작 (${INTERVAL}초 주기) pid=$$"
    while :; do
      lock_beat
      once yes; rc=$?
      # ★과부하 대응 직후(rc=2)에는 안정화를 기다리지 않는다.
      #   사다리는 여러 단계를 밟아야 하는데, 노드가 뜨는 중이라 ready 가 실패하면
      #   매 주기가 120초로 늘어져 다음 단계까지 몇 분이 더 걸린다.
      #   실측: peak2 붕괴 후 1단계까지 8분, 2단계까지 11분 걸렸다.
      #   무너지고 있는 중에 "흔들릴까 봐" 기다리는 건 손해가 더 크다.
      if [ "$rc" = 2 ]; then
        sleep "${URGENT_INTERVAL:-20}"
        continue
      fi
      # 구성을 바꿨으면 안정화될 때까지 지켜본 뒤 다음 주기로 간다.
      # 트래픽 중 파드/노드가 흔들리면 가용성(12점)이 깎이므로 조급하게 또 바꾸지 않는다.
      if ! ready >/dev/null 2>&1; then
        echo "   아직 불안정하다 — 60초 더 지켜본다 (자세히: ./autotune.sh ready)"
        sleep 60
      fi
      sleep "$INTERVAL"
    done ;;
  *) echo "사용: autotune.sh prepare|run|once|show" >&2; exit 1 ;;
esac
