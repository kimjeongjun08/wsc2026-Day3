#!/usr/bin/env bash
# probe.sh — 파드 응답 지연을 '지금' 잰다. CloudWatch 를 기다리지 않는다.
#
# 왜 필요한가:
#   실측: 11:47:53 시점에 ALB 지표의 최신 데이터포인트가 11:46:00 짜리였다.
#   버킷이 닫히고도 1분 가까이 지나야 보인다 — 실질 지연 1~3분이다.
#   트래픽이 계단으로 뛰는 회차에서 3분은 피크 구간의 20~30% 다. 그만큼 늦게 대응하면
#   그 구간의 요청은 이미 SLA 를 넘긴 뒤다. 성능 점수는 '요청' 가중이라 되돌릴 수 없다.
#
# 어떻게:
#   채점기가 때리는 그 경로(CloudFront)로 GET 을 몇 개 쏴서 왕복 시간을 잰다.
#   ★예전엔 ALB 로 직접 쐈다. 캐시를 피하려고. 그런데 그러면 CloudFront 구간이
#     통째로 빠진다. 실측(2026-08-21): 같은 요청이 ALB 직행 p90 95ms,
#     CloudFront 경유 p90 195ms 였다 — SLA 200ms 기준으로 여유와 벼랑 끝의 차이다.
#     그래서 probe 는 "100% 통과"라고 보고했는데 실제 채점은 78% 였다.
#     도구가 거짓 안심을 하고 증설을 미뤘다.
#   · 캐시는 쿼리 파라미터를 매번 다르게 넣어 피한다. CloudFront 는 키가 다르면
#     캐시하지 않고 그대로 전달한다. 캐시도 피하고 경로도 온전하다.
#   · GET 만 쓴다. 과제지가 임의 데이터 삽입을 경계하므로 POST 는 안 넣는다.
#   · 앱당 기본 10개, 총 20개 남짓 — 피크 312rps 옆에서 무시할 수 있는 양이다.
#   · 채점은 주입기가 자기 요청만 세므로 이 요청은 점수에 안 들어간다.
#
#   stress 는 안 쏜다. 요청 하나가 코어를 통째로 먹어서(실측 0.6코어·초) 재는 행위가
#   곧 부하가 된다. stress 는 파드 CPU 로 본다 — 포화하면 CPU 가 먼저 붙는다.
#
# 출력: {"user":{"pass":93,"p50":0.05,"p90":0.14,"n":15}, "product":{...}, "stress":{"cpu_pct":83}}
#   pass = SLA 안에 들어온 비율. 채점되는 값과 '같은 종류'의 값이라 바로 비교된다.
#   표본 15개라 오차가 ±15%p 쯤 된다 — 그래서 쓰는 쪽에서 여유를 크게 둔다.
set -uo pipefail
cd "$(dirname "$0")" || exit 1; source ./common.sh
REGION=${AWS_DEFAULT_REGION:-ap-northeast-2}
ALB_NAME=${ALB_NAME:-apdev-alb}
N=${PROBE_N:-15}
TMO=${PROBE_TIMEOUT:-2}

# 채점 대상 엔드포인트(CloudFront). ENDPOINT 로 넘기면 그걸 쓴다.
TARGET=${ENDPOINT:-}
if [ -z "$TARGET" ]; then
  TARGET=$(aws cloudfront list-distributions \
    --query "DistributionList.Items[?contains(Origins.Items[0].DomainName, '${ORIGIN_HINT:-apdev}')].DomainName | [0]" \
    --output text 2>/dev/null)
fi
# CloudFront 를 못 찾으면 ALB 로라도 잰다(값은 낙관적이지만 없는 것보단 낫다)
if [ -z "$TARGET" ] || [ "$TARGET" = None ]; then
  TARGET=$(aws elbv2 describe-load-balancers --region "$REGION" --names "$ALB_NAME" \
           --query 'LoadBalancers[0].DNSName' --output text 2>/dev/null)
  echo "probe: CloudFront 를 못 찾아 ALB 로 잰다 — 지연이 낙관적으로 나온다" >&2
