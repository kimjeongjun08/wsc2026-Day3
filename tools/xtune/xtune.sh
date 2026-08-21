#!/usr/bin/env bash
# xtune.sh — 비상용 '추가 애플리케이션' 튜너 (원본 tools/tuner 와 완전 분리).
#
# 언제 쓰나:
#   대회에 4번째 애플리케이션이 나오면, 원본 tuner(user/product/stress 전용)는 그 앱의
#   최적값을 못 잡는다. 이 도구는 '그 새 앱 하나'만 원본 방식으로 벤치마크·사이징·적용한다.
#   새 앱의 정체(무엇을 하는지 — DB/DDB/stress류든)를 몰라도, 실제로 부하를 걸어 재서 정한다.
#
# ★원본과 안 꼬이는 이유(동시 실행 안전):
#   1) 대상은 오직 인자로 준 새 앱뿐. user/product/stress 는 아예 대상 거부(guard).
#   2) iso 모드는 X 를 '전용 Karpenter 노드풀(<prefix>-xtune-pool)'에 격리한다. 원본은 <prefix>-pool /
#      <prefix>-stress-pool 만 회수·캡핑하므로 X 전용풀 노드를 절대 못 건드린다.
#   3) 원본 solve.py 는 미지 앱 트래픽을 '[skip]' 처리한다(코드 확인) → 원본도 안 깨진다.
#   4) 프로브 파드(xtune-probe)·상태파일(.xtune-state-<app>)·측정파일(xcurve-<app>.json) 전부
#      원본과 다른 이름 → 파일/파드 충돌 없음.
#   5) 원본 lib.sh 를 안 읽는다(채점서버 자격증명 GRADER/GPASS 불필요) — 독립 실행.
#
# 사용:
#   ./xtune.sh scaffold   <app> [옵션]           # 새 앱 인프라 파일 생성(deploy/svc/hpa/tgb/tf) + 체크리스트
#   ./xtune.sh measure    <app> [옵션]           # 동시성->지연/처리량 곡선 실측 (부하는 클러스터 내부)
#   ./xtune.sh recommend  <app>                  # 곡선으로 배치(pack-app/pack-stress/iso)·HPA·baseline 추천
#   ./xtune.sh apply <app> pack-app              # baseline 2: (user+product+X) | (stress)   — X 를 MNG 노드에
#   ./xtune.sh apply <app> pack-stress           # baseline 2: (user+product) | (stress+X)   — X 를 stress 노드에
#   ./xtune.sh apply <app> iso [노드수]          # baseline 3: (user+product) | (stress) | (X 전용)  — 무거운 X
#   ./xtune.sh remove     <app>                  # 롤백 (모드 자동 판별)
#   ./xtune.sh help
#
# ★배치 3가지 (원본 baseline = MNG1[user+product] + stress1 = 2대):
#   원본 apply.sh 는 apdev-pool / apdev-stress-pool 의 초과 NodeClaim 을 '회수(삭제)'한다 → X 를 그 두
#   풀에 올리면 원본이 X 노드를 지워 꼬인다. 그래서 X 는 '원본이 회수 안 하는 노드'에만 올린다:
#     · pack-app    : X 를 MNG 노드(user/product 와 동거)에 고정. 새 노드 0 -> baseline 2.
#                     MNG 은 관리형(고정)이라 회수 안 됨. user/product 가 지연민감(200ms)이라 X 가
#                     가볍고 io 성격일 때 적합. (무겁거나 CPU-burst면 user/product 지연을 해친다.)
#     · pack-stress : X 를 stress 노드(stress 와 동거)에 고정. 새 노드 0 -> baseline 2.
#                     stress SLO 가 관대(1s)라 CPU성 가벼운 X 에 적합. (stress 도 CPU-burn이라 둘 다
#                     무거우면 서로 굶는다 -> 그땐 iso.)
#     · iso         : X 전용 노드풀(원본이 안 건드림) -> baseline +노드수(=3). 무거운 X 의 안전한 정답.
#   recommend 가 측정으로 셋 중 하나를 고른다. baseline 2 로 성능이 안 나오면 iso 로 올려라.
#   (원본 3앱 자체의 baseline 을 3 으로 올리는 판단은 원본 solve.py --min-nodes 몫 — xtune 은 X 만.)
#
# 옵션(env):
#   NS=apdev  DEPLOY=<app>  SVC=<app>-svc:8080  LABEL=app=<app>  HPA=<app>-hpa
#   APATH=/v1/<app>  METHOD=POST|GET  BODY='{"...":"__N__"}'  QKEY= QVAL=  SLA_MS=1000
#   LEVELS="1 2 4 8 16 32"  DUR=10   TARGET_RPS=<피크 rps 추정>   REQ= UTIL= MAXREP=(수동 오버라이드)
#     · POST 면 BODY 필수. 요청마다 유니크가 필요하면 BODY 안에 __N__ 을 넣어라(카운터로 치환).
#     · GET 이고 조회키가 있으면 QKEY/QVAL 지정(존재하는 행을 조회해야 404 안 뜬다).
set -uo pipefail
cd "$(dirname "$0")"
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-ap-northeast-2}

