"""AG-UI on the same Runtime, so a browser can stream a turn without a backend in between.

AgentCore proxies a streamed response under `serverProtocol=HTTP` (measured: chunks arrive
as the container writes them, `text/event-stream` passes through), so the AG-UI contract is
served from the existing agent Runtime rather than a second one with `serverProtocol=AGUI`.
One runtime, one image, one deployment. See docs/agentcore-behavior.md.

The browser calls `/invocations` directly with its Cognito JWT as Bearer; the platform's
CUSTOM_JWT authorizer verifies it. It also names the actor, and is not believed on it —
building that actor's session runs the ownership check that refuses a mismatch.
"""

from __future__ import annotations

import json
import logging

from aiohttp import web

import agent_core

log = logging.getLogger("agent.agui")

# AG-UI's own request shape. `action` marks the router's JSON protocol instead, so the two
# share `/invocations` without ambiguity.
_AGUI_KEYS = ("threadId", "messages", "runId")


def is_agui_request(payload: dict) -> bool:
    return "action" not in payload and any(k in payload for k in _AGUI_KEYS)


def _sse(event) -> bytes:
    """One AG-UI event as an SSE frame. Pydantic models serialise by alias, which is what
    the protocol's camelCase field names require."""
    body = event.model_dump_json(by_alias=True, exclude_none=True)
    return f"data: {body}\n\n".encode()


async def _error_stream(request: web.Request, message: str) -> web.StreamResponse:
    """A RUN_ERROR stream. HTTP 200 on purpose: the AG-UI contract distinguishes a
    connection-level failure (non-2xx, before the stream) from an agent-level one, which
    travels as an event so the client can render it in the conversation."""
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-cache"})
    await resp.prepare(request)
    await resp.write(f'data: {json.dumps({"type": "RUN_ERROR", "message": message})}\n\n'
                     .encode())
    await resp.write_eof()
    return resp


async def handle(request: web.Request, payload: dict,
                 workload_token: str) -> web.StreamResponse:
    """Run one AG-UI turn, streaming events as they are produced."""
    # The caller names the actor and is not believed on it: building the session fetches
    # that actor's vaulted token, and lark_3lo's ownership check asks Lark whose token it is
    # and refuses a mismatch. So a page claiming somebody else gets a consent prompt, never
    # their data. Deriving the actor instead is not possible — customState participates in
    # the vault lookup and is built from the actor (see docs/agentcore-behavior.md).
    #
    # Session building is the one path that has been proven end to end, so it is reused
    # rather than re-implemented: a parallel check drifted and reported "not authorised" for
    # a user the proven path could serve.
    actor_id = payload.get("actorId") or ""
    if not actor_id:
        return await _error_stream(request, "请求缺少身份信息，请重新打开页面。")

    try:
        from ag_ui.core.types import RunAgentInput
        from ag_ui_langgraph import LangGraphAgent
    except Exception:  # noqa: BLE001 — absent-not-broken, like every optional surface here
        log.exception("ag-ui packages unavailable")
        return await _error_stream(request, "Web chat is not available in this build.")

    # The session carries the tools, the checkpointer and the summarisation middleware, so a
    # web turn lands in the same conversation as a Lark turn for the same person.
    session = await agent_core.aget_session(actor_id, workload_token=workload_token)
    log.info("agui gate: auth_url=%s identity_error=%s tools=%s keys=%s",
             bool(session.get("auth_url")), bool(session.get("identity_error")),
             len((session.get("graph") and getattr(session["graph"], "nodes", None)) or []) or "?",
             sorted(session.keys()))
    if session.get("auth_url"):
        return await _error_stream(
            request, "请先在 Lark 聊天里完成一次授权，然后回到网页继续。")
    if session.get("identity_error"):
        return await _error_stream(request, "暂时无法访问你的 Lark 账号，请稍后再试。")

    # thread_id comes from the session, never from the request: it decides whose
    # conversation this is.
    run_input = RunAgentInput(**{**payload,
                                "threadId": session["mem_sid"],
                                "runId": payload.get("runId") or session["mem_sid"]})
    agui = LangGraphAgent(name="agentcore-fullstack", graph=session["graph"],
                          config=session["config"])
    # The graph's MCP sessions live on agent_core's own loop, so the run has to happen
    # there and the events come back over a queue.
    events = agent_core.aiter_on_agent_loop(lambda: agui.run(run_input))

    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    agent_core.track_in_flight(+1)   # /ping reports HealthyBusy while this runs
    try:
        async for event in events:
            await resp.write(_sse(event))
    except Exception as e:  # noqa: BLE001 — the stream is open, so report in-band
        log.exception("agui turn failed for %s", actor_id)
        await resp.write(f'data: {json.dumps({"type": "RUN_ERROR", "message": type(e).__name__})}\n\n'
                         .encode())
    finally:
        agent_core.track_in_flight(-1)
        await resp.write_eof()
    return resp
