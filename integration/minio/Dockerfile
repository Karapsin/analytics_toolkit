# Build the existing release commits; upstream container pulls are unavailable.
FROM golang:1.24.7-alpine3.22 AS builder
ENV CGO_ENABLED=0 GOTOOLCHAIN=local

FROM builder AS server-build
# RELEASE.2025-09-07T16-13-09Z
RUN go install github.com/minio/minio@07c3a429bfed433e49018cb0f78a52145d4bedeb \
    && mkdir /licenses \
    && cp /go/pkg/mod/github.com/minio/minio@*/LICENSE /licenses/minio-LICENSE

FROM builder AS client-build
# RELEASE.2025-08-13T08-35-41Z
RUN go install github.com/minio/mc@7394ce0dd2a80935aded936b09fa12cbb3cb8096 \
    && mkdir /licenses \
    && cp /go/pkg/mod/github.com/minio/mc@*/LICENSE /licenses/mc-LICENSE

FROM alpine:3.22.1 AS runtime
RUN apk add --no-cache ca-certificates curl

FROM runtime AS server
COPY --from=server-build /go/bin/minio /usr/local/bin/minio
COPY --from=server-build /licenses /usr/share/licenses
ENTRYPOINT ["minio"]

FROM runtime AS client
COPY --from=client-build /go/bin/mc /usr/local/bin/mc
COPY --from=client-build /licenses /usr/share/licenses
ENTRYPOINT ["mc"]
