# Railpowered: one Minecraft server, a sleep proxy and a web dashboard.
#
# Two stages:
#   jre      downloads the pinned Eclipse Temurin JREs (8, 21, 25) and checks
#            each tarball against Adoptium's published SHA-256.
#   runtime  Ubuntu 24.04 with Python 3.12 (stdlib only), tini and the app.
#
# The JREs come from tarballs, not from the eclipse-temurin images, because
# copying /opt/java out of those images hit overlayfs whiteout errors on some
# builders. Extracting into a fresh stage of our own avoids that.
#
# To bump a JRE, run `python3 .github/scripts/jre_versions.py --write`
# (a weekly workflow opens an issue when a newer GA release exists).

FROM ubuntu:24.04 AS jre

# Release names as the Adoptium API reports them (release_name), plus the
# SHA-256 of each Linux JRE tarball for x64 (amd64) and aarch64 (arm64).
ARG JRE8_RELEASE=jdk8u504-b01
ARG JRE8_SHA256_AMD64=52dcd578baca1d3e449ea86768a9129c0ee04d7b22565695498353cc66940c61
ARG JRE8_SHA256_ARM64=9ae9c4dd80fc8f3c4081b480c7d42346e9e4cbee5ae58198fca11e0fc1a19163
ARG JRE21_RELEASE=jdk-21.0.12.1+1
ARG JRE21_SHA256_AMD64=2413149700df0f7d440500a84a8f764c535f21e5a5e87d38328b64eec2c5b500
ARG JRE21_SHA256_ARM64=14be1f35ebdbd1f6e8d57eb911a3ffb74d6d9aa255abc5daf2b1302002cf2cf2
ARG JRE25_RELEASE=jdk-25.0.4.1+1
ARG JRE25_SHA256_AMD64=1731a34baadec5479258ea0202e4d5d865d2efeee60cb0c7d7eb056fe96ca219
ARG JRE25_SHA256_ARM64=34828cbb93ed31c281c84ecb31ddab655d11a802f263c1fc019d42e9e0230fed
# Set by BuildKit for the platform being built; Railway builds amd64.
ARG TARGETARCH

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) arch=x64;     sha8="$JRE8_SHA256_AMD64"; sha21="$JRE21_SHA256_AMD64"; sha25="$JRE25_SHA256_AMD64" ;; \
      arm64) arch=aarch64; sha8="$JRE8_SHA256_ARM64"; sha21="$JRE21_SHA256_ARM64"; sha25="$JRE25_SHA256_ARM64" ;; \
      *) echo "Unsupported architecture: ${TARGETARCH:-}" >&2; exit 1 ;; \
    esac; \
    fetch() { \
      local major="$1" release="$2" sha="$3"; \
      local url="https://api.adoptium.net/v3/binary/version/${release//+/%2B}/linux/${arch}/jre/hotspot/normal/eclipse"; \
      echo "Fetching Temurin ${release} (${arch})"; \
      curl -fsSL --retry 5 --retry-all-errors --connect-timeout 20 -o "/tmp/jre${major}.tar.gz" "$url"; \
      echo "${sha}  /tmp/jre${major}.tar.gz" | sha256sum -c -; \
      mkdir -p "/opt/java/${major}"; \
      tar -xzf "/tmp/jre${major}.tar.gz" -C "/opt/java/${major}" --strip-components=1 --no-same-owner; \
      rm -f "/tmp/jre${major}.tar.gz"; \
      "/opt/java/${major}/bin/java" -version; \
    }; \
    fetch 8 "$JRE8_RELEASE" "$sha8"; \
    fetch 21 "$JRE21_RELEASE" "$sha21"; \
    fetch 25 "$JRE25_RELEASE" "$sha25"


FROM ubuntu:24.04 AS runtime

ARG JRE8_RELEASE=jdk8u504-b01
ARG JRE21_RELEASE=jdk-21.0.12.1+1
ARG JRE25_RELEASE=jdk-25.0.4.1+1

LABEL org.opencontainers.image.title="Railpowered" \
      org.opencontainers.image.description="One-click Railway template for a Minecraft server that sleeps when empty, wakes when a friend joins, and comes with a web dashboard." \
      org.opencontainers.image.source="https://github.com/rndaom/Railpowered" \
      org.opencontainers.image.url="https://github.com/rndaom/Railpowered" \
      org.opencontainers.image.documentation="https://github.com/rndaom/Railpowered#readme" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.base.name="docker.io/library/ubuntu:24.04" \
      railpowered.jre8="${JRE8_RELEASE}" \
      railpowered.jre21="${JRE21_RELEASE}" \
      railpowered.jre25="${JRE25_RELEASE}"

# python3: the manager (stdlib only, 3.12 on Ubuntu 24.04).
# ca-certificates: HTTPS to Mojang, Fabric, Forge and Modrinth at runtime.
# tini: PID 1, forwards SIGTERM to the manager and reaps orphaned processes.
# setpriv (util-linux, already in the base image) drops root in start.sh.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates python3 tini \
 && rm -rf /var/lib/apt/lists/*

# Minecraft and the dashboard run as this unprivileged user. Ubuntu's stock
# "ubuntu" user owns uid 1000, so it is replaced.
RUN if id -u ubuntu >/dev/null 2>&1; then userdel --remove ubuntu 2>/dev/null || userdel ubuntu; fi \
 && groupadd --gid 1000 minecraft \
 && useradd --uid 1000 --gid minecraft --home-dir /home/minecraft --create-home \
      --shell /usr/sbin/nologin minecraft

COPY --from=jre /opt/java /opt/java

ENV JAVA_8_HOME=/opt/java/8 \
    JAVA_21_HOME=/opt/java/21 \
    JAVA_25_HOME=/opt/java/25 \
    JAVA_HOME=/opt/java/25 \
    PATH="/opt/java/25/bin:${PATH}" \
    PYTHONUNBUFFERED=1

WORKDIR /server

# App code stays root-owned and read-only to the minecraft user. Everything
# the server writes lives on the volume at /server/data.
COPY manager.py installer.py server.properties start.sh ./
COPY mc_host/ mc_host/
COPY web/ web/

RUN chmod 0755 start.sh \
 && python3 -m compileall -q manager.py installer.py mc_host \
 && mkdir -p /server/data

# 8080: dashboard and /health. 25565: Minecraft (sleep proxy while asleep).
EXPOSE 8080 25565

# The container starts as root only so start.sh can hand an existing,
# root-owned Railway volume to the minecraft user; it then drops privileges
# with setpriv before starting Python. See start.sh.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/server/start.sh"]

# Railway passes its RAILWAY_* variables to builds that declare them. Kept last
# so a new commit only changes this metadata layer.
ARG RAILWAY_GIT_COMMIT_SHA=
LABEL org.opencontainers.image.revision="${RAILWAY_GIT_COMMIT_SHA}"
