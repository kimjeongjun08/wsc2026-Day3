#!/usr/bin/env bash
# verify.sh <배수> [분] — 실제 채점 회차를 돌리고 점수를 출력한다.
set -uo pipefail
cd "$(dirname "$0")"; source ./lib.sh
MULT=${1:-1}; MINS=${2:-6}

meta_set injection_rate_multiplier "$MULT"
gx "$GPY - <<'PY'
import sqlite3,json
c=sqlite3.connect('$GDATA/app.sqlite')
# 15분 이상이면 공식 스케줄(베이스라인 5분), 짧은 실험이면 베이스라인 1분으로 줄인다
base = 5 if $MINS >= 15 else 1
sch=json.dumps([{'name':'1_baseline','endMin':base,'level':'base','scale':1,'noise':True},
                {'name':'2_spike1','endMin':$MINS,'level':'peak1','scale':1,'noise':False}])
c.execute(\"insert into meta(key,value) values(?,?) on conflict(key) do update set value=excluded.value\",
          ('injection_schedule:$GUSER',sch)); c.commit()
PY"
echo "== x$MULT, ${MINS}분 회차 시작"
run_start >/dev/null || { echo "!! 회차 시작 실패 — 중단"; exit 1; }
if ! run_wait "$MINS"; then run_stop; echo "!! 회차가 정상적으로 안 돌았다 — 점수를 신뢰하지 마라"; exit 1; fi
run_stop
echo "-- 분당 추이"
gx "awk -F, 'NR>1{printf \"m%-3s %-11s u2xx=%-5s u5xx=%-3s up50=%-7s up95=%-7s ec2=%s\n\",\$3,\$4,\$5,\$7,\$20,\$23,\$29}' $GDATA/log_$GUSER.csv"
echo "-- 점수"
run_score | tail -20
