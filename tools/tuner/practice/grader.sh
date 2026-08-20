#!/usr/bin/env bash
# grader.sh — 연습 환경 전용. 사내 채점 서버(cloud.itnsa.cloud)를 SSH 로 제어한다.
#   대회장에는 이 서버가 없다. 대회에서 쓰는 도구(GO.sh 경로)는 이 파일을 부르지 않는다.
#   접속 정보는 코드에 박지 않는다. 옆의 .env 를 읽는다(커밋 금지).
source "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)/common.sh"

_GDIR=${BASH_SOURCE[0]:-$0}; _GDIR=$(cd "$(dirname "$_GDIR")" && pwd)
[ -f "$_GDIR/.env" ] && . "$_GDIR/.env"
GRADER=${GRADER:-}
GPASS=${GPASS:-}
GUSER=${GUSER:-labuser104}
GPY=/opt/cloudgame/engine-venv/bin/python
GDATA=/opt/cloudgame/data

# 채점 서버 SSH. VPN 이 순간 끊기는 일이 있어 3회 재시도하고,
# 끝내 실패하면 조용히 넘어가지 않고 큰 소리로 알린다 —
# 예전엔 stderr 를 버려서 "부하가 한 건도 안 들어온 회차"를 정상 회차로 착각했다.
gx() {
  local i out rc=1
  [ -n "$GRADER" ] && [ -n "$GPASS" ] || {
    echo "GX_UNAVAILABLE: 채점 서버 접속 정보가 없다 (.env 에 GRADER/GPASS). 연습 환경 전용 기능이다." >&2
    return 1
  }
  for i in 1 2 3; do
    out=$(sshpass -p "$GPASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
          -o ServerAliveInterval=5 root@"$GRADER" "$@" 2>/tmp/gx.err); rc=$?
    [ $rc -eq 0 ] && { printf '%s' "$out"; return 0; }
    sleep 5
  done
  echo "GX_FAILED($rc): $(tail -1 /tmp/gx.err)" >&2
  return 1
}

meta_set() { gx "$GPY - <<'PY'
import sqlite3
c=sqlite3.connect('$GDATA/app.sqlite')
c.execute('insert into meta(key,value) values(?,?) on conflict(key) do update set value=excluded.value',('$1:$GUSER','$2'))
c.commit()
PY"; }

# 회차 시작 (로그 초기화 + 선수별 플래그 on)
#   주의: admin.py start 는 전역 injection_running 만 켠다. 채점 엔진은
#   injection_running:<선수> 를 읽으므로 선수별 키를 직접 써야 한다.
run_start() {
  gx "rm -f $GDATA/log_$GUSER.csv $GDATA/results_$GUSER.json" || return 1
  gx "$GPY - <<'PY'
import sqlite3,time
c=sqlite3.connect('$GDATA/app.sqlite'); n=int(time.time())
for k,v in [('injection_running','1'),('injection_start_ts',str(n)),('injection_run_ts',str(n)),('injection_paused_at','0')]:
    c.execute('insert into meta(key,value) values(?,?) on conflict(key) do update set value=excluded.value',(k+':$GUSER',v))
c.commit(); print(n)
PY" || return 1
  # 실제로 켜졌는지 되읽어 확인한다 (VPN 순간 끊김으로 조용히 실패한 적 있음)
  local st
  st=$(gx "$GPY -c \"import sqlite3;print(sqlite3.connect('$GDATA/app.sqlite').execute('select value from meta where key=?',('injection_running:$GUSER',)).fetchone()[0])\"") || return 1
  [ "$st" = "1" ] || { echo "RUN_START_FAILED: injection_running=$st" >&2; return 1; }
}
run_stop() { meta_set injection_running 0; }
run_minutes() { gx "awk -F, 'NR>1{n++}END{print n+0}' $GDATA/log_$GUSER.csv"; }
run_score()  { gx "$GPY /opt/cloudgame/current/engine/score_csv.py $GDATA/log_$GUSER.csv"; }

# 회차가 끝날 때까지 기다린다. 진행이 멈추면 기다리지 말고 실패로 끝낸다 —
# 예전엔 타임아웃에도 성공을 반환해서 빈 회차의 점수를 그대로 믿었다.
run_wait() {
  local want=${1:-6}; local max=$((want+5)) i=0 m last=0 stuck=0
  while [ $i -lt $max ]; do
    sleep 60; i=$((i+1))
    m=$(run_minutes); m=${m:-0}
    [ "$m" -ge "$want" ] && return 0
    if [ "$m" -le "$last" ]; then
      stuck=$((stuck+1))
      [ $stuck -ge 2 ] && { echo "RUN_STALLED: ${m}분에서 정지 — 주입이 돌지 않는다" >&2; return 1; }
    else stuck=0; fi
    last=$m
  done
  echo "RUN_TIMEOUT: ${max}분 안에 ${want}분을 못 채웠다 (현재 ${m}분)" >&2
  return 1
}