fi
[ -z "$TARGET" ] || [ "$TARGET" = None ] && { echo '{}'; exit 1; }
ALB=${TARGET#http://}; ALB=${ALB#https://}; ALB=${ALB%%/*}

hex() { od -An -tx1 -N16 /dev/urandom | tr -d ' \n'; }

# ★URL 을 eval 로 만들지 않는다.
#   `eval echo "/v1/user?a=1&b=2"` 는 & 를 백그라운드 연산자로 읽어서 URL 이 잘린다.
#   실측: 그 상태로 15개를 쏘면 전부 5초 타임아웃 → "전 앱 0% 통과" 라는 거짓 신호가
#   나오고, 한 주기에 2분을 잡아먹는다. 방아쇠가 거짓말하면 도구 전체가 무의미하다.
#
# curl 한 번에 URL 여러 개를 넘긴다 — 연결을 재사용해서 15개가 1초 안에 끝난다.
# ★-o /dev/null 은 URL 하나당 하나씩 필요하다.
#   URL 을 여러 개 넘기면서 -o 를 한 번만 쓰면 첫 응답만 버려지고 나머지 본문이
#   stdout 으로 쏟아진다(실측: 측정값 사이에 <html> 이 섞여 파싱이 죽었다).
build_args() {  # $1=앱  → "-o /dev/null <url>" 를 N 번
  local i
  for i in $(seq 1 "$N"); do
    printf -- '-o\n/dev/null\n'
    case "$1" in
      user)    printf 'http://%s/v1/user?email=probe%s@k6.local&requestid=%s&uuid=%s\n' \
                      "$ALB" "$(hex)" "$(hex)" "$(hex)" ;;
      product) printf 'http://%s/v1/product?id=p-%s&requestid=%s&uuid=%s\n' \
                      "$ALB" "$(hex)" "$(hex)" "$(hex)" ;;
    esac
  done
}

times_for() {
  # ★mapfile 을 쓰지 않는다. bash 4 부터 있는 빌트인인데 macOS 기본 bash 는 3.2 다.
  #   없으면 조용히 빈 배열이 되고, probe 가 "결과 없음"을 낸다 — 방아쇠가 죽는다.
  #   대회 PC(WSL)는 bash 5 라 거기선 돌지만, 도구가 환경 따라 조용히 달라지면 안 된다.
  local args=() line
  while IFS= read -r line; do args+=("$line"); done < <(build_args "$1")
  [ "${#args[@]}" = 0 ] && return 1
  curl -s -m "$TMO" -w '%{time_total}\n' "${args[@]}" 2>/dev/null
}

U=$(times_for user)
P=$(times_for product)

# stress 는 CPU 로 본다 (limits.cpu 대비 사용률)
SCPU=$(kubectl top pods -n "${NS:-apdev}" --no-headers 2>/dev/null \
       | awk '/^stress-/{gsub(/m$/,"",$2); s+=$2; n++} END{if(n) printf "%.0f", s/n; else print ""}')

U="$U" P="$P" SCPU="${SCPU:-}" python3 - <<'PY'
import json, os


SLA = {"user": 0.200, "product": 0.200}


def stat(raw, sla):
    v = sorted(float(x) for x in raw.split() if x)
    if not v:
        return None
    def q(p):
        return v[min(len(v) - 1, int(round(p / 100.0 * (len(v) - 1))))]
    return {"pass": round(100.0 * sum(1 for x in v if x <= sla) / len(v)),
            "p50": round(q(50), 3), "p90": round(q(90), 3),
            "max": round(v[-1], 3), "n": len(v)}


out = {}
for name, key in (("user", "U"), ("product", "P")):
    s = stat(os.environ.get(key, ""), SLA[name])
    if s:
        out[name] = s
c = os.environ.get("SCPU", "")
if c:
    # stress limits.cpu = 2000m 기준 사용률
    out["stress"] = {"cpu_m": int(c), "cpu_pct": round(int(c) / 2000.0 * 100)}
print(json.dumps(out, ensure_ascii=False))
PY
