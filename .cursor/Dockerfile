# Cloud Agent development image.
#
# Provides Java 25 and a preinstalled Fabric Minecraft server so the admin
# panel (manager.py) can boot the server exactly like production. This mirrors
# the version pins in the repository-root Dockerfile, but is dev-oriented:
# it installs git, ensures a non-root `ubuntu` user, and does NOT copy repo
# sources into the image. The checked-out workspace is synced into /server at
# boot by .cursor/run.sh so edits take effect without rebuilding.
FROM eclipse-temurin:25-jre

ARG MINECRAFT_VERSION=26.2
ARG FABRIC_LOADER_VERSION=0.19.3
ARG FABRIC_INSTALLER_VERSION=1.1.1
ARG FABRIC_API_VERSION=0.153.0+26.2

ENV MINECRAFT_VERSION=${MINECRAFT_VERSION} \
    FABRIC_LOADER_VERSION=${FABRIC_LOADER_VERSION} \
    FABRIC_API_VERSION=${FABRIC_API_VERSION}

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl git python3 && \
    rm -rf /var/lib/apt/lists/*

# Ensure a non-root "ubuntu" user (uid 1000) exists for the Cloud Agent.
RUN if ! id ubuntu >/dev/null 2>&1; then useradd -m -u 1000 -s /bin/bash ubuntu; fi

WORKDIR /server

RUN curl -fSL -o /tmp/fabric-installer.jar \
        "https://maven.fabricmc.net/net/fabricmc/fabric-installer/${FABRIC_INSTALLER_VERSION}/fabric-installer-${FABRIC_INSTALLER_VERSION}.jar" && \
    java -jar /tmp/fabric-installer.jar server \
        -mcversion "${MINECRAFT_VERSION}" \
        -loader "${FABRIC_LOADER_VERSION}" \
        -downloadMinecraft \
        -noprofile && \
    rm /tmp/fabric-installer.jar

RUN mkdir -p /server/mods && \
    curl -fSL -o "/server/mods/fabric-api-${FABRIC_API_VERSION}.jar" \
        "https://cdn.modrinth.com/data/P7dR8mSH/versions/M8Kbv865/fabric-api-0.153.0%2B26.2.jar"

RUN chown -R ubuntu:ubuntu /server

USER ubuntu
