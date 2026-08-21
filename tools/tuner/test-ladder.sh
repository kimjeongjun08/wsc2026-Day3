#!/usr/bin/env bash
# test-ladder.sh — once() 가 판단(DELTA/BAD)을 실제 배치로 옮기는 부분을 검증한다.
#   AWS·클러스터 없이 돈다. 판단 자체는 test-decide.py 가 본다.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
SRC=$(pwd)
pass=0; fail=0

t() { # t <이름> <STATE> <DELTA> <BAD> <기대문자열>
  local name=$1 state=$2 delta=$3 bad=$4 want=$5
  local d; d=$(mktemp -d)
  cp autotune.sh common.sh "$d/"
  printf '%s\n' "$state" > "$d/.autotune-state"
  cat > "$d/alb_snapshot.sh" <<'S'
#!/usr/bin/env bash
echo '{"user":{"req":100,"e5":0,"rps":10,"p":{"50":0.05}},"product":{"req":100,"e5":0,"rps":10,"p":{"50":0.05}},"stress":{"req":10,"e5":0,"rps":1,"p":{"50":0.3}}}'
S
  cat > "$d/decide.py" <<S
import sys
print("   (스텁 판단)")
print("BAD=$bad"); print("WORST=user"); print("DELTA=$delta")
S
  cat > "$d/apply.sh" <<'S'
#!/usr/bin/env bash
echo "APPLY n=$1 mode=$2 cap=$3"
S
  cat > "$d/tune_requests.sh" <<'S'
#!/usr/bin/env bash
echo "REQ $1"
S
  cat > "$d/kubectl" <<'S'
#!/usr/bin/env bash
for a in "$@"; do [ "$a" = nodes ] && { echo "n1 Ready x x x"; echo "n2 Ready x x x"; exit 0; }; done
S
  chmod +x "$d"/*.sh "$d/kubectl"
  local got
  got=$(cd "$d" && PATH="$d:$PATH" bash -c '
    set +u; source ./common.sh 2>/dev/null
    STATE=.autotune-state; MAX_NODES=8; CAP_MARGIN=2; INTERVAL=60
    eval "$(sed -n "/^# ── 한 번 돌기/,/^# ── 안정화 확인/p" autotune.sh | sed "/^# ── 안정화 확인/d")"
    once yes 2>&1')
  if grep -q -- "$want" <<<"$got"; then
    echo "  [O] $name"; pass=$((pass+1))
  else
    echo "  [X] $name"; echo "      기대: $want"; echo "$got" | sed 's/^/      /'; fail=$((fail+1))
  fi
  rm -rf "$d"
}

echo "== 증설"
t "stress 밀림 + 동거 → 먼저 requests 상향(노드 0대)" "2 shared 4"  1 "stress"        "REQ 600m"
t "user 밀림 → 공유 +1"                                "2 shared 4"  1 "user"          "APPLY n=3 mode=shared cap=5"
t "user+stress 동시 (이미 requests 올림) → 둘 다"       "3 iso 5"     1 "user,stress"   "APPLY n=5 mode=iso2 cap=7"
t "위반 앱이 불분명해도 공유를 늘린다"                  "2 shared 4"  1 ""              "APPLY n=3 mode=shared cap=5"
t "상한을 넘기지 않는다"                                "8 shared 10" 1 "user"          "상한 8 대"

echo "== 축소 (여기가 제일 크게 번다)"
t "★축소는 상한도 같이 닫는다 (안 닫으면 노드가 안 준다)" "5 shared 7" -1 ""            "APPLY n=4 mode=shared cap=4"
t "stress 가 멀쩡하면 전용 노드부터 반납"                "4 iso 6"    -1 ""             "APPLY n=3 mode=shared cap=3"
t "stress 가 밀리는 중이면 전용은 건드리지 않는다"       "4 iso 6"    -1 "stress"       "APPLY n=3 mode=iso cap=3"
t "바닥 2대 아래로는 안 내린다"                          "2 shared 2" -1 ""             "더 내릴 곳이 없다"

echo "== 유지"
t "delta=0 이면 아무것도 안 한다"                        "3 shared 5"  0 ""             "(스텁 판단)"

echo
echo "$pass/$((pass+fail)) 통과"
[ "$fail" = 0 ]
