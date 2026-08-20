#!/usr/bin/env bash
# concurrency.sh <app> [post|get] — 앱의 "동시성 → 지연/처리량" 곡선을 실측한다.
#
# 왜 필요한가:
#   요청 하나의 CPU 는 10ms 인데 채점이 보는 지연은 180ms 였다. 차이는 전부 '줄 서는 시간'이다.
#   그런데 평균 CPU 이용률로 계산한 ρ 는 0.1 수준이라, 1/(1-ρ) 같은 평균 기반 공식으로는
#   이 대기를 절대 못 만든다(예측이 전부 99.8% 로 나왔다).
#   대기는 '순간 동시성'이 만든다 → 그러면 동시성을 직접 걸어서 재면 된다.
#
# 무엇을 재는가:
#   파드 1개(노드 1대, vCPU 2개)에 동시 요청 수를 1,2,4,8,16,32 로 올려가며
#   각 지점의 p50/p90 지연과 처리량(rps)을 기록한다.
#   코어를 늘리면 이 곡선이 그대로 스케일되므로, 코어 C개에 초당 λ개가 들어올 때의
#   지연을 곡선에서 읽을 수 있다.
#
# ★2xx 응답만 센다. 예전엔 이스케이프가 깨져 잘못된 본문을 보냈고, 앱이 즉시 400 을
#   돌려주는 바람에 "동시성 1에서 1.5ms, 처리량 160rps" 같은 물리적으로 불가능한
#   곡선이 나왔다. 실패 응답은 빠르므로 반드시 상태코드로 걸러야 한다.
set -uo pipefail
cd "$(dirname "$0")" || exit 1; source ./common.sh

APP=${1:-user}
VERB=${2:-post}
LEVELS=${LEVELS:-"1 2 4 8 16 32"}
DUR=${DUR:-10}
OUT=${OUT:-concurrency-$APP-$VERB.json}
NS=${NS:-apdev}

case "$APP" in
  user)    QKEY=email ;;
  product) QKEY=id ;;
  stress)  QKEY=id ;;
  *) echo "모르는 앱: $APP" >&2; exit 1 ;;
esac

# 파드 안에서 돌 워커 스크립트. 이스케이프 지옥을 피하려고 통째로 base64 로 넘긴다.
# ★stress 는 요청 본문의 length 에 따라 작업량이 크게 달라진다(앱 안에서 대략 length^2).
#   그래서 곡선도 length 에 종속이다. 예전엔 88 로 하드코딩해서, 실제 트래픽이
#   400 을 보내는데도 곡선은 그대로였다 — 튜너가 부하 증가를 못 봤다.
#   실전에서는 주입기가 보내는 값을 모르므로 STRESS_LEN 으로 넘겨서 맞춘다
#   (모르면 measure_stress_len.sh 로 실측 지연에서 역산한다).
STRESS_LEN=${STRESS_LEN:-88}
cat > /tmp/conc_worker.sh <<WORKER
APP=$APP
VERB=$VERB
QKEY=$QKEY
DUR=$DUR
STRESS_LEN=$STRESS_LEN
WORKER
cat >> /tmp/conc_worker.sh <<'WORKER'
END=$(( $(date +%s) + DUR ))
END=$(( $(date +%s) + $DUR ))

body() {
  U="$1"
  case "$APP" in
    user)    printf '{"requestid":"r","uuid":"u","username":"%s","email":"%s@x.com"}' "$U" "$U" ;;
    product) printf '{"requestid":"r","uuid":"u","id":"%s","name":"%s","price":9.9}' "$U" "$U" ;;
    stress)  printf '{"requestid":"r","uuid":"u","length":%s}' "$STRESS_LEN" ;;
  esac
}

# GET 은 존재하는 행을 조회해야 실제 트래픽과 같은 경로를 탄다 → 먼저 한 건 만든다
FIXED="conc-$(date +%s)-$$"
# ★조회 값은 저장된 값과 정확히 같아야 한다.
#   user 는 email 로 찾는데 실제 저장 이메일은 "<이름>@x.com" 이다.
#   예전엔 이름만 넘겨서 전부 404 였고, 404 는 빨라서 곡선이 통째로 가짜가 됐다.
case "$APP" in
  user)    QVAL="$FIXED@x.com" ;;
  *)       QVAL="$FIXED" ;;
esac
if [ "$VERB" = "get" ]; then
  curl -s -o /dev/null -X POST -H 'Content-Type: application/json' \
       -d "$(body "$FIXED")" "http://$APP-svc:8080/v1/$APP"
fi

