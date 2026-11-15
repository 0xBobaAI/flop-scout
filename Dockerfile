# Public image for the MCP registry and Glama. The devnet deploy on the VPS does
# not use this file: it runs uvicorn straight from a venv.
#
# Ownership proof for the registry is this label, and the value must match the
# `name` field in mcp/server.json exactly. internal/validators/registries/oci.go
# reads config.Labels["io.modelcontextprotocol.server.name"] and rejects a
# mismatch, so changing one side without the other breaks publication.
FROM python:3.12-slim

LABEL io.modelcontextprotocol.server.name="com.flopscout/scout" \
      org.opencontainers.image.title="Flop Scout" \
      org.opencontainers.image.description="Web search, page reading and FLOP miner reliability for agents, priced per call in FLOP." \
      org.opencontainers.image.source="https://github.com/0xBobaAI/flop-scout" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

# No build-essential on purpose: every native dependency ships a cp312
# manylinux wheel, so pip installs prebuilt code. If that ever stops being
# true, the build fails here instead of silently compiling Rust on a 1 vCPU box.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# Secrets and local state must never ride along in a layer. .dockerignore keeps
# them out of the context in the first place; this is the second line of defence
# because a copied file stays in the image history.
RUN rm -f .wallet.json .wallet.backup.json log.db *.db \
    && rm -rf data shots _whl \
    && adduser --system --group --no-create-home scout \
    && mkdir -p /data && chown scout:scout /data

USER scout

# The VPS runs uvicorn with --proxy-headers --forwarded-allow-ips=127.0.0.1.
# Without it request.client is the docker bridge, so every caller shares one
# rate-limit bucket and client_id() collapses them together. The allow-list
# stays narrow by default: trusting X-Forwarded-From from anyone is worse than
# trusting nobody.
ENV DATA_DIR=/data \
    PORT=8080 \
    FORWARDED_ALLOW_IPS=127.0.0.1

EXPOSE 8080

# A cheap local route, not /api/health: that one reaches the FLOP gateway and
# can spend a second, which turns a healthy container unhealthy under load.
HEALTHCHECK --interval=45s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/robots.txt" || exit 1

# exec form keeps uvicorn as PID 1 so SIGTERM reaches it and shutdown is graceful.
CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS}\""]
