FROM ubuntu:24.04

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        python3 \
        unzip && \
    rm -rf /var/lib/apt/lists/*

# Temurin JREs from Adoptium. Multi-stage COPY from eclipse-temurin hits
# overlayfs whiteout errors in some builders; tarball extract is reliable.
RUN mkdir -p /opt/java/8 /opt/java/21 /opt/java/25 && \
    curl -fSL -o /tmp/jre8.tar.gz \
        "https://api.adoptium.net/v3/binary/latest/8/ga/linux/x64/jre/hotspot/normal/eclipse" && \
    tar -xzf /tmp/jre8.tar.gz -C /opt/java/8 --strip-components=1 && \
    curl -fSL -o /tmp/jre21.tar.gz \
        "https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jre/hotspot/normal/eclipse" && \
    tar -xzf /tmp/jre21.tar.gz -C /opt/java/21 --strip-components=1 && \
    curl -fSL -o /tmp/jre25.tar.gz \
        "https://api.adoptium.net/v3/binary/latest/25/ga/linux/x64/jre/hotspot/normal/eclipse" && \
    tar -xzf /tmp/jre25.tar.gz -C /opt/java/25 --strip-components=1 && \
    rm -f /tmp/jre8.tar.gz /tmp/jre21.tar.gz /tmp/jre25.tar.gz && \
    /opt/java/8/bin/java -version && \
    /opt/java/21/bin/java -version && \
    /opt/java/25/bin/java -version

ENV JAVA_8_HOME=/opt/java/8 \
    JAVA_21_HOME=/opt/java/21 \
    JAVA_25_HOME=/opt/java/25 \
    JAVA_HOME=/opt/java/25 \
    PATH="/opt/java/25/bin:${PATH}" \
    MINECRAFT_VERSION=latest \
    SERVER_TYPE=vanilla

WORKDIR /server

COPY manager.py installer.py manager.json server.properties start.sh ./
COPY mc_host/ mc_host/
COPY templates/ templates/

RUN chmod +x start.sh && mkdir -p /server/jars

EXPOSE 8080 25565

CMD ["./start.sh"]
