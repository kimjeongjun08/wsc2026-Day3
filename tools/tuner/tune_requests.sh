#!/usr/bin/env bash
# tune_requests.sh <stress_cpu_request> — stress 의 cpu requests 만 바꾼다.
#
# 왜 이게 성능 레버인가:
#   리눅스 CFS 는 노드가 경합할 때 CPU 를 컨테이너의 cpu.requests 비율로 나눈다.
#   기본값은 stress 600m : user 70m ≈ 8.6:1 이라, 같은 노드에 태우면 순간 몰림에서
#   user 가 밀린다. 실측(x0.5, 4코어): stress 격리 96.99% vs 동거 86.88%.
#
# 안전한가:
#   requests 는 상한이 아니다. 상한은 limits 이고 그건 건드리지 않는다(stress limits.cpu=2).
#   노드가 한가하면 stress 는 여전히 필요한 만큼 쓴다. 경합 순간의 배분만 달라진다.
#   그래도 stress 가 밀릴 수 있으므로 회차에서 stress 통과율을 반드시 같이 본다.
#   과제지 기준: stress SLO 1초, 하드 5초. 실측 p50 은 동거에서 320~390ms 라 여유가 있다.
set -uo pipefail
cd "$(dirname "$0")" || exit 1; source ./common.sh
REQ=${1:?사용법: tune_requests.sh <requests 예: 300m> [limits 예: 1 | none]}
LIM=${2:-}

if [ -z "$LIM" ]; then
  echo "== stress cpu requests → $REQ (limits 는 그대로 둔다)"
  bx "kubectl -n $NS patch deploy stress -p '{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"stress\",\"resources\":{\"requests\":{\"cpu\":\"$REQ\"}}}]}}}}' >/dev/null"
elif [ "$LIM" = none ]; then
  # ★limits 를 아예 제거한다 = CFS 쿼터 없음.
  #   참고: t3.medium 은 2 vCPU 라 limits.cpu=2 는 노드 크기와 같아 사실상 무제한이다.
  #   그래서 "2" 와 "없음" 이 구분되는지부터 확인해야 한다.
  echo "== stress cpu requests → $REQ, limits.cpu 제거 (쿼터 없음)"
  # 병합 패치로는 기존 limits.cpu 가 안 지워진다 — JSON Patch 로 직접 제거해야 한다.
  bx "kubectl -n $NS patch deploy stress -p '{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"stress\",\"resources\":{\"requests\":{\"cpu\":\"$REQ\"}}}]}}}}' >/dev/null
  kubectl -n $NS patch deploy stress --type=json -p='[{\"op\":\"remove\",\"path\":\"/spec/template/spec/containers/0/resources/limits/cpu\"}]' 2>/dev/null | tail -1"
else
  echo "== stress cpu requests → $REQ, limits.cpu → $LIM"
  bx "kubectl -n $NS patch deploy stress -p '{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"stress\",\"resources\":{\"requests\":{\"cpu\":\"$REQ\"},\"limits\":{\"cpu\":\"$LIM\",\"memory\":\"512Mi\"}}}]}}}}' >/dev/null"
fi
bx "kubectl -n $NS rollout status deploy/stress --timeout=300s | tail -1
kubectl -n $NS get deploy stress -o jsonpath='{.spec.template.spec.containers[0].resources}'; echo"
