#!/bin/bash
# 대회 당일 인스턴스 타입 변경 스크립트
# 사용: ./change-instance.sh t3.large

NEW_TYPE="${1:?usage: ./change-instance.sh <instance-type>}"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Changing instance type to: $NEW_TYPE"

# eksctl.yaml
sed -i "s/instanceType: .*/instanceType: $NEW_TYPE  # ← 대회 당일 변경/" "$DIR/eksctl.yaml"

# karpenter.yaml
sed -i "s/values: \[\".*\"\]  # ← 대회 당일 변경/values: [\"$NEW_TYPE\"]  # ← 대회 당일 변경/" "$DIR/k8s/karpenter.yaml"

echo "Done. Changed:"
grep -n "instanceType\|instance-type" "$DIR/eksctl.yaml" "$DIR/k8s/karpenter.yaml"
echo ""
echo "Next: eksctl upgrade nodegroup --cluster=apdev-eks-cluster --name=apdev-ng --instance-types=$NEW_TYPE"
