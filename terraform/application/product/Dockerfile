FROM golang:alpine
COPY product app
RUN chmod +x app && apk add --no-cache curl libc6-compat
CMD ["./app"]
