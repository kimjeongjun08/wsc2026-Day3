#!/usr/bin/env bash
# pretable.sh — 트래픽 규모별 최적 구성 '표'를 트래픽 오기 전에 만들어 둔다.
#
# 왜:
#   과제지는 트래픽 양을 알려주지 않는다. 한 지점만 재서 구성을 정하면,
#   실제 트래픽이 그보다 크거나 작을 때 틀린 구성으로 시작하게 된다.
#   여러 부하 수준에서 미리 재두면, 트래픽이 시작된 순간 ALB 에서 rps 를 읽어
#   표를 찾아 '즉시' 전환할 수 있다. 계산도 적응 대기도 필요 없다.
#
# 사용:
#   ./pretable.sh "30 60 120" "2:shared 3:shared 3:iso 4:iso2"
#   결과: pretable.tsv  (rps → 최적 구성)
#
# ※ POST 는 DB 에 행을 만든다(과제지 경고). pretune.sh 의 POST_RATIO 기본 10% 를 지킨다.
set -uo pipefail
cd "$(dirname "$0")" || exit 1; source ./common.sh

LOADS=${1:-"30 60 120"}
CANDS=${2:-"2:shared 3:shared 3:iso 4:iso2"}
OUT=${OUT:-pretable.tsv}

printf "목표rps\t최적구성\t점수\n" > "$OUT"
for L in $LOADS; do
  echo "##################### 목표 ${L} rps #####################"
  # 워커 하나가 내는 rps 는 지연에 달렸다. 넉넉히 띄우고 달성 rps 를 결과에 남긴다.
  W=$(( L / 4 )); [ "$W" -lt 4 ] && W=4
  OUT=pretune-${L}rps.tsv WORKERS=$W DUR=${DUR:-40} ./pretune.sh "$CANDS" 2>&1 | tail -8
  BEST=$(awk -F'\t' 'NR>1{if($7+0>m){m=$7+0;b=$1;s=$7}}END{print b"\t"s}' "pretune-${L}rps.tsv")
  printf "%s\t%s\n" "$L" "$BEST" >> "$OUT"
done

echo
echo "===== 트래픽 규모별 최적 구성 ====="
column -t -s $'\t' "$OUT" 2>/dev/null || cat "$OUT"
echo
echo "트래픽이 시작되면 ALB 에서 rps 를 읽어 이 표에서 구성을 골라 즉시 적용하면 된다:"
echo "  ./autotune.sh show      # 지금 rps 확인"
echo "  ./apply.sh <노드> <배치> <노드>"
