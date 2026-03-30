# Stage 1: Build BetaFix plugin
FROM maven:3.8-openjdk-8 AS plugin-builder
WORKDIR /build
COPY plugins/BetaFix/pom.xml .
RUN mvn dependency:resolve
COPY plugins/BetaFix/src src/
RUN mvn package -q

# Stage 2: Runtime
FROM eclipse-temurin:8-jre

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /server

# Download Project Poseidon (CraftBukkit fork for Beta 1.7.3 with plugin support)
RUN curl -fSL -o server.jar \
    https://github.com/retromcorg/Project-Poseidon/releases/download/1.1.12-260328-0558-5ba3017/poseidon-craftbukkit-1.1.12-260328-0558-5ba3017.jar

# Copy compiled plugin from build stage
RUN mkdir -p /server/plugins
COPY --from=plugin-builder /build/target/BetaFix-*.jar /server/plugins/BetaFix.jar

# Copy application files
COPY manager.py .
COPY server.properties .
COPY ops.txt .
COPY whitelist.txt .
COPY start.sh .
COPY templates/ templates/

RUN chmod +x start.sh

# Web panel port — only expose HTTP; the MC port is handled by Railway's TCP proxy
EXPOSE 8080

CMD ["./start.sh"]
