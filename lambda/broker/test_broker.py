"""Unit tests for the mount-credential broker. No AWS: KMS, s3files and STS are mocked.

What these cover is the whole reason the broker exists — that a ticket cannot be used to
reach another user's files. The mount itself, the VPC and the bootstrap can only be
verified against a real deployment.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault("FILE_SYSTEM_ID", "fs-test")
os.environ.setdefault("MOUNT_ROLE_ARN", "arn:aws:iam::111122223333:role/mount")
os.environ.setdefault("KMS_KEY_ID", "alias/test")
os.environ.setdefault("ACCOUNT_ID", "111122223333")
os.environ.setdefault("AWS_REGION", "us-west-2")

import ticket as ticket_mod


# ------------------------------- tickets -------------------------------------
# The signature is the trust root: a microVM holds a ticket, and nothing else about it
# is trusted. So the tests care about what a *forged* or *stale* ticket does.

class FakeKMS:
    """Signs by prefixing the payload — enough to tell a valid pairing from a forged
    one without a real key."""

    class exceptions:
        class KMSInvalidSignatureException(Exception):
            pass

    def __init__(self):
        self.signed = []

    def sign(self, KeyId, Message, MessageType, SigningAlgorithm):  # noqa: N803
        self.signed.append((KeyId, Message, SigningAlgorithm))
        return {"Signature": b"sig:" + Message}

    def verify(self, KeyId, Message, MessageType, Signature, SigningAlgorithm):  # noqa: N803
        if Signature != b"sig:" + Message:
            raise FakeKMS.exceptions.KMSInvalidSignatureException("nope")
        return {"SignatureValid": True}


def _with_kms(fake):
    return mock.patch.object(ticket_mod.boto3, "client", return_value=fake)


def test_a_ticket_round_trips_and_names_the_subject():
    fake = FakeKMS()
    with _with_kms(fake):
        t = ticket_mod.sign_ticket("lark:ou_alice", "alias/test", 900, "us-west-2")
        claims = ticket_mod.verify_ticket(t, "alias/test", "us-west-2")
    assert claims["sub"] == "lark:ou_alice"
    assert claims["exp"] > int(time.time())


def test_a_tampered_subject_is_rejected():
    """The whole point: rewriting the payload to name another user must not verify."""
    fake = FakeKMS()
    with _with_kms(fake):
        t = ticket_mod.sign_ticket("lark:ou_alice", "alias/test", 900, "us-west-2")
        payload_b64, sig_b64 = t.split(".", 1)
        forged_payload = json.dumps({"sub": "lark:ou_bob", "exp": int(time.time()) + 900,
                                     "v": 1}, separators=(",", ":"), sort_keys=True)
        forged = (base64.urlsafe_b64encode(forged_payload.encode()).rstrip(b"=").decode()
                  + "." + sig_b64)
        with pytest.raises(ValueError, match="signature invalid"):
            ticket_mod.verify_ticket(forged, "alias/test", "us-west-2")


def test_an_expired_ticket_is_rejected_even_though_it_verifies():
    fake = FakeKMS()
    with _with_kms(fake):
        t = ticket_mod.sign_ticket("lark:ou_alice", "alias/test", -1, "us-west-2")
        with pytest.raises(ValueError, match="expired"):
            ticket_mod.verify_ticket(t, "alias/test", "us-west-2")


def test_a_malformed_ticket_never_reaches_the_json_parser():
    fake = FakeKMS()
    with _with_kms(fake):
        for bad in ("", "no-dot", "!!!.???"):
            with pytest.raises(ValueError):
                ticket_mod.verify_ticket(bad, "alias/test", "us-west-2")


def test_a_subject_that_could_traverse_is_never_signed():
    """The subject becomes a path component. A valid signature over "../../x" would
    launder a traversal, so it is refused at signing time rather than later."""
    fake = FakeKMS()
    with _with_kms(fake):
        for bad in ("", "lark:../../etc", "lark:a/b"):
            with pytest.raises(ValueError, match="unusable subject"):
                ticket_mod.sign_ticket(bad, "alias/test", 900, "us-west-2")


# ------------------------------- the broker ----------------------------------

class FakeS3Files:
    class exceptions:
        class ConflictException(Exception):
            def __init__(self, resource_id=None):
                super().__init__("exists")
                self.response = {"resourceId": resource_id} if resource_id else {}

    def __init__(self, existing=None, roots=None):
        self.existing = existing        # ap id returned via ConflictException
        self.roots = roots or {}        # ap id -> rootDirectory path
        self.created = []

    def create_access_point(self, **kw):
        self.created.append(kw)
        if self.existing:
            raise FakeS3Files.exceptions.ConflictException(self.existing)
        ap = "ap-new"
        self.roots[ap] = kw["rootDirectory"]["path"]
        return {"accessPointId": ap}

    def get_access_point(self, accessPointId):  # noqa: N803
        return {"rootDirectory": {"path": self.roots[accessPointId]}}


class FakeSTS:
    def __init__(self):
        self.calls = []

    def assume_role(self, **kw):
        self.calls.append(kw)
        return {"Credentials": {
            "AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t",
            "Expiration": mock.Mock(isoformat=lambda: "2026-01-01T00:00:00+00:00"),
        }}


def _load_broker(s3files, sts, subject="lark:ou_alice"):
    """Import the handler with its clients replaced and verification stubbed to a known
    subject, so each test exercises one thing."""
    import index
    return (mock.patch.object(index, "_s3files", s3files),
            mock.patch.object(index, "_sts", sts),
            mock.patch.object(index, "verify_ticket", return_value={"sub": subject}),
            index)


def test_the_root_directory_is_derived_from_the_subject_not_supplied():
    import index
    assert index.root_directory("lark:ou_alice") == "/users/lark_ou_alice"
    # A colon cannot appear in a path component, so it is replaced rather than dropped —
    # dropping it would let two different actors collide on one directory.
    assert index.root_directory("lark:ou_a") != index.root_directory("larkou_a")


def test_provision_pins_the_access_point_to_this_user_only():
    s3, sts = FakeS3Files(), FakeSTS()
    p1, p2, p3, index = _load_broker(s3, sts)
    with p1, p2, p3:
        out = index.broker("t", "provision")
    assert s3.created[0]["rootDirectory"]["path"] == "/users/lark_ou_alice"
    assert s3.created[0]["posixUser"] == {"uid": 1000, "gid": 1000}
    policy = json.loads(sts.calls[0]["Policy"])
    conditions = [st["Condition"]["StringEquals"] for st in policy["Statement"]
                  if "StringEquals" in (st.get("Condition") or {})]
    arns = {v for cond in conditions for k, v in cond.items() if k.endswith("AccessPointArn")}
    assert arns == {out["access_point_arn"]}, "credentials must pin exactly one AP"
    # Every mount/write permission must carry such a condition; an unconditioned one
    # would let these credentials mount anything.
    mounty = [st for st in policy["Statement"]
              if any("ClientMount" in a or "ClientWrite" in a
                     for a in (st["Action"] if isinstance(st["Action"], list) else [st["Action"]]))]
    assert mounty and all("StringEquals" in (st.get("Condition") or {}) for st in mounty)


def test_provision_is_idempotent_for_a_returning_user():
    """A user's Access Point is created once and reused across every later session."""
    s3, sts = FakeS3Files(existing="ap-existing"), FakeSTS()
    p1, p2, p3, index = _load_broker(s3, sts)
    with p1, p2, p3:
        out = index.broker("t", "provision")
    assert out["access_point_id"] == "ap-existing"


