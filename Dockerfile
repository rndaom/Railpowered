# Stage 1: Build plugins
FROM eclipse-temurin:8-jdk AS plugin-builder

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Download Poseidon JAR as compile dependency
RUN curl -fSL -o poseidon.jar \
    https://github.com/retromcorg/Project-Poseidon/releases/download/1.1.12-260328-0558-5ba3017/poseidon-craftbukkit-1.1.12-260328-0558-5ba3017.jar

# Copy all plugin sources (10 plugins, v8)
COPY plugins/ plugins/

# Build every plugin under plugins/
RUN mkdir -p /build/jars && \
    for plugin_dir in plugins/*/; do \
        [ -d "$plugin_dir/src" ] || continue; \
        name=$(basename "$plugin_dir"); \
        echo "=== Building $name ==="; \
        mkdir -p "classes/$name"; \
        find "$plugin_dir/src/main/java" -name "*.java" > "/tmp/$name-sources.txt"; \
        javac -cp poseidon.jar -d "classes/$name" @"/tmp/$name-sources.txt"; \
        cp "$plugin_dir/src/main/resources/plugin.yml" "classes/$name/plugin.yml"; \
        sed -i 's/${project.version}/1.0.0/' "classes/$name/plugin.yml"; \
        (cd "classes/$name" && jar cf "/build/jars/$name.jar" .); \
        echo "=== $name built ==="; \
    done

# Stage 2: Runtime
FROM eclipse-temurin:8-jre

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /server

# Download Project Poseidon (CraftBukkit fork for Beta 1.7.3 with plugin support)
RUN curl -fSL -o server.jar \
    https://github.com/retromcorg/Project-Poseidon/releases/download/1.1.12-260328-0558-5ba3017/poseidon-craftbukkit-1.1.12-260328-0558-5ba3017.jar

# Copy compiled plugins from build stage
RUN mkdir -p /server/plugins
COPY --from=plugin-builder /build/jars/ /server/plugins/

# Copy application files
COPY manager.py .
COPY server.properties .
COPY poseidon.yml .
COPY ops.txt .
COPY whitelist.txt .
COPY start.sh .
COPY templates/ templates/

RUN chmod +x start.sh

# Web panel port — only expose HTTP; the MC port is handled by Railway's TCP proxy
EXPOSE 8080

CMD ["./start.sh"]
