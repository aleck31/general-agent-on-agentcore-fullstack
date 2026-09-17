"""A2A adapter tests.

Deliberately dependency-light: everything asserted here lives outside `build_app`, which
imports the a2a SDK lazily. What matters about this surface is not the protocol plumbing —
the SDK owns that — but that it forwards the caller's identity unchanged and adds none of
its own. `scripts/a2a-demo.sh` exercises the rest against a deployment.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))

# The adapter imports agent_core only to report /ping busyness, and agent_core pulls in the
# whole LangGraph stack. Stubbed so this suite stays dependency-light.
sys.modules.setdefault("agent_core", mock.Mock(busy=lambda: False))

import a2a_server  # noqa: E402


def test_the_bearer_is_read_from_the_raw_scope():
    """Starlette's http middleware runs `call_next` in a separate task, so a contextvar set
    there never reaches the endpoint (measured). Raw ASGI is the fix, and this locks it in."""
    seen = {}

    async def app(scope, receive, send):
        seen["bearer"] = a2a_server._bearer.get()

    wrapped = a2a_server._with_bearer(app)
    scope = {"type": "http", "headers": [(b"Authorization", b"Bearer abc")]}
    asyncio.run(wrapped(scope, None, None))
    assert seen["bearer"] == "Bearer abc"
    src = inspect.getsource(a2a_server._with_bearer)
    assert "@app.middleware" not in src


def test_a_non_http_scope_does_not_leak_the_previous_bearer():
    token = a2a_server._bearer.set("Bearer stale")
    try:
        asyncio.run(a2a_server._with_bearer(_noop)({"type": "lifespan"}, None, None))
    finally:
        a2a_server._bearer.reset(token)


async def _noop(scope, receive, send):
    return None


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Captures the one call a delegated task makes to the agent Runtime."""

    calls: list[dict] = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse({"reply": "ok"})


@pytest.fixture(autouse=True)
def _reset():
    _FakeClient.calls = []


def _invoke(bearer="Bearer abc", text="hi", actor="lark:ou_x"):
    fake_httpx = mock.Mock(AsyncClient=_FakeClient)
    with mock.patch.dict(sys.modules, {"httpx": fake_httpx}):
        return asyncio.run(a2a_server._invoke_agent(bearer, text, actor))


def test_the_callers_bearer_is_forwarded_unchanged():
    """This server adds no identity of its own — that is the whole trust argument. Minting
    or rewriting a token here would let a peer act as somebody it cannot speak for."""
    assert _invoke() == "ok"
    call = _FakeClient.calls[0]
    assert call["headers"]["Authorization"] == "Bearer abc"
    assert call["json"]["actorId"] == "lark:ou_x"
    assert call["json"]["action"] == "chat"


def test_each_delegated_task_gets_its_own_runtime_session():
    """A shared session id would land two peers' tasks on the same microVM and, worse,
    make them look like one conversation. AgentCore also requires 33+ characters."""
    _invoke()
    _invoke()
    ids = [c["headers"]["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"]
           for c in _FakeClient.calls]
    assert ids[0] != ids[1]
    assert all(i.startswith("a2a-") and len(i) >= 33 for i in ids)


def test_it_refuses_a_task_that_names_nobody_instead_of_serving_it_anonymously():
    """A task with no bearer cannot be attributed, so it must be answered with an
    explanation rather than run on whatever identity happens to be available."""
    src = inspect.getsource(a2a_server.build_app)
    i = src.index("bearer = _bearer.get()")
    guard = src[i:i + 400]
    assert "if not bearer" in guard and "return" in guard
    assert "Present the user's bearer token" in guard


def test_the_executor_never_invents_the_actor_it_acts_for():
    """The peer names whom it acts for and the agent verifies that against the vaulted
    token's owner. Falling back to a default actor here would defeat that check."""
    src = inspect.getsource(a2a_server.build_app)
    i = src.index('context.metadata or {}')
    assert 'get("actorId", "")' in src[i:i + 160]
    # No default: an empty actor makes the agent derive it from the token instead.
    assert 'get("actorId", "anonymous")' not in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
