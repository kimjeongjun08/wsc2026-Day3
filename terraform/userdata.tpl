#!/bin/bash
set -ex

sed -i 's/PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config
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
