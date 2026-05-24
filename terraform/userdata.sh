#!/bin/bash
set -ex

REGION="${region}"
ACCOUNT_ID="${account_id}"
DB_HOST="${db_host}"
DB_USER="${db_user}"
DB_PASSWORD="${db_password}"
DB_NAME="${db_name}"
ARTIFACTS_BUCKET="apdev-artifacts-$ACCOUNT_ID"

# Install tools
dnf install -y docker mariadb105
systemctl enable --now docker
usermod -aG docker ec2-user

# Wait for RDS Proxy and create tables
for i in $(seq 1 60); do
  mysql -h "$DB_HOST" -u "$DB_USER" -p"$DB_PASSWORD" -e "SELECT 1" 2>/dev/null && break
  sleep 10
done

mysql -h "$DB_HOST" -u "$DB_USER" -p"$DB_PASSWORD" "$DB_NAME" <<'EOF'
CREATE TABLE IF NOT EXISTS user (
    id               VARCHAR(255)    NOT NULL,
    username         VARCHAR(255)    NOT NULL,
    email            VARCHAR(255)    NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_username (username),
    INDEX idx_user_email (email)
);
CREATE TABLE IF NOT EXISTS product (
    id               VARCHAR(255)    NOT NULL,
    name             VARCHAR(255)    NOT NULL,
    price            FLOAT(8)        NOT NULL,
    image_path       VARCHAR(500)    DEFAULT NULL,
    PRIMARY KEY (id),
    INDEX idx_product_name (name)
);
EOF

# Download app binaries from S3
mkdir -p /opt/application/{user,product,stress}
aws s3 cp s3://$ARTIFACTS_BUCKET/apps/user /opt/application/user/user
aws s3 cp s3://$ARTIFACTS_BUCKET/apps/product /opt/application/product/product
aws s3 cp s3://$ARTIFACTS_BUCKET/apps/stress /opt/application/stress/stress
chmod +x /opt/application/user/user /opt/application/product/product /opt/application/stress/stress

# ECR login
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com

# Build and push user
cat > /opt/application/user/Dockerfile <<'DOCKERFILE'
FROM public.ecr.aws/docker/library/alpine:latest
COPY user /app
RUN chmod +x /app && apk add --no-cache libc6-compat
ENTRYPOINT ["/app"]
DOCKERFILE
docker build -t $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/apdev-user:latest /opt/application/user/
docker push $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/apdev-user:latest

# Build and push product
cat > /opt/application/product/Dockerfile <<'DOCKERFILE'
FROM public.ecr.aws/docker/library/alpine:latest
COPY product /app
RUN chmod +x /app && apk add --no-cache libc6-compat
ENTRYPOINT ["/app"]
DOCKERFILE
docker build -t $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/apdev-product:latest /opt/application/product/
docker push $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/apdev-product:latest

# Build and push stress
cat > /opt/application/stress/Dockerfile <<'DOCKERFILE'
FROM public.ecr.aws/docker/library/alpine:latest
COPY stress /app
RUN chmod +x /app && apk add --no-cache libc6-compat
ENTRYPOINT ["/app"]
DOCKERFILE
docker build -t $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/apdev-stress:latest /opt/application/stress/
docker push $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/apdev-stress:latest

echo "=== USERDATA COMPLETE ==="
