#!/usr/bin/env bash
# 공통 헬퍼 — SSM 으로 bastion 에서 kubectl/mysql 을 돌리고, 채점 서버를 제어한다.
set -uo pipefail
export AWS_PROFILE=${AWS_PROFILE:-lee}
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-ap-northeast-2}
BASTION=${BASTION:-i-0745c7754a8bde26a}
GRADER=${GRADER:-172.16.145.170}
GPASS=${GPASS:-'Skill39**'}
GUSER=${GUSER:-labuser104}
GPY=/opt/cloudgame/engine-venv/bin/python
GDATA=/opt/cloudgame/data
NS=${NS:-apdev}

# bastion 에서 셸 스크립트를 실행하고 stdout 을 돌려준다.
# SSM 의 JSON 이스케이프를 피하려고 파일로 넘긴다.
bx() {
  local script="$1" tmp cid st
  tmp=$(mktemp)
  python3 - "$script" > "$tmp" <<'PY'
import json,sys
body = sys.argv[1]
print(json.dumps({"commands": ["export KUBECONFIG=/root/.kube/config", body]}))
PY
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

gx() { sshpass -p "$GPASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@"$GRADER" "$@" 2>/dev/null; }

meta_set() { gx "$GPY - <<'PY'
import sqlite3
c=sqlite3.connect('$GDATA/app.sqlite')
c.execute('insert into meta(key,value) values(?,?) on conflict(key) do update set value=excluded.value',('$1:$GUSER','$2'))
c.commit()
PY"; }

# 회차 시작 (로그 초기화 + 선수별 플래그 on)
run_start() {
  gx "rm -f $GDATA/log_$GUSER.csv $GDATA/results_$GUSER.json"
  gx "$GPY - <<'PY'
import sqlite3,time
c=sqlite3.connect('$GDATA/app.sqlite'); n=int(time.time())
for k,v in [('injection_running','1'),('injection_start_ts',str(n)),('injection_run_ts',str(n)),('injection_paused_at','0')]:
    c.execute('insert into meta(key,value) values(?,?) on conflict(key) do update set value=excluded.value',(k+':$GUSER',v))
c.commit(); print(n)
PY"
}
run_stop() { meta_set injection_running 0; }
run_minutes() { gx "awk -F, 'NR>1{n++}END{print n+0}' $GDATA/log_$GUSER.csv"; }
run_score()  { gx "$GPY /opt/cloudgame/current/engine/score_csv.py $GDATA/log_$GUSER.csv"; }

# 회차가 끝날 때까지 기다린다 (기대 분수 도달 or 타임아웃)
run_wait() {
  local want=${1:-6}; local max=$((want+5)) i=0 m
  while [ $i -lt $max ]; do
    sleep 60; i=$((i+1))
    m=$(run_minutes)
    [ "${m:-0}" -ge "$want" ] && return 0
  done
  return 0
}
