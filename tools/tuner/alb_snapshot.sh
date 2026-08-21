#!/usr/bin/env bash
# alb_snapshot.sh — 채점되는 값을 ALB 에서 직접 읽는다. 한 번의 API 호출로 끝낸다.
#
# 왜 백분위까지 읽나:
#   채점되는 성능 지표는 '평균 지연'이 아니라 'SLA 안에 들어온 요청의 비율'이다.
#   실측 대조군에서 user 평균 지연은 SLA 를 넘었지만 통과율은 48.6% 였다.
#   평균만 보면 "넘었다/안 넘었다" 두 가지 상태밖에 못 본다 — tier 를 겨냥할 수 없다.
#   백분위를 읽으면 SLA 가 분포 어디에 놓였는지가 나오고, 그게 곧 채점값이다.
#
# 출력: {"user":{"rps":..,"req":..,"e5":..,"p":{"10":..,..}}, "product":{..}, "stress":{..}}
set -uo pipefail
cd "$(dirname "$0")" || exit 1
REGION=${AWS_DEFAULT_REGION:-ap-northeast-2}
ALB_NAME=${ALB_NAME:-apdev-alb}
WIN_MIN=${WIN_MIN:-3}          # 최근 몇 분을 볼지

lb=$(aws elbv2 describe-load-balancers --region "$REGION" --names "$ALB_NAME" \
     --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null) || exit 1
[ -z "$lb" ] || [ "$lb" = None ] && { echo "ALB 를 못 찾았다: $ALB_NAME" >&2; exit 1; }
lbdim=${lb##*loadbalancer/}

tgs=$(aws elbv2 describe-target-groups --region "$REGION" --load-balancer-arn "$lb" \
      --query 'TargetGroups[].[TargetGroupName,TargetGroupArn]' --output text 2>/dev/null)
[ -z "$tgs" ] && { echo "타깃그룹이 없다" >&2; exit 1; }

START=$(date -u -v-${WIN_MIN}M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "${WIN_MIN} minutes ago" +%Y-%m-%dT%H:%M:%SZ)
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
PERIOD=$((WIN_MIN*60))

# get-metric-data 로 앱 3개 × (요청수, 5xx, 백분위 8개) 를 한 번에 가져온다.
Q=$(LBDIM="$lbdim" PERIOD="$PERIOD" python3 - <<'PY'
import json, os, sys
lbdim, period = os.environ["LBDIM"], int(os.environ["PERIOD"])
qs, i = [], 0
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    name, arn = line.split()
    app = name.replace("apdev-", "")
    tg = arn.split(":", 5)[-1]
    dims = [{"Name": "LoadBalancer", "Value": lbdim}, {"Name": "TargetGroup", "Value": tg}]
    def add(metric, stat, tag):
        global i; i += 1
        qs.append({"Id": f"q{i}", "Label": f"{app}|{tag}",
                   "MetricStat": {"Metric": {"Namespace": "AWS/ApplicationELB",
                                             "MetricName": metric, "Dimensions": dims},
                                  "Period": period, "Stat": stat}})
    add("RequestCount", "Sum", "req")
    add("HTTPCode_Target_5XX_Count", "Sum", "e5")
    for p in (10, 30, 50, 70, 80, 90, 95, 99):
        add("TargetResponseTime", f"p{p}", f"p{p}")
print(json.dumps(qs))
PY
<<<"$tgs")

RAW=$(aws cloudwatch get-metric-data --region "$REGION" \
      --start-time "$START" --end-time "$END" \
      --metric-data-queries "$Q" --output json 2>/dev/null) || exit 1

PERIOD="$PERIOD" python3 - "$RAW" <<'PY'
import json, os, sys
period = int(os.environ["PERIOD"])
out = {}
for r in json.loads(sys.argv[1]).get("MetricDataResults", []):
    label = r.get("Label", "")
    if "|" not in label: continue
    app, tag = label.split("|", 1)
    vals = r.get("Values") or []
    v = vals[0] if vals else None       # 최신 데이터포인트
    d = out.setdefault(app, {"req": 0.0, "e5": 0.0, "p": {}})
    if tag == "req":  d["req"] = v or 0.0
    elif tag == "e5": d["e5"] = v or 0.0
    elif v is not None: d["p"][tag[1:]] = v
for d in out.values():
    d["rps"] = round(d["req"] / period, 2)
print(json.dumps(out, ensure_ascii=False))
PY
