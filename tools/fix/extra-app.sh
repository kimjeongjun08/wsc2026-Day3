#!/usr/bin/env bash
# extra-app.sh — 대회에서 애플리케이션이 '추가'될 때. (평소엔 안 쓴다 — 대비용)
#
#   ./extra-app.sh deploy    spec.json 의 extra 앱을 클러스터+ALB 에 배포
#   ./extra-app.sh status    파드/HPA/타깃 상태 한 눈에
#   ./extra-app.sh remove    배포 전부 회수 (k8s + ALB 규칙 + TG)
#
# 순서 (GO.sh 방식 그대로):
#   1) spec.json 의 extra 를 채우고 enabled=true
#   2) python3 spec.py --apply       ← waf 허용 + 튜너 score.py 에 앱 편입
#   3) cd ../../terraform && terraform apply   ← waf 반영
#   4) ECR 에 이미지 push 후 image 값 채움
#   5) ./extra-app.sh deploy
#   6) 돌던 ./GO.sh watch 는 그대로 둔다 — TG 이름(apdev-<앱>)으로 자동 발견해서
#      기존 앱들과 함께 감시·채점한다. baseline 2대 유지도 튜너가 그대로 한다
#      (이 앱의 requests 를 기존 앱과 같은 70m 급으로 잡아 2대에 동거 가능하게 했다).
set -uo pipefail
cd "$(dirname "$0")" || exit 1

REGION=${REGION:-ap-northeast-2}
NS=${NS:-apdev}
PROJECT=${PROJECT:-apdev}
CMD=${1:-status}

# spec.json 에서 extra 필드 읽기
cfg() { python3 -c "import json;print(json.load(open('spec.json'))['extra'].get('$1',''))"; }
ENABLED=$(cfg enabled); NAME=$(cfg name); IMAGE=$(cfg image); PORT=$(cfg port)
APP_PATH=$(cfg path); HPATH=$(cfg health_path); CPU=$(cfg cpu_request_m)
MEM=$(cfg mem_request_mi); HMIN=$(cfg hpa_min); HMAX=$(cfg hpa_max)
TG_NAME="${PROJECT}-${NAME}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "!! $1 가 없다"; exit 1; }; }
need kubectl; need aws; need python3

alb_arn() {
  aws elbv2 describe-load-balancers --region "$REGION" --names "${PROJECT}-alb" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null
}
listener_arn() {
  aws elbv2 describe-listeners --region "$REGION" --load-balancer-arn "$1" \
    --query 'Listeners[?Port==`80`]|[0].ListenerArn' --output text 2>/dev/null
}
tg_arn() {
  aws elbv2 describe-target-groups --region "$REGION" --names "$TG_NAME" \
    --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null
}

preflight() {
  local ok=1
  [ "$ENABLED" = "True" ] || { echo "!! spec.json 의 extra.enabled 가 false 다 — 먼저 켜고 python3 spec.py --apply"; ok=0; }
  case "$IMAGE" in ""|"<"*) echo "!! extra.image 가 비었다 — ECR push 후 spec.json 에 채워라"; ok=0;; esac
  kubectl --request-timeout=10s get ns "$NS" >/dev/null 2>&1 || { echo "!! 클러스터/네임스페이스($NS)에 못 붙는다"; ok=0; }
  aws sts get-caller-identity >/dev/null 2>&1 || { echo "!! AWS 자격증명이 안 먹는다"; ok=0; }
  # 튜너 score.py 에 편입됐는지 — 안 됐으면 감시 밖에서 돈다
  grep -q "\"$NAME\"" ../tuner/score.py 2>/dev/null \
    || echo "?? tuner/score.py 에 '$NAME' 이 없다 — python3 spec.py --apply 를 먼저 돌려라 (튜너가 이 앱을 채점에 안 넣는다)"
  [ "$ok" = 1 ] || exit 1
}

k8s_manifest() {
  # 기존 앱(deploy.yaml의 user)과 같은 골격: 분산 강제 + stress 노드 회피 + 우선순위
  cat <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${NAME}
  namespace: ${NS}
spec:
  replicas: ${HMIN}
  selector: { matchLabels: { app: ${NAME} } }
  strategy:
    type: RollingUpdate
    rollingUpdate: { maxUnavailable: 0, maxSurge: 1 }
  template:
    metadata:
      labels: { app: ${NAME} }
    spec:
      terminationGracePeriodSeconds: 35
      priorityClassName: high-priority
      dnsConfig:
        options: [ { name: ndots, value: "2" } ]
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: DoNotSchedule
          nodeTaintsPolicy: Honor
          minDomains: 2
          labelSelector: { matchLabels: { app: ${NAME} } }
      containers:
        - name: app
          image: ${IMAGE}
          ports: [ { containerPort: ${PORT} } ]
          readinessProbe:
            httpGet: { path: ${HPATH}, port: ${PORT} }
            periodSeconds: 5
            timeoutSeconds: 3
          resources:
            requests: { cpu: ${CPU}m, memory: ${MEM}Mi }
            limits: { memory: 256Mi }
---
apiVersion: v1
kind: Service
metadata:
  name: ${NAME}-svc
  namespace: ${NS}
spec:
  type: ClusterIP
  selector: { app: ${NAME} }
  ports: [ { protocol: TCP, port: ${PORT}, targetPort: ${PORT} } ]
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: ${NAME}-hpa
  namespace: ${NS}
spec:
  scaleTargetRef: { apiVersion: apps/v1, kind: Deployment, name: ${NAME} }
  minReplicas: ${HMIN}
  maxReplicas: ${HMAX}
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 0
      policies: [ { type: Percent, value: 100, periodSeconds: 15 } ]
    scaleDown:
      stabilizationWindowSeconds: 180
  metrics:
    - type: Resource
      resource:
        name: cpu
        target: { type: Utilization, averageUtilization: 33 }
---
apiVersion: elbv2.k8s.aws/v1beta1
kind: TargetGroupBinding
metadata:
  name: ${NAME}-tgb
  namespace: ${NS}
spec:
  serviceRef: { name: ${NAME}-svc, port: ${PORT} }
  targetGroupARN: $(tg_arn)
  targetType: ip
EOF
}

