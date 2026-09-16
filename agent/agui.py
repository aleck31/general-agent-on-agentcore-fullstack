"""AG-UI on the same Runtime, so a browser can stream a turn without a backend in between.

AgentCore proxies a streamed response under `serverProtocol=HTTP` (measured: chunks arrive
as the container writes them, `text/event-stream` passes through), so the AG-UI contract is
served from the existing agent Runtime rather than a second one with `serverProtocol=AGUI`.
One runtime, one image, one deployment. See docs/agentcore-behavior.md.

The browser calls `/invocations` directly with its Cognito JWT as Bearer; the platform's
CUSTOM_JWT authorizer verifies it. Nothing in the request body may name a user — see
`lark_3lo.actor_from_workload_token` for how the caller is derived instead.
"""

from __future__ import annotations

import json
import logging

from aiohttp import web

import agent_core
import lark_3lo

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
    kind, actor_id = await agent_core.arun_in_thread(
        lark_3lo.actor_from_workload_token, workload_token)
    if kind == "needs_consent":
        return await _error_stream(
            request, "请先在 Lark 聊天里发一条消息完成授权，然后回到网页继续。")
    if kind != "actor":
        return await _error_stream(request, "无法确认你的身份，请稍后再试。")

    try:
        from ag_ui.core.types import RunAgentInput
        from ag_ui_langgraph import LangGraphAgent
    except Exception:  # noqa: BLE001 — absent-not-broken, like every optional surface here
        log.exception("ag-ui packages unavailable")
        return await _error_stream(request, "Web chat is not available in this build.")

    # The session carries the tools, the checkpointer and the summarisation middleware, so a
    # web turn lands in the same conversation as a Lark turn for the same person.
    session = await agent_core.aget_session(actor_id, workload_token=workload_token)

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
