FROM golang:alpine
COPY stress app
RUN chmod +x app && apk add --no-cache curl libc6-compat
CMD ["./app"]

