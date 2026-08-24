#!/usr/bin/env bash
# GO.sh — 이것만 실행하면 된다.
#
#   ./GO.sh          트래픽 전: 준비 → 최적값 탐색 → 적용 → 안정화 확인
#   ./GO.sh watch    트래픽 시작 후: 감시·조정 루프를 백그라운드로 켠다
#   ./GO.sh status   지금 상태 한 눈에
#   ./GO.sh monitor  실시간 관제 (튜너 생존이 1순위 · 10초 갱신 · Ctrl+C 종료)
#   ./GO.sh score    지금까지의 누적 점수 전망 (회차 중 아무 때나)
#   ./GO.sh check    판단 로직 자체 점검 (AWS 없이, 수 초)
#   ./GO.sh doctor   ★트래픽 전 진단. 조용히 망가진 것을 찾는다 (클러스터 필요, 수 초)
#   ./GO.sh tune     사전 탐색(선택). 기본 경로에서 뺐다 — setup 주석 참고
#
# 40점이 나오는 조건 — 채점표 산수 그대로다:
#   비용 12점 = 회차 '분' 평균 노드 2.00대 이하. 0.5대마다 정확히 1점씩 깎인다.
#     → 비용이 '분' 평균이므로, 매 분 제약을 만족하는 최소 노드 수를 쓰면
#       회차 길이와 무관하게 평균이 최소가 된다. 그래서 이 도구는 회차가
#       15분이든 120분이든 같은 판단을 한다. 따로 맞출 게 없다.
#   성능 12점 = 세 앱 모두 SLA 안에 든 요청 90% 이상 (user·product 200ms, stress 1s).
#   → 노드 1대 = 2점. 성능은 앱당 최대 4점. 그래서 노드를 사서 이기려는 전략은
#     거의 항상 진다. 기본자세는 '2대 동거(shared)'이고 증설은 산수로 이득이
#     증명될 때만 한다. stress 를 전용 노드로 빼면 최소 3대 = 비용 10점이 천장이다.
#
# 하는 일 (트래픽 전):
#   1) 앱 처리 한계 측정 (동시성 곡선)
#   2) 후보 구성을 자동으로 뽑아 직접 부하를 넣어 비교
#   3) 가장 높은 구성을 적용
#   4) "트래픽 받아도 되는 상태"인지 확인
#
# 전제: terraform apply 가 끝났고, bastion 을 제거했고, kubectl 이 클러스터에 붙어 있을 것.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

CMD=${1:-setup}

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "!! $1 가 없다"; exit 1; }
}

