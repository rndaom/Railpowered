# Stage 1: Build BetaFix plugin
FROM eclipse-temurin:8-jdk AS plugin-builder

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Download Poseidon JAR as compile dependency
RUN curl -fSL -o poseidon.jar \
    https://github.com/retromcorg/Project-Poseidon/releases/download/1.1.12-260328-0558-5ba3017/poseidon-craftbukkit-1.1.12-260328-0558-5ba3017.jar

# Copy plugin source
COPY plugins/BetaFix/src src/

# Compile against Poseidon JAR
RUN mkdir -p classes
RUN find src/main/java -name "*.java" > sources.txt && cat sources.txt
RUN javac -cp poseidon.jar -d classes @sources.txt 2>&1 || (echo "JAVAC FAILED" && cat sources.txt && exit 1)

# Create plugin.yml with version baked in
RUN cp src/main/resources/plugin.yml classes/plugin.yml && \
    sed -i 's/${project.version}/1.0.0/' classes/plugin.yml

# Package into JAR
RUN cd classes && jar cf /build/BetaFix.jar .

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
COPY --from=plugin-builder /build/BetaFix.jar /server/plugins/BetaFix.jar

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
