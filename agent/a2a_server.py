"""A2A server: lets another agent delegate work that must happen as a specific person.

What this agent has that a peer does not is the identity chain — it can act in Lark as the
calling human, with that person's own vaulted token, and Lark adjudicates. So the capability
worth exposing over A2A is not "reasoning": it is "do this as that user".

Why a separate Runtime rather than another path on the existing one: the A2A contract binds
port 9000 and serves at the root (`POST /`, `GET /.well-known/agent-card.json`, `GET /ping`),
while the HTTP/AG-UI contract is 8080 under `/invocations`. Same image, different entrypoint
mode — `entrypoint.sh` branches on SERVER_MODE.

This server runs no turn of its own. A vaulted consent is scoped to the Runtime that obtained
it — measured: the same user, same workload, same provider and scopes, seen from a second
Runtime, reads as "never consented" — so only the agent Runtime can act for a user. A2A is
therefore a protocol adapter: it forwards the caller's own bearer to the agent Runtime and
returns what comes back. See docs/agentcore-behavior.md.

Which also keeps the trust boundary honest. A2A carries no end-user identity of its own, so a
caller wanting us to act as someone must hold that person's token — and a caller able to do
that is already trusted to speak for them. Nothing here widens that.
"""

from __future__ import annotations

import contextvars
import logging
import os
import urllib.parse
import uuid

import agent_core

log = logging.getLogger("agent.a2a")

_PORT = int(os.environ.get("A2A_PORT", "9000"))

# The caller's bearer for the request being served, forwarded unchanged: this server adds no
# identity of its own. A contextvar because the executor is handed a RequestContext, not the
# HTTP request.
_bearer: contextvars.ContextVar[str] = contextvars.ContextVar("bearer", default="")

_AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
_REGION = os.environ.get("AWS_REGION", "us-west-2")


async def _invoke_agent(bearer: str, text: str, actor_id: str = "") -> str:
    """One synchronous turn on the agent Runtime, as the caller."""
    import httpx
    url = (f"https://bedrock-agentcore.{_REGION}.amazonaws.com/runtimes/"
           f"{urllib.parse.quote(_AGENT_RUNTIME_ARN, safe='')}/invocations?qualifier=DEFAULT")
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post(url, json={"action": "chat", "message": text,
                                    "actorId": actor_id},
                         headers={"Authorization": bearer,
                                  "Content-Type": "application/json",
                                  "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id":
                                      "a2a-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]})
    r.raise_for_status()
    return r.json().get("reply", "")


def build_app():
    """The FastAPI app serving the A2A contract."""
    from a2a.types import AgentCapabilities, AgentCard, AgentSkill, Message, Part, Role
    from a2a.server.agent_execution import AgentExecutor, RequestContext
    from a2a.server.events import EventQueue
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes import (add_a2a_routes_to_fastapi, create_agent_card_routes,
                                   create_jsonrpc_routes)
    from a2a.server.tasks import InMemoryTaskStore
    from fastapi import FastAPI

    # enqueue_event is a coroutine on EventQueueSource even though the base class signature
    # reads synchronous — not awaiting it produces no event and the caller hangs (measured).
    def _reply(text: str) -> Message:
        return Message(message_id=str(uuid.uuid4()), role=Role.ROLE_AGENT,
                       parts=[Part(text=text)])

    class ForwardingExecutor(AgentExecutor):
        """Forwards a delegated task to the agent Runtime, as the caller."""

        async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
            bearer = _bearer.get()
            if not bearer:
                await event_queue.enqueue_event(_reply(
                    "This task carries no end-user identity, so it cannot be attributed to "
                    "anyone. Present the user's bearer token."))
                return
            # The peer names whom it acts for; the agent verifies that against the vaulted
            # token's owner, so a wrong name yields a consent prompt rather than their data.
            actor = (context.metadata or {}).get("actorId", "") if context.metadata else ""
            try:
                reply = await _invoke_agent(bearer, context.get_user_input(), actor)
            except Exception as e:  # noqa: BLE001
                log.exception("a2a forward failed")
                await event_queue.enqueue_event(_reply(f"Delegation failed ({type(e).__name__})."))
                return
            await event_queue.enqueue_event(_reply(reply))

        async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
            raise NotImplementedError("A delegated turn runs to completion or fails.")

    card = AgentCard(
        name="lark-identity-agent",
        description=(
            "Acts inside Lark (Feishu) as the end user the request is for, using that "
            "person's own consented token. Delegate work here when it must be attributable "
            "to a human and adjudicated by Lark's own permissions."),
        version="0.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[AgentSkill(
            id="act-as-user-in-lark",
            name="Act as the user in Lark",
            description=(
                "Read or write in the caller's Lark tenant as a specific person: their "
                "documents, their approvals. Requires that person to have consented once, "
                "and requires the caller to present their identity token — a caller able to "
                "do that is trusted to speak for them."),
            tags=["lark", "identity", "documents", "approvals"],
        )],
    )

    app = FastAPI(title="lark-identity-agent (A2A)")

    @app.get("/ping")
    async def ping():
        return {"status": "HealthyBusy" if agent_core.busy() else "Healthy"}

    handler = DefaultRequestHandler(agent_executor=ForwardingExecutor(),
                                    task_store=InMemoryTaskStore(), agent_card=card)
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        # v0.3 compat on: `message/send` is the method name AWS documents and standard
        # A2A clients call. Without it the endpoint answers "Method not found" (measured).
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/", enable_v0_3_compat=True),
    )
    return app


def _with_bearer(app):
    """Raw ASGI, not Starlette's http middleware: that runs `call_next` in a separate task,
    so a contextvar set there never reaches the endpoint (measured)."""
    async def _mw(scope, receive, send):
        if scope.get("type") == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            _bearer.set(headers.get("authorization", ""))
        await app(scope, receive, send)
    return _mw


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("A2A contract on :%d", _PORT)
    uvicorn.run(_with_bearer(build_app()), host="0.0.0.0", port=_PORT, log_level="info")


if __name__ == "__main__":
    main()
