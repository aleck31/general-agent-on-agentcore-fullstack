#!/bin/bash
# Container entrypoint: pick the server for this mode, then exec it.
set -e

# One image, two contracts. A2A binds port 9000 and serves at the root; the HTTP/AG-UI
# contract is 8080 under /invocations — so they cannot share a server, only an image.
# `if`, not `[ … ] && …`: under `set -e` a false test would end the script.
SERVER=/app/server.py
if [ "${SERVER_MODE:-http}" = "a2a" ]; then SERVER=/app/a2a_server.py; fi

# opentelemetry-instrument is required by AgentCore: it is what gets container stdout and
# traces into CloudWatch. exec so the agent is PID 1's successor and receives signals.
exec opentelemetry-instrument python3 "$SERVER"
