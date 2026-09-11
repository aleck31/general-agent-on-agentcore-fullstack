"""Per-user file mounts: sign the ticket, then bootstrap the mount inside the session.

Both halves live in the router because it is the only component that establishes who a
turn belongs to. The ticket's subject is therefore never an argument — it comes from the
same verified actor the Cognito JWT is minted for. A signer that accepted a subject would
move the security boundary from IAM to "who can call the signer".

The mount cannot be declared on the Runtime (`filesystemConfigurations` is Runtime-scoped
and we need one Access Point per user), so it is performed in the session by a bootstrap
script sent with `InvokeAgentRuntimeCommand` — which also starts the microVM. See
.dev/adr/0007 for why this shape and not the simpler declarative one.

Everything here is a no-op when the feature is off, and every failure is non-fatal: a turn
without a mount is a turn without file tools, not a failed turn.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shlex
import time
import urllib.parse
import urllib.request

import boto3
from botocore.config import Config

import cognito

logger = logging.getLogger()

_REGION = os.environ.get("AWS_REGION", "us-west-2")
TICKET_KEY_ID = os.environ.get("MOUNT_TICKET_KEY_ID", "")
FILE_SYSTEM_ID = os.environ.get("S3FILES_FS_ID", "")
RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
QUALIFIER = os.environ.get("AGENTCORE_QUALIFIER", "DEFAULT")
MOUNT_PATH = os.environ.get("MOUNT_PATH", "/mnt/user")
# Must outlive the session, because the efs-utils watchdog re-reads it on every credential
# refresh. maxLifetime caps a microVM at 8 h, so 8 h plus a margin.
TICKET_TTL = int(os.environ.get("MOUNT_TICKET_TTL_SECONDS", str(8 * 3600 + 600)))
BOOTSTRAP_TIMEOUT = int(os.environ.get("MOUNT_BOOTSTRAP_TIMEOUT", "120"))
SIGNING_ALGORITHM = "ECDSA_SHA_256"

_RETRY = Config(retries={"mode": "standard", "max_attempts": 3})


def enabled() -> bool:
    return bool(TICKET_KEY_ID and FILE_SYSTEM_ID and RUNTIME_ARN)


# The mount is set up by a script sent inline rather than baked into the image: keeping
# the orchestration in the control plane means an untrusted microVM holds less of it at
# rest. Only cred_helper.py is baked in, because the watchdog has to re-run it.
#
# `findmnt` first, so re-running this is cheap and safe — the microVM may be replaced
# mid-session, and the same script has to be able to put the mount back.
_BOOTSTRAP = r"""
set -euo pipefail
TICKET="$1"
MOUNT_PATH="${MOUNT_PATH:?}"
FS_ID="${S3FILES_FS_ID:?}"
TICKET_FILE="${MOUNT_TICKET_FILE:-/dev/shm/mount_ticket}"
AP_FILE="${MOUNT_AP_FILE:-/dev/shm/mount_ap}"
PROFILE=mount
# The absolute interpreter, not a bare `python3`: the mount helper runs under the system
# python (which has botocore but not boto3) and spawns credential_process with its own
# PATH, so a relative name resolves to the wrong interpreter and the import fails.
HELPER="/usr/bin/python3.11 /app/cred_helper.py"

if findmnt -T "$MOUNT_PATH" >/dev/null 2>&1; then echo "already mounted"; exit 0; fi

# tmpfs only: the ticket is a bearer capability and must never reach durable storage.
umask 077
printf '%s' "$TICKET" > "$TICKET_FILE"

# Credentials come from the broker through credential_process, so no static keys land
# anywhere. The watchdog re-invokes the helper on each refresh.
mkdir -p /root/.aws
printf '[profile %s]\ncredential_process = %s\n' "$PROFILE" "$HELPER" > /root/.aws/config

# First call creates this user's Access Point if it is new and reports its id; later
# refreshes reuse the id from the file and never create anything.
# $HELPER unquoted on purpose: it is "python3 /app/cred_helper.py", so quoting it would
# make the shell look for a single command with a space in its name.
# shellcheck disable=SC2086
$HELPER --provision > "$AP_FILE"
AP_ID="$(cat "$AP_FILE")"

mkdir -p "$MOUNT_PATH"
mount -t s3files -o "accesspoint=$AP_ID,awsprofile=$PROFILE,nodirects3read" \
      "$FS_ID:/" "$MOUNT_PATH"
findmnt -T "$MOUNT_PATH"
"""


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign_ticket(actor_id: str) -> str:
    """A KMS-signed ticket naming `actor_id`, which the caller must have verified.

    The subject becomes a path component in the broker, so anything that could traverse
    is refused here rather than laundered by a valid signature."""
    if not actor_id or "/" in actor_id or ".." in actor_id:
        raise ValueError(f"refusing to sign an unusable subject: {actor_id!r}")
    payload = json.dumps({"sub": actor_id, "exp": int(time.time()) + TICKET_TTL, "v": 1},
                         separators=(",", ":"), sort_keys=True).encode()
    sig = boto3.client("kms", region_name=_REGION, config=_RETRY).sign(
        KeyId=TICKET_KEY_ID, Message=payload, MessageType="RAW",
        SigningAlgorithm=SIGNING_ALGORITHM,
    )["Signature"]
    return _b64u(payload) + "." + _b64u(sig)


def bootstrap(session_id: str, actor_id: str) -> bool:
    """Mount this user's files into the session. True if the mount is in place.

    Over HTTPS with the user's own Cognito JWT, not through boto3: the Runtime is
    configured CUSTOM_JWT, and SigV4 against it is refused outright with "Authorization
    method mismatch" (verified on a real deployment — the same mutual exclusivity that
    makes the router invoke /invocations this way). The command endpoint is
    POST /runtimes/{arn}/commands.

    Starting the command also starts the session's microVM, so this doubles as the warm-up
    for a new session. Safe to call again: the script exits early when the mount is
    present."""
    if not enabled():
        return False
    command = " ".join([
        "bash", "-c", shlex.quote(_BOOTSTRAP), "bootstrap",
        shlex.quote(sign_ticket(actor_id)),
    ])
    url = (f"https://bedrock-agentcore.{_REGION}.amazonaws.com/runtimes/"
           f"{urllib.parse.quote(RUNTIME_ARN, safe='')}/commands"
           f"?qualifier={urllib.parse.quote(QUALIFIER)}")
    req = urllib.request.Request(
        url, method="POST",
        data=json.dumps({"command": command, "timeout": BOOTSTRAP_TIMEOUT}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cognito.user_jwt(actor_id)}",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=BOOTSTRAP_TIMEOUT + 30) as r:
            raw = r.read().decode(errors="replace")
    except Exception:  # noqa: BLE001 — no mount means no file tools, not a failed turn
        logger.exception("mount bootstrap failed for %s", actor_id)
        return False

    # The response is an AWS event stream. Only two things matter: the exit code, and
    # whatever went to stderr if it is not zero — so scan the JSON fragments rather than
    # decode the framing.
    codes = [int(m) for m in re.findall(r'"exitCode"\s*:\s*(-?\d+)', raw)]
    if codes and codes[-1] == 0:
        logger.info("mount ready at %s for %s", MOUNT_PATH, actor_id)
        return True
    errs = "".join(re.findall(r'"stderr"\s*:\s*"(.*?)"', raw))[:500]
    logger.error("mount bootstrap failed for %s (exit %s): %s",
                 actor_id, codes[-1] if codes else "unknown", errs)
    return False
