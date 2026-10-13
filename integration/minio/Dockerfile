# The upstream release images are no longer publicly pullable. Build the same
# releases from immutable source; compilation runs only on validation runners.
FROM golang:1.24.2-alpine3.21 AS client-build
RUN apk add --no-cache git ca-certificates
WORKDIR /src
RUN git init && git remote add origin https://github.com/minio/mc.git \
    && git fetch --depth=1 origin 7394ce0dd2a80935aded936b09fa12cbb3cb8096 \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = 7394ce0dd2a80935aded936b09fa12cbb3cb8096
ENV CGO_ENABLED=0 MC_RELEASE=RELEASE
RUN go build -trimpath -ldflags "$(go run buildscripts/gen-ldflags.go 2025-08-13T08:35:41Z)" -o /out/mc .

FROM golang:1.24.2-alpine3.21 AS server-build
RUN apk add --no-cache git ca-certificates
WORKDIR /src
RUN git init && git remote add origin https://github.com/minio/minio.git \
    && git fetch --depth=1 origin 07c3a429bfed433e49018cb0f78a52145d4bedeb \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = 07c3a429bfed433e49018cb0f78a52145d4bedeb
ENV CGO_ENABLED=0 MINIO_RELEASE=RELEASE
RUN go build -trimpath -ldflags "$(go run buildscripts/gen-ldflags.go 2025-09-07T16:13:09Z)" -o /out/minio .

FROM alpine:3.21 AS runtime
RUN apk add --no-cache ca-certificates curl

FROM runtime AS client
COPY --from=client-build /out/mc /usr/bin/mc
COPY --from=client-build /src/LICENSE /src/CREDITS /licenses/
ENTRYPOINT ["mc"]

FROM runtime AS server
COPY --from=server-build /out/minio /usr/bin/minio
COPY --from=server-build /src/LICENSE /src/CREDITS /licenses/
ENTRYPOINT ["minio"]
