#!/usr/bin/env bash
# 공통 헬퍼 — 클러스터에 kubectl 을 돌리고, 채점 서버를 제어한다.
set -uo pipefail
export AWS_PROFILE=${AWS_PROFILE:-lee}
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-ap-northeast-2}
# 접속 정보는 코드에 박지 않는다. 옆에 .env 가 있으면 읽는다(커밋 금지).
_LIBDIR=${BASH_SOURCE[0]:-$0}; _LIBDIR=$(cd "$(dirname "$_LIBDIR")" && pwd)
[ -f "$_LIBDIR/.env" ] && . "$_LIBDIR/.env"
BASTION=${BASTION:-}
# 채점 서버 접속 정보는 verify.sh 에서만 쓴다. 없어도 튜닝 도구는 전부 돈다 —
# 대회장에는 채점 서버 SSH 가 없으므로 여기서 필수로 걸면 GO.sh 가 통째로 죽는다.
GRADER=${GRADER:-}
GPASS=${GPASS:-}
GUSER=${GUSER:-labuser104}
GPY=/opt/cloudgame/engine-venv/bin/python
GDATA=/opt/cloudgame/data
NS=${NS:-apdev}

# 클러스터에 대고 셸 스크립트를 실행하고 stdout 을 돌려준다.
#   기본은 로컬 kubectl 이다. 과제 스펙이 t3.medium 외 인스턴스를 금지하고
#   미사용 EC2 를 감점하므로 bastion 없이 도는 게 정상 구성이어야 한다.
#   EKS API 에 로컬로 못 붙는 환경에서만 USE_SSM=1 로 bastion 을 경유한다.
bx() {
  local script="$1" tmp cid st
  if [ "${USE_SSM:-0}" != "1" ]; then
    bash -c "$script"
    return $?
  fi
  tmp=$(mktemp)
  python3 -c 'import json,sys; print(json.dumps({"commands":["export KUBECONFIG=/root/.kube/config", sys.argv[1]]}))' "$script" > "$tmp"
  cid=$(aws ssm send-command --instance-ids "$BASTION" --document-name AWS-RunShellScript \
        --parameters file://"$tmp" --query 'Command.CommandId' --output text 2>/dev/null)
  rm -f "$tmp"
  [ -z "$cid" ] && { echo "SSM_SEND_FAILED" >&2; return 1; }
  while :; do
    st=$(aws ssm get-command-invocation --command-id "$cid" --instance-id "$BASTION" --query Status --output text 2>/dev/null)
    [ "$st" = "InProgress" ] || [ "$st" = "Pending" ] || break
    sleep 5
  done
  aws ssm get-command-invocation --command-id "$cid" --instance-id "$BASTION" \
     --query 'StandardOutputContent' --output text 2>/dev/null
}

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
