FROM eclipse-temurin:8-jre AS java8
FROM eclipse-temurin:21-jre AS java21
FROM eclipse-temurin:25-jre AS java25

FROM ubuntu:24.04

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        python3 \
        unzip && \
    rm -rf /var/lib/apt/lists/*

COPY --from=java8 /opt/java/openjdk /opt/java/8
COPY --from=java21 /opt/java/openjdk /opt/java/21
COPY --from=java25 /opt/java/openjdk /opt/java/25

ENV JAVA_8_HOME=/opt/java/8 \
    JAVA_21_HOME=/opt/java/21 \
    JAVA_25_HOME=/opt/java/25 \
    JAVA_HOME=/opt/java/8 \
    PATH="/opt/java/8/bin:${PATH}" \
    MINECRAFT_VERSION=1.2.5 \
    SERVER_TYPE=vanilla

WORKDIR /server

ARG MC_125_SHA=d8321edc9470e56b8ad5c67bbd16beba25843336
RUN mkdir -p /server/jars && \
    curl -fSL -o /server/jars/minecraft_server.1.2.5.jar \
        "https://launcher.mojang.com/v1/objects/${MC_125_SHA}/server.jar" && \
    echo "${MC_125_SHA}  /server/jars/minecraft_server.1.2.5.jar" | sha1sum -c -

COPY manager.py installer.py manager.json server.properties start.sh ./
COPY templates/ templates/

RUN chmod +x start.sh

EXPOSE 8080

CMD ["./start.sh"]
