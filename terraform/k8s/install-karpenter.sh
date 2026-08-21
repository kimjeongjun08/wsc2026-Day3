export KARPENTER_NAMESPACE="kube-system"
export KARPENTER_VERSION="1.8.6"
export K8S_VERSION="1.34"

export AWS_PARTITION="aws"
export CLUSTER_NAME="wsi2026-cluster"
export AWS_DEFAULT_REGION="ap-northeast-2"
export AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export TEMPOUT="$(mktemp)"

export ALIAS_VERSION="$(aws ssm get-parameter \
  --name "/aws/service/eks/optimized-ami/${K8S_VERSION}/amazon-linux-2023/x86_64/standard/recommended/image_id" \
  --query Parameter.Value \
  --output text | \
  xargs aws ec2 describe-images \
  --image-ids \
  --query 'Images[0].Name' \
  --output text | \
  sed -r 's/^.*(v[[:digit:]]+).*$/\1/')"

eksctl utils associate-iam-oidc-provider \
  --cluster "${CLUSTER_NAME}" \
  --region "${AWS_DEFAULT_REGION}" \
  --approve

curl -fsSL \
  https://raw.githubusercontent.com/aws/karpenter-provider-aws/v${KARPENTER_VERSION}/website/content/en/preview/getting-started/getting-started-with-karpenter/cloudformation.yaml \
  > "${TEMPOUT}"

aws cloudformation deploy \
  --stack-name "Karpenter-${CLUSTER_NAME}" \
  --template-file "${TEMPOUT}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "ClusterName=${CLUSTER_NAME}"

eksctl create iamserviceaccount \
  --cluster "${CLUSTER_NAME}" \
  --region "${AWS_DEFAULT_REGION}" \
  --namespace kube-system \
  --name karpenter \
  --role-name "KarpenterControllerRole-${CLUSTER_NAME}" \
  --attach-policy-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:policy/KarpenterControllerPolicy-${CLUSTER_NAME}" \
  --approve \
  --override-existing-serviceaccounts

helm registry logout public.ecr.aws || true

# ★Karpenter 에 CPU 상한을 걸지 않는다. 요청은 넉넉히 준다.
#   실측(2026-08-21 ambush, 노드 CPU 103%): karpenter 파드가 0/1 Ready 로
#   7번 재시작하며 죽어 있었다. 노드를 만드는 컨트롤러가, 노드가 필요한 바로
#   그 순간에 굶어 죽는다. 그동안 튜너는 매 주기 "3/iso 로 가자"고 결정했지만
#   노드는 하나도 안 생겼다 — 판단은 옳은데 실행이 불가능한 상태였다.
#
#   두 가지가 원인이다:
#     · CPU 요청이 실질적으로 없다시피 하면 CFS 가 최소 지분만 준다.
#     · limits.cpu 를 걸면 쿼터에 막혀 스로틀된다. 조정이 밀리고 프로브가 실패한다.
#   컨트롤 플레인 컴포넌트에는 CPU 상한을 걸지 않는 게 정석이다.
#   메모리 상한은 남긴다(누수 방어).
helm upgrade --install karpenter \
  oci://public.ecr.aws/karpenter/karpenter \
  --version "${KARPENTER_VERSION}" \
  --namespace "${KARPENTER_NAMESPACE}" \
  --create-namespace \
  --set serviceAccount.create=false \
  --set serviceAccount.name=karpenter \
  --set "settings.clusterName=${CLUSTER_NAME}" \
  --set "settings.interruptionQueue=${CLUSTER_NAME}" \
  --set replicas=1 \
  --set controller.resources.requests.cpu=300m \
  --set controller.resources.requests.memory=512Mi \
  --set controller.resources.limits.memory=1Gi

kubectl rollout restart deployment karpenter -n kube-system

kubectl wait \
  --for=condition=available \
  deployment/karpenter \
  -n kube-system \
  --timeout=300s