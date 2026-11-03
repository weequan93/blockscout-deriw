FROM postgres@sha256:54e0dd8e8cddcdbb9156c2486eed5a43eccb7214d7c6828097d41ce049fb3984
ARG TARGETARCH
RUN apk add --no-cache rsync acl attr ca-certificates curl
RUN case "$TARGETARCH" in \
      arm64) archive_hash=5f01ac0ad46caf685afee7614e36e406c48e73c1d84a71378dd4afe5d63a11d2 ;; \
      amd64) archive_hash=151f0cb3de61c77f89230e276761cb0f232f0e89d9c2efca3c172282d14193ef ;; \
      *) exit 1 ;; \
    esac && \
    curl -fsSL --connect-timeout 10 --max-time 120 \
      "https://github.com/containerd/containerd/releases/download/v2.2.4/containerd-static-2.2.4-linux-$TARGETARCH.tar.gz" \
      -o /tmp/containerd.tar.gz && \
    printf '%s  %s\n' "$archive_hash" /tmp/containerd.tar.gz | sha256sum -c - && \
    tar -xzf /tmp/containerd.tar.gz -C /usr/local bin/containerd && rm /tmp/containerd.tar.gz
ENTRYPOINT ["sh"]
