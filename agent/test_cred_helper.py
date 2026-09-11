"""Unit tests for the mount credential helper. The broker is mocked; no AWS.

This is the one piece of the mount machinery baked into the image, and the efs-utils
watchdog re-runs it for the life of the session — so its stdout contract matters as much
as its logic: anything other than exactly the expected shape and the mount silently loses
its credentials instead of erroring.
"""

from __future__ import annotations

import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(__file__))


def _load(tmp_path, broker_reply, *, ap_file_contents=None, ticket="tkt"):
    """Import cred_helper with tmpfs paths redirected and the Lambda call mocked."""
    ticket_file = tmp_path / "ticket"
    ticket_file.write_text(ticket)
    ap_file = tmp_path / "ap"
    if ap_file_contents is not None:
        ap_file.write_text(ap_file_contents)

    env = {
        "MOUNT_TICKET_FILE": str(ticket_file),
        "MOUNT_AP_FILE": str(ap_file),
        "MOUNT_BROKER_FN": "broker-fn",
        "AWS_REGION": "us-west-2",
    }
    calls = []

    class FakeLambda:
        def invoke(self, FunctionName, Payload):  # noqa: N803
            calls.append((FunctionName, json.loads(Payload)))
            reply = broker_reply(json.loads(Payload)) if callable(broker_reply) else broker_reply
            return {"Payload": mock.Mock(read=lambda: json.dumps(reply).encode())}

    with mock.patch.dict(os.environ, env, clear=False):
        import importlib
        import cred_helper
        importlib.reload(cred_helper)
        with mock.patch.object(cred_helper.boto3, "client", return_value=FakeLambda()):
            yield cred_helper, calls


_CREDS = {"AccessKeyId": "AKIA", "SecretAccessKey": "secret", "SessionToken": "tok",
          "Expiration": "2026-01-01T00:00:00+00:00"}


def test_provision_prints_only_the_access_point_id(tmp_path, capsys):
    """The bootstrap redirects this into a file, so a stray newline or log line would end
    up inside the access point id."""
    for helper, calls in _load(tmp_path, {"access_point_id": "ap-1",
                                          "credentials": _CREDS}):
        assert helper.main(["cred_helper.py", "--provision"]) == 0
        out = capsys.readouterr().out
        assert out == "ap-1", f"stdout must be exactly the id, got {out!r}"
        assert calls[0][1]["action"] == "provision"


def test_credentials_emit_exactly_the_credential_process_shape(tmp_path, capsys):
    for helper, calls in _load(tmp_path, {"credentials": _CREDS}, ap_file_contents="ap-1"):
        assert helper.main(["cred_helper.py"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"Version": 1, "AccessKeyId": "AKIA",
                           "SecretAccessKey": "secret", "SessionToken": "tok",
                           "Expiration": "2026-01-01T00:00:00+00:00"}
        # The refresh path must reuse the recorded access point, not create one.
        assert calls[0][1] == {"ticket": "tkt", "action": "credentials", "apId": "ap-1"}


def test_a_refresh_without_a_recorded_access_point_falls_back_to_provision(tmp_path, capsys):
    """Covers the ordering case where a refresh runs before the bootstrap wrote the file.
    Provision is idempotent, so this costs one call and returns the same id."""
    def reply(payload):
        if payload["action"] == "provision":
            return {"access_point_id": "ap-1", "credentials": _CREDS}
        return {"credentials": _CREDS}

    for helper, calls in _load(tmp_path, reply):   # no ap file
        assert helper.main(["cred_helper.py"]) == 0
        assert [c[1]["action"] for c in calls] == ["provision", "credentials"]
        assert calls[1][1]["apId"] == "ap-1"
        assert json.loads(capsys.readouterr().out)["Version"] == 1


def test_a_refused_request_fails_loudly_instead_of_printing_nothing(tmp_path, capsys):
    """An empty stdout would read to the mount helper as "no credentials available",
    which is indistinguishable from a permission problem. Fail instead."""
    for helper, _ in _load(tmp_path, {"error": "PermissionError"}, ap_file_contents="ap-1"):
        with pytest.raises(RuntimeError, match="broker refused"):
            helper.main(["cred_helper.py"])
        assert capsys.readouterr().out == ""


def test_the_ticket_is_read_from_tmpfs_every_call(tmp_path, capsys):
    """The router can replace the ticket mid-session (a re-bootstrap), so nothing may be
    cached across invocations — which the watchdog gets for free by re-running us."""
    for helper, calls in _load(tmp_path, {"credentials": _CREDS},
                               ap_file_contents="ap-1", ticket="first"):
        helper.main(["cred_helper.py"])
        assert calls[0][1]["ticket"] == "first"


def test_a_missing_broker_name_is_an_error_not_a_silent_no_op(tmp_path):
    for helper, _ in _load(tmp_path, {"credentials": _CREDS}, ap_file_contents="ap-1"):
        with mock.patch.object(helper, "BROKER_FN", ""):
            with pytest.raises(RuntimeError, match="MOUNT_BROKER_FN"):
                helper.main(["cred_helper.py"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
