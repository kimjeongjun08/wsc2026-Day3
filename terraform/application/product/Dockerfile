FROM golang:alpine

COPY product app

RUN chmod +x app

RUN apk add --no-cache curl libc6-compat

ENV MYSQL_USER=admin
ENV MYSQL_PASSWORD="Skill53##"
ENV MYSQL_HOST=apdev-rds-proxy.proxy-cdnblsxzzpgz.ap-northeast-2.rds.amazonaws.com
ENV MYSQL_PORT=3306
ENV MYSQL_DBNAME=apdev
ENV S3_BUCKET=apdev-product-images-003150130236

CMD ["./app"]
