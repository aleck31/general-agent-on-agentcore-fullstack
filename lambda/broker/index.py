"""Credential broker — hands a microVM mount credentials for its own user's files.

Called from inside the session by `cred_helper.py`, which presents a KMS-signed ticket.
Stateless: the ticket names the user, and the user determines the Access Point, so there
is nothing to look up in a database.

    provision   → get-or-create this user's Access Point, return its id + credentials
    credentials → mint short-lived credentials for an Access Point already known

Isolation rests on two independent things, neither of which is agent code:

  1. The Access Point's `rootDirectory` is fixed server-side to /users/<actor>, so a
     mount through it cannot see another user's prefix whatever the client asks for.
  2. The returned credentials carry an STS session policy that permits mounting only
     that one Access Point ARN. Code running as root in the container still cannot
     mount anything else.

Keyed by user, not by session — deliberately different from the reference
implementation (walkley/acruntime-s3files-isolation). Reasons in .dev/adr/0007.

The `credentials` path deliberately creates nothing: it verifies that the Access Point
it was asked about really belongs to the ticket's subject. That is cheaper than the
reference implementation's create-and-catch-the-conflict on every refresh, and it is a
binding check that design has no equivalent of — without it a microVM could present its
own valid ticket alongside someone else's Access Point id.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

import boto3
from botocore.config import Config

from ticket import verify_ticket

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-west-2")
FILE_SYSTEM_ID = os.environ["FILE_SYSTEM_ID"]
MOUNT_ROLE_ARN = os.environ["MOUNT_ROLE_ARN"]
KMS_KEY_ID = os.environ["KMS_KEY_ID"]
ACCOUNT_ID = os.environ["ACCOUNT_ID"]
# An hour, not the reference implementation's 15 minutes: an 8-hour session refreshed
# every 15 min costs ~32 broker round trips, each a Lambda invoke plus KMS verify plus
# STS. The credentials are scoped to a single Access Point either way, so the shorter
# window buys little.
CRED_TTL = int(os.environ.get("CRED_TTL_SECONDS", "3600"))
# The mount squashes all file ownership to this uid/gid, so the container's own user id
# need not match anything.
UID = int(os.environ.get("POSIX_UID", "1000"))
GID = int(os.environ.get("POSIX_GID", "1000"))

_RETRY = Config(retries={"mode": "standard", "max_attempts": 4})
_s3files = boto3.client("s3files", region_name=REGION, config=_RETRY)
_sts = boto3.client("sts", region_name=REGION, config=_RETRY)


def root_directory(subject: str) -> str:
    """The one path this subject's Access Point may expose. `subject` is an actor id
    ("lark:ou_..."); the colon becomes an underscore because this is a path component."""
    return "/users/" + subject.replace(":", "_")


def _client_token(subject: str) -> str:
    """Deterministic and <=64 chars, so creating this user's Access Point twice is
    idempotent rather than a duplicate."""
    return hashlib.sha256(subject.encode()).hexdigest()


def _ap_arn(ap_id: str) -> str:
    return (f"arn:aws:s3files:{REGION}:{ACCOUNT_ID}:"
            f"file-system/{FILE_SYSTEM_ID}/access-point/{ap_id}")


def get_or_create_access_point(subject: str) -> str:
    """This user's Access Point, created on first use. Idempotent via clientToken."""
    try:
        return _s3files.create_access_point(
            fileSystemId=FILE_SYSTEM_ID,
            clientToken=_client_token(subject),
            posixUser={"uid": UID, "gid": GID},
            rootDirectory={
                "path": root_directory(subject),
                "creationPermissions": {
                    "ownerUid": UID, "ownerGid": GID, "permissions": "700",
                },
            },
            tags=[{"key": "actor", "value": subject}],
        )["accessPointId"]
    except _s3files.exceptions.ConflictException as e:
        # Already created for this user; the error carries the id.
        ap_id = e.response.get("resourceId")
        if not ap_id:
            raise
        return ap_id


