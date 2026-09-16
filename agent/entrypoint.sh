#!/bin/bash
# Container entrypoint. Starts the efs-utils watchdog, then the agent.
#
# The watchdog is what keeps a mount alive: it re-runs the credential_process helper as
# the mount's short-lived credentials expire. There is no init system in the container, so
# nothing else would start it. It is harmless when no mount exists — it simply finds
# nothing to maintain — so this runs unconditionally rather than branching on whether
# files storage is enabled.
set -e

if [ -x /usr/bin/amazon-efs-mount-watchdog ]; then
  /usr/bin/amazon-efs-mount-watchdog >/var/log/efs-watchdog.log 2>&1 &
fi

# One image, two contracts. A2A binds port 9000 and serves at the root; the HTTP/AG-UI
# contract is 8080 under /invocations — so they cannot share a server, only an image.
# `if`, not `[ … ] && …`: under `set -e` a false test would end the script.
SERVER=/app/server.py
if [ "${SERVER_MODE:-http}" = "a2a" ]; then SERVER=/app/a2a_server.py; fi

# opentelemetry-instrument is required by AgentCore: it is what gets container stdout and
# traces into CloudWatch. exec so the agent is PID 1's successor and receives signals.
exec opentelemetry-instrument python3 "$SERVER"
