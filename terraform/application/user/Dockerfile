FROM golang:alpine
COPY user app
RUN chmod +x app && apk add --no-cache curl libc6-compat
CMD ["./app"]
