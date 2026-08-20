#!/usr/bin/env bash
# scenario.sh — 채점 주입기의 트래픽 모양을 시나리오별로 바꾼다.
#
# 튜너가 "노드 수를 외운 것"이 아니라 "계산하는 것"임을 증명하려면,
# 트래픽 축을 따로따로 흔들어보고 그때마다 답이 바뀌는지 봐야 한다.
#
#   scenario.sh rates '{"user_post":45,"user_get":45,"product_get":90,...}'   # peak1 값 지정
#   scenario.sh stress-length 400        # stress 요청당 작업량 (앱 안에서 length^2 로 증가)
#   scenario.sh show                     # 현재 설정 확인
#   scenario.sh reset                    # 기본값 복구
set -uo pipefail
cd "$(dirname "$0")"; source ./grader.sh

CMD=${1:?사용법: scenario.sh rates|stress-length|show|reset [값]}

case "$CMD" in
rates)
  SPEC=${2:?peak1 RPS 를 JSON 으로 넘겨라}
  python3 - "$SPEC" > /tmp/rates.json <<'PY'
import json, sys
want = json.loads(sys.argv[1])
# 채점 엔진의 rates 는 {kind: {base, peak1, peak2}} 구조다.
# peak1 만 지정하면 base 는 그 1/10, peak2 는 3배로 자동 배치한다(원본 비율과 유사).
out = {}
for kind, p1 in want.items():
    out[kind] = {"base": round(p1/10.0, 2), "peak1": float(p1), "peak2": float(p1)*3}
print(json.dumps(out))
PY
  RATES=$(cat /tmp/rates.json)
  meta_set injection_rates "$RATES" && echo "rates 설정: $RATES"
  ;;
stress-length)
  L=${2:?길이를 넘겨라 (예: 400)}
  meta_set injection_stress_length "{\"min\": $((L/4)), \"max\": $L}" \
    && echo "stress length → min=$((L/4)) max=$L  (앱 작업량은 length^2 로 증가)"
  ;;
show)
  gx "$GPY - <<'PY'
import sqlite3, json
c = sqlite3.connect('$GDATA/app.sqlite')
for k in ('injection_rates','injection_stress_length','injection_rate_multiplier','injection_schedule'):
    r = c.execute('select value from meta where key=?', (k + ':$GUSER',)).fetchone()
    print(k, '=', (r[0][:300] if r else '(기본값)'))
PY"
  ;;
reset)
  gx "$GPY - <<'PY'
import sqlite3
c = sqlite3.connect('$GDATA/app.sqlite')
for k in ('injection_rates','injection_stress_length'):
    c.execute('delete from meta where key=?', (k + ':$GUSER',))
c.commit(); print('기본값으로 복구')
PY"
  ;;
*) echo "모르는 명령: $CMD" >&2; exit 1 ;;
esac
