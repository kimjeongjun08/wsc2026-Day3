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

CANDS=${1:-"2:shared 3:shared 3:iso 4:iso 4:iso2"}
RPS=${RPS:-50}                 # 총 목표 초당 요청수
DUR=${DUR:-45}                 # 후보당 부하 시간(초)
POST_RATIO=${POST_RATIO:-10}   # POST 비율(%) — DB 오염을 줄이려고 기본 10%
OUT=${OUT:-pretune-results.tsv}

# 앱별 요청 비율. 채점 주입기의 기본 비율(user:product:stress ≈ 44:48:2.5)을 따른다.
W_USER=${W_USER:-44}
W_PRODUCT=${W_PRODUCT:-48}
W_STRESS=${W_STRESS:-3}

SLA_USER=200; SLA_PRODUCT=200; SLA_STRESS=1000

echo "== 사전 튜닝 — 엔드포인트 $ENDPOINT"
echo "   후보: $CANDS"
echo "   부하: 총 ${RPS}rps × ${DUR}초, POST 비율 ${POST_RATIO}%"
echo "   ※ POST 는 DB 에 행을 만든다. 과제지가 임의 데이터 삽입을 경계하므로 최소로 유지한다."
echo

# 부하 워커 — 파드 안에서 돈다 (외부에서 쏘면 내 노트북 회선이 병목이 된다)
cat > /tmp/pre_worker.sh <<WORKER
EP=$ENDPOINT
DUR=$DUR
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

# 목표 rps 를 워커 수로 낸다. 워커 하나가 초당 몇 개를 내는지는 지연에 달렸으므로,
# 넉넉히 띄우고 실제 달성 rps 를 결과에 같이 기록한다.
WORKERS=${WORKERS:-24}

run_load() {   # 결과: 앱별 통과율과 달성 rps
  bx "kubectl -n $NS exec loadgen -- sh -c 'i=0; while [ \$i -lt $WORKERS ]; do WID=\$i sh /tmp/w.sh & i=\$((i+1)); done; wait'" 2>/dev/null
}

[ -f "$OUT" ] || printf "구성\tuser%%\tproduct%%\tstress%%\t성능\t비용\t합계\t달성rps\tPOST건수\n" > "$OUT"

BEST=""; BEST_SCORE=-1
for C in $CANDS; do
  T=${C%%:*}; MODE=${C##*:}
  echo "########## 후보: ${T}노드 / stress=$MODE ##########"
  ./apply.sh "$T" "$MODE" "$T" >/dev/null 2>&1
  echo "$T $MODE" > "${STATE:-.autotune-state}"
  # 안정화될 때까지 기다린다 (트래픽 전이라 여유가 있다)
  for i in $(seq 1 40); do ./autotune.sh ready >/dev/null 2>&1 && break; sleep 15; done

  # ★워커 파드는 구성 변경 때 같이 죽을 수 있다(노드 회수). 후보마다 다시 띄운다.
  #   예전엔 한 번만 띄웠다가, 노드가 줄어드는 후보에서 워커가 사라져 결과가 전부 0 이 됐다.
  start_driver
  RAW=$(run_load)
  printf '%s\n' "$RAW" | python3 -c "
import sys
sla={'user':0.2,'product':0.2,'stress':1.0}
ok={}; tot={}; posts=0; n=0
for l in sys.stdin:
    p=l.split()
    if len(p)!=4: continue
    app,m,code,t=p[0],p[1],p[2],float(p[3])
    if m=='POST': posts+=1
    n+=1
    if not code.startswith('2'): continue
    tot[app]=tot.get(app,0)+1
    if t<=sla.get(app,0.2): ok[app]=ok.get(app,0)+1
PERF=[90,87.5,85,82.5,80,70,50,30]
def pts(p): return sum(0.5 for x in PERF if p>=x)
res={}
for a in ('user','product','stress'):
    res[a]=100.0*ok.get(a,0)/tot[a] if tot.get(a) else 0.0
perf=sum(pts(res[a]) for a in res)
gate=min(res.values()) if res else 0
cost=sum(1.0 for i in range(12) if 1.0+0.25*i >= $T/2.0) if gate>=30 else 0.0
print(f\"{res['user']:.2f}\t{res['product']:.2f}\t{res['stress']:.2f}\t{perf:.1f}\t{cost:.1f}\t{4+12+perf+cost:.1f}\t{n/$DUR:.1f}\t{posts}\")
" > /tmp/pre_row.txt
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
