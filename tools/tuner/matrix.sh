#!/usr/bin/env bash
# matrix.sh — 여러 트래픽 시나리오에서 "솔버가 고른 구성이 실제로 최고점인가"를 검증한다.
#
# 각 시나리오마다:
#   1) 트래픽 설정 (scenario.sh)
#   2) 솔버가 최적 구성을 계산 (solve.py)
#   3) 그 구성과 '이웃 구성' 하나를 각각 적용해서 회차를 돌린다
#   4) 솔버 선택이 실제로 더 높은 점수면 통과
#
# 이웃 구성: 솔버 선택에서 노드 ±1 또는 stress 배치를 뒤집은 것.
# 솔버가 트래픽을 무시하고 같은 답만 내는지, 정말 계산하는지가 여기서 갈린다.
set -uo pipefail
cd "$(dirname "$0")"; source ./lib.sh

MINS=${MINS:-8}                    # 매트릭스용 짧은 회차 (순위 비교가 목적)
RESULT=${RESULT:-matrix-results.tsv}
[ -f "$RESULT" ] || printf "시나리오\t구성\t점수\t성능\t비용\tuser%%\tproduct%%\tstress%%\tavg_ec2\n" > "$RESULT"

# 한 구성으로 회차를 돌리고 점수를 파싱해 기록한다
run_one() {   # $1=시나리오명 $2=노드수 $3=배치
  local name=$1 nodes=$2 mode=$3 out sc
  echo "   -- 적용: ${nodes}노드 / stress=$mode"
  ./apply.sh "$nodes" "$mode" >/dev/null 2>&1
  out=$(./verify.sh "${MULT:-1}" "$MINS" 2>&1)
  if ! printf '%s' "$out" | grep -q "합계"; then
    echo "   !! 회차 실패 — 건너뜀"; return 1
  fi
  sc=$(printf '%s' "$out" | python3 -c "
import sys, re
t = sys.stdin.read()
def g(p, d='0'):
    m = re.search(p, t)
    return m.group(1) if m else d
print('\t'.join([
    g(r'합계\s+([0-9.]+)'), g(r'성능 효율성\s+([0-9.]+)'), g(r'비용 최적화\s+([0-9.]+)'),
    g(r'user\s+avail=[^p]*perf=\s*([0-9.]+)'), g(r'product\s+avail=[^p]*perf=\s*([0-9.]+)'),
    g(r'stress\s+avail=[^p]*perf=\s*([0-9.]+)'), g(r'avg_ec2=([0-9.]+)')]))
")
  printf "%s\t%s노드/%s\t%s\n" "$name" "$nodes" "$mode" "$sc" >> "$RESULT"
  echo "   → $(printf '%s' "$sc" | cut -f1) 점"
}

# 시나리오 실행: 이름, peak1 rates JSON, 솔버에 넘길 트래픽 JSON
scenario() {  # $1=이름 $2=rates $3=traffic
  local name=$1 rates=$2 traffic=$3
  echo "########## 시나리오: $name ##########"
  ./scenario.sh rates "$rates" >/dev/null

  local pick nodes mode alt_nodes alt_mode
  pick=$(python3 solve.py --traffic "$traffic" --min-nodes 2 --max-nodes 8 --top 3)
  echo "$pick" | tail -6
  nodes=$(echo "$pick" | sed -n 's/^최적: 노드 \([0-9]*\)대.*/\1/p')
  mode=$(echo "$pick"  | sed -n 's/^최적:.*stress=\([a-z]*\).*/\1/p')
  [ -z "$nodes" ] && { echo "솔버가 답을 못 냈다"; return 1; }

  # 이웃: 배치를 뒤집은 같은 노드 수 (배치 판단이 맞는지 보는 게 핵심)
  alt_nodes=$nodes
  if [ "$mode" = "iso" ]; then alt_mode=shared; else alt_mode=iso; fi
  [ "$alt_mode" = "iso" ] && [ "$alt_nodes" -lt 3 ] && alt_nodes=3

  run_one "$name" "$nodes" "$mode"
  run_one "$name(이웃)" "$alt_nodes" "$alt_mode"
  echo
}

echo "회차 길이 ${MINS}분, 결과 파일 $RESULT"
