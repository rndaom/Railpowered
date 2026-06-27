FROM eclipse-temurin:25-jre

ARG MINECRAFT_VERSION=26.2
ARG FABRIC_LOADER_VERSION=0.19.3
ARG FABRIC_INSTALLER_VERSION=1.1.1
ARG FABRIC_API_VERSION=0.153.0+26.2

ENV MINECRAFT_VERSION=${MINECRAFT_VERSION}
ENV FABRIC_LOADER_VERSION=${FABRIC_LOADER_VERSION}
ENV FABRIC_API_VERSION=${FABRIC_API_VERSION}

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl python3 && \
    rm -rf /var/lib/apt/lists/*

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

COPY manager.py .
COPY server.properties .
COPY server-mods/ mods/
COPY start.sh .
COPY templates/ templates/

RUN chmod +x start.sh

EXPOSE 8080

CMD ["./start.sh"]
