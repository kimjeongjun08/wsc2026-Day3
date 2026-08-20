#!/usr/bin/env bash
# test-ladder.sh — 과부하 사다리의 분기를 트래픽 없이 검증한다.
#
# 왜 필요한가:
#   사다리가 맞게 갈라지는지 확인하려고 채점 회차를 60분씩 돌리는 건 낭비다.
#   실제 부하가 필요한 건 "그래서 지연이 내려가나"(물리)뿐이고,
#   "어느 가지로 가나"(로직)는 스텁으로 몇 초면 끝난다.
#   실제로 이 테스트를 먼저 돌려 하네스 결함을 잡았다.
#
# 사용: ./test-ladder.sh        (클러스터·AWS 자격증명 불필요)
set -uo pipefail
cd "$(dirname "$0")" || exit 1
SRC=$PWD
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
cp autotune.sh common.sh "$WORK/"
cd "$WORK"
printf '#!/usr/bin/env bash\necho "APPLY nodes=$1 mode=$2 cap=${3:-}"\n' > apply.sh
printf '#!/usr/bin/env bash\necho "REQ $1"\n' > tune_requests.sh
chmod +x apply.sh tune_requests.sh

pass=0; fail=0
case_is() {  # <설명> <state> <위반> <트래픽> <기대문자열>
  local desc=$1 state=$2 ov=$3 traffic=$4 want=$5 got
  echo "$state" > .autotune-state
  # 헤더(변수 선언부)를 뺀 슬라이스만 평가하므로 -u 는 여기서만 푼다
  got=$( set +u; source ./common.sh 2>/dev/null
    STATE=.autotune-state; MAX_NODES=8; CAP_MARGIN=2; INTERVAL=60; MIN_GAIN=1.0
    eval "$(sed -n '/^# ── 과부하 방어/,$p' autotune.sh | sed '/^case "\${1:-run}"/,$d')"
    eval "read_traffic()   { echo '$traffic'; }"
    eval "overload_nodes() { echo '$ov'; }"
    recalibrate_curves() { :; }
    ask_solver()     { echo "최적: 노드 2대 / stress=shared → 예상 40.0/40"; }
    ready()          { return 0; }
    once yes 2>&1 )
  if grep -qF "$want" <<<"$got"; then
    echo "  [O] $desc"; pass=$((pass+1))
  else
    echo "  [X] $desc"; echo "      기대: $want"; echo "      실제: $(grep -E 'APPLY|REQ|과부하' <<<"$got" | tr '\n' ' ')"; fail=$((fail+1))
  fi
}

echo "== 과부하 사다리 분기"
rm -f .stress-req-bumped
case_is "1단계 stress 굶주림 → requests 상향"      "2 shared 4" "stress 3" '{"user":22,"product":45,"stress":7.0}' "REQ 600m"
touch .stress-req-bumped
case_is "2단계 stress 7rps → 4/iso2"               "2 shared 4" "stress 3" '{"user":22,"product":45,"stress":7.0}' "APPLY nodes=4 mode=iso2"
case_is "2단계 stress 2.5rps → 3/iso"              "2 shared 4" "stress 3" '{"user":22,"product":45,"stress":2.5}' "APPLY nodes=3 mode=iso"
case_is "2단계 iso 에서 iso2 로 승급"               "3 iso 5"   "stress 4" '{"user":22,"product":45,"stress":7.0}' "APPLY nodes=4 mode=iso2"
case_is "3단계 user 과부하 → 노드+1, 배치 유지"     "2 shared 4" "user 3"   '{"user":66,"product":45,"stress":0.5}' "APPLY nodes=3 mode=shared"
case_is "상한 도달 시 더 늘리지 않는다"             "8 shared 10" "user 9"  '{"user":66,"product":45,"stress":0.5}' "더 늘릴 수 없다"

echo
echo "통과 $pass / 실패 $fail"
[ "$fail" = 0 ]
