#!/usr/bin/env bash
# pretune.sh — 트래픽이 오기 전에 '직접 부하를 넣어' 최적 구성을 확정한다.
#
# 왜 필요한가:
#   과제지는 트래픽 양을 알려주지 않는다. 그래서 "일단 안전하게 시작하고 트래픽 보고 조정"을
#   하면, 조정이 끝나기 전 구간의 점수를 그대로 잃는다. 점수는 트래픽 구간 평균이라
#   초반 손실은 되돌릴 수 없다.
#   그런데 앱과 인프라는 이미 우리 손에 있다 — 우리가 직접 부하를 만들어 재보면 된다.
#
# 무엇을 하는가:
#   후보 구성마다 (노드 수 × stress 배치)
#     1) 구성을 적용하고 안정화를 기다린다
#     2) 공개 엔드포인트로 부하를 넣는다 (CloudFront → WAF → ALB → 파드 전 구간)
#     3) 앱별 SLA 통과율을 재고 채점 공식으로 점수를 매긴다
#   가장 높은 구성을 남긴다.
#
# ★DB 오염 주의
#   과제지: "발생하는 트래픽 외 임의의 데이터를 삽입하면 성능 저하가 생길 수 있으므로 주의".
#   POST 는 행을 만든다. 그래서 기본은 GET 위주(POST_RATIO 로 조절)이고,
#   총 POST 건수를 세어 마지막에 출력한다. 필요 이상으로 돌리지 마라.
#
# 사용:
#   ENDPOINT=http://xxx.cloudfront.net ./pretune.sh "2:shared 3:iso 3:shared 4:iso2"
#   RPS=50 DUR=60 ./pretune.sh            # 후보 생략 시 기본 후보군
set -uo pipefail
cd "$(dirname "$0")"; source ./lib.sh

ENDPOINT=${ENDPOINT:-}
if [ -z "$ENDPOINT" ]; then
  ENDPOINT=$(aws cloudfront list-distributions \
    --query "DistributionList.Items[?contains(Origins.Items[0].DomainName, 'apdev')].DomainName | [0]" \
    --output text 2>/dev/null)
  [ -n "$ENDPOINT" ] && [ "$ENDPOINT" != None ] && ENDPOINT="http://$ENDPOINT"
fi
[ -z "$ENDPOINT" ] && { echo "ENDPOINT 를 찾지 못했다 — 환경변수로 넘겨라" >&2; exit 1; }

RPS=${RPS:-50}                 # 총 목표 초당 요청수
# 후보당 부하 시간(초).
#   stress 는 전체의 3% 뿐이라 판정에 필요한 150건을 채우려면 시간이 든다.
#     필요 시간 ≈ 150 / (실제달성rps × 0.03)
#   주의: '실제 달성 rps' 는 목표보다 낮게 나온다(페이싱 + 응답 대기).
#   노드가 많은 구성일수록 응답이 빨라 오히려 총 요청이 줄어드는 경우도 있다.
#   표본이 모자라면 결과에 '표본부족' 으로 찍히니, 그때는 DUR 을 올려서 다시 재라.
DUR=${DUR:-180}
POST_RATIO=${POST_RATIO:-10}   # POST 비율(%) — DB 오염을 줄이려고 기본 10%
OUT=${OUT:-pretune-results.tsv}

# 앱별 요청 비율. 채점 주입기의 기본 비율(user:product:stress ≈ 44:48:2.5)을 따른다.
W_USER=${W_USER:-44}
W_PRODUCT=${W_PRODUCT:-48}
# ★비율은 채점 주입기와 똑같이 둔다. 절대 바꾸지 마라.
#   표본을 늘리려고 stress 비율을 3% → 12% 로 올린 적이 있는데, 그러면 stress 가
#   실제보다 4배 무거운 것처럼 보여 격리 구성이 유리하게 나온다. 판단이 실제로 뒤집혔다:
#     채점 x0.5(47rps)              최적 = 2노드/shared (40.0)
#     pretune(45rps, stress 12%)    최적 = 4노드/iso2   (33.5)
#   측정 편의로 넣은 값이 결론을 왜곡한 것이다. 표본은 비율이 아니라 시간으로 확보한다.
W_STRESS=${W_STRESS:-3}