# Windows 에서 클론하면 git 이 줄바꿈을 CRLF 로 바꿔놓는다(core.autocrlf 기본값).
# 그 상태로 리눅스에서 돌리면 bash 가 \r 에 걸려 죽거나, 더 나쁘게는
# 상태 파일 값이 "shared\r" 로 읽혀 조용히 틀린다. 발견하면 바로 고친다.
fix_crlf() {
  local f bad=""
  for f in *.sh *.py practice/*.sh; do
    [ -f "$f" ] || continue
    grep -qU $'\r' "$f" 2>/dev/null && bad="$bad $f"
  done
  [ -z "$bad" ] && return 0
  echo "!! CRLF 줄바꿈이 섞여 있다 (Windows 에서 클론한 흔적). 고친다:$bad"
  for f in $bad; do tr -d '\r' < "$f" > "$f.lf" && mv "$f.lf" "$f"; done
  chmod +x ./*.sh practice/*.sh 2>/dev/null
  echo "   고쳤다. 다시 안 겪으려면:  git config --global core.autocrlf input"
  echo "   그리고 다시 실행해라:  ./GO.sh $CMD"
  exit 1
}

# 시작하기 전에 "정말 붙어 있나"를 5초 안에 확인한다.
# 예전엔 죽은 클러스터를 가리켜도 몇 분씩 조용히 돌았다 — 대회에서 그러면 끝이다.
preflight() {
  local ok=1
  echo "== 사전 점검"

  if ! kubectl --request-timeout=10s get --raw /version >/dev/null 2>&1; then
    echo "   [X] 클러스터에 못 붙는다"
    echo "       aws eks update-kubeconfig --region ap-northeast-2 --name apdev-cluster"
    ok=0
  else
    echo "   [O] 클러스터 연결"
  fi

  if [ "$ok" = 1 ] && ! kubectl --request-timeout=10s get ns "${NS:-apdev}" >/dev/null 2>&1; then
    echo "   [X] 네임스페이스 '${NS:-apdev}' 가 없다 — terraform apply 가 끝났나?"
    ok=0
  elif [ "$ok" = 1 ]; then
    echo "   [O] 네임스페이스 ${NS:-apdev}"
  fi

  if ! aws sts get-caller-identity >/dev/null 2>&1; then
    echo "   [X] AWS 자격증명이 안 먹는다 (AWS_PROFILE=${AWS_PROFILE:-미설정})"
    echo "       export AWS_PROFILE=<본인 프로파일>"
    ok=0
  else
    echo "   [O] AWS 자격증명 (${AWS_PROFILE:-default})"
  fi

  [ "$ok" = 1 ] || { echo; echo "!! 사전 점검 실패 — 위 문제를 먼저 고쳐라"; exit 1; }
  echo
}

case "$CMD" in
setup)
  fix_crlf
  need kubectl; need aws; need python3
  preflight
  echo "############################################################"
  echo "# 1/3  준비 — 클러스터 상태 정리"
  echo "############################################################"
  ./autotune.sh prepare || { echo "!! 준비 실패"; exit 1; }

  echo
  echo "############################################################"
  echo "# 2/3  출발 구성 — 2대 동거로 시작한다"
  echo "############################################################"
  # ★사전 탐색(pretune)은 기본 경로에서 뺐다. 근거 셋:
  #
  #   1) 채점은 '변하는 곡선 전체'를 적분하는데 pretune 은 '한 지점'에서 고른다.
  #      피크 기준으로 고른 구성을 회차 내내 지불하게 된다. 비용은 분 평균이라
  #      낮게 시작해 필요할 때만 올리는 쪽이 언제나 유리하다 —
  #      필요했던 분만큼만 내면 되기 때문이다.
  #
  #   2) 측정이 부정확하다. 실측(2026-08-21): pretune 이 2대 구성의 stress 를
  #      70.8% 로 쟀는데 같은 구성에서 채점기 실측은 94.4% 였다. 24%p 오차는
  #      노드를 한 대 더 사게 만들기에 충분하다(비용 2점).
  #      pretune 은 stress 를 길이 88 고정으로 쏘고, 측정 창에 노드 수렴 직후의
  #      불안정 구간이 섞인다.
  #
  #   3) 이제 노드 수를 강제로 못 만든다. minDomains 를 2 로 고정한 뒤로는
  #      수요(Pending 파드)가 있어야 노드가 생긴다. 부하가 없는 사전 단계에서
  #      "3대로 만들고 재라"는 요구 자체가 성립하지 않는다.
  #
  #   그리고 무엇보다 — 2대 출발로 채점기 공식 40.0/40 을 받았다(practice x0.5).
  #   증설은 한 번도 없었다. 탐색이 찾아줄 것이 남아 있지 않다.
  #
  #   그래도 돌려보고 싶으면:  ./GO.sh tune
  echo "   2대 / stress 동거 / 상한 2 로 출발한다."
  echo "   부하가 오면 감시 루프가 실측을 보고 필요한 만큼만 올린다."
  ./apply.sh "${COLD_NODES:-2}" "${COLD_MODE:-shared}" "${COLD_NODES:-2}" | tail -3
  ./tune_requests.sh "${COLD_STRESS_REQ:-100m}" | tail -1

  echo
  echo "############################################################"
  echo "# 3/3  안정화 확인"
  echo "############################################################"
  for i in $(seq 1 30); do
    ./autotune.sh ready && break
    echo "   ... 30초 후 재확인 ($i/30)"
    sleep 30
  done

  echo
  echo "############################################################"
  echo "# 진단 — 조용히 망가진 것이 없는지"
  echo "############################################################"
  ./doctor.sh || echo "   ※ 위 문제를 고치고 ./GO.sh doctor 로 다시 확인해라"

  echo
  echo "=========================================================="
  echo " 준비 끝. 남은 것 두 가지:"
  echo "   1) 채점 플랫폼에 엔드포인트 등록  (미등록이면 0점)"
  echo "   2) 트래픽 시작되면:  ./GO.sh watch"
  echo "=========================================================="
  ;;

watch)
  # ★감독 루프를 도구 안에 둔다.
  #   실측에서 운영 루프가 76분 만에 조용히 죽은 적이 있다. 원인은 아직 모른다.
  #   대회 중에 그러면 그 뒤 구간은 통째로 방치된다. 죽으면 다시 띄운다.
  #   setsid 로 세션에서 떼어낸다 — nohup 만으로는 SSH 가 끊길 때 같이 죽는다.
  # ★기존 감독 루프를 먼저 끈다.
  #   실측 사고(2026-08-21 공식 120분 회차): watch 를 두 번 불렀는데 첫 번째를
  #   안 껐다. 잠금이 두 번째를 막았지만(정상), 첫 번째는 이미 죽어 있었고
  #   아무도 그걸 몰랐다. 결과: 120분 회차 내내 튜너가 판단을 한 번도 안 했다.
  #   노드 변화는 HPA+Karpenter 기본 동작이었고, 점수는 도구와 무관한 값이었다.
  #   로그를 봐도 "다른 튜너가 잡고 있다"만 1437번 찍혀 있어 정상처럼 보인다.
  if pgrep -f '[.]supervise[.]sh' >/dev/null 2>&1 || pgrep -f 'autotune[.]sh run' >/dev/null 2>&1; then
    echo "이미 돌고 있는 감시 루프를 끈다 (중복 실행은 회차를 통째로 버린다)"
    pkill -f '[.]supervise[.]sh' 2>/dev/null
    pkill -f 'autotune[.]sh run' 2>/dev/null
    sleep 3
  fi
  # 남의 잠금이 남아 있으면 여기서 넘겨받는다 — 위에서 우리 프로세스는 다 껐다.
  kubectl -n "${NS:-apdev}" delete cm tuner-lock >/dev/null 2>&1

  # ★회차 원장은 여기서 초기화한다.
  #   watch 는 '회차 시작할 때 한 번' 부르는 명령이다. 감독 루프가 중간에
  #   재기동할 때는 원장이 이어져야 하므로 autotune 쪽에서는 절대 안 지운다.
  #   이어서 돌리려면: RESUME=1 ./GO.sh watch
  if [ "${RESUME:-0}" = 1 ]; then
    echo "이전 원장을 이어서 쓴다 (RESUME=1)"
  else
    rm -f .round-ledger.json .stress-req-bumped
    # ★로그도 회차마다 새로 시작한다.
    #   이어 붙이면 지난 회차의 판단 줄이 남아, 로그를 훑어 원인을 볼 때
    #   옛 줄을 지금 것으로 착각한다(실측: 버린 회차의 줄을 보고 있었다).
    #   지난 회차는 지우지 말고 이름을 붙여 남긴다.
    [ -s autotune.log ] && mv autotune.log "autotune-$(date +%m%d-%H%M).log"
    echo "회차 원장 초기화 (이전 로그는 autotune-*.log 로 보관)"
  fi
  # ★출발 지분을 여기서 보장한다.
  #   CFS 는 경합할 때 cpu.requests 비율로 CPU 를 나눈다. 배포 기본값은
  #   stress 600m : user 70m = 8.6 : 1 이라, 경합이 생기는 순간 user 가 밀린다.
  #   실측(2026-08-21 practice, 52rps): 그 상태의 2대에서 user p50 132ms / p90 264ms 였고,
  #   노드를 3대로 늘리자 p50 11ms 로 10배 빨라졌다. DB 는 내내 놀고 있었다
  #   (읽기 0.4ms, 쓰기 1.2ms, CPU 5.8%) — 순수한 CPU 지분 문제였다.
  #   setup 을 거치면 tune_requests.sh 가 낮춰주지만, apply.sh 만 직접 부르면
  #   건너뛴다. 사람 손에 맡길 일이 아니다. watch 시작 때 스스로 확인한다.
  CUR_REQ=$(kubectl -n "${NS:-apdev}" get deploy stress \
            -o jsonpath='{.spec.template.spec.containers[0].resources.requests.cpu}' 2>/dev/null)
  if [ "${CUR_REQ:-}" != "${START_STRESS_REQ:-100m}" ]; then
    echo "stress cpu.requests ${CUR_REQ:-?} → ${START_STRESS_REQ:-100m} (경합 시 user 가 밀리지 않게)"
    ./tune_requests.sh "${START_STRESS_REQ:-100m}" >/dev/null 2>&1
  else
    echo "stress cpu.requests ${CUR_REQ} — 그대로 둔다"
  fi

  # ★바닥을 먼저 고정한다.
  #   상태 파일이 없으면 튜너는 노드 수를 소유하지 못한다. 그 사이 Karpenter 가
  #   NodePool 상한까지 제 판단으로 노드를 붙인다.
  #   실측(2026-08-24): 상태 파일 없이 watch 만 켰더니 baseline 이 3대로 시작했고
  #   비용이 12/12 대신 10/12 였다. doctor 가 경고해도 사람이 넘기면 그만이라
  #   도구가 스스로 처리한다.
  if [ ! -s "${STATE:-.tuner-state}" ]; then
    echo "상태 파일이 없다 — 바닥을 ${COLD_NODES:-2}대로 먼저 고정한다"
    ./apply.sh "${COLD_NODES:-2}" "${COLD_MODE:-shared}" "${COLD_NODES:-2}" 2>&1 | tail -2
  fi

  echo "감시·조정 루프를 켠다 (죽으면 자동 재기동). 로그: autotune.log"
  cat > .supervise.sh <<'SUP'
#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
n=0
while :; do
  n=$((n+1))
  echo "=== [$(date +%H:%M:%S)] 운영 루프 기동 #$n" >> autotune.log
  ./autotune.sh run >> autotune.log 2>&1
  rc=$?   # ★먼저 잡아둔다. 아래 문자열의 $(date) 가 $? 를 덮어쓴다.
  if [ -f .no-restart ]; then
    echo "=== [$(date +%H:%M:%S)] 재기동하지 않는다 (다른 튜너가 이 클러스터를 잡고 있다)" >> autotune.log
    rm -f .no-restart
    exit 0
  fi
  echo "=== [$(date +%H:%M:%S)] 운영 루프 종료 rc=$rc — 5초 뒤 재기동" >> autotune.log
  sleep 5
done
SUP
  chmod +x .supervise.sh
  # setsid 가 있으면 세션에서 완전히 떼어낸다(SSH 가 끊겨도 안 죽는다).
  # macOS 에는 없으므로 nohup 으로 떨어진다 — 대회 PC(WSL)에는 있다.
  if command -v setsid >/dev/null 2>&1; then
    setsid ./.supervise.sh > /dev/null 2>&1 < /dev/null &
  else
    nohup ./.supervise.sh > /dev/null 2>&1 < /dev/null &
  fi
  echo "PID $! — 끄려면: pkill -f '[.]supervise[.]sh'; pkill -f 'autotune[.]sh run'"
  echo "회차 길이는 안 물어본다 — 15분이든 2시간이든 같은 판단으로 돈다."
  sleep 3
  tail -5 autotune.log
  ;;

monitor)
  # 실시간 관제. 최우선은 "튜너가 지금 판단하고 있나" — 이게 죽어 있으면 나머지는 의미 없다.
  # 10초마다 갱신, Ctrl+C 로 나간다. 읽기만 한다(원장·로그·k8s 조회) — 회차에 개입하지 않는다.
  trap 'echo; echo "monitor 종료 (watch 는 계속 돈다)"; exit 0' INT
  _mt() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0; }
  while :; do
    BUF=$(
      echo "════ GO.sh monitor  $(date '+%H:%M:%S')  · 10초 갱신 · Ctrl+C 종료 ════"

      # 1. 튜너 생존 ── 실측 사고(08-21): 잠금만 남기고 죽은 튜너를 120분간 아무도 몰랐다
      NP=$(pgrep -c -f 'autotune[.]sh run' 2>/dev/null | head -1); case "$NP" in (''|*[!0-9]*) NP=0;; esac
      SP=$(pgrep -c -f '[.]supervise[.]sh' 2>/dev/null | head -1); case "$SP" in (''|*[!0-9]*) SP=0;; esac
      if [ "$NP" = 0 ] && [ "$SP" = 0 ]; then
        echo "■ 튜너    [X] 안 돈다 — ./GO.sh watch 로 켜라"
      elif [ "$NP" -gt 2 ]; then
        echo "■ 튜너    [X] 루프 ${NP}개 — 중복 실행이다. pkill -f '[.]supervise[.]sh'; pkill -f 'autotune[.]sh run' 후 watch 다시"
      elif [ -f .round-ledger.json ]; then
        AGE=$(( $(date +%s) - $(_mt .round-ledger.json) ))
        if [ "$AGE" -gt 180 ]; then
          echo "■ 튜너    [X] 살아만 있고 판단을 안 한다 — 원장 ${AGE}초째 그대로 (autotune.log 를 봐라)"
        else
          echo "■ 튜너    [O] 판단 중 — 원장 ${AGE}초 전 갱신 (supervise ${SP} / loop ${NP})"
        fi
      else
        echo "■ 튜너    [!] 돌고는 있는데 원장이 아직 없다 (트래픽 시작 전이면 정상)"
      fi
      if [ -f autotune.log ]; then
        LAGE=$(( $(date +%s) - $(_mt autotune.log) ))
        echo "  최근 로그 (${LAGE}초 전):"
        grep -v '^$' autotune.log | tail -3 | cut -c1-110 | sed 's/^/    /'
      fi

      # 2. 노드 — 도구가 생각하는 상태와 실제가 맞는지 한 줄에서 비교
      ST=$(cat .autotune-state 2>/dev/null)
      NRDY=$(kubectl get nodes --no-headers 2>/dev/null | awk '$2=="Ready"{n++} END{print n+0}')
      NALL=$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')
      NCL=$(kubectl get nodeclaims --no-headers 2>/dev/null | wc -l | tr -d ' ')
      if [ -n "$ST" ]; then
        echo "■ 노드    실제 ${NRDY}/${NALL} Ready · nodeclaim ${NCL}   도구: 목표 $(awk '{print $1}' <<<"$ST")대 / $(awk '{print $2}' <<<"$ST") / 상한 $(awk '{print $3}' <<<"$ST")"
      else
        echo "■ 노드    실제 ${NRDY}/${NALL} Ready · nodeclaim ${NCL}   도구: 상태 파일 없음 (setup 전?)"
      fi

      # 3. 증설 컨트롤러 — 판단이 옳아도 이게 죽으면 실행이 안 된다 (doctor 4d 와 같은 자리)
      for D in karpenter aws-load-balancer-controller; do
        L=$(kubectl -n kube-system get deploy "$D" --no-headers 2>/dev/null)
        if [ -z "$L" ]; then echo "■ ${D}  [X] 없음"; else
          echo "$L" | awk -v d="$D" '{split($2,a,"/"); m=(a[1]!=""&&a[1]==a[2])?"[O]":"[X]"; printf "■ %-24s %s %s ready\n", d, m, $2}'
        fi
      done

      # 4. HPA / 파드
      echo "■ HPA (cpu → 파드수, min~max)"
      kubectl -n apdev get hpa --no-headers 2>/dev/null \
        | awk '{printf "    %-14s %-14s %3s개 (%s~%s)\n", $1, $3, $6, $4, $5}'
      PEND=$(kubectl -n apdev get pods --no-headers 2>/dev/null | awk '$3=="Pending"{n++} END{print n+0}')
      BADP=$(kubectl -n apdev get pods --no-headers -o custom-columns='N:.metadata.name,R:.status.containerStatuses[0].restartCount,RD:.status.containerStatuses[0].ready' 2>/dev/null \
             | awk 'NR>0 && ($2!="0"||$3!="true"){n++} END{print n+0}')
      echo "■ 파드    Pending ${PEND}개 · 재시작/NotReady ${BADP}개"
      kubectl -n apdev get events --sort-by=.lastTimestamp 2>/dev/null \
        | grep -Ei "Unhealthy|Killing|BackOff|FailedScheduling" | tail -2 | cut -c1-110 | sed 's/^/    /'

      # 5. 누적 점수 (원장 기반 — CloudWatch 추가 호출 없음)
      if [ -f .round-ledger.json ]; then
        echo "■ 점수 전망"
        ./GO.sh score 2>/dev/null | tail -7 | sed 's/^/    /'
      fi
    )
    printf '\033[H\033[2J%s\n' "$BUF"
    sleep 10
  done
  ;;

status)
  echo "=== 노드 ==="
  kubectl get nodes -L role --no-headers 2>/dev/null | awk '{print "  ", $1, $6}'
  echo "=== 파드 ==="
  kubectl -n apdev get pods --no-headers 2>/dev/null | awk '{split($1,a,"-"); c[a[1]]++} END{for(k in c) print "  ", k, c[k]"개"}'
  # ★파드가 ALB 에서 빠지거나 재시작하고 있는지 본다.
  #   포화 구간에서 헬스체크가 타임아웃하면 멀쩡한 파드가 빠진다 — 용량이 제일
  #   필요한 순간에 용량이 줄고, 성능 손실이 가용성 손실로 번진다.
  #   재시작 카운트가 0 이 아니거나 Ready 가 아닌 파드가 있으면 그게 원인일 수 있다.
  echo "=== 파드 안정성 (재시작 / 준비) ==="
  kubectl -n apdev get pods --no-headers -o custom-columns=\
'N:.metadata.name,R:.status.containerStatuses[0].restartCount,RD:.status.containerStatuses[0].ready' 2>/dev/null \
    | awk '{if($2!="0"||$3!="true") print "   [!]", $0; else ok++} END{if(ok) print "   [O] 정상 파드", ok"개 (재시작 0)"}'
  kubectl -n apdev get events --sort-by=.lastTimestamp 2>/dev/null \
    | grep -Ei "Unhealthy|Killing|BackOff" | tail -5 | sed 's/^/   /'
  echo "=== 지금 트래픽과 추천 ==="
  ./autotune.sh show 2>&1 | tail -8
  echo "=== 안정화 ==="
  ./autotune.sh ready 2>&1 | tail -8
  ;;

score)
  # 회차 중 아무 때나. decide.py 가 쌓아온 원장으로 누적 점수를 보여준다.
  python3 -c '
import json, os, sys
sys.path.insert(0, ".")
import score
p = ".round-ledger.json"
if not os.path.exists(p):
    print("아직 원장이 없다 - 트래픽이 시작되고 한 주기 지나면 생긴다"); raise SystemExit
led = json.load(open(p))["led"]
perf, avail, avg = score.ledger_metrics(led)
s = score.total(perf, avail, avg or 2.0)
print("누적 %.0f분 - 분 평균 노드 %.2f대 (비용비 %.2f)" % (led["minutes"], avg, avg/2))
for a in score.APPS:
    print("  %-8s 통과율 %6.2f%%   성공률 %6.2f%%" % (a, perf[a] or 0, avail[a] or 0))
print("  비정상 %4.1f/4   고가용성 %5.1f/12   성능 %5.1f/12   비용 %5.1f/12"
      % (s["abnormal"], s["availability"], s["performance"], s["cost"]))
print("  -> %.1f/40" % s["total"] + ("   [경고] 통과율 30%% 미만이라 비용이 통째로 0 이다" if s["gated"] else ""))
'
  ;;

tune)
  # 사전 탐색을 굳이 돌려보고 싶을 때만. 기본 경로에서 뺀 이유는 setup 주석 참고.
  echo "※ 사전 탐색은 기본 경로에서 제외됐다. 이유는 GO.sh 의 setup 주석을 읽어라."
  ./pretune.sh "$@"
  ;;

doctor)
  # 트래픽 전에 '조용히 망가진 것'을 찾는다. 여기서 걸리는 건 전부 실제로 당한 것들이다.
  exec ./doctor.sh
  ;;

check)
  fix_crlf
  echo "== 판단 로직 (AWS 불필요)"
  python3 test-decide.py || exit 1
  echo
  echo "== 배치 변환"
  ./test-ladder.sh || exit 1
  ;;

*)
  echo "사용: ./GO.sh [setup|watch|status|score|check|doctor|tune]" >&2; exit 1 ;;
esac