CMD=${1:-help}
APP=${2:-}
NS=${NS:-apdev}
DEPLOY=${DEPLOY:-$APP}
SVC=${SVC:-$APP-svc:8080}
LABEL=${LABEL:-app=$APP}
HPA=${HPA:-$APP-hpa}
APATH=${APATH:-/v1/$APP}
METHOD=${METHOD:-POST}
BODY=${BODY:-}
QKEY=${QKEY:-}
QVAL=${QVAL:-}
SLA_MS=${SLA_MS:-1000}
LEVELS=${LEVELS:-"1 2 4 8 16 32"}
DUR=${DUR:-10}
VCPU=${VCPU:-2}
PROBE=xtune-probe
CURVE="xcurve-$APP.json"
STATE=".xtune-state-$APP"

need() { command -v "$1" >/dev/null 2>&1 || { echo "!! $1 가 없다"; exit 1; }; }
require_app() { [ -n "$APP" ] || { echo "앱 이름을 줘라: ./xtune.sh $CMD <app>" >&2; exit 1; }; }

# ★안전장치: 원본 tuner 담당 앱은 절대 대상이 될 수 없다 → 동시 실행 시 원본 자원 무접촉 보장.
guard_originals() {
  case "$APP" in
    user|product|stress)
      echo "!! '$APP' 는 원본 tuner(tools/tuner) 담당이다. xtune 은 '추가' 앱 전용이라 거부한다." >&2
      exit 1 ;;
  esac
}