SLA_USER=200; SLA_PRODUCT=200; SLA_STRESS=1000

# 후보를 안 주면 솔버가 알아서 고른다.
#   사람이 "2:shared 3:iso ..." 같은 조합을 외워서 타이핑할 이유가 없다.
#   solve.py 가 곡선과 트래픽으로 상위 후보를 뽑고, 그것들만 실제로 세워서 재면 된다.
CANDS=${1:-}
if [ -z "$CANDS" ]; then
  TRAFFIC=${TRAFFIC:-}
  if [ -z "$TRAFFIC" ]; then
    # 트래픽을 모르면 자체 부하 목표(RPS)를 채점 주입기 비율로 나눠 쓴다
    TRAFFIC=$(python3 -c "
r=$RPS; tot=$W_USER+$W_PRODUCT+$W_STRESS
u=r*$W_USER/tot; p=r*$W_PRODUCT/tot; s=r*$W_STRESS/tot
import json; print(json.dumps({'user_post':round(u*0.1,2),'user_get':round(u*0.9,2),
 'product_get':round(p*0.9,2),'product_post':round(p*0.1,2),'stress':round(s,2)}))")
  fi
  # ★후보는 기본 2개다. 후보 하나당 노드 재구성에 5~8분이 걸려서,
  #   대회의 1시간 예산에 4개는 안 들어간다(실측). 솔버 1·2위만 실제로 확인한다.
  CANDS=$(python3 solve.py --traffic "$TRAFFIC" --min-nodes 2 --max-nodes 6 --top "${TOP:-2}" 2>/dev/null \
          | awk '/^ *[0-9]+ +(shared|iso[0-9]*) /{print $1":"$2}' | awk '!seen[$0]++' | head -"${TOP:-2}")
  CANDS=$(echo $CANDS | tr "\n" " ")
  [ -z "$CANDS" ] && CANDS="2:shared 3:shared 3:iso"
  echo "   (후보 자동 선정: $CANDS)"
fi

# 워커 수. 각 워커는 PACE_MS 간격으로 쏘므로 총 rps ≈ WORKERS / (PACE_MS/1000) = RPS 가 된다.
WORKERS=${WORKERS:-24}

echo "== 사전 튜닝 — 엔드포인트 $ENDPOINT"
echo "   후보: $CANDS"
echo "   부하: 총 ${RPS}rps × ${DUR}초, POST 비율 ${POST_RATIO}%"
echo "   ※ POST 는 DB 에 행을 만든다. 과제지가 임의 데이터 삽입을 경계하므로 최소로 유지한다."
echo

# 부하 워커 — 파드 안에서 돈다 (외부에서 쏘면 내 노트북 회선이 병목이 된다)
# ★워커마다 목표 간격을 준다.
#   페이싱이 없으면 워커가 "가능한 한 빨리" 쏘므로, 빠른 구성일수록 부하가 커진다.
#   그러면 "같은 부하에서 어느 구성이 나은가"가 아니라 "구성마다 다른 부하"를 비교하게 된다.
#   실측: 목표 40rps 인데 2노드에서 70rps, 3노드에서 92rps 가 들어갔다.
PACE_MS=$(( 1000 * WORKERS / (RPS<1 ? 1 : RPS) ))
cat > /tmp/pre_worker.sh <<WORKER
EP=$ENDPOINT
DUR=$DUR
PACE_MS=$PACE_MS
POST_RATIO=$POST_RATIO
W_USER=$W_USER
W_PRODUCT=$W_PRODUCT
W_STRESS=$W_STRESS
WORKER
cat >> /tmp/pre_worker.sh <<'WORKER'
END=$(( $(date +%s) + DUR ))
TOT=$((W_USER + W_PRODUCT + W_STRESS))
UA='Mozilla/5.0'
# GET 대상 행을 하나씩 만들어 둔다 (POST 1건씩만 — 오염 최소)
SEED="pre-$(date +%s)-$$"
curl -s -o /dev/null -X POST -H 'Content-Type: application/json' -H "User-Agent: $UA" \
  -d "{\"requestid\":\"r\",\"uuid\":\"u\",\"username\":\"$SEED\",\"email\":\"$SEED@x.com\"}" "$EP/v1/user"
curl -s -o /dev/null -X POST -H 'Content-Type: application/json' -H "User-Agent: $UA" \
  -d "{\"requestid\":\"r\",\"uuid\":\"u\",\"id\":\"$SEED\",\"name\":\"$SEED\",\"price\":9.9}" "$EP/v1/product"

N=0
while [ "$(date +%s)" -lt "$END" ]; do
  N=$((N+1))
  R=$(( (N * 7919) % TOT ))          # 결정적 분배 (난수 없이 비율 유지)
  P=$(( (N * 31) % 100 ))
  if [ "$R" -lt "$W_USER" ]; then
    APP=user
    if [ "$P" -lt "$POST_RATIO" ]; then
      M=POST; B="{\"requestid\":\"r\",\"uuid\":\"u\",\"username\":\"$SEED-$WID-$N\",\"email\":\"$SEED-$WID-$N@x.com\"}"
    else
      M=GET;  Q="email=$SEED@x.com&requestid=r&uuid=u"
    fi
  elif [ "$R" -lt "$((W_USER + W_PRODUCT))" ]; then
    APP=product
    if [ "$P" -lt "$POST_RATIO" ]; then
      M=POST; B="{\"requestid\":\"r\",\"uuid\":\"u\",\"id\":\"$SEED-$WID-$N\",\"name\":\"p\",\"price\":9.9}"
    else
      M=GET;  Q="id=$SEED&requestid=r&uuid=u"
    fi
  else
    APP=stress; M=POST; B='{"requestid":"r","uuid":"u","length":88}'
  fi

  if [ "$M" = GET ]; then
    O=$(curl -s -m 30 -o /dev/null -w '%{http_code} %{time_total}\n' -H "User-Agent: $UA" "$EP/v1/$APP?$Q")
  else
    O=$(curl -s -m 30 -o /dev/null -w '%{http_code} %{time_total}\n' -X POST \
        -H 'Content-Type: application/json' -H "User-Agent: $UA" -d "$B" "$EP/v1/$APP")
  fi
  echo "$APP $M $O"
  # 목표 간격에 못 미치면 남은 만큼 쉰다 (초과했으면 그대로 진행)
  SPENT=$(echo "$O" | awk '{printf "%d", $2*1000}')
  REST=$(( PACE_MS - SPENT ))
  [ "$REST" -gt 0 ] && sleep "$(awk -v m=$REST 'BEGIN{printf "%.3f", m/1000}')"
done
WORKER
B64=$(base64 < /tmp/pre_worker.sh | tr -d '\n')

# 워커 파드는 stress 노드 taint 도 무시하도록 tolerations 를 넣는다
start_driver() {
  bx "kubectl -n $NS delete pod loadgen --ignore-not-found >/dev/null 2>&1
kubectl -n $NS run loadgen --image=curlimages/curl:8.5.0 --restart=Never \
  --overrides='{\"spec\":{\"tolerations\":[{\"operator\":\"Exists\"}]}}' \
  --command -- sleep 7200 >/dev/null
for i in \$(seq 1 60); do
  [ \"\$(kubectl -n $NS get pod loadgen -o jsonpath='{.status.phase}' 2>/dev/null)\" = Running ] && break
  sleep 3
done
kubectl -n $NS exec loadgen -- sh -c \"echo $B64 | base64 -d > /tmp/w.sh\"" >/dev/null
}

run_load() {   # 결과: 앱별 통과율과 달성 rps
  bx "kubectl -n $NS exec loadgen -- sh -c 'i=0; while [ \$i -lt $WORKERS ]; do WID=\$i sh /tmp/w.sh & i=\$((i+1)); done; wait'" 2>/dev/null
}

[ -f "$OUT" ] || printf "구성\tuser%%\tproduct%%\tstress%%\t성능\t비용\t합계\t달성rps\tPOST건수\n" > "$OUT"

# 중간에 끊겨도 워커 파드를 남기지 않는다
trap 'bx "kubectl -n $NS delete pod loadgen --ignore-not-found >/dev/null 2>&1" >/dev/null 2>&1' EXIT INT TERM

BEST=""; BEST_SCORE=-1
DONE_LIST=""
for C in $CANDS; do
  # 같은 구성을 두 번 재면 시간만 버린다 (실측: 상위 2개가 둘 다 4:iso2 로 나온 적 있음)
  case " $DONE_LIST " in (*" $C "*) echo "   (중복 후보 $C 건너뜀)"; continue ;; esac
  DONE_LIST="$DONE_LIST $C"
  T=${C%%:*}; MODE=${C##*:}
  echo "########## 후보: ${T}노드 / stress=$MODE ##########"
  ./apply.sh "$T" "$MODE" "$T" >/dev/null 2>&1
  echo "$T $MODE" > "${STATE:-.autotune-state}"
  # ★안정화 판정에서 '변경 후 2분 경과' 규칙은 빼고 기다린다.
  #   그 규칙은 채점 트래픽을 받기 직전에 필요한 것이고, 여기선 시간만 먹는다.
  #   파드가 다 뜨고 ALB 타깃이 healthy 면 부하를 넣어도 된다.
  for i in $(seq 1 40); do
    READY=$(./autotune.sh ready 2>&1 | grep -c '^   \[X\]' || true)
    BLOCK=$(./autotune.sh ready 2>&1 | grep '^   \[X\]' | grep -vc '구성 변경' || true)
    [ "${BLOCK:-1}" = 0 ] && break
    sleep 10
  done

  # ★워커 파드는 구성 변경 때 같이 죽을 수 있다(노드 회수). 후보마다 다시 띄운다.
  #   예전엔 한 번만 띄웠다가, 노드가 줄어드는 후보에서 워커가 사라져 결과가 전부 0 이 됐다.
  start_driver
  RAW=$(run_load)
  printf '%s\n' "$RAW" | python3 score_pretune.py "$T" "$DUR" > /tmp/pre_row.txt
  ROW=$(cat /tmp/pre_row.txt)
  ACHIEVED=$(echo "$ROW" | cut -f7)
  if [ "$(python3 -c "print(1 if ${ACHIEVED:-0} < 1 else 0)")" = 1 ]; then
    echo "   !! 부하가 들어가지 않았다 (달성 ${ACHIEVED}rps) — 이 후보는 무효"
    printf "%s노드/%s\t측정실패\n" "$T" "$MODE" >> "$OUT"
    continue
  fi
  printf "%s노드/%s\t%s\n" "$T" "$MODE" "$ROW" | tee -a "$OUT"
  SC=$(echo "$ROW" | cut -f6)
  if [ "$(python3 -c "print(1 if ${SC:-0} > $BEST_SCORE else 0)")" = 1 ]; then
    BEST_SCORE=$SC; BEST="$T $MODE"
  fi
  echo
done

bx "kubectl -n $NS delete pod loadgen --ignore-not-found >/dev/null 2>&1" >/dev/null

echo "===== 사전 튜닝 결과 ====="
column -t -s $'\t' "$OUT" 2>/dev/null || cat "$OUT"
echo
echo "최적: $BEST  (자체 부하 기준 $BEST_SCORE 점)"
echo "적용하려면: ./apply.sh $BEST"
echo
echo "※ 이 점수는 '자체 부하' 기준이라 채점 점수와 정확히 같지는 않다."
echo "  채점 주입기의 요청 비율·본문·몰림 패턴이 다르기 때문이다."
echo "  구성끼리의 '순위'를 정하는 용도로 쓰고, 실제 트래픽이 오면 autotune 이 다듬는다."
