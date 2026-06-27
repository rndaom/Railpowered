FROM eclipse-temurin:25-jre

ARG MINECRAFT_VERSION=26.2
ARG FABRIC_LOADER_VERSION=0.19.3
ARG FABRIC_INSTALLER_VERSION=1.1.1

ENV MINECRAFT_VERSION=${MINECRAFT_VERSION}
ENV FABRIC_LOADER_VERSION=${FABRIC_LOADER_VERSION}

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

COPY manager.py .
COPY server.properties .
COPY start.sh .
COPY templates/ templates/

RUN chmod +x start.sh

EXPOSE 8080

CMD ["./start.sh"]