# ── 전용 노드풀 이름/노드클래스 자동 발견 (하드코딩 없이) ──
discover_prefix() {   # 예: 'apdev' — <prefix>-nodeclass, <prefix>-xtune-pool 산정에 씀
  local nc
  nc=$(kubectl get ec2nodeclass -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  [ -n "$nc" ] || { echo "!! EC2NodeClass 를 못 찾았다 — Karpenter 배포 상태 확인" >&2; return 1; }
  echo "${nc%-nodeclass}"
}

# ── MNG(관리형 노드그룹 = 고정 baseline 노드) 라벨 발견 — pack-app 이 X 를 여기 고정하는 데 씀 ──
#   Karpenter 가 아닌 노드(=MNG)의 eks nodegroup 라벨을 읽는다. 관리형이라 원본이 회수 안 하는 안전한 고정 대상.
discover_mng_label() {
  local ng
  ng=$(kubectl get nodes -l '!karpenter.sh/nodepool' \
        -o jsonpath='{.items[0].metadata.labels.eks\.amazonaws\.com/nodegroup}' 2>/dev/null)
  [ -n "$ng" ] || { echo "!! MNG 노드그룹 라벨을 못 찾았다(비-Karpenter 노드 없음?)" >&2; return 1; }
  echo "eks.amazonaws.com/nodegroup=$ng"
}

# ─────────────────────────────────────────────────────────────────────────────
# measure — 파드 1개(2코어)에 동시성을 1..32 로 걸어 지연/처리량 곡선을 실측한다.
#   원본 concurrency.sh 와 같은 원리(2xx 만 집계, 파드 내부에서 부하 → 노트북 회선 병목 배제).
#   대상 앱의 deploy/hpa 만 잠깐 만진 뒤 원복한다(원본 3앱은 안 건드림).
# ─────────────────────────────────────────────────────────────────────────────
measure() {
  require_app; guard_originals; need kubectl
  [ "$METHOD" = GET ] || [ -n "$BODY" ] || { echo "!! POST 는 BODY 가 필요하다(env BODY=...)." >&2; exit 1; }
  kubectl -n "$NS" get deploy "$DEPLOY" >/dev/null 2>&1 || { echo "!! deploy '$DEPLOY' 없음(NS=$NS). DEPLOY= 로 지정." >&2; exit 1; }

  # 원상복구용 현재 HPA 한도 저장
  local OMIN OMAX
  OMIN=$(kubectl -n "$NS" get hpa "$HPA" -o jsonpath='{.spec.minReplicas}' 2>/dev/null)
  OMAX=$(kubectl -n "$NS" get hpa "$HPA" -o jsonpath='{.spec.maxReplicas}' 2>/dev/null)

  restore() {
    kubectl -n "$NS" delete pod "$PROBE" --ignore-not-found >/dev/null 2>&1
    if [ -n "${OMIN:-}" ]; then
      kubectl -n "$NS" patch hpa "$HPA" -p "{\"spec\":{\"minReplicas\":$OMIN,\"maxReplicas\":$OMAX}}" >/dev/null 2>&1 || true
    fi
  }
  trap restore EXIT INT TERM

  echo "== [$APP] 측정: 파드 1개 기준, 동시성 $LEVELS, 지점당 ${DUR}초 (2xx 만 집계)"
  [ -n "${OMIN:-}" ] && kubectl -n "$NS" patch hpa "$HPA" -p '{"spec":{"minReplicas":1,"maxReplicas":1}}' >/dev/null 2>&1
  kubectl -n "$NS" scale deploy "$DEPLOY" --replicas=1 >/dev/null
  kubectl -n "$NS" rollout status deploy/"$DEPLOY" --timeout=240s >/dev/null
  local i
  for i in $(seq 1 60); do
    [ "$(kubectl -n "$NS" get pods -l "$LABEL" --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l)" -eq 1 ] && break
    sleep 3
  done

  # 워커 스크립트(이스케이프 회피: base64 로 파드에 넣는다)
  cat > /tmp/xtune_worker.sh <<WORKER
METHOD=$METHOD
SVC=$SVC
APATH=$APATH
DUR=$DUR
QKEY=$QKEY
QVAL=$QVAL
BODY='$BODY'
WORKER
  cat >> /tmp/xtune_worker.sh <<'WORKER'
END=$(( $(date +%s) + DUR ))
worker() {
  W=$1; N=0
  while [ "$(date +%s)" -lt "$END" ]; do
    N=$((N+1))
    if [ "$METHOD" = GET ]; then
      Q=""; [ -n "$QKEY" ] && Q="?$QKEY=$QVAL"
      curl -s -m 30 -o /dev/null -w '%{http_code} %{time_total}\n' "http://$SVC$APATH$Q"
    else
      B=$(printf '%s' "$BODY" | sed "s/__N__/$W-$N/g")
      curl -s -m 30 -o /dev/null -w '%{http_code} %{time_total}\n' -X "$METHOD" \
           -H 'Content-Type: application/json' -d "$B" "http://$SVC$APATH"
    fi
  done
}
i=0; while [ "$i" -lt "$CONC" ]; do worker "$i" & i=$((i+1)); done; wait
WORKER
  local B64; B64=$(base64 < /tmp/xtune_worker.sh | tr -d '\n')

  # 대상 파드/노드 (CPU 실측용)
  local POD NODE
  POD=$(kubectl -n "$NS" get pods -l "$LABEL" --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
  NODE=$(kubectl -n "$NS" get pods -l "$LABEL" --field-selector=status.phase=Running -o jsonpath='{.items[0].spec.nodeName}')
  cpu_ctr() { kubectl get --raw "/api/v1/nodes/$NODE/proxy/metrics/resource" 2>/dev/null \
              | awk -v p="$POD" '$0 ~ /container_cpu_usage_seconds_total/ && $0 ~ p {print $(NF-1)}' | head -1; }

  # 프로브 파드 (모든 노드 taint 무시하도록 tolerations)
  kubectl -n "$NS" delete pod "$PROBE" --ignore-not-found >/dev/null 2>&1
  kubectl -n "$NS" run "$PROBE" --image=curlimages/curl:8.5.0 --restart=Never \
    --overrides='{"spec":{"tolerations":[{"operator":"Exists"}]}}' --command -- sleep 3600 >/dev/null
  for i in $(seq 1 60); do
    [ "$(kubectl -n "$NS" get pod "$PROBE" -o jsonpath='{.status.phase}' 2>/dev/null)" = Running ] && break
    sleep 3
  done
  kubectl -n "$NS" exec "$PROBE" -- sh -c "echo $B64 | base64 -d > /tmp/w.sh"

  local C0 C1 TOT=0 ROWS=""
  C0=$(cpu_ctr)
  for C in $LEVELS; do
    local RAW LINE
    RAW=$(kubectl -n "$NS" exec "$PROBE" -- sh -c "CONC=$C DUR=$DUR sh /tmp/w.sh" 2>/dev/null)
    LINE=$(printf '%s\n' "$RAW" | awk -v c="$C" -v dur="$DUR" '
      $1 ~ /^2[0-9][0-9]$/ { ok[++n]=$2 }
      $1 !~ /^2[0-9][0-9]$/ && NF==2 { bad++ }
      END{ if(n==0){printf "%s,0,0,0,%d,0\n",c,bad+0; exit}
           asort(ok); printf "%s,%.1f,%.1f,%.1f,%d,%d\n",
             c, ok[int(n*0.5)]*1000, ok[int(n*0.9)]*1000, n/dur, bad+0, n }')
    echo "   동시성 $(printf '%2s' "$C"): $(echo "$LINE" | awk -F, '{printf "p50=%6.1fms p90=%7.1fms 처리량=%6.1f rps 실패=%s",$2,$3,$4,$5}')"
    TOT=$((TOT + $(echo "$LINE" | cut -d, -f6)))
    ROWS="$ROWS$LINE\n"
  done
  C1=$(cpu_ctr)

  # CPU 실측: 요청당 CPU ms (io/cpu 바운드 분류에 씀). 실패해도 곡선은 유효.
  local CPUMS
  CPUMS=$(python3 -c "
c0='$C0' or '0'; c1='$C1' or '0'; tot=$TOT
try:
  d=float(c1)-float(c0)
  print(round(d/tot*1000,2) if tot>0 and d>0 else 0)
except: print(0)" 2>/dev/null || echo 0)

  printf "$ROWS" | SLA_MS=$SLA_MS APP=$APP CURVE=$CURVE CPUMS=$CPUMS VCPU=$VCPU python3 -c "
import sys, os, json
sla=float(os.environ['SLA_MS']); pts=[]
for l in sys.stdin:
    l=l.strip()
    if not l: continue
    c,p50,p90,rps,bad,n=l.split(',')
    pts.append({'concurrency':int(c),'p50_ms':float(p50),'p90_ms':float(p90),
                'rps':float(rps),'failed':int(bad),'ok':int(n)})
under=[p for p in pts if p['p90_ms']>0 and p['p90_ms']<=sla]
sat = under[-1] if under else (pts[0] if pts else {'rps':0,'concurrency':0})
out={'app':os.environ['APP'],'sla_ms':sla,'vcpu_per_pod':int(os.environ['VCPU']),
     'cpu_ms_per_req':float(os.environ['CPUMS']),
     'rps_at_sla_per_pod':round(sat['rps'],1),'sat_concurrency':sat['concurrency'],
     'points':pts}
json.dump(out, open(os.environ['CURVE'],'w'), indent=2, ensure_ascii=False)
print(f\"\n저장: {os.environ['CURVE']}  (SLA 내 파드당 처리량 ~{out['rps_at_sla_per_pod']} rps, 요청당 CPU {out['cpu_ms_per_req']}ms)\")
"
  echo "→ 추천 보기:  ./xtune.sh recommend $APP"
}

# ─────────────────────────────────────────────────────────────────────────────
# recommend — 측정 곡선으로 pack/iso·HPA·requests·baseline 추천 (클러스터 변경 없음)
#   python 은 이스케이프 버그를 피해 quoted-heredoc + env 로 넘긴다.
# ─────────────────────────────────────────────────────────────────────────────
recommend() {
  require_app; guard_originals
  [ -f "$CURVE" ] || { echo "!! $CURVE 없음 — 먼저 ./xtune.sh measure $APP" >&2; exit 1; }
  APPNAME="$APP" CURVE="$CURVE" TARGET_RPS="${TARGET_RPS:-}" python3 <<'PY'
import json, os, math
d=json.load(open(os.environ['CURVE'])); app=os.environ['APPNAME']
R=d['rps_at_sla_per_pod']; cpums=d['cpu_ms_per_req']; vcpu=d['vcpu_per_pod']; node_m=vcpu*1000
p50=next((p['p50_ms'] for p in d['points'] if p['concurrency']==1), 0)
cpu_bound = cpums>0 and p50>0 and cpums>=0.5*p50
kind='cpu-bound' if cpu_bound else 'io/latency-bound'
if cpu_bound: req_m=max(100, node_m//2); util=50
else:         req_m=max(50, int(round(cpums))); util=80
print(f'== [{app}] 추천 (측정 기반)')
print(f'  앱 성격      : {kind}  (요청당 CPU {cpums}ms, 동시성1 지연 {p50}ms)')
print(f'  파드당 용량  : SLA({int(d["sla_ms"])}ms) 내 ~{R} rps  (포화 동시성 {d["sat_concurrency"]})')
print(f'  cpu requests : {req_m}m,  HPA util {util}%,  HPA min 2')
tr=os.environ.get('TARGET_RPS','')
if not tr:
    print(f'  HPA max/노드 : 피크 rps 를 알면 TARGET_RPS=<피크> 로 다시 실행 → max/baseline 계산')
    print(f'    (파드당 {R}rps 이니 예: 피크 {int(R*4)}rps 면 max~4)')
    print(f'\n  다음:  TARGET_RPS=<피크rps> ./xtune.sh recommend {app}   후  apply pack|iso')
else:
    tr=float(tr); maxrep=max(2, math.ceil(tr/max(R,0.01)))
    dem_m=math.ceil(tr*cpums)                           # X 실제 총 CPU 요구(mCPU) = 목표rps × 요청당CPU
    iso_nodes=max(1, math.ceil(dem_m/int(0.7*node_m)))  # 실요구 기준 전용 노드수(가용률 0.7)
    pack_req=max(50, min(250, int(round(cpums))))       # 동거용 소극적 request(eager 예약 안 함)
    print(f'  목표 {tr}rps  -> HPA max {maxrep},  실제 CPU 요구 ~{dem_m}m ({dem_m/node_m:.1f} 코어)')
    print()
    if (not cpu_bound) and dem_m<=1400 and maxrep<=3:
        print(f'  >> 추천: PACK-APP (baseline 2).  (user+product+{app}) | (stress)')
        print(f'     io 성격 + 가벼움({dem_m}m) -> user/product 노드(MNG) 여유에 동거. 지연도 좋다.')
        print(f'     ./xtune.sh apply {app} pack-app         # req~{pack_req}m util{util} max{maxrep}')
        print(f'     대안(stress 노드 동거):  ./xtune.sh apply {app} pack-stress')
    elif cpu_bound and dem_m<=500 and maxrep<=2:
        print(f'  >> 추천: PACK-STRESS (baseline 2).  (user+product) | (stress+{app})')
        print(f'     CPU성이나 매우 가벼움({dem_m}m) -> 지연관대한 stress 노드 여유에 동거(user/product 보호).')
        print(f'     ./xtune.sh apply {app} pack-stress      # req~{pack_req}m util{util} max{maxrep}')
        print(f'     ※ stress 와 CPU 경합 커지면 iso:  ./xtune.sh apply {app} iso {iso_nodes}')
    else:
        why='CPU 요구가 큼' if (dem_m>1400 or (cpu_bound and dem_m>500)) else '스케일이 큼'
        print(f'  >> 추천: ISO 전용 {iso_nodes}대 -> baseline {2+iso_nodes}. X 가 무거움({why}) → 전용 노드.')
        print(f'     ./xtune.sh apply {app} iso {iso_nodes}      # util{util} max{maxrep}')
        print(f'     ※ 억지 pack 은 user/product 지연 or stress<30%(비용게이트0) 위험. 무거운 X 는 iso.')
    print()
    print(f'  참고: 원본 baseline = MNG1(user+product)+stress1 = 2. iso 는 여기에 X 전용노드 +{iso_nodes}.')
PY
}

# ─────────────────────────────────────────────────────────────────────────────
# apply — 새 앱에만 적용. pack(baseline2, stress 동거) | iso(baseline3+, 전용풀)
#   ★원본 3앱/노드풀은 안 건드린다. 전부 이 앱 전용 자원만.
# ─────────────────────────────────────────────────────────────────────────────
apply_x() {
  require_app; guard_originals; need kubectl
  local MODE=${3:-iso} NODES
  [ "$MODE" = pack ] && MODE=pack-stress   # 하위호환(옛 pack = stress 동거)
  case "$MODE" in
    pack-app|pack-stress|iso) NODES=${4:-2} ;;
    [0-9]*)   NODES=$MODE; MODE=iso ;;      # 하위호환: 3번째가 숫자면 iso 노드수
    *) echo "모드는 pack-app|pack-stress|iso 여야 한다: '$MODE'" >&2; exit 1 ;;
  esac
  kubectl -n "$NS" get deploy "$DEPLOY" >/dev/null 2>&1 || { echo "!! deploy '$DEPLOY' 없음(NS=$NS)." >&2; exit 1; }

  # 곡선에서 REQ/UTIL/MAXREP 자동 채움(수동 지정 없을 때) — 이스케이프 없는 heredoc
  local AREQ AUTIL AMAX
  if [ -f "$CURVE" ]; then
    eval "$(CURVE="$CURVE" TARGET_RPS="${TARGET_RPS:-}" MODE="$MODE" python3 <<'PY'
import json,math,os
d=json.load(open(os.environ['CURVE'])); R=d['rps_at_sla_per_pod']; cpums=d['cpu_ms_per_req']; vcpu=d['vcpu_per_pod']; node_m=vcpu*1000
p50=next((p['p50_ms'] for p in d['points'] if p['concurrency']==1),0)
cb=cpums>0 and p50>0 and cpums>=0.5*p50; util=50 if cb else 80
mode=os.environ.get('MODE','iso'); tr=os.environ.get('TARGET_RPS','')
mx=max(2,math.ceil(float(tr)/max(R,0.01))) if tr else (2 if mode.startswith('pack') else 6)
# pack: 동거용 소극적 request. iso: eager(cpu는 노드 절반 선점).
req=max(50,min(250,int(round(cpums)))) if mode.startswith('pack') else (max(100,node_m//2) if cb else max(50,int(round(cpums))))
print(f'AREQ={req}m; AUTIL={util}; AMAX={mx}')
PY
)"
  fi
  local REQ=${REQ:-${AREQ:-150m}} UTIL=${UTIL:-${AUTIL:-60}} MAXREP=${MAXREP:-${AMAX:-6}}
  local CN; CN=$(kubectl -n "$NS" get deploy "$DEPLOY" -o jsonpath='{.spec.template.spec.containers[0].name}')

  if [ "$MODE" = pack-app ]; then
    # baseline 2: X 를 MNG 노드(user/product 동거)에 고정. MNG 은 관리형 고정노드라 원본이 회수 안 함.
    local MNGSEL; MNGSEL=$(discover_mng_label) || exit 1
    [ "$MAXREP" -gt 2 ] && echo "   (주의) pack-app 인데 MAXREP=$MAXREP → MNG 용량 초과 시 Pending(MNG 고정). 가벼운 X 만."
    echo "== [$APP] pack-app (baseline 2): MNG 노드 co-locate [$MNGSEL] + HPA(min2/max$MAXREP,util$UTIL) + req $REQ"
    kubectl -n "$NS" patch deploy "$DEPLOY" --type=json \
      -p="[{\"op\":\"add\",\"path\":\"/spec/template/spec/nodeSelector\",\"value\":{\"${MNGSEL%=*}\":\"${MNGSEL#*=}\"}}]" >/dev/null
    kubectl -n "$NS" patch deploy "$DEPLOY" --type=json -p='[{"op":"remove","path":"/spec/template/spec/tolerations"}]' >/dev/null 2>&1 || true
    echo "pack-app $DEPLOY - -" > "$STATE"
  elif [ "$MODE" = pack-stress ]; then
    # baseline 2: X 를 stress 노드에 얹는다(항상 존재 → 새 노드 0). 가벼운 X 전용.
    [ "$MAXREP" -gt 3 ] && echo "   (주의) pack-stress 인데 MAXREP=$MAXREP 크다 → stress 노드에 안 들어갈 수 있다. iso 권장."
    echo "== [$APP] pack-stress (baseline 2): stress 노드 co-locate + HPA(min2/max$MAXREP,util$UTIL) + req $REQ"
    kubectl -n "$NS" patch deploy "$DEPLOY" --type=json -p='[
      {"op":"add","path":"/spec/template/spec/nodeSelector","value":{"role":"stress"}},
      {"op":"add","path":"/spec/template/spec/tolerations","value":[{"key":"workload","value":"stress","effect":"NoSchedule","operator":"Equal"}]}]' >/dev/null
    echo "pack-stress $DEPLOY - -" > "$STATE"
  else
    # baseline 3+: X 전용 노드풀(원본이 안 건드림). stress 풀을 클론해 이름/label/taint/limits 만 변경.
    local PREFIX POOL; PREFIX=$(discover_prefix) || exit 1; POOL="$PREFIX-xtune-pool"
    echo "== [$APP] iso (baseline +$NODES): 전용풀 $POOL + nodeSelector role=xtune + HPA(min2/max$MAXREP,util$UTIL) + req $REQ"
    kubectl get nodepool "$PREFIX-stress-pool" -o json 2>/dev/null \
      | POOL="$POOL" NODES="$NODES" VCPU="$VCPU" python3 -c "
import json,sys,os
d=json.load(sys.stdin)
d['metadata']={'name':os.environ['POOL']}
t=d['spec']['template']
t.setdefault('metadata',{}).setdefault('labels',{})['role']='xtune'
t['spec']['taints']=[{'key':'workload','value':'xtune','effect':'NoSchedule'}]
d['spec']['limits']={'cpu':str(int(os.environ['NODES'])*int(os.environ['VCPU'])),'memory':str(int(os.environ['NODES'])*8)+'Gi'}
d.pop('status',None)
print(json.dumps(d))" \
      | kubectl apply -f - >/dev/null || { echo "!! 전용풀 생성 실패($PREFIX-stress-pool 존재 확인)"; exit 1; }
    kubectl -n "$NS" patch deploy "$DEPLOY" --type=json -p='[
      {"op":"add","path":"/spec/template/spec/nodeSelector","value":{"role":"xtune"}},
      {"op":"add","path":"/spec/template/spec/tolerations","value":[{"key":"workload","value":"xtune","effect":"NoSchedule","operator":"Equal"}]}]' >/dev/null
    echo "iso $DEPLOY $POOL $NODES" > "$STATE"
  fi

  # HPA min/max/util + requests (공통)
  if kubectl -n "$NS" get hpa "$HPA" >/dev/null 2>&1; then
    kubectl -n "$NS" patch hpa "$HPA" -p "{\"spec\":{\"minReplicas\":2,\"maxReplicas\":$MAXREP}}" >/dev/null
    kubectl -n "$NS" patch hpa "$HPA" --type=json \
      -p="[{\"op\":\"replace\",\"path\":\"/spec/metrics/0/resource/target/averageUtilization\",\"value\":$UTIL}]" >/dev/null 2>&1 || true
  else
    echo "   (주의) HPA '$HPA' 없음 — 수동: kubectl -n $NS autoscale deploy $DEPLOY --min=2 --max=$MAXREP --cpu-percent=$UTIL"
  fi
  kubectl -n "$NS" patch deploy "$DEPLOY" -p "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"$CN\",\"resources\":{\"requests\":{\"cpu\":\"$REQ\"}}}]}}}}" >/dev/null

  kubectl -n "$NS" rollout status deploy/"$DEPLOY" --timeout=300s | tail -1
  echo "✅ 적용 완료(mode=$MODE). 롤백: ./xtune.sh remove $APP"
}

# ─────────────────────────────────────────────────────────────────────────────
# remove — apply 를 되돌린다 (STATE 의 모드에 따라 pack/iso 처리)
# ─────────────────────────────────────────────────────────────────────────────
remove_x() {
  require_app; guard_originals; need kubectl
  local MODE="" POOL=""
  if [ -f "$STATE" ]; then MODE=$(awk '{print $1}' "$STATE"); POOL=$(awk '{print $3}' "$STATE"); fi
  MODE=${MODE:-iso}
  echo "== [$APP] 롤백 (mode=$MODE)"
  kubectl -n "$NS" patch deploy "$DEPLOY" --type=json -p='[
    {"op":"remove","path":"/spec/template/spec/nodeSelector"},
    {"op":"remove","path":"/spec/template/spec/tolerations"}]' >/dev/null 2>&1 || true
  if [ "$MODE" = iso ]; then
    { [ -n "$POOL" ] && [ "$POOL" != "-" ]; } || { local PREFIX; PREFIX=$(discover_prefix) && POOL="$PREFIX-xtune-pool"; }
    kubectl delete nodepool "$POOL" --ignore-not-found >/dev/null 2>&1
    echo "   전용풀 $POOL 삭제."
  else
    echo "   $MODE 모드였음 — 동거 노드 co-locate 해제(전용풀 없음)."
  fi
  rm -f "$STATE"
  kubectl -n "$NS" rollout status deploy/"$DEPLOY" --timeout=300s 2>/dev/null | tail -1
  echo "✅ 롤백 완료. HPA/requests 는 필요시 수동 원복."
}

# ─────────────────────────────────────────────────────────────────────────────
# scaffold — 새 앱의 인프라 파일을 templates/ 에서 치환해 generated/ 로 뽑는다.
#   ★원본(deploy.yaml·alb.tf·waf.tf·setup.sh 등)은 안 건드린다 — 생성물 + 체크리스트만 준다.
#   terraform 은 terraform/ 안의 .tf 를 자동 로드하므로 xapp-<app>.tf 를 떨구면 기존 무수정으로 추가된다.
#   옵션(env): APATH PORT HEALTH PRIORITY IMAGE REQ UTIL MAXREP
#     WAF 스니펫(spec.json apps 형식) 채우기: GETQ="email requestid uuid"  POSTBODY="name price"  PUT=1  (모르면 예시)
# ─────────────────────────────────────────────────────────────────────────────
scaffold() {
  require_app; guard_originals
  local TDIR GDIR; TDIR="$(cd "$(dirname "$0")" && pwd)/templates"; GDIR="$(cd "$(dirname "$0")" && pwd)/generated"
  [ -d "$TDIR" ] || { echo "!! templates/ 없음: $TDIR" >&2; exit 1; }
  mkdir -p "$GDIR"
  local PATHGLOB="${APATH}*" PORT=${PORT:-8080} HEALTH=${HEALTH:-/healthcheck}
  local PRIORITY=${PRIORITY:-40} REQ=${REQ:-100m} UTIL=${UTIL:-60} MAXREP=${MAXREP:-6}

  # IMAGE: env 우선 → AWS 로 조립 시도 → placeholder
  local IMAGE=${IMAGE:-}
  if [ -z "$IMAGE" ]; then
    local ACC PFX; ACC=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
    PFX=$(discover_prefix 2>/dev/null || echo "")
    if [ -n "$ACC" ] && [ -n "$PFX" ]; then IMAGE="$ACC.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com/$PFX-$APP:latest"
    else IMAGE="ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/PROJECT-$APP:latest"; fi
  fi
  # TG_ARN: terraform apply 후에만 존재
  local PFX2 TGARN; PFX2=$(discover_prefix 2>/dev/null || echo "")
  TGARN=$( [ -n "$PFX2" ] && aws elbv2 describe-target-groups --names "$PFX2-$APP" \
             --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null )
  { [ -n "$TGARN" ] && [ "$TGARN" != None ]; } || TGARN="__FILL_AFTER_terraform_apply__"

  sub() {  # $1=template $2=out
    sed -e "s|__APP__|$APP|g" -e "s|__NS__|$NS|g" -e "s|__PORT__|$PORT|g" \
        -e "s|__HEALTH__|$HEALTH|g" -e "s|__PATHGLOB__|$PATHGLOB|g" -e "s|__PATH__|$APATH|g" \
        -e "s|__PRIORITY__|$PRIORITY|g" -e "s|__IMAGE__|$IMAGE|g" -e "s|__REQ__|$REQ|g" \
        -e "s|__UTIL__|$UTIL|g" -e "s|__MAXREP__|$MAXREP|g" -e "s|__TG_ARN__|$TGARN|g" \
        "$1" > "$2"
  }
  sub "$TDIR/deploy.yaml.tmpl"  "$GDIR/$APP-deploy.yaml"
  sub "$TDIR/service.yaml.tmpl" "$GDIR/$APP-service.yaml"
  sub "$TDIR/hpa.yaml.tmpl"     "$GDIR/$APP-hpa.yaml"
  sub "$TDIR/tgb.yaml.tmpl"     "$GDIR/$APP-tgb.yaml"
  sub "$TDIR/xapp.tf.tmpl"      "$GDIR/xapp-$APP.tf"

  # WAF 엔드포인트 스니펫 — tools/spec.json 의 "apps" 에 붙여넣을 JSON(경로/메서드/필드만).
  #   WAF 가 뭘 검사할지는 spec.py 관례가 파생(정규식/마커는 JSON 에 안 둠).
  #   ★WAF 는 화이트리스트+default block 이라 새 경로를 spec 에 안 넣으면 정상 트래픽이 404/403 로 막힌다.
  #   채우기: GETQ="email requestid uuid"  POSTBODY="name price"  PUT=1 [PUTQ="requestid uuid"]  (모르면 비워서 예시)
  local mj="" x arr
  _jarr() { arr=""; for x in $1; do arr="$arr${arr:+, }\"$x\""; done; printf '[%s]' "$arr"; }
  [ -n "${GETQ:-}" ]     && mj="$mj${mj:+,}
        \"GET\": { \"query\": $(_jarr "$GETQ") }"
  [ -n "${POSTBODY:-}" ] && mj="$mj${mj:+,}
        \"POST\": { \"body\": $(_jarr "$POSTBODY") }"
  [ -n "${PUT:-}" ]      && mj="$mj${mj:+,}
        \"PUT\": { \"query\": $(_jarr "${PUTQ:-requestid uuid}") }"
  [ -z "$mj" ] && mj="
        \"POST\": { \"body\": [\"field1\", \"field2\"] }"
  {
    echo "    \"$APP\": {"
    echo "      \"path\": \"$APATH\","
    echo "      \"methods\": {$mj"
    echo "      }"
    echo "    }"
  } > "$GDIR/$APP-spec-apps-entry.json"

  echo "== [$APP] scaffold 생성 완료 → generated/  (path=$APATH, port=$PORT, priority=$PRIORITY)"
  ls "$GDIR" | grep -E "(^$APP-|^xapp-$APP)" | sed 's/^/   /'
  echo
  echo "== 통합 체크리스트 (원본 무수정 — 아래는 사람이 확인·반영) =="
  cat <<EOF
  1) 앱 바이너리 :  terraform/application/$APP/$APP  에 실행파일 배치
  2) terraform   :  generated/xapp-$APP.tf  ->  terraform/ 로 복사
                    (ECR repo + ALB target group + listener rule[priority $PRIORITY, $PATHGLOB])
  3) WAF (필수!) :  generated/$APP-spec-apps-entry.json 을 tools/spec.json 의 "apps" 에 붙여넣고 →
                    cd tools && python apply_spec.py --apply
                    → ① waf.tf 경로 locals 자동(404 방지)  ② generated_waf_rules.tf.txt 의 AllowValid HCL
                       을 waf.tf 에 반영(안 하면 정상 트래픽 default 403) → terraform apply
                    ★ WAF 는 화이트리스트+default block. 이거 안 하면 $APATH 정상 트래픽이 다 막힌다.
  4) setup.sh   :  - ECR 빌드 루프에 '$APP' 추가:  for APP in user product stress $APP
                    - DB 필요하면 SQL heredoc 에 CREATE TABLE $APP (...) 추가
                    - (재현성) k8s 적용 섹션에 아래 7)·8) apply 라인 추가
  5) 배포        :  cd terraform && terraform apply    # ECR/TG/listener 생성 + 이미지 빌드·푸시
  6) k8s 적용    :  kubectl apply -f generated/$APP-deploy.yaml -f generated/$APP-service.yaml -f generated/$APP-hpa.yaml
  7) TGB        :  terraform output tg_${APP}_arn 으로 ARN 확인 →
                    generated/$APP-tgb.yaml 의 targetGroupARN 채우고  kubectl apply -f generated/$APP-tgb.yaml
  8) 튜닝(배치)  :  ./xtune.sh measure $APP  ->  recommend $APP  ->  apply $APP <pack-app|pack-stress|iso>
EOF
  case "$IMAGE" in ACCOUNT_ID*) echo "  ※ IMAGE placeholder 사용됨 — deploy 적용 전 실제 ECR URL 로 교체(또는 IMAGE=... 로 재실행).";; esac
  [ "$TGARN" = "__FILL_AFTER_terraform_apply__" ] && echo "  ※ TG ARN 은 terraform apply(5) 후 7) 로 채운다(아직 target group 없음)."
}

case "$CMD" in
  measure)   measure ;;
  recommend) recommend ;;
  apply)     apply_x "$@" ;;
  remove)    remove_x ;;
  scaffold)  scaffold ;;
  help|*)
    sed -n '2,/^set -uo pipefail/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
    ;;
esac
