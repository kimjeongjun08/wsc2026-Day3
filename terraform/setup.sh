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
export SUBNET_IDS="${subnet_ids}"
export ALB_SG_ID="${alb_sg_id}"
export TG_USER_ARN="${tg_user_arn}"
export TG_PRODUCT_ARN="${tg_product_arn}"
export TG_STRESS_ARN="${tg_stress_arn}"
export ECR_PREFIX="${ecr_prefix}"

# === EKS creation in background ===
eksctl create cluster -f /home/ec2-user/k8s/eksctl.yaml > /home/ec2-user/eks.log 2>&1 &
EKS_PID=$!
echo "EKS creating in background (PID: $EKS_PID)"

# === While EKS is creating, do MySQL + ECR in parallel ===

# MySQL: create tables
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
echo "=== MySQL tables created ==="

# ECR: login & build/push images
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com

for APP in user product stress; do
  mkdir -p /tmp/build-$APP
  cp /home/ec2-user/application/$APP/$APP /tmp/build-$APP/app

  cat > /tmp/build-$APP/Dockerfile <<'EOF'
FROM golang:alpine
COPY app app
RUN chmod +x app && apk add --no-cache curl libc6-compat
CMD ["./app"]
EOF

  docker build -t $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$ECR_PREFIX-$APP:latest /tmp/build-$APP/
  docker push $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$ECR_PREFIX-$APP:latest
done
echo "=== ECR images pushed ==="

# === Wait for EKS to finish ===
echo "Waiting for EKS cluster..."
while true; do
  STATUS=$(aws eks describe-cluster --name $CLUSTER_NAME --region $REGION --query "cluster.status" --output text 2>/dev/null || echo "NOT_FOUND")
  if [ "$STATUS" = "ACTIVE" ]; then break; fi
  sleep 15
done
echo "=== EKS cluster ready ==="

# Configure kubectl
aws eks update-kubeconfig --name $CLUSTER_NAME --region $REGION

# Open NodePort range on EKS node SG from ALB SG
NODE_SG=$(aws ec2 describe-security-groups --region $REGION \
  --filters "Name=tag:aws:eks:cluster-name,Values=$CLUSTER_NAME" "Name=tag:kubernetes.io/cluster/$CLUSTER_NAME,Values=owned" \
  --query "SecurityGroups[0].GroupId" --output text 2>/dev/null || true)

if [ -z "$NODE_SG" ] || [ "$NODE_SG" = "None" ]; then
  NODE_SG=$(aws ec2 describe-security-groups --region $REGION \
    --filters "Name=tag:kubernetes.io/cluster/$CLUSTER_NAME,Values=owned" \
    --query "SecurityGroups[?contains(GroupName,'node') || contains(GroupName,'nodegroup')].GroupId" --output text | head -1)
fi

if [ -z "$NODE_SG" ] || [ "$NODE_SG" = "None" ]; then
  NODE_SG=$(aws eks describe-cluster --name $CLUSTER_NAME --region $REGION \
    --query "cluster.resourcesVpcConfig.clusterSecurityGroupId" --output text)
fi

aws ec2 authorize-security-group-ingress --region $REGION \
  --group-id "$NODE_SG" \
  --protocol tcp --port 30001-30003 \
  --source-group "$ALB_SG_ID" 2>/dev/null || true
echo "=== NodePort SG rule added ==="

# === Install Karpenter ===
export KARPENTER_VERSION="1.8.6"

eksctl utils associate-iam-oidc-provider --cluster $CLUSTER_NAME --region $REGION --approve

TEMPOUT="$(mktemp)"
curl -fsSL "https://raw.githubusercontent.com/aws/karpenter-provider-aws/v$KARPENTER_VERSION/website/content/en/preview/getting-started/getting-started-with-karpenter/cloudformation.yaml" > "$TEMPOUT"

aws cloudformation deploy \
  --stack-name "Karpenter-$CLUSTER_NAME" \
  --template-file "$TEMPOUT" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "ClusterName=$CLUSTER_NAME" \
  --region $REGION

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

# Apply Karpenter NodePool + EC2NodeClass
kubectl apply -f /home/ec2-user/k8s/karpenter.yaml
echo "=== Karpenter NodePool applied ==="

# Apply k8s manifests
kubectl create namespace apdev --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f /home/ec2-user/k8s/configmap.yaml
kubectl apply -f /home/ec2-user/k8s/deploy.yaml
kubectl apply -f /home/ec2-user/k8s/service.yaml
kubectl apply -f /home/ec2-user/k8s/hpa.yaml
kubectl apply -f /home/ec2-user/k8s/pdb.yaml

# Wait for deployments to be ready
kubectl rollout status deploy/user -n apdev --timeout=120s
kubectl rollout status deploy/product -n apdev --timeout=120s
kubectl rollout status deploy/stress -n apdev --timeout=120s

# Register EKS node instances to ALB target groups
INSTANCE_IDS=$(aws ec2 describe-instances --region $REGION \
  --filters "Name=tag:kubernetes.io/cluster/$CLUSTER_NAME,Values=owned" "Name=instance-state-name,Values=running" \
  --query "Reservations[].Instances[].InstanceId" --output text)

for ID in $INSTANCE_IDS; do
  aws elbv2 register-targets --region $REGION --target-group-arn "$TG_USER_ARN" --targets Id=$ID
  aws elbv2 register-targets --region $REGION --target-group-arn "$TG_PRODUCT_ARN" --targets Id=$ID
  aws elbv2 register-targets --region $REGION --target-group-arn "$TG_STRESS_ARN" --targets Id=$ID
done
echo "=== Targets registered to ALB ==="

echo "=== SETUP COMPLETE ==="
