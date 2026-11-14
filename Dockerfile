# Railpowered: one Minecraft server, a sleep proxy and a web dashboard.
#
# Two stages:
#   jre      downloads the pinned Eclipse Temurin JREs (8, 17, 21, 25) and checks
#            each tarball against Adoptium's published SHA-256.
#   runtime  Ubuntu 24.04 with Python 3.12 (stdlib only), tini and the app.
#
# The JREs come from tarballs, not from the eclipse-temurin images, because
# copying /opt/java out of those images hit overlayfs whiteout errors on some
# builders. Extracting into a fresh stage of our own avoids that.
#
# To bump a JRE, run `python3 .github/scripts/jre_versions.py --write`
# (a weekly workflow opens an issue when a newer GA release exists).

FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS jre

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Packages are not version-pinned, so every build gets Ubuntu's security fixes.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Release names as the Adoptium API reports them (release_name), plus the
# SHA-256 of each Linux JRE tarball for x64 (amd64) and aarch64 (arm64).
ARG JRE8_RELEASE=jdk8u512-b09
ARG JRE8_SHA256_AMD64=57468dc8bfd71dd0578d9717e63a8c13b9efe0a2f2c85c2a13966d1c0f9b1e5f
ARG JRE8_SHA256_ARM64=9b45f49bc6e54cf9170aebe0ee45f48a833222325cb3b2d6d21220eee4c5207b
# Java 17: what Mojang declares for 1.17-1.20.4; older Forge/ModLauncher
# builds for those versions refuse to start on Java 21.
ARG JRE17_RELEASE=jdk-17.0.21+9
ARG JRE17_SHA256_AMD64=9f41bf88c7900c7097444a1bdeb84146ae692bc08842490ab098063c74bf9a8a
ARG JRE17_SHA256_ARM64=1114f09d6174f54270c3117410b5e4022b43ebc0b43b751af08b927c55ed7bb5
ARG JRE21_RELEASE=jdk-21.0.13+9
ARG JRE21_SHA256_AMD64=a535a1df9c6284d9618872984e026ae0352d1f7e9e0605425c0e6114f47351dd
ARG JRE21_SHA256_ARM64=b1734db7ade49c3f6acc6ae06233b8c2dd90e3e2f00f67000ae1c972aaa64d8c
ARG JRE25_RELEASE=jdk-25.0.5+8
ARG JRE25_SHA256_AMD64=6fac98f0c8202efc3135a9e9e42aa61963ec42d1c5cbb40caf231e7b14a24ef6
ARG JRE25_SHA256_ARM64=963931723891302ebb2687178532c3d793d71e2577cd058061c7efb396140a86
# Set by BuildKit for the platform being built; Railway builds amd64.
ARG TARGETARCH

# The SHELL above is bash, so ${release//+/%2B} is fine.
# hadolint ignore=SC3060
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) arch=x64;     sha8="$JRE8_SHA256_AMD64"; sha17="$JRE17_SHA256_AMD64"; sha21="$JRE21_SHA256_AMD64"; sha25="$JRE25_SHA256_AMD64" ;; \
      arm64) arch=aarch64; sha8="$JRE8_SHA256_ARM64"; sha17="$JRE17_SHA256_ARM64"; sha21="$JRE21_SHA256_ARM64"; sha25="$JRE25_SHA256_ARM64" ;; \
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
    fetch 17 "$JRE17_RELEASE" "$sha17"; \
    fetch 21 "$JRE21_RELEASE" "$sha21"; \
    fetch 25 "$JRE25_RELEASE" "$sha25"


FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS runtime

# python3: the manager (stdlib only, 3.12 on Ubuntu 24.04).
# ca-certificates: HTTPS to Mojang, Fabric, Forge and Modrinth at runtime.
# tini: PID 1, forwards SIGTERM to the manager and reaps orphaned processes.
# setpriv (util-linux, already in the base image) drops root in start.sh.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates python3 tini \
 && rm -rf /var/lib/apt/lists/*

# Minecraft and the dashboard run as this unprivileged user. Ubuntu's stock
# "ubuntu" user owns uid 1000, so it is replaced.
RUN if id -u ubuntu >/dev/null 2>&1; then userdel --remove ubuntu || true; fi \
 && groupadd --gid 1000 minecraft \
 && useradd --uid 1000 --gid minecraft --home-dir /home/minecraft --create-home \
      --shell /usr/sbin/nologin minecraft

COPY --from=jre /opt/java /opt/java

# Declared after the apt layers so a JRE bump does not invalidate them.
ARG JRE8_RELEASE=jdk8u512-b09
ARG JRE17_RELEASE=jdk-17.0.21+9
ARG JRE21_RELEASE=jdk-21.0.13+9
ARG JRE25_RELEASE=jdk-25.0.5+8

LABEL org.opencontainers.image.title="Railpowered" \
      org.opencontainers.image.description="One-click Railway template for a Minecraft server that sleeps when empty, wakes when a friend joins, and comes with a web dashboard." \
      org.opencontainers.image.source="https://github.com/rndaom/Railpowered" \
      org.opencontainers.image.url="https://github.com/rndaom/Railpowered" \
      org.opencontainers.image.documentation="https://github.com/rndaom/Railpowered#readme" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.base.name="docker.io/library/ubuntu:24.04" \
      railpowered.jre8="${JRE8_RELEASE}" \
      railpowered.jre17="${JRE17_RELEASE}" \
      railpowered.jre21="${JRE21_RELEASE}" \
      railpowered.jre25="${JRE25_RELEASE}"

ENV JAVA_8_HOME=/opt/java/8 \
    JAVA_17_HOME=/opt/java/17 \
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
