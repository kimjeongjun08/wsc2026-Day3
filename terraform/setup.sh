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

# === MySQL + ECR in parallel while waiting for EKS nodes ===

# MySQL: create tables + load dump
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

# dump 로드
aws s3 cp s3://$SETUP_BUCKET/load_user.dump /home/ec2-user/load_user.dump --region $REGION
if [ -s /home/ec2-user/load_user.dump ]; then
  mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASS" "$DB_NAME" < /home/ec2-user/load_user.dump
  echo "=== Dump loaded ==="
fi
echo "=== MySQL tables created ==="

# ECR: login & build/push images
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com

for APP in user product stress; do
  mkdir -p /home/ec2-user/build-$APP
  cp /home/ec2-user/application/$APP/$APP /home/ec2-user/build-$APP/app

  cat > /home/ec2-user/build-$APP/Dockerfile <<'EOF'
FROM alpine:3.20
COPY app app
RUN chmod +x app && apk add --no-cache curl libc6-compat ca-certificates
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

# === Install AWS Load Balancer Controller ===
eksctl utils associate-iam-oidc-provider --cluster $CLUSTER_NAME --region $REGION --approve

aws iam create-policy \
  --policy-name AWSLoadBalancerControllerIAMPolicy \
  --policy-document file:///home/ec2-user/k8s/iam_policy.json 2>/dev/null || true

eksctl create iamserviceaccount \
  --cluster=$CLUSTER_NAME --region=$REGION \
  --namespace=kube-system \
  --name=aws-load-balancer-controller \
  --role-name AmazonEKSLoadBalancerControllerRole \
  --attach-policy-arn=arn:aws:iam::$ACCOUNT_ID:policy/AWSLoadBalancerControllerIAMPolicy \
  --approve --override-existing-serviceaccounts

# SA가 안 만들어졌을 경우 대비
kubectl get sa aws-load-balancer-controller -n kube-system 2>/dev/null || \
  kubectl create sa aws-load-balancer-controller -n kube-system
kubectl annotate sa aws-load-balancer-controller -n kube-system \
  eks.amazonaws.com/role-arn=arn:aws:iam::$ACCOUNT_ID:role/AmazonEKSLoadBalancerControllerRole \
  --overwrite

helm repo add eks https://aws.github.io/eks-charts
helm repo update eks
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system \
  --set clusterName=$CLUSTER_NAME \
  --set serviceAccount.create=false \
  --set serviceAccount.name=aws-load-balancer-controller \
  --set region=$REGION \
  --set vpcId=$VPC_ID

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

eksctl create iamserviceaccount \
  --cluster $CLUSTER_NAME --region $REGION \
  --namespace kube-system --name karpenter \
  --role-name "KarpenterControllerRole-$CLUSTER_NAME" \
  --attach-policy-arn "arn:aws:iam::$ACCOUNT_ID:policy/KarpenterControllerPolicy-$CLUSTER_NAME" \
  --approve --override-existing-serviceaccounts

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

kubectl apply -f /home/ec2-user/k8s/karpenter.yaml
echo "=== Karpenter NodePool applied ==="

# === Deploy applications ===
kubectl create namespace apdev --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f /home/ec2-user/k8s/configmap.yaml
kubectl apply -f /home/ec2-user/k8s/deploy.yaml
kubectl apply -f /home/ec2-user/k8s/service.yaml
kubectl apply -f /home/ec2-user/k8s/hpa.yaml
kubectl apply -f /home/ec2-user/k8s/pdb.yaml

# Wait for deployments to be ready
kubectl rollout status deploy/user -n apdev --timeout=180s
kubectl rollout status deploy/product -n apdev --timeout=180s
kubectl rollout status deploy/stress -n apdev --timeout=180s

# Apply TargetGroupBindings (LBC registers pod IPs to ALB TGs)
kubectl apply -f /home/ec2-user/k8s/tgb.yaml
echo "=== TGB applied - pods registered to ALB ==="

echo "=== SETUP COMPLETE ==="

# setup 버킷 아티팩트 삭제 (images 버킷은 유지)
aws s3 rm s3://$SETUP_BUCKET/ --recursive --region $REGION 2>/dev/null || true
echo "=== Setup artifacts cleaned ==="
