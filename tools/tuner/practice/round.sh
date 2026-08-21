#!/usr/bin/env bash
# round.sh <분> [이름] — 지금 meta 에 들어 있는 스케줄 그대로 한 회차를 돌린다.
#
#   verify.sh 와 다른 점: 스케줄을 덮어쓰지 않는다.
#   증설·축소를 한 회차에서 같이 보려면 스케줄을 직접 짜야 하는데,
#   verify.sh 는 baseline+spike1 로 고정해버린다.
#
# ★회차 CSV 는 매번 같은 파일에 덮어쓴다. 끝나면 이름 붙여 보관한다 —
#   1·2회차를 이렇게 잃었다.
set -uo pipefail
cd "$(dirname "$0")" || exit 1; source ./grader.sh
MINS=${1:?사용법: round.sh <분> [이름]}
NAME=${2:-round}

echo "== 스케줄 확인"
gx "$GPY -c \"import sqlite3,json;print(sqlite3.connect('$GDATA/app.sqlite').execute('select value from meta where key=?',('injection_schedule:$GUSER',)).fetchone()[0])\""
echo "== 엔드포인트"
gx "$GPY -c \"import sqlite3;print(sqlite3.connect('$GDATA/app.sqlite').execute('select url from endpoints where user_id=(select id from users where username=?)',('$GUSER',)).fetchone())\""

echo "== 회차 시작 (${MINS}분)"
run_start >/dev/null || { echo "!! 시작 실패"; exit 1; }
if ! run_wait "$MINS"; then run_stop; echo "!! 정상적으로 안 돌았다 — 점수를 믿지 마라"; exit 1; fi
run_stop

TS=$(date +%m%d-%H%M)
gx "cp $GDATA/log_$GUSER.csv $GDATA/log_${NAME}-${TS}.csv"
echo "== 보관: log_${NAME}-${TS}.csv"

echo "== 분당 추이 (p50/p95 는 user)"
gx "awk -F, 'NR>1{printf \"m%-3s %-11s u2xx=%-6s u5xx=%-4s p50=%-7s p95=%-7s ec2=%s\n\",\$3,\$4,\$5,\$7,\$20,\$23,\$29}' $GDATA/log_$GUSER.csv"
echo "== 점수"
run_score | tail -22
