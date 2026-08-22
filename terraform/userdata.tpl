#!/bin/bash
set -ex

# SSH 비밀번호 인증 활성화 (AL2023 호환)
sed -i 's/PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config
sed -i 's/^#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
# AL2023은 sshd_config.d 에서 오버라이드할 수 있으므로 거기도 설정
mkdir -p /etc/ssh/sshd_config.d
echo 'PasswordAuthentication yes' > /etc/ssh/sshd_config.d/99-password.conf
systemctl restart sshd
echo 'Skill53##' | passwd --stdin ec2-user

# Install tools
curl -O https://s3.us-west-2.amazonaws.com/amazon-eks/1.33.0/2025-05-01/bin/linux/amd64/kubectl
chmod +x ./kubectl && mv ./kubectl /usr/bin/
ln -s /usr/bin/kubectl /usr/local/bin/k 2>/dev/null || true

curl --silent --location "https://github.com/weaveworks/eksctl/releases/latest/download/eksctl_$(uname -s)_amd64.tar.gz" | tar xz -C /tmp
mv /tmp/eksctl /usr/bin/

curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

dnf install -y docker mariadb105
systemctl enable --now docker
usermod -aG docker ec2-user

# Write setup.sh
cat > /home/ec2-user/setup.sh <<'SETUPEOF'
${setup_script}
SETUPEOF
chmod +x /home/ec2-user/setup.sh
chown ec2-user:ec2-user /home/ec2-user/setup.sh

# Download k8s manifests from S3
mkdir -p /home/ec2-user/k8s
aws s3 cp s3://${artifacts_bucket}/k8s/ /home/ec2-user/k8s/ --recursive --region ${region}
chown -R ec2-user:ec2-user /home/ec2-user/k8s

# Download application binaries from S3
mkdir -p /home/ec2-user/application/{user,product,stress}
for APP in user product stress; do
  aws s3 cp s3://${artifacts_bucket}/application/$APP/$APP /home/ec2-user/application/$APP/$APP --region ${region}
  chmod +x /home/ec2-user/application/$APP/$APP
done
chown -R ec2-user:ec2-user /home/ec2-user/application

# ★setup.sh 를 실제로 실행한다. 원본은 파일만 써두고 실행하지 않아 apply 후 아무것도 배포되지 않았다.
#   · systemd-run 으로 cloud-init 세션에서 분리한다. 백그라운드(&)로 띄우면 cloud-init 이
#     끝날 때 프로세스 그룹째 정리되어 로그만 남고 중간에 죽는다.
#   · HOME=/root 를 반드시 준다. 비어 있으면 kubectl 이 /root/.kube/config 를 못 찾아
#     localhost:8080 으로 붙고, setup.sh 의 "노드 Ready 대기" 루프에서 영원히 멈춘다.
#   · 진행 로그는 /home/ec2-user/setup.log (setup.sh 가 스스로 리다이렉트한다).
systemd-run --unit=apdev-setup --setenv=HOME=/root /bin/bash /home/ec2-user/setup.sh
