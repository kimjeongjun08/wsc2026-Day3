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
cd "$(dirname "$0")"; source ./common.sh

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
CAP_MARGIN=${CAP_MARGIN:-2}       # 하한 위로 몇 대까지 자동 증설을 허용할지

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

# ── ALB 실측 지연으로 곡선을 자기보정한다 ────────────────────────────────
# 곡선은 내가 보낸 요청 본문으로 잰 것이다. 실제 트래픽의 본문이 다르면 비용이 다르다.
#   실측: stress 는 length 88 → 226ms, 150 → 489ms, 250 → 3299ms (초선형)
# 주입기가 뭘 보내는지는 볼 수 없으므로, "곡선 예측 vs ALB 실측"의 비율을 계수로 저장해
# 숨은 변수(요청 본문·앱 버전·DB 크기 증가)를 통째로 흡수한다.
recalibrate_curves() {
  local lb lbdim tg name rt traffic nodes mode
  traffic=$(read_traffic) || return 1
  nodes=$(awk '{print $1}' "$STATE" 2>/dev/null)
  mode=$(awk '{print $2}' "$STATE" 2>/dev/null)
  [ -z "${nodes:-}" ] && return 0
  lb=$(aws elbv2 describe-load-balancers --region "$REGION" --names "$ALB_NAME" \
       --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null)
  lbdim=${lb##*loadbalancer/}
  local obs="{" first=1
  for tg in $(aws elbv2 describe-target-groups --region "$REGION" --load-balancer-arn "$lb" \
              --query 'TargetGroups[].TargetGroupArn' --output text 2>/dev/null); do
    name=$(aws elbv2 describe-target-groups --region "$REGION" --target-group-arns "$tg" \
           --query 'TargetGroups[0].TargetGroupName' --output text); name=${name#apdev-}
    rt=$(aws cloudwatch get-metric-statistics --region "$REGION" \
         --namespace AWS/ApplicationELB --metric-name TargetResponseTime \
         --dimensions Name=LoadBalancer,Value="$lbdim" Name=TargetGroup,Value="${tg##*:}" \
         --start-time "$(date -u -v-6M +%Y-%m-%dT%H:%M:%S 2>/dev/null || date -u -d '6 minutes ago' +%Y-%m-%dT%H:%M:%S)" \
         --end-time "$(date -u +%Y-%m-%dT%H:%M:%S)" --period 60 --statistics Average \
         --extended-statistics p50 --query 'sort_by(Datapoints,&Timestamp)[-1].ExtendedStatistics.p50' \
         --output text 2>/dev/null)
    [ -z "$rt" ] || [ "$rt" = "None" ] && continue
    [ $first -eq 0 ] && obs="$obs,"; obs="$obs\"$name\":$(python3 -c "print(round($rt*1000,1))")"; first=0
  done
  obs="$obs}"
  [ "$obs" = "{}" ] && return 0
  python3 - "$traffic" "$obs" "$nodes" "$mode" <<'PY'
import json, sys, importlib.util
traffic, obs, nodes, mode = json.loads(sys.argv[1]), json.loads(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
spec = importlib.util.spec_from_file_location("solve", "solve.py")
solve = importlib.util.module_from_spec(spec); spec.loader.exec_module(solve)
cal = json.load(open("calibration.json"))
curves = solve.load_curves(".")
profiles = solve.load_profiles(".", traffic)
row = solve.evaluate(profiles, traffic, {"user":200,"product":200,"stress":1000},
                     2.0, cal, nodes, mode, curves)
if not row:
    raise SystemExit(0)
scale = cal.get("curve_scale", {})
for app, seen_ms in obs.items():
    key = f"{app}-post" if f"{app}-post" in curves else next((k for k in curves if k.startswith(app)), None)
    if not key: continue
    c = row["apps"].get(app, {}).get("rho", 1.0)
    pred = solve.curve_latency(curves[key], c)
    if pred <= 0: continue
    new = seen_ms / pred
    old = scale.get(app, 1.0)
    scale[app] = round(0.5*old + 0.5*max(0.2, min(20.0, new)), 3)   # 급변 방지로 절반씩 반영
    print(f"   보정 {app}: 곡선예측 {pred:.0f}ms vs ALB실측 {seen_ms:.0f}ms → 계수 {old:.2f}→{scale[app]:.2f}")
cal["curve_scale"] = scale
json.dump(cal, open("calibration.json","w"), indent=2, ensure_ascii=False)
PY
}

# ── 솔버에게 물어본다 ─────────────────────────────────────────────────────
ask_solver() {
  python3 solve.py --traffic "$1" --min-nodes 2 --max-nodes "$MAX_NODES" --top 5
}

pick_of() { sed -n 's/^최적: 노드 \([0-9]*\)대 \/ stress=\([a-z0-9]*\).*/\1 \2/p' <<<"$1"; }
score_of() { sed -n 's/^최적:.*예상 \([0-9.]*\)\/40.*/\1/p' <<<"$1"; }

# ── 과부하 방어 ───────────────────────────────────────────────────────────
# 모델은 정상 구간에서는 정확했지만(실측 오차 0.5점), 시스템이 무너지는 구간은 못 본다.
#   실측: 앱을 2배 무겁게 했더니 stress 응답이 12.5초까지 늘어 주입기 타임아웃으로
#   실패 처리됐고(가용성 54.96%), 점수는 16.5 였다. 모델은 35.5 를 예측했다.
# 그래서 모델과 별개로 ALB 실측 지연을 보고 직접 방어한다.
# SLA 를 넘고 있으면 계산을 기다리지 않고 노드를 늘린다.
overload_nodes() {   # SLA 초과 중이면 늘려야 할 노드 수를 출력, 아니면 빈 값
    local lb lbdim tg name rt cur worst=0
    cur=$(awk '{print $1}' "$STATE" 2>/dev/null)
    [ -z "${cur:-}" ] && return 0
    lb=$(aws elbv2 describe-load-balancers --region "$REGION" --names "$ALB_NAME" \
         --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null)
    lbdim=${lb##*loadbalancer/}
    for tg in $(aws elbv2 describe-target-groups --region "$REGION" --load-balancer-arn "$lb" \
                --query 'TargetGroups[].TargetGroupArn' --output text 2>/dev/null); do
      name=$(aws elbv2 describe-target-groups --region "$REGION" --target-group-arns "$tg" \
             --query 'TargetGroups[0].TargetGroupName' --output text); name=${name#apdev-}
      rt=$(aws cloudwatch get-metric-statistics --region "$REGION" \
           --namespace AWS/ApplicationELB --metric-name TargetResponseTime \
           --dimensions Name=LoadBalancer,Value="$lbdim" Name=TargetGroup,Value="${tg##*:}" \
           --start-time "$(date -u -v-4M +%Y-%m-%dT%H:%M:%S 2>/dev/null || date -u -d '4 minutes ago' +%Y-%m-%dT%H:%M:%S)" \
           --end-time "$(date -u +%Y-%m-%dT%H:%M:%S)" --period 60 --statistics Average \
           --query 'sort_by(Datapoints,&Timestamp)[-1].Average' --output text 2>/dev/null)
      [ -z "$rt" ] || [ "$rt" = "None" ] && continue
      local sla=0.2; [ "$name" = stress ] && sla=1.0
      if [ "$(python3 -c "print(1 if $rt > $sla else 0)")" = 1 ]; then
        echo "   [!] $name 응답 $(python3 -c "print(round($rt*1000))")ms > SLA $(python3 -c "print(int($sla*1000))")ms" >&2
        worst=1
      fi
    done
    [ "$worst" = 1 ] && echo $((cur+1))
    return 0
}

# ── 한 번 돌기 ────────────────────────────────────────────────────────────
once() {
  local apply=${1:-yes} traffic out nodes mode score cur
  traffic=$(read_traffic) || return 1
  echo "[$(date +%H:%M:%S)] 트래픽: $traffic"
  # 트래픽이 거의 없으면(아직 시작 전) 아무것도 바꾸지 않는다
  if [ "$(python3 -c "import json;print(1 if sum(json.loads('$traffic').values())<1 else 0)")" = 1 ]; then
    echo "   트래픽이 아직 없다 — 대기"; return 0
  fi
  # SLA 를 넘고 있으면 모델을 기다리지 않고 즉시 늘린다 (과부하 붕괴는 회복이 느리다)
  local urgent
  urgent=$(overload_nodes)
  if [ -n "${urgent:-}" ] && [ "$urgent" -le "$MAX_NODES" ]; then
    echo "   과부하 감지 → 노드 $urgent 대로 즉시 증설"
    if [ "$apply" = "yes" ]; then
      m=$(awk '{print $2}' "$STATE" 2>/dev/null)
      ./apply.sh "$urgent" "${m:-shared}" "$((urgent+CAP_MARGIN))" | tail -2
      echo "$urgent ${m:-shared} $((urgent+CAP_MARGIN))" > "$STATE"
    fi
    return 0
  fi
  recalibrate_curves || true
  out=$(ask_solver "$traffic") || return 1
  echo "$out" | tail -7
  read -r nodes mode <<<"$(pick_of "$out")"
  score=$(score_of "$out")
  [ -z "$nodes" ] && { echo "   솔버가 답을 못 냈다"; return 1; }

  cur=$(cat "$STATE" 2>/dev/null || echo "")
  if [ "$cur" = "$nodes $mode" ]; then
    echo "   현재 구성과 동일 ($nodes/$mode) — 유지"
    return 0
  fi
  # 구조 변경은 롤아웃을 부른다. 최근에 바꿨으면 참는다.
  if [ -f "$STATE" ]; then
    local mt age
    mt=$(mtime "$STATE"); age=$(( $(date +%s) - ${mt:-0} ))
    if [ "$age" -lt "$STRUCT_COOLDOWN" ]; then
      echo "   구조 변경 대기 (${age}초 전에 바꿨다, 최소 ${STRUCT_COOLDOWN}초)"
      return 0
    fi
  fi
  if [ "$apply" != "yes" ]; then
    echo "   추천: $nodes 노드 / $mode (예상 $score) — show 모드라 적용 안 함"
    return 0
  fi
  echo "   구성 변경: ${cur:-미설정} → $nodes/$mode (예상 $score)"
  # ★상한은 하한까지 좁히지 않는다.
  #   트래픽은 오르내린다(베이스라인 2rps ↔ 스파이크 22rps, 11배). 상한을 좁히면
  #   다음 스파이크에 Karpenter 가 못 늘린다. 비용은 구간 '평균'이라, 낮을 때 적게 쓰고
  #   높을 때 늘리는 쪽이 항상 이긴다 (계속 4대 평균 4.0 vs 오르내리며 평균 3.3).
  ./apply.sh "$nodes" "$mode" "$((nodes+CAP_MARGIN))" | tail -3
  echo "$nodes $mode $((nodes+CAP_MARGIN))" > "$STATE"
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

  # 2) Pending 파드가 없는가 (있으면 노드가 모자라거나 제약이 안 맞는 것)
  n=$(kubectl -n "$NS" get pods --field-selector=status.phase=Pending --no-headers 2>/dev/null | wc -l | tr -d ' ')
  if [ "${n:-0}" != 0 ]; then echo "   [X] Pending 파드 ${n}개"; fail=1; else echo "   [O] Pending 파드 없음"; fi

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
  local traffic
  traffic=$(read_traffic) || true
  if [ -n "${traffic:-}" ] && \
     [ "$(python3 -c "import json;print(1 if sum(json.loads('$traffic').values())>2 else 0)" 2>/dev/null)" = 1 ]; then
    echo "!! 트래픽이 흐르는 중이다 ($traffic). 곡선 측정은 파드를 1개로 줄이므로 위험하다."
    echo "   측정을 건너뛰고 콜드 스타트 구성만 적용한다."
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
  echo "== 콜드 스타트 구성 적용 (하한 ${COLD_NODES:-2} / 상한 ${COLD_CAP:-6})"
  ./apply.sh "${COLD_NODES:-2}" "${COLD_MODE:-shared}" "${COLD_CAP:-6}" | tail -3
  ./tune_requests.sh "${COLD_STRESS_REQ:-100m}" | tail -1
  echo "${COLD_NODES:-2} ${COLD_MODE:-shared} ${COLD_CAP:-6}" > "$STATE"

  # 트래픽이 들어오는 순간에 이미 안정 상태여야 한다. 될 때까지 확인한다.
  echo
  local i
  for i in $(seq 1 30); do
    ready && break
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
    echo "운영 루프 시작 (${INTERVAL}초 주기, ${MIN_GAIN}점 이상 개선될 때만 변경)"
    while :; do
      once yes
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
