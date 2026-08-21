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
  echo "[$(date +%H:%M:%S)] $(SNAP="$snap" python3 -c '
import json, os
d = json.loads(os.environ["SNAP"])
print(" ".join(f"{k}={v.get(chr(114)+chr(112)+chr(115),0)}rps" for k, v in sorted(d.items())))' 2>/dev/null)"

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
    # stress 는 노드를 사기 전에 먼저 CFS 지분부터 돌려준다 — 0대짜리 대응이라 제일 싸다.
    if [[ ",$bad," == *,stress,* ]] && [ "$cur_iso" = 0 ] && [ ! -f .stress-req-bumped ]; then
      echo "   stress 밀림 → cpu.requests 를 ${STRESS_REQ_HI:-600m} 로 (노드 0대)"
      ./tune_requests.sh "${STRESS_REQ_HI:-600m}" | tail -1
      : > .stress-req-bumped
      return 2
    fi
    [[ ",$bad," == *,stress,* ]] && want_iso=$((cur_iso+1))
    if [[ ",$bad," == *,user,* ]] || [[ ",$bad," == *,product,* ]]; then
      want_shared=$((want_shared+1))
    fi
    # 어느 쪽으로도 안 늘었으면(위반 앱이 애매하면) 공유를 늘린다
    if [ "$want_iso" = "$cur_iso" ] && [ "$want_shared" = "$((cur_n-cur_iso))" ]; then
      want_shared=$((want_shared+1))
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
  ./apply.sh "$new_n" "$new_mode" "$cap" | tail -2
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
  # 지난 회차에서 stress requests 를 올렸다는 표시는 여기서 지운다.
  # 안 지우면 다음 회차가 "이미 올렸다"고 판단해 1단계를 건너뛴다.
  rm -f .stress-req-bumped
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
  ./tune_requests.sh "${COLD_STRESS_REQ:-100m}" | tail -1


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
    # ★죽으면 왜 죽었는지 남긴다.
    #   실측: 긴 회차에서 이 루프가 76분 만에 조용히 끝났다. 로그 마지막 줄은
    #   정상적인 트래픽 라인이었고 에러가 없었다. 원인을 못 찾은 채 감독 루프로
    #   덮어놨었는데, 덮기만 하면 다음에도 똑같이 모른다. 신호와 종료를 기록한다.
    trap 'echo "[$(date +%H:%M:%S)] 종료 신호 받음: $s" >&2' HUP INT TERM
    for s in HUP INT TERM; do trap "echo \"[\$(date +%H:%M:%S)] 신호 $s 받고 종료한다\" >&2; exit 129" $s; done
    trap 'rc=$?; echo "[$(date +%H:%M:%S)] 루프 종료 rc=$rc (line $LINENO)" >&2' EXIT
    echo "운영 루프 시작 (${INTERVAL}초 주기) pid=$$"
    while :; do
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
