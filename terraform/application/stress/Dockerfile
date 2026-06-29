FROM alpine:3.20
COPY stress app
RUN chmod +x app && apk add --no-cache curl libc6-compat ca-certificates
CMD ["./app"]