# alpine 의 busybox date 는 %N(나노초)을 지원하지 않는다.
# 예전에 %s%N 을 쓰다가 username 이 초 단위로 겹쳐 UNIQUE 위반 500 이 쏟아졌고,
# 실패 응답이 빨라서 "처리량 160rps" 같은 가짜 곡선이 나왔다. 워커별 카운터로 유일성을 만든다.
worker() {
  W=$1
  N=0
  while [ "$(date +%s)" -lt "$END" ]; do
    N=$((N+1))
    if [ "$VERB" = "get" ]; then
      OUT=$(curl -s -m 30 -o /dev/null -w '%{http_code} %{time_total}\n' \
            "http://$APP-svc:8080/v1/$APP?$QKEY=$QVAL&requestid=r&uuid=u")
    else
      U="c-$(date +%s)-$W-$N"
      OUT=$(curl -s -m 30 -o /dev/null -w '%{http_code} %{time_total}\n' -X POST \
            -H 'Content-Type: application/json' -d "$(body "$U")" \
            "http://$APP-svc:8080/v1/$APP")
    fi
    echo "$OUT"
  done
}

i=0
while [ "$i" -lt "$CONC" ]; do worker "$i" & i=$((i+1)); done
wait
WORKER
# 첫 END 줄은 오타 방지용 더미였다 — 제거한다
sed -i.bak '/^END=\$(( \$(date +%s) + DUR ))$/d' /tmp/conc_worker.sh
B64=$(base64 < /tmp/conc_worker.sh | tr -d '\n')

echo "== $APP($VERB) 동시성 곡선 — 파드 1개 기준, 지점당 ${DUR}초 (2xx 만 집계)"

bx "kubectl -n $NS patch hpa ${APP}-hpa -p '{\"spec\":{\"minReplicas\":1,\"maxReplicas\":1}}' >/dev/null 2>&1 || true
kubectl -n $NS scale deploy $APP --replicas=1 >/dev/null
kubectl -n $NS rollout status deploy/$APP --timeout=240s >/dev/null
for i in \$(seq 1 60); do
  c=\$(kubectl -n $NS get pods -l app=$APP --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l)
  [ \"\$c\" -eq 1 ] && break
  sleep 3
done
kubectl -n $NS delete pod conc --ignore-not-found >/dev/null 2>&1
kubectl -n $NS run conc --image=curlimages/curl:8.5.0 --restart=Never --command -- sleep 3600 >/dev/null
for i in \$(seq 1 60); do
  [ \"\$(kubectl -n $NS get pod conc -o jsonpath='{.status.phase}' 2>/dev/null)\" = Running ] && break
  sleep 3
done
kubectl -n $NS exec conc -- sh -c \"echo $B64 | base64 -d > /tmp/w.sh\"" >/dev/null

ROWS=""
for C in $LEVELS; do
  RAW=$(bx "kubectl -n $NS exec conc -- sh -c 'CONC=$C DUR=$DUR sh /tmp/w.sh'" 2>/dev/null)
  LINE=$(printf '%s\n' "$RAW" | awk -v c="$C" -v dur="$DUR" '
    $1 ~ /^2[0-9][0-9]$/ { ok[++n]=$2 }
    $1 !~ /^2[0-9][0-9]$/ && NF==2 { bad++ }
    END{
      if(n==0){ printf "%s,0,0,0,%d\n", c, bad+0; exit }
      asort(ok)
      printf "%s,%.1f,%.1f,%.1f,%d\n", c, ok[int(n*0.5)]*1000, ok[int(n*0.9)]*1000, n/dur, bad+0
    }' 2>/dev/null || printf '%s\n' "$RAW" | python3 -c "
import sys
ok=[]; bad=0
for l in sys.stdin:
    p=l.split()
    if len(p)!=2: continue
    if p[0].startswith('2'): ok.append(float(p[1]))
    else: bad+=1
ok.sort()
if not ok: print(f'$C,0,0,0,{bad}')
else: print(f'$C,{ok[int(len(ok)*0.5)]*1000:.1f},{ok[int(len(ok)*0.9)]*1000:.1f},{len(ok)/$DUR:.1f},{bad}')
")
  echo "   동시성 $(printf '%2s' "$C"): $(echo "$LINE" | awk -F, '{printf "p50=%6.1fms p90=%7.1fms 처리량=%5.1f rps  실패=%s", $2,$3,$4,$5}')"
  ROWS="$ROWS$LINE\n"
done

bx "kubectl -n $NS delete pod conc --ignore-not-found >/dev/null 2>&1
kubectl -n $NS patch hpa ${APP}-hpa -p '{\"spec\":{\"minReplicas\":2,\"maxReplicas\":20}}' >/dev/null 2>&1 || true" >/dev/null

printf "$ROWS" | python3 -c "
import sys, json
pts=[]
for l in sys.stdin:
    l=l.strip()
    if not l: continue
    c,p50,p90,rps,bad = l.split(',')
    pts.append({'concurrency':int(c),'p50_ms':float(p50),'p90_ms':float(p90),
                'rps':float(rps),'failed':int(bad)})
json.dump({'app':'$APP','verb':'$VERB','vcpu_per_pod_node':2,'points':pts},
          open('$OUT','w'), indent=2, ensure_ascii=False)
print('저장: $OUT')
"
