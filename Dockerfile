FROM eclipse-temurin:25-jre

ARG MINECRAFT_VERSION=26.2
ARG SERVER_DOWNLOAD_URL=https://piston-data.mojang.com/v1/objects/823e2250d24b3ddac457a60c92a6a941943fcd6a/server.jar
ARG SERVER_SHA1=823e2250d24b3ddac457a60c92a6a941943fcd6a

ENV MINECRAFT_VERSION=${MINECRAFT_VERSION}

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl python3 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /server

RUN curl -fSL -o /server/server.jar "${SERVER_DOWNLOAD_URL}" && \
    echo "${SERVER_SHA1}  /server/server.jar" | sha1sum -c -

COPY manager.py .
COPY server.properties .
COPY start.sh .
COPY templates/ templates/

RUN chmod +x start.sh

EXPOSE 8080 25565

CMD ["./start.sh"]
