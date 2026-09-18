"""Code-execution tool tests.

What matters here is not that a sandbox runs code — the platform does that — but that the
right workspace is mounted and that everything the model writes lands in it. Both are ours
to get wrong, and either mistake is invisible in a passing conversation: files silently
vanish, or worse, belong to somebody else.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import code_tools  # noqa: E402

_FS = "arn:aws:s3files:ap-northeast-1:1234:file-system/fs-abc"


@pytest.fixture(autouse=True)
def _deployed():
    with mock.patch.object(code_tools, "_INTERPRETER_ID", "ci-1"), \
         mock.patch.object(code_tools, "_FS_ARN", _FS), \
         mock.patch.object(code_tools, "_WORKSPACE", "/mnt/workspace"):
        yield


class _FakeBox:
    """Records what would have been sent to InvokeCodeInterpreter."""

    def __init__(self):
        self.calls = []

    def invoke(self, name, arguments):
        self.calls.append((name, arguments))
        return "ok"


def _tools(box=None):
    return {t.name: t for t in code_tools.tools_for("lark:ou_x", box or _FakeBox())}


def test_absent_not_broken_when_no_interpreter_is_deployed():
    """Same convention as search and long-term memory: the agent simply cannot run code."""
    with mock.patch.object(code_tools, "_INTERPRETER_ID", ""):
        assert code_tools.tools_for("lark:ou_x") == []
    with mock.patch.object(code_tools, "_FS_ARN", ""):
        assert code_tools.tools_for("lark:ou_x") == []


def test_every_tool_runs_in_the_mounted_workspace():
    """The sandbox's own working directory is elsewhere and dies with the session, so a
    tool that forgets to cd writes files that silently disappear between turns."""
    box = _FakeBox()
    t = _tools(box)
    t["run_code"].invoke({"code": "print(1)"})
    t["run_command"].invoke({"command": "ls"})
    t["list_files"].invoke({})
    assert len(box.calls) == 3
    for name, args in box.calls:
        payload = args.get("code") or args.get("command")
        assert "/mnt/workspace" in payload, (name, payload)


def test_the_workspace_is_never_named_by_the_model():
    """No tool takes a path outside the workspace or an actor id: the mount is chosen for
    the model, not by it."""
    for tool in _tools().values():
        fields = tool.args_schema.model_fields
        assert "actor_id" not in fields and "actorId" not in fields
        assert "mount_path" not in fields and "workspace" not in fields


def test_a_broken_sandbox_reports_instead_of_killing_the_turn():
    class _Boom:
        def invoke(self, name, arguments):
            raise RuntimeError("no session")
    out = _tools(_Boom())["run_code"].invoke({"code": "print(1)"})
    assert "could not run" in out and "RuntimeError" in out


def test_the_access_point_root_is_derived_from_the_actor_and_sanitised():
    """It becomes a filesystem path, and an actor id carries a colon. The shape also has
    to match what the previous implementation used, or existing files become unreachable."""
    assert code_tools._ap_path("lark:ou_abc") == "/users/lark_ou_abc"
    assert code_tools._ap_path("lark:../../etc") == "/users/lark_.._.._etc"


def test_an_existing_access_point_is_reused_rather_than_duplicated():
    """A second Access Point for the same user would split their files in two, and Access
    Points are a limited resource."""
    fake = mock.Mock()
    fake.list_access_points.return_value = {"accessPoints": [
        {"rootDirectory": {"path": "/users/other"}, "accessPointArn": "arn:other"},
        {"rootDirectory": {"path": "/users/lark_ou_x"}, "accessPointArn": "arn:mine"},
    ]}
    with mock.patch.object(code_tools.boto3, "client", return_value=fake):
        assert code_tools._access_point_for("lark:ou_x") == "arn:mine"
    assert fake.create_access_point.call_count == 0


def test_a_new_access_point_is_created_idempotently_and_rooted_server_side():
    fake = mock.Mock()
    fake.list_access_points.return_value = {"accessPoints": []}
    fake.create_access_point.return_value = {"accessPointArn": "arn:new"}
    with mock.patch.object(code_tools.boto3, "client", return_value=fake):
        assert code_tools._access_point_for("lark:ou_x") == "arn:new"
    kw = fake.create_access_point.call_args.kwargs
    # rootDirectory is what confines the mount, and the service enforces it.
    assert kw["rootDirectory"]["path"] == "/users/lark_ou_x"
    # clientToken makes a concurrent second call return the same Access Point.
    assert kw["clientToken"] and kw["clientToken"] == mock.ANY
    assert code_tools._access_point_for.__doc__


def test_the_session_mounts_this_actors_access_point_and_starts_lazily():
    """Starting a session costs a microVM, so it must not happen while merely building the
    tool list — only when the model actually runs something."""
    client = mock.Mock()
    client.start_code_interpreter_session.return_value = {"sessionId": "s-1"}
    with mock.patch.object(code_tools.boto3, "client", return_value=client):
        box = code_tools._Sandbox("lark:ou_x")
        assert client.start_code_interpreter_session.call_count == 0
        with mock.patch.object(code_tools, "_access_point_for", return_value="arn:mine"):
            assert box.session_id() == "s-1"
            assert box.session_id() == "s-1"          # reused, not restarted
    assert client.start_code_interpreter_session.call_count == 1
    kw = client.start_code_interpreter_session.call_args.kwargs
    mount = kw["filesystemConfigurations"][0]["s3FilesConfiguration"]
    assert mount["accessPointArn"] == "arn:mine"
    # Measured constraint: mountPath must match /mnt/[a-zA-Z0-9._-]+/?
    assert mount["mountPath"].startswith("/mnt/")


def test_closing_the_session_stops_the_sandbox():
    """A code session left running is billed until it times out, and _close_session is the
    only place that knows a cached entry is being discarded."""
    import agent_core
    src = inspect.getsource(agent_core._close_session)
    assert 'get("sandbox")' in src and ".close()" in src


def test_output_is_capped_from_the_middle_so_the_exception_survives():
    """A Python traceback puts the exception last, so trimming the tail throws away the one
    line that says what went wrong and keeps the call stack that does not."""
    body = "HEAD-MARKER\n" + "x" * 99999 + "\nValueError: the actual problem"
    client = mock.Mock()
    client.invoke_code_interpreter.return_value = {"stream": [
        {"result": {"structuredContent": {"stdout": body, "stderr": ""}}}]}
    with mock.patch.object(code_tools.boto3, "client", return_value=client):
        box = code_tools._Sandbox("lark:ou_x")
        box._session_id = "s-1"
        out = box.invoke("executeCommand", {"command": "boom"})
    assert len(out) < len(body)
    assert out.startswith("HEAD-MARKER")
    assert out.endswith("ValueError: the actual problem")
    assert "chars omitted" in out


def test_an_expired_session_is_replaced_instead_of_failing_every_later_call():
    """A code session times out well before the cached agent session does, so a held id
    goes stale mid-conversation. Retrying is safe here and only here: the error is raised
    before the code runs, so nothing executes twice."""
    client = mock.Mock()
    client.start_code_interpreter_session.side_effect = [
        {"sessionId": "s-old"}, {"sessionId": "s-new"}]
    client.invoke_code_interpreter.side_effect = [
        Exception("ValidationException: Code interpreter session s-old is not active"),
        {"stream": [{"result": {"structuredContent": {"stdout": "ok", "stderr": ""}}}]}]
    with mock.patch.object(code_tools.boto3, "client", return_value=client), \
         mock.patch.object(code_tools, "_access_point_for", return_value="arn:mine"):
        box = code_tools._Sandbox("lark:ou_x")
        assert box.invoke("executeCommand", {"command": "ls"}) == "ok"
    assert client.start_code_interpreter_session.call_count == 2
    assert client.invoke_code_interpreter.call_args_list[-1].kwargs["sessionId"] == "s-new"


def test_any_other_failure_is_not_retried():
    """Retrying a call that may already have run would duplicate a write."""
    client = mock.Mock()
    client.start_code_interpreter_session.return_value = {"sessionId": "s-1"}
    client.invoke_code_interpreter.side_effect = Exception("ThrottlingException: slow down")
    with mock.patch.object(code_tools.boto3, "client", return_value=client), \
         mock.patch.object(code_tools, "_access_point_for", return_value="arn:mine"):
        box = code_tools._Sandbox("lark:ou_x")
        with pytest.raises(Exception, match="Throttling"):
            box.invoke("executeCommand", {"command": "ls"})
    assert client.invoke_code_interpreter.call_count == 1


def test_a_failure_with_no_output_still_says_something():
    """"" back to the model reads as success. It is not."""
    client = mock.Mock()
    client.invoke_code_interpreter.return_value = {"stream": [
        {"result": {"isError": True, "content": [], "structuredContent": {}}}]}
    with mock.patch.object(code_tools.boto3, "client", return_value=client):
        box = code_tools._Sandbox("lark:ou_x")
        box._session_id = "s-1"
        assert "failed" in box.invoke("executeCode", {"language": "python", "code": ""})


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
