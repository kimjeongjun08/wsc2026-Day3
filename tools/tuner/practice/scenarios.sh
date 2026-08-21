#!/usr/bin/env bash
# scenarios.sh <이름> — 검증용 트래픽 곡선을 골라 넣는다. (스케줄만 바꾸고 회차는 안 돈다)
#
# 한 가지 곡선만 통과하는 도구는 도구가 아니다. 대회 곡선은 우리가 못 정한다.
# 아래는 서로 다른 방식으로 도구를 깨려고 만든 것들이다.
set -uo pipefail
cd "$(dirname "$0")" || exit 1; source ./grader.sh

case "${1:-}" in
ladder)
  # 증설 → 축소 → 재증설을 한 회차에서 전부 본다. 25분.
  SCH='[{"name":"1_baseline","endMin":4,"level":"base","scale":1.0,"noise":true},
       {"name":"4_spike2","endMin":14,"level":"peak2","scale":1.0},
       {"name":"6_down2","endMin":19,"level":"base","scale":2.0},
       {"name":"7_spike3","endMin":25,"level":"peak1","scale":0.75}]' ;;
practice)
  # 후배 연습용. 공식 모양의 축소판. x0.5 로 돌린다. 15분.
  #   여기서 40점이 나와야 한다 — 2대로 완주 가능한 부하다.
  SCH='[{"name":"1_baseline","endMin":5,"level":"base","scale":1.0,"noise":true},
       {"name":"2_spike1","endMin":15,"level":"peak1","scale":1.0}]' ;;
ambush)
  # ★기습: 예열 없이 곧바로 최고 강도. 반응 속도만 본다. 12분.
  #   CloudWatch(1~3분 지연)로 방아쇠를 당기면 여기서 무조건 진다.
  SCH='[{"name":"1_baseline","endMin":2,"level":"base","scale":1.0,"noise":true},
       {"name":"4_spike2","endMin":12,"level":"peak2","scale":1.0}]' ;;
drift)
  # ★비용 시험: 거의 내내 한가하다. 노드를 2대로 붙들고 있어야 한다. 20분.
  #   비용은 '분' 평균이라 이런 구간에서 새는 게 제일 크다.
  SCH='[{"name":"1_baseline","endMin":6,"level":"base","scale":1.0,"noise":true},
       {"name":"2_spike1","endMin":9,"level":"peak1","scale":1.0},
       {"name":"3_valley","endMin":20,"level":"base","scale":1.1,"noise":true}]' ;;
trap)
  # ★함정: 깊은 계곡 뒤의 막판 훅. 축소했다가 다시 못 올리면 여기서 무너진다. 22분.
  SCH='[{"name":"1_baseline","endMin":3,"level":"base","scale":1.0,"noise":true},
       {"name":"4_spike2","endMin":11,"level":"peak2","scale":1.0},
       {"name":"6_down2","endMin":17,"level":"base","scale":1.0},
       {"name":"7_spike3","endMin":22,"level":"peak2","scale":0.8}]' ;;
show)
  gx "$GPY -c \"import sqlite3;print(sqlite3.connect('$GDATA/app.sqlite').execute('select value from meta where key=?',('injection_schedule:$GUSER',)).fetchone()[0])\""
  exit 0 ;;
*)
  echo "사용: scenarios.sh ladder|practice|ambush|drift|trap|show" >&2
  echo "  ladder    증설→축소→재증설 (25분, x1.0)"
  echo "  practice  후배 연습용 공식 축소판 (15분, x0.5 로 돌릴 것) — 40점 목표"
  echo "  ambush    예열 없이 곧바로 최고 강도 (12분) — 반응 속도"
  echo "  drift     거의 내내 한가함 (20분) — 비용이 새는지"
  echo "  trap      깊은 계곡 뒤 막판 훅 (22분) — 축소 후 복귀"
  exit 1 ;;
esac

SCH=$(python3 -c "import json,sys;print(json.dumps(json.loads(sys.argv[1])))" "$SCH")
meta_set injection_schedule "$SCH" && echo "곡선 설정: $1"
python3 - "$SCH" <<'PY'
import json, sys
print("  구간          시간(분)")
prev = 0
for ph in json.loads(sys.argv[1]):
    print("  %-12s %d-%d  %s x%s" % (ph["name"], prev, ph["endMin"], ph["level"], ph["scale"]))
    prev = ph["endMin"]
PY
