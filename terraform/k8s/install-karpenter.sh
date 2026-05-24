#!/bin/bash
# Karpenter 설치 스크립트
CLUSTER_NAME=apdev-eks-cluster
REGION=ap-northeast-2
ACCOUNT_ID=$(aws sts get-caller-identity --query "Account" --output text)
KARPENTER_VERSION=1.1.0

# Karpenter IAM Role (IRSA)
eksctl create iamserviceaccount \
  --cluster=$CLUSTER_NAME \
  --namespace=kube-system \
  --name=karpenter \
  --role-name=KarpenterControllerRole-$CLUSTER_NAME \
  --attach-policy-arn=arn:aws:iam::$ACCOUNT_ID:policy/KarpenterControllerPolicy \
  --approve \
  --region=$REGION 2>/dev/null || true

# Karpenter Controller Policy (필요 시)
cat > /tmp/karpenter-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:CreateLaunchTemplate", "ec2:CreateFleet", "ec2:RunInstances",
        "ec2:CreateTags", "ec2:TerminateInstances", "ec2:DeleteLaunchTemplate",
        "ec2:DescribeInstances", "ec2:DescribeSecurityGroups", "ec2:DescribeSubnets",
        "ec2:DescribeLaunchTemplates", "ec2:DescribeInstanceTypes",
        "ec2:DescribeInstanceTypeOfferings", "ec2:DescribeAvailabilityZones",
        "ec2:DescribeImages", "ec2:DescribeSpotPriceHistory",
        "ssm:GetParameter", "pricing:GetProducts",
        "iam:PassRole", "eks:DescribeCluster",
        "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile",
        "iam:GetInstanceProfile", "iam:AddRoleToInstanceProfile",
        "iam:RemoveRoleFromInstanceProfile", "iam:TagInstanceProfile",
        "sqs:*"
      ],
      "Resource": "*"
    }
  ]
}
EOF

aws iam create-policy \
  --policy-name KarpenterControllerPolicy \
  --policy-document file:///tmp/karpenter-policy.json 2>/dev/null || true

# Helm으로 Karpenter 설치
helm registry logout public.ecr.aws 2>/dev/null || true
helm upgrade --install karpenter oci://public.ecr.aws/karpenter/karpenter \
  --version $KARPENTER_VERSION \
  --namespace kube-system \
  --set "settings.clusterName=$CLUSTER_NAME" \
  --set "settings.interruptionQueue=Karpenter-$CLUSTER_NAME" \
  --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="arn:aws:iam::${ACCOUNT_ID}:role/KarpenterControllerRole-$CLUSTER_NAME" \
  --wait

# NodePool + EC2NodeClass 적용
kubectl apply -f karpenter.yaml

echo "=== Karpenter installed ==="
kubectl get nodepools
kubectl get ec2nodeclasses
