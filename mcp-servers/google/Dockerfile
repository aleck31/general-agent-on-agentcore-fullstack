# Google MCP server for AgentCore Runtime (ARM64).
#
# No CLI to wrap and no dependencies: Google's REST APIs are called with node's own https,
# so this image is the base plus one file. The Lark server needs a CLI because that is how
# it borrows lark-cli's per-request token handling; here the token is simply a bearer.
#
# MCP contract: streamable HTTP on 0.0.0.0:8000. The caller's Google access token arrives in
# the same custom passthrough header the Lark server uses — see server.js.
FROM public.ecr.aws/docker/library/node:24-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.js /app/server.js

EXPOSE 8000

# Non-root: this server holds no credential of its own and needs nothing from the filesystem.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser
ENV HOME=/home/appuser

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/ || exit 1

CMD ["node", "/app/server.js"]
