#!/bin/bash
curl -O https://s3.us-west-2.amazonaws.com/amazon-eks/1.33.0/2025-05-01/bin/linux/amd64/kubectl
chmod +x ./kubectl
sudo mv ./kubectl /usr/bin/
sudo ln -s /usr/bin/kubectl /usr/local/bin/k
k version --client
curl --silent --location "https://github.com/weaveworks/eksctl/releases/latest/download/eksctl_$(uname -s)_amd64.tar.gz" | tar xz -C /tmp
sudo mv /tmp/eksctl /usr/bin/
eksctl version
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
sudo su - ec2-user
CLUSTER_NAME=wsi2026-cluster
REGION=ap-northeast-2
ACCOUNT_ID=$(aws sts get-caller-identity --query "Account" --output text)
eksctl utils associate-iam-oidc-provider --cluster wsi2026-cluster --approve --region ap-northeast-2
curl -O https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v2.13.0/docs/install/iam_policy.json
aws iam create-policy \
    --policy-name AWSLoadBalancerControllerIAMPolicy \
    --policy-document file://iam_policy.json
eksctl create iamserviceaccount \
  --cluster=wsi2026-cluster \
  --namespace=kube-system \
  --name=aws-load-balancer-controller \
  --role-name AmazonEKSLoadBalancerControllerRole \
  --attach-policy-arn=arn:aws:iam::003150130236:policy/AWSLoadBalancerControllerIAMPolicy \
  --approve \
  --region=ap-northeast-2
helm repo add eks https://aws.github.io/eks-charts
helm repo update
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller -n kube-system --set clusterName=wsi2026-cluster --set serviceAccount.create=false --set serviceAccount.name=aws-load-balancer-controller --set region=ap-northeast-2