def assert_access_point_belongs_to(ap_id: str, subject: str) -> None:
    """Refuse to mint credentials for an Access Point that is not this user's.

    The ticket is what the caller proves; the Access Point id is merely what it asks
    about, so the two have to be tied together here."""
    root = (_s3files.get_access_point(accessPointId=ap_id)
            .get("rootDirectory") or {}).get("path", "")
    expected = root_directory(subject)
    if root.rstrip("/") != expected.rstrip("/"):
        raise PermissionError(
            f"access point {ap_id} is rooted at {root!r}, not {expected!r}")


def _session_policy(ap_arn: str) -> str:
    """Permit mounting exactly one Access Point. This is what makes the credentials
    useless for reaching any other user's prefix."""
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "MountThisAccessPointOnly",
                "Effect": "Allow",
                "Action": ["s3files:ClientMount", "s3files:ClientWrite"],
                "Resource": "*",
                "Condition": {"StringEquals": {"s3files:AccessPointArn": ap_arn}},
            },
            {
                # The mount helper comes from amazon-efs-utils and authorises against
                # the EFS action names as well.
                "Sid": "EfsCompatThisAccessPointOnly",
                "Effect": "Allow",
                "Action": ["elasticfilesystem:ClientMount",
                           "elasticfilesystem:ClientWrite"],
                "Resource": "*",
                "Condition": {
                    "StringEquals": {"elasticfilesystem:AccessPointArn": ap_arn}},
            },
            {
                "Sid": "DiscoverMountTargets",
                "Effect": "Allow",
                "Action": "elasticfilesystem:DescribeMountTargets",
                "Resource": "*",
            },
        ],
    })


def mint_credentials(subject: str, ap_id: str) -> dict:
    creds = _sts.assume_role(
        RoleArn=MOUNT_ROLE_ARN,
        # Not the raw actor id: a role session name may not contain a colon, and it
        # lands in CloudTrail, where a hash is enough to correlate.
        RoleSessionName=("mnt-" + hashlib.sha256(subject.encode()).hexdigest())[:64],
        Policy=_session_policy(_ap_arn(ap_id)),
        DurationSeconds=CRED_TTL,
    )["Credentials"]
    return {
        "AccessKeyId": creds["AccessKeyId"],
        "SecretAccessKey": creds["SecretAccessKey"],
        "SessionToken": creds["SessionToken"],
        "Expiration": creds["Expiration"].isoformat(),
    }


def broker(ticket: str, action: str = "credentials", ap_id: str = "") -> dict:
    """Core logic, importable for tests."""
    subject = verify_ticket(ticket, KMS_KEY_ID, REGION)["sub"]

    if action == "provision":
        ap_id = get_or_create_access_point(subject)
        return {"access_point_id": ap_id, "access_point_arn": _ap_arn(ap_id),
                "credentials": mint_credentials(subject, ap_id)}

    if action != "credentials":
        raise ValueError(f"unknown action: {action}")
    if not ap_id:
        raise ValueError("credentials requires an access point id")
    assert_access_point_belongs_to(ap_id, subject)
    return {"access_point_id": ap_id, "credentials": mint_credentials(subject, ap_id)}


def handler(event, _context):
    """event = {"ticket": ..., "action": "provision"|"credentials", "apId": ...}."""
    try:
        if not isinstance(event, dict):
            event = json.loads(event)
        action = event.get("action", "credentials")
        result = broker(event["ticket"], action, event.get("apId", ""))
        log.info("issued %s for ap=%s", action, result.get("access_point_id"))
        return result
    except Exception as e:  # noqa: BLE001
        # The caller is a microVM that may be running model-generated code, so it is told
        # that this failed and nothing about why. The reason goes to CloudWatch.
        log.exception("broker refused a request")
        return {"error": type(e).__name__}