deploy() {
  preflight
  local lb lsn vpc tg prio
  lb=$(alb_arn); [ -n "$lb" ] && [ "$lb" != None ] || { echo "!! ALB ${PROJECT}-alb 를 못 찾았다"; exit 1; }
  lsn=$(listener_arn "$lb")
  vpc=$(aws elbv2 describe-load-balancers --region "$REGION" --load-balancer-arns "$lb" \
        --query 'LoadBalancers[0].VpcId' --output text)

  tg=$(tg_arn)
  if [ -z "$tg" ] || [ "$tg" = None ]; then
    echo "== TG 생성: $TG_NAME (기존 앱과 같은 헬스체크 설정)"
    tg=$(aws elbv2 create-target-group --region "$REGION" --name "$TG_NAME" \
      --protocol HTTP --port "$PORT" --vpc-id "$vpc" --target-type ip \
      --health-check-path "$HPATH" --health-check-interval-seconds 5 \
      --health-check-timeout-seconds 4 --healthy-threshold-count 2 \
      --unhealthy-threshold-count 3 \
      --query 'TargetGroups[0].TargetGroupArn' --output text) || exit 1
  else
    echo "== TG 재사용: $TG_NAME"
  fi

  if ! aws elbv2 describe-rules --region "$REGION" --listener-arn "$lsn" \
       --query 'Rules[].Actions[].TargetGroupArn' --output text | grep -q "$tg"; then
    prio=$(aws elbv2 describe-rules --region "$REGION" --listener-arn "$lsn" \
      --query 'Rules[?Priority!=`default`].Priority' --output text | tr '\t' '\n' | sort -n | tail -1)
    prio=$(( ${prio:-0} + 1 ))
    echo "== 리스너 규칙 생성: ${APP_PATH}* → $TG_NAME (priority $prio)"
    aws elbv2 create-rule --region "$REGION" --listener-arn "$lsn" --priority "$prio" \
      --conditions "Field=path-pattern,Values=${APP_PATH}*" \
      --actions "Type=forward,TargetGroupArn=$tg" >/dev/null || exit 1
  else
    echo "== 리스너 규칙 이미 있음"
  fi

  echo "== k8s 배포 (Deployment/Service/HPA/TGB)"
  k8s_manifest | kubectl apply -f - || exit 1
  kubectl -n "$NS" rollout status deploy/"$NAME" --timeout=180s
  status
  echo
  echo "다음: waf 반영 안 했으면 python3 spec.py --apply && terraform apply."
  echo "      ./GO.sh watch 가 돌고 있으면 그대로 둬라 — TG 이름으로 자동 편입된다."
}

remove() {
  local lb lsn tg rule
  echo "== k8s 회수"
  kubectl -n "$NS" delete tgb "${NAME}-tgb" hpa "${NAME}-hpa" svc "${NAME}-svc" deploy "$NAME" --ignore-not-found
  lb=$(alb_arn)
  if [ -n "$lb" ] && [ "$lb" != None ]; then
    lsn=$(listener_arn "$lb"); tg=$(tg_arn)
    if [ -n "$tg" ] && [ "$tg" != None ]; then
      rule=$(aws elbv2 describe-rules --region "$REGION" --listener-arn "$lsn" \
        --query "Rules[?Actions[?TargetGroupArn=='$tg']].RuleArn" --output text)
      [ -n "$rule" ] && { echo "== 리스너 규칙 삭제"; aws elbv2 delete-rule --region "$REGION" --rule-arn "$rule"; }
      echo "== TG 삭제: $TG_NAME"
      aws elbv2 delete-target-group --region "$REGION" --target-group-arn "$tg"
    fi
  fi
  echo "끝. spec.json 의 extra.enabled 를 false 로 되돌리고 python3 spec.py --apply 도 잊지 마라 (waf·튜너 원복)."
}

status() {
  echo "== 파드/HPA"
  kubectl -n "$NS" get deploy,hpa,pods -l app="$NAME" 2>/dev/null || true
  local tg; tg=$(tg_arn)
  if [ -n "$tg" ] && [ "$tg" != None ]; then
    echo "== TG 타깃 상태 ($TG_NAME)"
    aws elbv2 describe-target-health --region "$REGION" --target-group-arn "$tg" \
      --query 'TargetHealthDescriptions[].[Target.Id,TargetHealth.State]' --output text
  else
    echo "== TG 없음 (미배포)"
  fi
}

case "$CMD" in
  deploy) deploy ;;
  remove) remove ;;
  status) status ;;
  *) echo "사용법: ./extra-app.sh deploy|status|remove"; exit 1 ;;
esac
