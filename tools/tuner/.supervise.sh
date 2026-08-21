#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
n=0
while :; do
  n=$((n+1))
  echo "=== [$(date +%H:%M:%S)] 운영 루프 기동 #$n" >> autotune.log
  ./autotune.sh run >> autotune.log 2>&1
  rc=$?   # ★먼저 잡아둔다. 아래 문자열의 $(date) 가 $? 를 덮어쓴다.
  echo "=== [$(date +%H:%M:%S)] 운영 루프 종료 rc=$rc — 5초 뒤 재기동" >> autotune.log
  sleep 5
done
