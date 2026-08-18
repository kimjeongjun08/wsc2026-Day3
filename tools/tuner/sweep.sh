#!/usr/bin/env bash
# sweep.sh <배수> <공유노드 후보...> — 후보를 실제로 돌려 최고 점수 구성을 찾는다.
#
# 솔버(solve.py)는 모델로 좁히고, 여기서 실측으로 확정한다.
# 모델은 틀릴 수 있으므로 최종 판단은 항상 실측이다. 결과는 observations.json 에
# 누적되어 다음 보정(calibrate.py)의 입력이 된다 — 돌릴수록 모델이 정확해진다.
set -uo pipefail
cd "$(dirname "$0")"; source ./lib.sh
MULT=${1:?사용법: sweep.sh <배수> <노드후보...>}; shift
CANDS=("$@"); [ ${#CANDS[@]} -eq 0 ] && CANDS=(2 3 4 5)
RESULTS=sweep-x$MULT.tsv
: > "$RESULTS"

for md in "${CANDS[@]}"; do
  echo "########## 공유노드 $md 대 (총 $((md+1))) ##########"
  ./apply.sh "$md" >/dev/null 2>&1
  out=$(./verify.sh "$MULT" 6 2>&1)
  echo "$out" | grep -E "^m[0-9]|합계|perf=|cost_ratio|avg_ec2" | sed 's/^/  /'
  score=$(echo "$out" | awk '/합계/{print $2}')
  ec2=$(echo "$out"   | grep -o 'avg_ec2=[0-9.]*' | head -1 | cut -d= -f2)
  up=$(echo "$out"    | awk '/user  *avail/{for(i=1;i<=NF;i++) if($i ~ /^perf=/) print $(i+0)}' | tr -d 'perf=%')
  uperf=$(echo "$out" | sed -n 's/.*user  *avail=[ 0-9.]*%  *perf= *\([0-9.]*\)%.*/\1/p')
  printf "%s\t%s\t%s\t%s\n" "$md" "${score:-NA}" "${ec2:-NA}" "${uperf:-NA}" >> "$RESULTS"
  # 관측 누적 → 모델 재보정에 사용
  python3 - "$MULT" "$ec2" "$uperf" <<'PY'
import json,sys,os
mult=float(sys.argv[1]); ec2=sys.argv[2]; up=sys.argv[3]
if not ec2 or not up or ec2=="NA" or up=="NA": raise SystemExit
base={"user":44,"product":48,"stress":2.5}
t={k:v*mult for k,v in base.items()}
f=os.path.join(os.path.dirname(os.path.abspath(__file__)),"observations.json")
o=json.load(open(f))
o.append({"note":f"sweep x{mult}","traffic":t,"nodes":float(ec2),
          "observed":{"user":float(up)}})
json.dump(o,open(f,"w"),indent=2,ensure_ascii=False)
PY
done

echo
echo "===== 결과 (x$MULT) ====="
printf "%-8s %-8s %-10s %s\n" "공유노드" "점수" "avg_ec2" "user성능"
sort -k2 -rn "$RESULTS" | while IFS=$'\t' read -r a b c d; do printf "%-8s %-8s %-10s %s\n" "$a" "$b" "$c" "$d"; done
echo "-- 최적: $(sort -k2 -rn "$RESULTS" | head -1 | cut -f1) 공유노드"
