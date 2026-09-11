#!/usr/bin/python3.11
"""credential_process helper for the per-user file mount.

The only piece of the mount machinery baked into the image. It has to be, because the
efs-utils watchdog re-invokes it for the whole life of the session every time the mount's
credentials need refreshing — the bootstrap script that sets everything up is sent inline
by the router and is gone by then.

    cred_helper.py --provision   → print this user's Access Point id (creating it if new)
    cred_helper.py               → print credential_process JSON for the mount

Both read the KMS-signed ticket from tmpfs and present it to the broker Lambda, which is
the only thing that can turn it into credentials. This process never holds a long-lived
credential and never learns another user's Access Point.
"""

from __future__ import annotations

import json
import os
import sys

import boto3

TICKET_FILE = os.environ.get("MOUNT_TICKET_FILE", "/dev/shm/mount_ticket")
AP_FILE = os.environ.get("MOUNT_AP_FILE", "/dev/shm/mount_ap")
BROKER_FN = os.environ.get("MOUNT_BROKER_FN", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")


def _call_broker(action: str, ap_id: str = "") -> dict:
    if not BROKER_FN:
        raise RuntimeError("MOUNT_BROKER_FN is not set")
    with open(TICKET_FILE) as f:
        ticket = f.read().strip()
    payload = {"ticket": ticket, "action": action}
    if ap_id:
        payload["apId"] = ap_id
    resp = boto3.client("lambda", region_name=REGION).invoke(
        FunctionName=BROKER_FN, Payload=json.dumps(payload).encode(),
    )
    result = json.loads(resp["Payload"].read() or "{}")
    if "error" in result:
        # The broker deliberately returns only an exception type; the reason is in its
        # own log, not ours.
        raise RuntimeError(f"broker refused the request: {result['error']}")
    return result


def _access_point_id() -> str:
    """The Access Point recorded at bootstrap. Falling back to provision covers the
    ordering case where a refresh somehow runs before the file exists — provision is
    idempotent, so this costs a create call that returns the same id."""
    try:
        with open(AP_FILE) as f:
            ap_id = f.read().strip()
        if ap_id:
            return ap_id
    except OSError:
        pass
    return _call_broker("provision")["access_point_id"]


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "--provision":
        # stdout is redirected into the access-point file by the bootstrap, so it must
        # carry the id and nothing else.
        sys.stdout.write(_call_broker("provision")["access_point_id"])
        return 0

    creds = _call_broker("credentials", _access_point_id())["credentials"]
    # The exact shape credential_process expects; anything else and the mount helper
    # reports no credentials rather than a parse error.
    sys.stdout.write(json.dumps({
        "Version": 1,
        "AccessKeyId": creds["AccessKeyId"],
        "SecretAccessKey": creds["SecretAccessKey"],
        "SessionToken": creds["SessionToken"],
        "Expiration": creds["Expiration"],
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as e:  # noqa: BLE001
        # A failure here means the mount goes stale, so make it findable in the container
        # log rather than silently returning nothing.
        sys.stderr.write(f"cred_helper: {type(e).__name__}: {e}\n")
        sys.exit(1)
