#!/usr/bin/env bash
# GO.sh — 이것만 실행하면 된다.
#
#   ./GO.sh          트래픽 전: 준비 → 최적값 탐색 → 적용 → 안정화 확인
#   ./GO.sh watch    트래픽 시작 후: 감시·조정 루프를 백그라운드로 켠다
#   ./GO.sh status   지금 상태 한 눈에
#   ./GO.sh score    지금까지의 누적 점수 전망 (회차 중 아무 때나)
#   ./GO.sh check    판단 로직 자체 점검 (AWS 없이, 수 초)
#   ./GO.sh doctor   ★트래픽 전 진단. 조용히 망가진 것을 찾는다 (클러스터 필요, 수 초)
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
  echo "# 1/4  준비 — 앱 처리 한계 측정 + 초기 구성"
  echo "############################################################"
  ./autotune.sh prepare || { echo "!! 준비 실패"; exit 1; }

  echo
  echo "############################################################"
  echo "# 2/4  최적값 탐색 — 직접 부하를 넣어 후보 구성 비교"
  echo "############################################################"
  echo "   ※ 몇 분 걸린다. POST 가 DB 에 행을 만들므로 최소로만 넣는다."
  BEST=$(./pretune.sh 2>&1 | tee /dev/stderr | sed -n 's/^최적: \([0-9]*\) \([a-z0-9]*\).*/\1 \2/p' | tail -1)

  echo
  echo "############################################################"
  echo "# 3/4  적용"
  echo "############################################################"
  if [ -n "${BEST:-}" ]; then
    set -- $BEST
    echo "   최적 구성: $1 노드 / stress=$2"
    # 상한은 하한보다 2대 높게 — 예상 못 한 스파이크를 Karpenter 가 흡수하도록
    ./apply.sh "$1" "$2" "$1" | tail -3
  else
    echo "   탐색이 결론을 못 냈다 — 콜드 스타트 구성을 유지한다 (하한 2 / 상한 6)"
  fi
  ./tune_requests.sh 100m | tail -1

  echo
  echo "############################################################"
  echo "# 4/4  안정화 확인"
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
  echo "사용: ./GO.sh [setup|watch|status|score|check|doctor]" >&2; exit 1 ;;
esac
