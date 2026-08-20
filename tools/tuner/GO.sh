#!/usr/bin/env bash
# GO.sh — 이것만 실행하면 된다.
#
#   ./GO.sh          트래픽 전: 준비 → 최적값 탐색 → 적용 → 안정화 확인
#   ./GO.sh watch    트래픽 시작 후: 감시·조정 루프를 백그라운드로 켠다
#   ./GO.sh status   지금 상태 한 눈에
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

case "$CMD" in
setup)
  need kubectl; need aws; need python3
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
    ./apply.sh "$1" "$2" "$(( $1 + 2 ))" | tail -3
    echo "$1 $2 $(( $1 + 2 ))" > .autotune-state
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
  echo "=========================================================="
  echo " 준비 끝. 남은 것 두 가지:"
  echo "   1) 채점 플랫폼에 엔드포인트 등록  (미등록이면 0점)"
  echo "   2) 트래픽 시작되면:  ./GO.sh watch"
  echo "=========================================================="
  ;;

watch)
  echo "감시·조정 루프를 백그라운드로 켠다. 로그: autotune.log"
  nohup ./autotune.sh run > autotune.log 2>&1 &
  echo "PID $! — 끄려면: pkill -f 'autotune.sh run'"
  sleep 3
  tail -5 autotune.log
  ;;

status)
  echo "=== 노드 ==="
  kubectl get nodes -L role --no-headers 2>/dev/null | awk '{print "  ", $1, $6}'
  echo "=== 파드 ==="
  kubectl -n apdev get pods --no-headers 2>/dev/null | awk '{split($1,a,"-"); c[a[1]]++} END{for(k in c) print "  ", k, c[k]"개"}'
  echo "=== 지금 트래픽과 추천 ==="
  ./autotune.sh show 2>&1 | tail -8
  echo "=== 안정화 ==="
  ./autotune.sh ready 2>&1 | tail -8
  ;;

*)
  echo "사용: ./GO.sh [setup|watch|status]" >&2; exit 1 ;;
esac
