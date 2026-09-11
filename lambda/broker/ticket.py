"""Mount tickets: KMS-signed, and they name a *user*, never a session.

    ticket = base64url(payload_json) + "." + base64url(signature)
    payload = {"sub": "lark:ou_...", "exp": <unix_ts>, "v": 1}

The router signs (kms:Sign) with the actor it has already verified from the inbound
JWT; the broker verifies (kms:Verify). The signing key never leaves KMS, so nothing
inside a microVM can forge a ticket for another user.

Two properties this scheme does NOT have, stated because they shape how it may be used:

  - It is a bearer capability. Whoever holds a valid ticket can obtain credentials for
    that user's files until it expires. It lives in tmpfs inside the one microVM that
    needs it, and expiry is the only revocation.
  - It says nothing about *who asked* for it. So the signer must never accept a subject
    as input — the router derives it from the verified identity, which is the whole
    reason ticket minting lives there. The reference implementation this follows signs
    whatever session id its CLI is handed; that is safe for an operator tool and unsafe
    the moment it is wired to a service.
"""

from __future__ import annotations

import base64
import json
import time

import boto3
from botocore.config import Config

SIGNING_ALGORITHM = "ECDSA_SHA_256"

# KMS sits on the critical path of every credential refresh — the mount goes stale if it
# fails — so retry rather than let a throttle unmount a user's files.
_KMS_CONFIG = Config(retries={"mode": "standard", "max_attempts": 4})


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _payload(subject: str, ttl_seconds: int) -> bytes:
    return json.dumps({"sub": subject, "exp": int(time.time()) + ttl_seconds, "v": 1},
                      separators=(",", ":"), sort_keys=True).encode()


def sign_ticket(subject: str, key_id: str, ttl_seconds: int, region: str) -> str:
    """Mint a ticket for `subject` (an actor id — "lark:{open_id}"), which the caller
    must have verified rather than accepted."""
    if not subject or "/" in subject or ".." in subject:
        # The subject becomes a path component downstream. Reject anything that could
        # traverse before it is ever signed, so a bad value cannot be laundered by a
        # valid signature.
        raise ValueError(f"refusing to sign an unusable subject: {subject!r}")
    payload = _payload(subject, ttl_seconds)
    sig = boto3.client("kms", region_name=region, config=_KMS_CONFIG).sign(
        KeyId=key_id, Message=payload, MessageType="RAW",
        SigningAlgorithm=SIGNING_ALGORITHM,
    )["Signature"]
    return _b64u(payload) + "." + _b64u(sig)


def verify_ticket(ticket: str, key_id: str, region: str) -> dict:
    """Verify signature and expiry; return the claims. Raises ValueError on anything
    wrong. The signature is checked *before* the payload is parsed as JSON, so unsigned
    bytes never reach the parser."""
    try:
        payload_b64, sig_b64 = ticket.split(".", 1)
        payload = _b64u_decode(payload_b64)
        signature = _b64u_decode(sig_b64)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"malformed ticket: {e}") from None

    kms = boto3.client("kms", region_name=region, config=_KMS_CONFIG)
    try:
        resp = kms.verify(
            KeyId=key_id, Message=payload, MessageType="RAW",
            Signature=signature, SigningAlgorithm=SIGNING_ALGORITHM,
        )
    except kms.exceptions.KMSInvalidSignatureException:
        raise ValueError("signature invalid") from None
    if not resp.get("SignatureValid"):
        raise ValueError("signature invalid")

    try:
        claims = json.loads(payload)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"signed payload is not JSON: {e}") from None
    if not isinstance(claims, dict) or not claims.get("sub"):
        raise ValueError("ticket names no subject")
    if int(claims.get("exp", 0)) < int(time.time()):
        raise ValueError("ticket expired")
    return claims
