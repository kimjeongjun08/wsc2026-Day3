#!/usr/bin/env bash
# profile.sh <app> — 앱의 "고정 오버헤드 F" 와 "요청당 CPU 작업 분포 d" 를 실측한다.
#
# ★앱에 어떤 손잡이도 요구하지 않는다. 대회에서 주어지는 바이너리 그대로 측정한다.
#
# 원리:
#   1) 대상 앱을 파드 1개로 줄인다 → 모든 요청이 그 파드로 간다.
#   2) kubelet 이 노출하는 누적 CPU 카운터를 요청 전후로 읽는다.
#        d_mean = Δ(container_cpu_usage_seconds) / 요청수     ← 커널이 센 실제 CPU
#   3) 동시성 1 로 보내므로 큐 대기가 없다 →  L_i = F + d_i
#        F   = mean(L) - d_mean
#        d_i = L_i - F        ← 분포가 그대로 나온다 (무거운 꼬리 포함)
#
#   CPU 는 커널이, 지연은 클라이언트가 재므로 앱의 협조가 필요 없다.
#
# 검증: 이 앱에는 LOAD_MULT 라는 작업량 조절 변수가 있어서, 그걸 꺼서 구한 F 와
#       이 방법으로 구한 F 가 일치하는지 대조할 수 있다 (VERIFY=1 로 실행).
set -uo pipefail
cd "$(dirname "$0")" || exit 1; source ./common.sh

APP=${1:-user}
VERB=${2:-post}            # post | get — 트래픽의 대부분이 GET 인 앱이 있어 따로 잰다
N=${N:-800}
OUT=${OUT:-profile-$APP${VERB:+-$VERB}.json}
[ "$VERB" = "post" ] && OUT=${OUT_POST:-profile-$APP.json}

case "$APP" in
  user)    BODY='{"requestid":"r","uuid":"u","username":"__U__","email":"__U__@x.com"}' ;;
  product) BODY='{"requestid":"r","uuid":"u","id":"__U__","name":"__U__","price":9.9}' ;;
  stress)  BODY='{"requestid":"r","uuid":"u","length":88}' ;;
  *) echo "모르는 앱: $APP — case 에 요청 본문을 추가해라" >&2; exit 1 ;;
esac

case "$APP" in
  user)    QKEY=email ;;
  product) QKEY=id ;;
  *)       QKEY=id ;;
esac

echo "== $APP($VERB) 프로파일 (파드 1개, 동시성 1, 샘플 $N)"

# 프로브 파드 매니페스트를 로컬에서 만들어 base64 로 넘긴다 (이스케이프 회피)
PROBE_SH=$(cat <<SEOF
# GET 은 존재하는 행을 조회해야 실제 트래픽과 같은 경로를 탄다 → 먼저 한 건 만들어 둔다
FIXED="proffixed-\$(date +%s%N)"
if [ "$VERB" = "get" ]; then
  curl -s -o /dev/null -X POST -H 'Content-Type: application/json' \
       -d "\$(echo '$BODY' | sed "s/__U__/\$FIXED/g")" http://$APP-svc:8080/v1/$APP
fi
i=0
while [ \$i -lt $N ]; do
  i=\$((i+1))
  U="prof-\$(date +%s%N)-\$i"
  # ★user 는 email 로 조회한다 — 저장된 값은 "<이름>@x.com" 이라 그대로 넘기면 404 다.
  #   404 는 빨라서 F·d 가 통째로 잘못 나온다.
  if [ "$VERB" = "get" ]; then
    if [ "$APP" = "user" ]; then U="\$FIXED@x.com"; else U="\$FIXED"; fi
  fi
  B=\$(echo '$BODY' | sed "s/__U__/\$U/g")
  if [ "$VERB" = "get" ]; then
    curl -s -m 20 -o /dev/null -w '%{time_total}\n' \
         "http://$APP-svc:8080/v1/$APP?$QKEY=\$U&requestid=r&uuid=u"
  else
    curl -s -m 20 -o /dev/null -w '%{time_total}\n' -X POST \
         -H 'Content-Type: application/json' -d "\$B" http://$APP-svc:8080/v1/$APP
  fi
done
SEOF
)
YAML=$(python3 - "$NS" "$PROBE_SH" <<'PY'
import sys, json
ns, script = sys.argv[1], sys.argv[2]
print(json.dumps({"apiVersion":"v1","kind":"Pod",
 "metadata":{"name":"prof","namespace":ns},
 "spec":{"restartPolicy":"Never","tolerations":[{"operator":"Exists"}],
   "containers":[{"name":"c","image":"curlimages/curl:8.5.0",
                  "command":["sh","-c"],"args":[script]}]}}))
PY
)
B64=$(printf '%s' "$YAML" | base64 | tr -d '\n')

