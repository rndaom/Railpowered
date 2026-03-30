FROM eclipse-temurin:8-jre

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /server

# Download Beta 1.7.3 server jar from OmniArchive (official community archive)
RUN curl -fSL -o server.jar https://vault.omniarchive.uk/archive/java/server-beta/b1.7/b1.7.3.jar

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
