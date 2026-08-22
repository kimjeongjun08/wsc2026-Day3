#!/bin/bash
exec > /home/ec2-user/setup.log 2>&1
set -ex

export REGION="${region}"
export ACCOUNT_ID="${account_id}"
export DB_HOST="${db_host}"
export DB_PORT="${db_port}"
export DB_USER="${db_user}"
export DB_PASS="${db_pass}"
export DB_NAME="${db_name}"
export S3_BUCKET="${s3_bucket}"
export CLUSTER_NAME="${cluster_name}"
export VPC_ID="${vpc_id}"
export SETUP_BUCKET="${setup_bucket}"
export ECR_PREFIX="${ecr_prefix}"

# === MySQL dump를 백그라운드로 (RDS 준비되면 바로 로드) ===
# ★dump는 중요하지만 다른 작업(ECR, LBC, Karpenter)을 막으면 안 된다.
#   RDS가 아직 안 됐으면 until 루프에서 대기하다가 준비되면 즉시 로드.
(
until mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASS" -e "SELECT 1" 2>/dev/null; do sleep 3; done
mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASS" "$DB_NAME" <<'SQL'
CREATE TABLE IF NOT EXISTS user (
  id VARCHAR(255) NOT NULL,
  username VARCHAR(255) NOT NULL,
  email VARCHAR(255) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_username (username),
  KEY idx_email (email)
);
CREATE TABLE IF NOT EXISTS product (
  id VARCHAR(255) NOT NULL,
  name VARCHAR(255) NOT NULL,
  price FLOAT(8) NOT NULL,
  image_path VARCHAR(500) DEFAULT NULL,
  PRIMARY KEY (id)
);
SQL

aws s3 cp s3://$SETUP_BUCKET/load_user.dump /home/ec2-user/load_user.dump --region $REGION
ROW_COUNT=$(mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASS" "$DB_NAME" -sN -e "SELECT COUNT(*) FROM user" 2>/dev/null || echo "0")
if [ -s /home/ec2-user/load_user.dump ] && [ "$ROW_COUNT" = "0" ]; then
  mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASS" "$DB_NAME" < /home/ec2-user/load_user.dump
  echo "=== Dump loaded ==="
else
  echo "=== Dump skipped (already loaded: $ROW_COUNT rows) ==="
fi
echo "=== MySQL tables created ==="
) &
MYSQL_PID=$!

# ECR: login & build/push images
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com

for APP in user product stress; do
  mkdir -p /home/ec2-user/build-$APP
  cp /home/ec2-user/application/$APP/$APP /home/ec2-user/build-$APP/app

  cat > /home/ec2-user/build-$APP/Dockerfile <<'EOF'
FROM golang:alpine
COPY app app
RUN chmod +x app && apk add --no-cache curl libc6-compat
CMD ["./app"]
EOF

  docker build -t $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$ECR_PREFIX-$APP:latest /home/ec2-user/build-$APP/
  docker push $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$ECR_PREFIX-$APP:latest
done
echo "=== ECR images pushed ==="

# Wait for EKS nodes to be ready
aws eks update-kubeconfig --name $CLUSTER_NAME --region $REGION
echo "Waiting for nodes..."
until kubectl get nodes | grep -q " Ready"; do sleep 10; done
echo "=== Nodes ready ==="

# MNG 노드 인스턴스에 Name 태그 추가
MNG_INSTANCES=$(aws ec2 describe-instances \
  --filters "Name=tag:eks:nodegroup-name,Values=$ECR_PREFIX-ng" "Name=instance-state-name,Values=running" \
  --query "Reservations[].Instances[].InstanceId" --output text --region $REGION)
for IID in $MNG_INSTANCES; do
  aws ec2 create-tags --resources $IID --tags Key=Name,Value="$ECR_PREFIX-mng-node" --region $REGION
done

# === Install AWS Load Balancer Controller ===
eksctl utils associate-iam-oidc-provider --cluster $CLUSTER_NAME --region $REGION --approve

# ★멱등성 가드: 이전 클러스터 잔재(같은 이름의 IRSA 역할)로 SA 누락/신뢰정책 stale 방지.
#   클러스터 재생성 시 역할이 옛 OIDC를 신뢰해 STS 403(AssumeRoleWithWebIdentity)이 남 → SA 보장 +
#   신뢰정책을 현재 OIDC로 강제. (이게 없으면 재배포 시 Karpenter 컨트롤러가 역할 assume 실패 → rollout 타임아웃.)
OIDC_PROVIDER="oidc.eks.$REGION.amazonaws.com/id/$(aws eks describe-cluster --name $CLUSTER_NAME --region $REGION --query 'cluster.identity.oidc.issuer' --output text | sed 's|.*/||')"
ensure_irsa() {  # $1=role-name $2=namespace $3=sa-name
  kubectl get sa "$3" -n "$2" >/dev/null 2>&1 || kubectl create sa "$3" -n "$2"
  kubectl annotate sa "$3" -n "$2" eks.amazonaws.com/role-arn="arn:aws:iam::$ACCOUNT_ID:role/$1" --overwrite
  cat > /tmp/irsa-$3.json <<TRUST
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Federated":"arn:aws:iam::$ACCOUNT_ID:oidc-provider/$OIDC_PROVIDER"},"Action":"sts:AssumeRoleWithWebIdentity","Condition":{"StringEquals":{"$OIDC_PROVIDER:sub":"system:serviceaccount:$2:$3","$OIDC_PROVIDER:aud":"sts.amazonaws.com"}}}]}
TRUST
  # 역할이 있을 때만 trust 갱신(stale 케이스). 없으면 eksctl가 fresh로 만들 예정이라 skip → NoSuchEntity로 안 죽음.
  if aws iam get-role --role-name "$1" >/dev/null 2>&1; then
    aws iam update-assume-role-policy --role-name "$1" --policy-document file:///tmp/irsa-$3.json || true
  else
    echo "  (ensure_irsa: 역할 $1 아직 없음 — eksctl가 생성 예정, trust 갱신 skip)"
  fi
}

# ★create-policy 는 실패를 삼키면 안 된다.
#   예전엔 `2>/dev/null || true` 였다. 실측 사고(2026-08-21): 이 명령이 조용히 실패해
#   정책이 아예 안 만들어졌고, eksctl 은 없는 ARN 을 붙이려다 실패했는데 그것도
#   넘어갔다. 결과: AmazonEKSLoadBalancerControllerRole 에 정책이 하나도 없었다.
#   그러면 컨트롤러가 DescribeTargetHealth 에서 403 을 맞고 TargetGroupBinding
#   조정 전체를 포기한다 → 파드가 노드를 옮겨도 타깃이 갱신되지 않는다.
#   증상은 엉뚱하게 나타난다: 파드는 전부 Running·healthcheck 200 인데
#   ALB 타깃이 전부 unhealthy 고 사용자는 502 를 받는다.
#   노드가 흔들리는 회차(=오토스케일링을 하는 모든 회차)에서 가용성 12점이 통째로 날아간다.
LBC_POLICY_ARN="arn:aws:iam::$ACCOUNT_ID:policy/AWSLoadBalancerControllerIAMPolicy"
if aws iam create-policy \
     --policy-name AWSLoadBalancerControllerIAMPolicy \
     --policy-document file:///home/ec2-user/k8s/iam_policy.json >/dev/null 2>&1; then
  echo "  LBC 정책 생성됨"
else
  # 이미 있으면 새 버전을 올려 기본으로 만든다(버전은 5개까지라 옛것부터 지운다).
  if aws iam get-policy --policy-arn "$LBC_POLICY_ARN" >/dev/null 2>&1; then
    for v in $(aws iam list-policy-versions --policy-arn "$LBC_POLICY_ARN" \
               --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text); do
      aws iam delete-policy-version --policy-arn "$LBC_POLICY_ARN" --version-id "$v" >/dev/null 2>&1
    done
    aws iam create-policy-version --policy-arn "$LBC_POLICY_ARN" \
      --policy-document file:///home/ec2-user/k8s/iam_policy.json --set-as-default >/dev/null \
      && echo "  LBC 정책 최신 버전으로 갱신됨"
  else
    echo "  !! LBC 정책을 만들지도 찾지도 못했다 — 이대로면 ALB 타깃이 갱신되지 않는다" >&2
  fi
fi

eksctl create iamserviceaccount \
  --cluster=$CLUSTER_NAME --region=$REGION \
  --namespace=kube-system \
  --name=aws-load-balancer-controller \
  --role-name AmazonEKSLoadBalancerControllerRole \
  --attach-policy-arn=arn:aws:iam::$ACCOUNT_ID:policy/AWSLoadBalancerControllerIAMPolicy \
  --approve --override-existing-serviceaccounts

# ★역할에 정책이 실제로 붙었는지 확인한다. eksctl 이 조용히 넘어가는 경우가 있다.
if ! aws iam list-attached-role-policies --role-name AmazonEKSLoadBalancerControllerRole \
     --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null | grep -q AWSLoadBalancerControllerIAMPolicy; then
  echo "  LBC 역할에 정책이 없다 — 직접 붙인다"
  aws iam attach-role-policy --role-name AmazonEKSLoadBalancerControllerRole \
    --policy-arn "$LBC_POLICY_ARN" || echo "  !! 정책 부착 실패" >&2
fi

# SA가 안 만들어졌을 경우 대비
kubectl get sa aws-load-balancer-controller -n kube-system 2>/dev/null || \
  kubectl create sa aws-load-balancer-controller -n kube-system
kubectl annotate sa aws-load-balancer-controller -n kube-system \
  eks.amazonaws.com/role-arn=arn:aws:iam::$ACCOUNT_ID:role/AmazonEKSLoadBalancerControllerRole \
  --overwrite
ensure_irsa AmazonEKSLoadBalancerControllerRole kube-system aws-load-balancer-controller  # ★신뢰정책 현재 OIDC로 강제(고정이름 역할=잔존 위험)

helm repo add eks https://aws.github.io/eks-charts
helm repo update eks
# ★LB 컨트롤러에도 CPU 요청을 준다(상한은 안 건다).
#   이 컨트롤러가 멈추면 파드가 노드를 옮겨도 ALB 타깃이 갱신되지 않는다.
#   실측(2026-08-21): 요청이 없어 과부하에서 굶었고, 별개로 IAM 정책 누락까지
#   겹치면서 파드는 전부 Running·healthcheck 200 인데 ALB 는 502 를 반환했다.
#   가용성 12점이 통째로 걸린 컴포넌트다. 앱과 CPU 를 두고 경쟁하게 두면 안 된다.
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system \
  --set clusterName=$CLUSTER_NAME \
  --set serviceAccount.create=false \
  --set serviceAccount.name=aws-load-balancer-controller \
  --set region=$REGION \
  --set vpcId=$VPC_ID \
  --set replicaCount=2 \
  --set resources.requests.cpu=150m \
  --set resources.requests.memory=256Mi \
  --set resources.limits.memory=512Mi

kubectl rollout status deploy/aws-load-balancer-controller -n kube-system --timeout=120s
echo "=== LBC installed ==="

# === Install Karpenter ===
export KARPENTER_VERSION="1.8.6"

TEMPOUT="/home/ec2-user/karpenter-cfn.yaml"
curl -fsSL "https://raw.githubusercontent.com/aws/karpenter-provider-aws/v$KARPENTER_VERSION/website/content/en/preview/getting-started/getting-started-with-karpenter/cloudformation.yaml" > "$TEMPOUT"

aws cloudformation deploy \
  --stack-name "Karpenter-$CLUSTER_NAME" \
  --template-file "$TEMPOUT" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "ClusterName=$CLUSTER_NAME" \
  --region $REGION

# Karpenter 노드가 EKS 클러스터에 조인할 수 있도록 access entry 추가
aws eks create-access-entry --cluster-name $CLUSTER_NAME \
  --principal-arn "arn:aws:iam::$ACCOUNT_ID:role/KarpenterNodeRole-$CLUSTER_NAME" \
  --type EC2_LINUX --region $REGION 2>/dev/null || true

# Karpenter 노드에서 S3 등 접근 가능하도록 AdminAccess 추가
aws iam attach-role-policy \
  --role-name "KarpenterNodeRole-$CLUSTER_NAME" \
  --policy-arn "arn:aws:iam::aws:policy/AdministratorAccess" 2>/dev/null || true

eksctl create iamserviceaccount \
  --cluster $CLUSTER_NAME --region $REGION \
  --namespace kube-system --name karpenter \
  --role-name "KarpenterControllerRole-$CLUSTER_NAME" \
  --attach-policy-arn "arn:aws:iam::$ACCOUNT_ID:policy/KarpenterControllerPolicy-$CLUSTER_NAME" \
  --approve --override-existing-serviceaccounts

# ★역할에 정책이 실제로 붙었는지 확인한다. eksctl 이 조용히 넘어가는 경우가 있다.
if ! aws iam list-attached-role-policies --role-name AmazonEKSLoadBalancerControllerRole \
     --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null | grep -q AWSLoadBalancerControllerIAMPolicy; then
  echo "  LBC 역할에 정책이 없다 — 직접 붙인다"
  aws iam attach-role-policy --role-name AmazonEKSLoadBalancerControllerRole \
    --policy-arn "$LBC_POLICY_ARN" || echo "  !! 정책 부착 실패" >&2
fi
ensure_irsa KarpenterControllerRole-$CLUSTER_NAME kube-system karpenter  # ★SA 보장(eksctl 스킵 대비) + 신뢰정책 현재 OIDC로 강제 → rollout 타임아웃 방지

helm registry logout public.ecr.aws || true
helm upgrade --install karpenter oci://public.ecr.aws/karpenter/karpenter \
  --version "$KARPENTER_VERSION" \
  --namespace kube-system --create-namespace \
  --set serviceAccount.create=false \
  --set serviceAccount.name=karpenter \
  --set "settings.clusterName=$CLUSTER_NAME" \
  --set "settings.interruptionQueue=$CLUSTER_NAME" \
  --set replicas=1

kubectl rollout status deploy/karpenter -n kube-system --timeout=300s
echo "=== Karpenter installed ==="

# === VPC CNI Prefix Delegation ===
#   ★파드 수 상한을 푸는 설정. 이게 없으면 노드당 파드 수가 ENI 개수로 제한된다
#     (t3.medium 17파드). 앱 request가 70m이면 CPU상 18파드가 들어가는데 ENI에서 먼저
#     막히고, 시스템 파드(coredns/metrics-server/LBC/karpenter 등 6~8개)를 빼면
#     앱은 9~11파드에서 멈춘다 → 나머지가 Pending → 카펜터가 노드를 만든다.
#     즉 CPU는 남는데 노드만 늘어난다(비용 손실 + 부팅 60초 성능 손실).
#   ★Prefix Delegation을 켜면 ENI 하나가 /28 프리픽스(16 IP)를 받아 상한이 크게 올라간다.
#     kubelet 쪽 maxPods도 함께 올려야 실제로 쓸 수 있다
#     (Karpenter 노드는 EC2NodeClass, MNG 노드는 launch template에서 설정).
#   ★NodePool 적용(=첫 카펜터 노드 생성)과 앱 배포보다 앞에 둔다 — 그 뒤에 뜨는 노드부터
#     프리픽스를 할당받는다.
kubectl -n kube-system set env ds/aws-node ENABLE_PREFIX_DELEGATION=true
kubectl -n kube-system set env ds/aws-node WARM_PREFIX_TARGET=1
# ★WARM_ENI_TARGET 은 반드시 지운다. WARM_PREFIX_TARGET 과 같이 있으면 prefix 할당이 동작하지 않는다.
#   실측: 둘 다 있으면 신규 노드 ENI 에 Ipv4Prefixes 가 하나도 안 붙어 파드가 IP 를 못 받는다.
kubectl -n kube-system set env ds/aws-node WARM_ENI_TARGET-
kubectl -n kube-system rollout status ds/aws-node --timeout=180s
echo "=== VPC CNI Prefix Delegation enabled ==="

kubectl apply -f /home/ec2-user/k8s/karpenter.yaml
echo "=== Karpenter NodePool applied ==="

# === Deploy applications ===
kubectl create namespace apdev --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f /home/ec2-user/k8s/priorityclass.yaml

# ★configmap을 직접 생성 (Proxy 주소를 런타임에 조회).
#   terraform에서 S3에 올리면 Proxy 생성을 기다려야 해서 bastion 시작이 늦어진다.
#   여기서 직접 만들면 "Proxy가 준비될 때까지 대기 → 준비되면 즉시 configmap 생성".
echo "Waiting for RDS Proxy..."
PROXY_HOST=""
until [ -n "$PROXY_HOST" ]; do
  PROXY_HOST=$(aws rds describe-db-proxies --region $REGION \
    --query "DBProxies[?DBProxyName=='${ECR_PREFIX}-proxy'].Endpoint" --output text 2>/dev/null)
  [ "$PROXY_HOST" = "None" ] && PROXY_HOST=""
  [ -z "$PROXY_HOST" ] && sleep 5
done
echo "RDS Proxy ready: $PROXY_HOST"

cat <<CFGEOF | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
  namespace: apdev
data:
  AWS_REGION: "$REGION"
  MYSQL_DBNAME: "$DB_NAME"
  MYSQL_HOST: "$PROXY_HOST"
  MYSQL_PASSWORD: "$DB_PASS"
  MYSQL_PORT: "$DB_PORT"
  MYSQL_USER: "$DB_USER"
  S3_BUCKET: "$S3_BUCKET"
CFGEOF
echo "=== ConfigMap created (proxy: $PROXY_HOST) ==="
# ★deploy.yaml의 이미지 플레이스홀더(ACCOUNT_ID/REGION/PROJECT)는 terraform이 S3에
#   업로드할 때 이미 실제 값으로 치환한다(ec2.tf의 aws_s3_object.k8s_deploy).
#   그래서 여기서 별도 치환을 하지 않는다 — 이중 치환은 매칭 실패만 만든다.
#   ★로컬 파일을 직접 kubectl apply 하면 플레이스홀더가 그대로 들어가
#     파드가 InvalidImageName으로 실패한다(실측). 반드시 S3에서 받은 파일을 쓴다.
kubectl apply -f /home/ec2-user/k8s/deploy.yaml

# ★LBC webhook 준비 대기 (newtech 미러) — service/tgb가 LBC webhook을 호출. 웹훅 인증서 + IRSA(역할
#   assume) 전파를 기다려야 STS 403 안 남. rollout + webhook endpoint ready 대기.
kubectl -n kube-system rollout status deploy/aws-load-balancer-controller --timeout=120s
for i in $(seq 1 12); do
  if kubectl get endpoints -n kube-system aws-load-balancer-webhook-service -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null | grep -q .; then
    echo "LBC webhook ready"; break
  fi
  echo "LBC webhook 대기 중... ($i/12)"; sleep 5
done

# service.yaml — webhook 인증서/IRSA 전파 지연 대비 retry
for i in $(seq 1 6); do
  if kubectl apply -f /home/ec2-user/k8s/service.yaml; then
    echo "service.yaml applied"; break
  fi
  echo "service.yaml apply 재시도 ($i/6) — webhook/IRSA 대기"; sleep 10
done

kubectl apply -f /home/ec2-user/k8s/hpa.yaml
kubectl apply -f /home/ec2-user/k8s/pdb.yaml
# ★자리표시 파드. replicas 0 으로 시작하고 튜너가 필요할 때만 올린다.
#   부하가 없으면 Karpenter 는 노드를 안 만든다(Pending 파드가 없으니까).
#   그때 "N대로 만들라"는 지시를 실행하는 유일한 수단이다.
kubectl apply -f /home/ec2-user/k8s/overprovisioning.yaml
# overprovisioning(pause)은 prewarm.py가 실행 시 자기완결형으로 직접 apply → 여기서 파일 apply 안 함(파일 없음).

# Wait for deployments to be ready
kubectl rollout status deploy/user -n apdev --timeout=180s
kubectl rollout status deploy/product -n apdev --timeout=180s
kubectl rollout status deploy/stress -n apdev --timeout=180s

# Apply TargetGroupBindings (LBC registers pod IPs to ALB TGs)
# ★webhook(mtargetgroupbinding)이 IRSA로 DescribeTargetGroups 호출 → 신뢰정책 전파 지연 시 STS 403.
#   retry로 흡수(최대 120s). 실제로 403 났던 지점이라 방어 강화.
for i in $(seq 1 12); do
  if kubectl apply -f /home/ec2-user/k8s/tgb.yaml; then
    echo "tgb.yaml applied"; break
  fi
  echo "tgb.yaml apply 재시도 ($i/12) — LBC IRSA(신뢰정책) 전파 대기"; sleep 10
done
echo "=== TGB applied - pods registered to ALB ==="

# MySQL dump가 끝날 때까지 대기 (아직 안 끝났으면)
echo "Waiting for MySQL dump to complete..."
wait $MYSQL_PID 2>/dev/null || true
echo "=== MySQL dump confirmed ==="

echo "=== SETUP COMPLETE ==="

# setup 버킷 아티팩트 삭제 (images 버킷은 유지)
aws s3 rm s3://$SETUP_BUCKET/ --recursive --region $REGION 2>/dev/null || true
echo "=== Setup artifacts cleaned ==="
