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
SEED=${SEED_FILE:-.probe-seed}

# ★공식 트래픽과 같은 구성으로 쏜다.
#   실측(2026-08-21 practice 회차): probe 는 7분 내내 "user 100% 통과"라고 했는데
#   실제 채점은 70% 였다. 통과율 70% 인 모집단에서 15개를 뽑아 전부 통과할 확률은
#   0.5% 다 — probe 가 다른 것을 재고 있었다는 뜻이다.
#   probe 는 '없는 이메일 조회'(404, 제일 싼 경로)만 쐈고, 실제 user 트래픽은
#   절반이 POST(INSERT)다. 방아쇠가 제일 싼 요청만 재면 영원히 안 울린다.
#   공식 비율은 user_post : user_get = 1 : 1, product 는 거의 GET 이다.
#
#   POST 는 행을 만든다. 주기당 4건, 15분 회차면 60건이다 —
#   채점 주입기가 같은 시간에 만드는 수만 건에 비하면 무시할 수 있다.
build_args() {  # $1=앱  → "-o /dev/null <url>" 목록 (POST 는 별도 처리)
  local i n=$1
  for i in $(seq 1 "$N"); do
    printf -- '-o\n/dev/null\n'
    case "$n" in
      user)    printf 'http://%s/v1/user?email=%s&requestid=%s&uuid=%s\n' \
                      "$ALB" "$(seed_email)" "$(hex)" "$(hex)" ;;
      product) printf 'http://%s/v1/product?id=%s&requestid=%s&uuid=%s\n' \
                      "$ALB" "$(seed_pid)" "$(hex)" "$(hex)" ;;
    esac
  done
}

# 씨앗: 실제로 존재하는 이메일/상품을 조회해야 '있는 행을 찾는' 비용이 반영된다.
seed_email() {
  local e
  e=$(awk '/^e /{print $2}' "$SEED" 2>/dev/null | shuf -n1 2>/dev/null)
  [ -n "${e:-}" ] && { echo "$e"; return; }
  echo "probe$(hex)@k6.local"
}
seed_pid() {
  local p
  p=$(awk '/^p /{print $2}' "$SEED" 2>/dev/null | shuf -n1 2>/dev/null)
  [ -n "${p:-}" ] && { echo "$p"; return; }
  echo "p-$(hex 10)"
}

# 쓰기 경로도 잰다. 읽기만 재면 DB 쓰기 경합이 안 보인다.
post_times() {
  local i em pid t out=""
  for i in $(seq 1 "${POST_N:-4}"); do
    em="probe$(hex)@k6.local"
    t=$(curl -s -o /dev/null -m "$TMO" -w '%{time_total}' \
        -H 'Content-Type: application/json' \
        -d "{\"requestid\":\"$(hex)\",\"uuid\":\"$(hex)\",\"username\":\"u$(hex)\",\"email\":\"$em\"}" \
        "http://$ALB/v1/user" 2>/dev/null) || t=$TMO
    out="$out $t"
    echo "e $em" >> "$SEED"
  done
  # 씨앗 파일이 무한정 커지지 않게 최근 것만 남긴다
  tail -n 200 "$SEED" > "$SEED.tmp" 2>/dev/null && mv "$SEED.tmp" "$SEED"
  echo "$out"
}

times_for() {
  local args=() line
  while IFS= read -r line; do args+=("$line"); done < <(build_args "$1")
  [ "${#args[@]}" = 0 ] && return 1
  curl -s -m "$TMO" -w '%{time_total}\n' "${args[@]}" 2>/dev/null
}

U="$(times_for user) $(post_times)"
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