def test_credentials_refuses_another_users_access_point():
    """The attack this check exists for: a valid ticket for Alice, plus Bob's access
    point id. Alice proves who she is; the id is only what she asked about."""
    s3 = FakeS3Files(roots={"ap-bob": "/users/lark_ou_bob"})
    sts = FakeSTS()
    p1, p2, p3, index = _load_broker(s3, sts)
    with p1, p2, p3:
        with pytest.raises(PermissionError, match="rooted at"):
            index.broker("t", "credentials", "ap-bob")
    assert sts.calls == [], "no credentials may be minted for a refused request"


def test_credentials_accepts_the_users_own_access_point_without_creating_anything():
    s3 = FakeS3Files(roots={"ap-alice": "/users/lark_ou_alice"})
    sts = FakeSTS()
    p1, p2, p3, index = _load_broker(s3, sts)
    with p1, p2, p3:
        out = index.broker("t", "credentials", "ap-alice")
    assert out["credentials"]["AccessKeyId"] == "AKIA"
    assert s3.created == [], "the refresh path must not create access points"


def test_credentials_requires_an_access_point_id():
    s3, sts = FakeS3Files(), FakeSTS()
    p1, p2, p3, index = _load_broker(s3, sts)
    with p1, p2, p3:
        with pytest.raises(ValueError, match="access point id"):
            index.broker("t", "credentials", "")


def test_the_handler_tells_a_microvm_nothing_about_why_it_failed():
    """The caller may be running model-generated code, so the reason goes to the log."""
    s3 = FakeS3Files(roots={"ap-bob": "/users/lark_ou_bob"})
    sts = FakeSTS()
    p1, p2, p3, index = _load_broker(s3, sts)
    with p1, p2, p3:
        out = index.handler({"ticket": "t", "action": "credentials", "apId": "ap-bob"},
                            None)
    assert out == {"error": "PermissionError"}
    assert "lark_ou_bob" not in json.dumps(out)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
