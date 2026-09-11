#!/usr/bin/env bash
# Run every unit suite. These live next to the code they test (the agent and each
# Lambda), so this walks them; the e2e smoke tests in this directory need a
# deployed stack and are run separately — see README.md here.
#
# Each suite gets its own process: the router and shim dirs both define index.py,
# and the agent and router both define identity.py, so a single pytest session
# would import the wrong module for one of them.
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

echo "== agent =="
# The LangGraph stack is pure Python, so unlike Strands it installs here and the turn
# loop is tested through a real compiled graph rather than a copy of its source.
uv run --with boto3 --with aiohttp --with httpx --with pytest \
       --with langchain --with langchain-aws --with langgraph \
       --with langchain-mcp-adapters \
       python -m pytest agent/test_agent.py -q

echo "== cred helper =="
# Baked into the agent image but dependency-light on purpose, so it needs none of the
# LangGraph stack the agent suite installs.
uv run --with boto3 --with pytest python -m pytest agent/test_cred_helper.py -q

echo "== router =="
uv run --with cryptography --with boto3 --with pytest python -m pytest lambda/router/test_router.py -q

echo "== shim =="
uv run --with boto3 --with pytest python -m pytest lambda/shim/test_shim.py -q

echo "== broker =="
uv run --with boto3 --with pytest python -m pytest lambda/broker/test_broker.py -q

echo "== approval guards (node) =="
# Node's own runner — the guards are dependency-free by design, so no new toolchain.
# Pointed at the file, not the directory: server.js starts listening on load.
node --test mcp-servers/approval/test_guards.mjs

echo "== all green =="