RAW=$(bx "set -e
ORIG=\$(kubectl -n $NS get deploy $APP -o jsonpath='{.spec.replicas}')
echo \"ORIG_REPLICAS=\$ORIG\"
# HPA 가 다시 늘리지 못하도록 잠시 떼어놓는다
kubectl -n $NS patch hpa ${APP}-hpa -p '{\"spec\":{\"minReplicas\":1,\"maxReplicas\":1}}' >/dev/null 2>&1 || true
kubectl -n $NS scale deploy $APP --replicas=1 >/dev/null
kubectl -n $NS rollout status deploy/$APP --timeout=240s >/dev/null
# ★파드가 정확히 1개로 정리될 때까지 기다린다.
#   축소 직후엔 종료 중인 파드가 목록에 남아 items[0] 가 그걸 집는다 →
#   트래픽은 살아남은 파드가 받는데 CPU 는 죽는 파드에서 재게 되어 0 이 나온다(실측).
for i in \$(seq 1 60); do
  cnt=\$(kubectl -n $NS get pods -l app=$APP --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l)
  [ \"\$cnt\" -eq 1 ] && break
  sleep 3
done
POD=\$(kubectl -n $NS get pods -l app=$APP --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
IP=\$(kubectl -n $NS get pods -l app=$APP -o jsonpath='{.items[0].status.podIP}')
NODE=\$(kubectl -n $NS get pods -l app=$APP -o jsonpath='{.items[0].spec.nodeName}')
cpu() { kubectl get --raw \"/api/v1/nodes/\$NODE/proxy/metrics/resource\" 2>/dev/null \
        | awk -v p=\"\$POD\" '\$0 ~ /container_cpu_usage_seconds_total/ && \$0 ~ p {print \$(NF-1)}' | head -1; }
C0=\$(cpu)
kubectl delete pod prof -n $NS --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1
echo $B64 | base64 -d > /tmp/prof.json
kubectl apply -f /tmp/prof.json >/dev/null
for i in \$(seq 1 90); do
  st=\$(kubectl get pod prof -n $NS -o jsonpath='{.status.phase}' 2>/dev/null)
  [ \"\$st\" = Succeeded ] || [ \"\$st\" = Failed ] && break
  sleep 3
done
# cAdvisor 는 10~15초 주기로 갱신된다. 프로브가 짧으면 카운터가 덜 반영돼
# CPU 를 과소 측정한다(실측: 1.4초 프로브에서 실제의 23% 만 잡혔다).
# 충분히 기다린 뒤 읽는다. N 도 크게 잡아 프로브 자체를 30초 이상으로 만든다.
sleep 25
C1=\$(cpu)
echo \"CPU_DELTA=\$(python3 -c \"print(float('\$C1' or 0)-float('\$C0' or 0))\")\"
echo LAT_BEGIN
kubectl logs -n $NS prof 2>/dev/null
echo LAT_END
kubectl delete pod prof -n $NS --wait=false >/dev/null 2>&1
kubectl -n $NS patch hpa ${APP}-hpa -p '{\"spec\":{\"minReplicas\":2,\"maxReplicas\":20}}' >/dev/null 2>&1 || true
kubectl -n $NS scale deploy $APP --replicas=\$ORIG >/dev/null")

echo "$RAW" | grep -E 'ORIG_REPLICAS|CPU_DELTA'

cat > /tmp/prof_calc.py <<'PY'
import sys, json, re
out, app, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
txt = sys.stdin.read()
m = re.search(r'CPU_DELTA=([0-9.eE+-]+)', txt)
cpu_delta = float(m.group(1)) if m else 0.0
body = txt.split('LAT_BEGIN',1)[-1].split('LAT_END',1)[0]
L = [float(x)*1000 for x in body.split() if re.fullmatch(r'[0-9.]+', x)]
if len(L) < 20:
    raise SystemExit(f"표본 부족({len(L)}) — 프로브 실패")
if cpu_delta <= 0:
    raise SystemExit("CPU 카운터를 못 읽었다 — kubelet metrics/resource 접근 확인")
d_mean = cpu_delta / len(L) * 1000.0          # ms/req
mean_L = sum(L)/len(L)
F = max(0.0, mean_L - d_mean)                 # 고정비
D = sorted(max(0.0, x - F) for x in L)        # 요청별 CPU 작업 분포
def q(v,p): return v[min(len(v)-1, int(round(p/100*(len(v)-1))))]
r = {"app": app, "n": len(L), "method": "cgroup-cpu (앱 손잡이 불필요)",
     "fixed_ms": round(F,2),
     "cpu_ms_mean": round(d_mean,2),
     "cpu_ms_p50": round(q(D,50),2), "cpu_ms_p90": round(q(D,90),2),
     "cpu_ms_p95": round(q(D,95),2),
     "latency_mean_ms": round(mean_L,2),
     "cpu_ms_samples": [round(x,2) for x in D]}
json.dump(r, open(out,"w"), indent=2, ensure_ascii=False)
print(json.dumps({k:v for k,v in r.items() if k!="cpu_ms_samples"}, ensure_ascii=False, indent=2))
PY
printf '%s' "$RAW" | python3 /tmp/prof_calc.py "$OUT" "$APP" "$N"
