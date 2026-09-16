"""Core agent: a LangGraph ReAct agent on Bedrock, with AgentCore Memory for session
continuity and per-user MCP tool identity.

Server-reuse model: the model, the MCP sessions and the compiled graph are built ONCE
per session and cached — not rebuilt per message. Rebuilding per message re-handshakes
every MCP server and re-lists tools, which adds ~15–20s of latency. AgentCore gives
each session its own microVM, so the cache holds essentially one entry per container.

Memory is two independent backends, because the two jobs have different requirements
(see .dev/adr/0008):
  - DynamoDBSaver — the checkpointer, i.e. the conversation itself. Full graph state
    (messages with their tool calls and results, channels, pending writes), addressed by
    thread_id alone — the sha256 of the actor. Nothing about the container is part of the
    key, so a turn cut off at the 15-minute invoke cap resumes in a brand-new microVM.
    Written per superstep, so the durable record advances *during* a turn: there is no
    shutdown hook on a microVM, and a design that flushes at the end loses the turn.
  - AgentCore Memory — long-term records only, reached through the `remember`/`recall`
    tools in memory_tools. Nothing is written per turn: retrieval is the priced operation,
    so storing every exchange and querying blindly is the pattern that makes cost scale
    with chat volume instead of with need. What to keep is the model's explicit decision.

Per-user Lark access (agent-side 3LO): for each end-user the agent fetches that user's
vaulted Lark token from AgentCore Identity (GetResourceOauth2Token, USER_FEDERATION)
and connects directly to the lark-mcp Runtime, passing the token in a custom header
(the server reads it and calls Lark as that user). The token gates tool *calls*, not
the connection — lark-mcp lists its tools without one — so an unauthorized user gets a
working session and is only asked to consent if the model actually reaches for a Lark
tool. 3LO is agent-side because a Runtime-hosted MCP server cannot be handed a per-user
token by the Gateway at all (see docs/agentcore-behavior.md).

`chat_result` returns the final text; `chat_async` streams it into a Lark card.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import queue
import threading
import time
from contextlib import AsyncExitStack

from langchain.agents import create_agent
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_mcp_adapters.sessions import create_session
from langchain_mcp_adapters.tools import load_mcp_tools

import lark_3lo
import lark_notify
import memory_tools
import websearch

log = logging.getLogger("agent.core")

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-5")
_MEMORY_ID = os.environ.get("BEDROCK_AGENTCORE_MEMORY_ID", "")
_CHECKPOINT_TABLE = os.environ.get("CHECKPOINT_TABLE", "")
_CHECKPOINT_BUCKET = os.environ.get("CHECKPOINT_BUCKET", "")
# One thread per user, rotated only by /reset, so the state has to expire on its own.
_CHECKPOINT_TTL = int(os.environ.get("CHECKPOINT_TTL_DAYS", "365")) * 86400
# Empty unless ./deploy.sh mcp approval ran — the approval tools are opt-in.
_APPROVAL_MCP_URL = os.environ.get("APPROVAL_MCP_URL", "")
_SYSTEM = os.environ.get(
    "AGENT_SYSTEM_PROMPT",
    "You are a helpful assistant embedded in Lark. Be concise. "
    "Use the provided tools when they help answer the user.",
)
# Rebuild a cached session before its Cognito access token (~1h) expires.
_SESSION_TTL = int(os.environ.get("SESSION_TTL_SECONDS", "3000"))  # 50 min
# An unauthorized session is cached only briefly: it works, but the moment the user
# consents we want the next turn to pick up the token.
_UNAUTH_TTL = int(os.environ.get("UNAUTH_SESSION_TTL_SECONDS", "60"))

# Bedrock caches nothing without a cachePoint block, and `cache_control` is only read from
# per-call kwargs — hence the subclass below rather than a constructor argument.
# Placement, TTL choice and the measured break-even: docs/architecture.md.
_CACHE_TTL = os.environ.get("PROMPT_CACHE_TTL", "1h")   # "5m" | "1h" | "" disables


class _CachingChatBedrockConverse(ChatBedrockConverse):
    """ChatBedrockConverse that asks for prompt caching on every request.

    langchain-aws does the placement AWS documents — after the system prompt, after the
    tool definitions, and a rolling pair at the end of the message list, up to Bedrock's
    limit of four. All we supply is the intent.

    Before tuning this, know that the 1,024-token minimum for Sonnet 5 is cumulative over
    tools + system + messages in that order. Measured against the deployed servers, our
    tool definitions are ~1,618 tokens with the approval server and ~400 without it, and
    the system prompt is ~32 — so on a minimal deployment the tools checkpoint earns
    nothing until the conversation itself grows past the minimum.
    """

    cache_ttl: str = "1h"

    def _with_cache(self, kwargs: dict) -> dict:
        # An explicit per-call value wins: a caller asking for something specific should
        # not be silently overridden.
        if self.cache_ttl and "cache_control" not in kwargs:
            kwargs["cache_control"] = {"ttl": self.cache_ttl}
        return kwargs

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return super()._generate(messages, stop=stop, run_manager=run_manager,
                                 **self._with_cache(kwargs))

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        return super()._stream(messages, stop=stop, run_manager=run_manager,
                               **self._with_cache(kwargs))


# The class defines no _agenerate/_astream, so the async paths run these in a thread —
# overriding the two synchronous entry points covers streaming and non-streaming, sync
# and async alike.
_model = _CachingChatBedrockConverse(model=_MODEL_ID, region_name=_REGION,
                                     cache_ttl=_CACHE_TTL)

# --------------------------- bounding an endless thread ----------------------
# The thread is permanent per user and DynamoDBSaver has no prune, so summarisation is the
# only bound. Threshold-triggered, never per turn: it rewrites the cached prefix.
# Why late, and why `tokens` over `fraction`: .dev/adr/0008.
_SUMMARIZE_AT_TOKENS = int(os.environ.get("SUMMARIZE_AT_TOKENS", "120000"))  # 0 disables
_SUMMARIZE_KEEP = int(os.environ.get("SUMMARIZE_KEEP_MESSAGES", "20"))


def _middleware() -> list:
    if not _SUMMARIZE_AT_TOKENS:
        return []
    from langchain.agents.middleware import SummarizationMiddleware
    return [SummarizationMiddleware(
        # Caching deliberately off for this one: the summary call happens once per
        # threshold crossing and is never re-read, so a cachePoint would only buy a
        # 1.25-2x write premium.
        model=_CachingChatBedrockConverse(model=_MODEL_ID, region_name=_REGION,
                                         cache_ttl=""),
        trigger=("tokens", _SUMMARIZE_AT_TOKENS),
        keep=("messages", _SUMMARIZE_KEEP),
    )]


# session_id -> {graph, config, stack, created, ...}. One microVM ≈ one session.
_sessions: dict[str, dict] = {}
_lock = threading.Lock()

# Background turns in flight. AgentCore may reclaim an idle container, which would
# kill them, so /ping reports HealthyBusy while this is non-zero.
_in_flight = 0
_in_flight_lock = threading.Lock()


def busy() -> bool:
    with _in_flight_lock:
        return _in_flight > 0


def _track(delta: int) -> None:
    global _in_flight
    with _in_flight_lock:
        _in_flight += delta


# --------------------------- the process's one event loop --------------------
# MCP sessions are anyio-based and bound to the loop that opened them, and we keep a
# session open across turns — so every await touching one has to happen on that same
# loop. One long-lived loop in a daemon thread gives us that, and keeps this module's
# API synchronous for the aiohttp handlers (which call it in an executor), the card
# writer thread, and the tests. A loop per turn, which the previous Strands
# implementation could get away with, would strand the cached sessions.

_LOOP = asyncio.new_event_loop()
threading.Thread(target=_LOOP.run_forever, name="agent-loop", daemon=True).start()


def _run(coro, timeout: float | None = None):
    """Run a coroutine on the agent loop from a synchronous caller."""
    return asyncio.run_coroutine_threadsafe(coro, _LOOP).result(timeout)


def _session_id_for(actor_id: str) -> str:
    """Deterministic per-user session id: one long conversation thread per user,
    shared across reconnects and entrypoints (STM retains it 30 days)."""
    return "sess-" + hashlib.sha256(actor_id.encode()).hexdigest()[:32]


# Shared only by the fallback: an InMemorySaver rebuilt with every session would drop
# history every time the cache expires (50 min), which is worse than the degradation the
# fallback is there to provide.
_fallback_saver = None


def _checkpointer():
    """The conversation's durable state, in DynamoDB.

    Not AgentCoreMemorySaver: that one carries a mandatory 3-365 day event expiry, bills
    per write, and needs the Memory resource to exist at all. Fidelity is identical —
    both go through JsonPlusSerializer and produce the same CheckpointTuple — so the
    choice is only about the dependency surface. AgentCore Memory is still used, for
    long-term records; see _long_term_tools().

    Built per session rather than once, because it holds a boto3 client and a stale one
    outliving its credentials is the failure that avoids."""
    global _fallback_saver
    if not _CHECKPOINT_TABLE:
        # Tests, and any runtime that predates provision writing the env var. A session
        # still works; history just dies with the container.
        if _fallback_saver is None:
            from langgraph.checkpoint.memory import InMemorySaver
            log.warning("CHECKPOINT_TABLE unset: history will not outlive this container")
            _fallback_saver = InMemorySaver()
        return _fallback_saver
    from langgraph_checkpoint_aws import DynamoDBSaver
    return DynamoDBSaver(
        table_name=_CHECKPOINT_TABLE,
        region_name=_REGION,
        ttl_seconds=_CHECKPOINT_TTL,
        # Compress first so spilling to S3 stays the exception: DynamoDB caps an item at
        # 400 KB, and one unbounded thread per user would otherwise reach it.
        enable_checkpoint_compression=True,
        s3_offload_config=(
            {"bucket_name": _CHECKPOINT_BUCKET, "key_prefix": "state"}
            if _CHECKPOINT_BUCKET else None),
    )


def _long_term_tools(actor_id: str) -> list:
    """Long-term memory reaches the model as two tools (remember/recall), not as a Store.

    A LangGraph Store would write every exchange as conversational events — the
    by-product pattern ADR 0008 rejected, and the one that makes retrieval cost scale with
    chat volume rather than with need. Nothing reads a Store here now that /status counts
    checkpoints instead of events."""
    return memory_tools.tools_for(actor_id)


_INTERRUPTED_TOOL = "The previous attempt was interrupted before this tool returned."


async def _arepair_interrupted_turn(graph, config: dict) -> int:
    """Answer any tool call the previous turn was killed before completing.

    Checkpoints are written per superstep, so a turn cut off between "the model emitted
    tool_calls" and "the tool returned" leaves the trailing AIMessage with calls that have
    no matching ToolMessage. Bedrock then rejects every subsequent request — each toolUse
    must have a toolResult — which is the worst failure available here: the history is
    intact but permanently unreachable, and it looks like the agent broke rather than like
    a turn was interrupted. LangGraph could resume the pending task itself, but only via
    ainvoke(None), and we always arrive carrying a new user message.

    Only the trailing message is repaired. That is the only place an interrupted turn can
    leave one, and appending is only a valid fix there — a toolResult has to follow its
    toolUse, so a dangling call deeper in the history is a different bug that this must not
    paper over."""
    state = await graph.aget_state(config)
    messages = (getattr(state, "values", None) or {}).get("messages") or []
    last = messages[-1] if messages else None
    pending = [tc for tc in (getattr(last, "tool_calls", None) or []) if tc.get("id")] \
        if isinstance(last, AIMessage) else []
    if not pending:
        return 0
    # as_node is not optional here: once a graph has more than one node LangGraph cannot
    # infer which one a write came from and raises InvalidUpdateError("Ambiguous update").
    # "tools" is the honest attribution — a ToolMessage is what that node produces. Learned
    # the hard way: the unit test's fake graph accepted the call without it, so this shipped
    # broken and only a poisoned checkpoint on the real table surfaced it.
    nodes = graph.get_graph().nodes
    await graph.aupdate_state(config, {"messages": [
        ToolMessage(content=_INTERRUPTED_TOOL, tool_call_id=tc["id"],
                    name=tc.get("name") or "tool")
        for tc in pending]}, as_node="tools" if "tools" in nodes else "model")
    return len(pending)


async def _aopen_tools(stack: AsyncExitStack, connection: dict) -> list:
    """Open an MCP session that stays alive for the session's lifetime and return its
    tools. The stack owns the teardown, so one close() releases every server."""
    session = await stack.enter_async_context(create_session(connection))
    await session.initialize()
    return await load_mcp_tools(session)


async def _abuild_session(actor_id: str, email: str, mem_sid: str,
                          workload_token: str = "") -> dict:
    """Build a session for this user. Agent-side 3LO: fetch the user's vaulted Lark
    token if there is one and open MCP sessions to the tool servers (SigV4 + the token
    in the custom header), kept open for the session.

    The token gates tool *calls*, not the connection: lark-mcp answers initialize and
    tools/list without it and only rejects tools/call with "authorize first" (verified
    against the deployed Runtime — an empty header returns the full tool list). So an
    unauthorized user still gets a session with the Lark tools listed, and can chat
    freely; consent is asked for when a tool is actually reached, which is the point at
    which the user can see what it is for."""
    tools: list = []
    auth_url = None
    identity_error = None
    stack = AsyncExitStack()
    try:
        kind, value = await asyncio.to_thread(
            lark_3lo.get_user_lark_token, actor_id, False, workload_token)
    except Exception as e:  # noqa: BLE001 — never crash the turn on identity hiccups
        log.exception("3LO token lookup failed for %s", actor_id)
        kind, value = "error", str(e)

    if kind == "auth_url":
        auth_url = value  # remembered for the tool-call path, not returned upfront
    elif kind != "token":
        # Surface identity failures instead of running tool-less: an agent that merely
        # says "I have no tools" reads as model behaviour and hides the real cause (a
        # missing IAM permission, most likely).
        identity_error = value

    token = value if kind == "token" else ""

    if identity_error is None:
        try:
            tools += await _aopen_tools(stack, lark_3lo.mcp_connection_for(token))
        except Exception:  # noqa: BLE001 — chat without Lark tools beats no reply
            log.exception("lark-mcp unavailable for %s", actor_id)

        # Approval tools, when that Runtime is deployed. Separate server because its
        # write operations run on the app's tenant token (Lark accepts no user token
        # there) and its decision limits have to be bound to specific tools — see
        # .dev/adr/0006. The user token is still passed: add_sign needs it.
        if _APPROVAL_MCP_URL:
            try:
                tools += await _aopen_tools(
                    stack, lark_3lo.mcp_connection_for(token, url=_APPROVAL_MCP_URL))
            except Exception:  # noqa: BLE001 — optional, like search
                log.exception("approval tools unavailable for %s", actor_id)

    # Search doesn't depend on the user's Lark grant, so add it even when the Lark
    # tools are unavailable — an unauthorised user can still ask questions.
    if websearch.available():
        try:
            conn = await asyncio.to_thread(websearch.connection_for, actor_id)
            tools += await _aopen_tools(stack, conn)
        except Exception:  # noqa: BLE001 — search is optional, Lark tools are not
            log.exception("web search unavailable for %s", actor_id)

    tools += _long_term_tools(actor_id)
    saver = _checkpointer()
    # langchain.agents.create_agent, not langgraph.prebuilt.create_react_agent: the
    # latter is deprecated as of LangGraph 1.0 and slated for removal in 2.0 (AWS's
    # devguide example still imports it). Note there are no pre/post model hooks on
    # this API — middleware replaces them — which is another reason the conversational
    # record is written at turn end instead.
    graph = create_agent(model=_model, tools=tools, system_prompt=_SYSTEM,
                         checkpointer=saver, middleware=_middleware())
    # thread_id is the whole checkpoint address (chosen by the router, so /reset can
    # rotate it) — DynamoDBSaver derives its partition key from it alone, and since it is
    # the sha256 of the actor, per-user isolation follows from the key. The agent never
    # picks it. actor_id is carried for AgentCoreMemorySaver, which rejects a config
    # without it, so switching back stays a one-line change.
    config = {"configurable": {"thread_id": mem_sid, "actor_id": actor_id}}
    # Building a session is exactly the moment a replaced microVM picks up a thread whose
    # last turn may have been killed mid tool call, so repair before the first invoke.
    try:
        repaired = await _arepair_interrupted_turn(graph, config)
        if repaired:
            log.info("answered %d tool call(s) left by an interrupted turn", repaired)
    except Exception:  # noqa: BLE001 — a failed repair must not cost the user a session
        log.warning("could not check for an interrupted turn", exc_info=True)
    return {
        "graph": graph, "stack": stack,
        "config": config,
        "actor_id": actor_id, "mem_sid": mem_sid,
        "created": time.time(),
        "auth_url": auth_url, "identity_error": identity_error,
        # Tool results seen during the current turn, for the auth-wall scan.
        "tool_texts": [],
    }


def _build_session(actor_id: str, email: str, mem_sid: str,
                   workload_token: str = "") -> dict:
    return _run(_abuild_session(actor_id, email, mem_sid, workload_token))


def _close_session(s: dict) -> None:
    """Release every MCP session this cached entry holds. Best-effort: a connection
    that is already gone must not stop us discarding the entry."""
    stack = s.get("stack")
    if stack is None:
        return
    try:
        _run(stack.aclose(), timeout=30)
    except Exception:  # noqa: BLE001
        log.warning("closing MCP sessions failed", exc_info=True)


_AUTH_PROMPT = (
    "To do that I need access to your Lark account. Please authorize once here, "
    "then send your message again:\n{url}"
)

# lark-mcp's reply when a tools/call arrives without a user token. It shows up as a
# tool result, not necessarily in the model's final reply — the model may paraphrase or
# translate. Scanning the tool results is what makes this robust.
_NEEDS_TOKEN_MARKER = "no user token (authorize first)"


def _hit_auth_wall(session: dict) -> bool:
    """True when this turn needs consent — either because the stream was aborted at a
    Lark tool call (the fast path, before the model can editorialise) or because a tool
    result carried lark-mcp's refusal (the fallback, e.g. an already-authorized session
    whose token went stale mid-turn)."""
    if session.pop("walled_tool", None):
        return True
    return _hit_auth_wall_from_tool_results(session)


def _hit_auth_wall_from_tool_results(session: dict) -> bool:
    """True if this turn produced a tool result asking the user to authorize.

    Reads the tool results collected while streaming rather than the model's answer,
    because the model paraphrases errors — 'no user token is available' would slip
    through a string check on the reply, and did in end-to-end testing."""
    if not session.get("auth_url"):
        return False
    return any(_NEEDS_TOKEN_MARKER in t for t in session.get("tool_texts") or [])


_IDENTITY_ERROR = (
    "I can't reach your Lark account right now, so I have no Lark tools this turn "
    "(other questions still work).\nDetails: {err}"
)


def _get_session(actor_id: str, email: str, mem_sid: str, fresh: bool = False,
                 workload_token: str = "") -> dict:
    """Return the cached session for this user, rebuilding it if absent or near token
    expiry. A pending-authorization session (no token yet) is NOT cached — so the next
    turn re-checks the vault and picks up a freshly consented token."""
    cache_key = f"{actor_id}|{mem_sid}"
    with _lock:
        s = _sessions.get(cache_key)
        # `fresh` is for the consent-resume path: the user has just authorized, so a
        # cached unauthorized session must not be reused — its MCP sessions hold an
        # empty token and the turn would wall again. Measured: consent completed 31 s
        # after the prompt, well inside _UNAUTH_TTL, so waiting for expiry is not an
        # option.
        if s and fresh:
            _close_session(s)
            _sessions.pop(cache_key, None)
            s = None
        if s:
            # An unauthorized session works (tools listed, calls rejected), so it is
            # worth caching — but only briefly, or the user consents and keeps being
            # told to authorize until the full TTL lapses.
            ttl = _UNAUTH_TTL if s.get("auth_url") else _SESSION_TTL
            if (time.time() - s["created"]) < ttl:
                return s
            _close_session(s)
        s = _build_session(actor_id, email, mem_sid, workload_token)
        if not s.get("identity_error"):
            _sessions[cache_key] = s
        return s


def chat_result(actor_id: str, message: str, email: str = "",
                mem_sid: str = "", workload_token: str = "") -> dict:
    """Non-streaming chat → {reply, needs_auth, auth_url}. History via Memory.
    When the user hasn't authorized Lark yet, needs_auth is True and auth_url is the
    raw consent URL, so the caller (router) can drive the wait-for-consent loop instead
    of asking the user to re-send."""
    s = _get_session(actor_id, email, mem_sid or _session_id_for(actor_id),
                     workload_token=workload_token)
    if s.get("identity_error"):
        # Answer without tools, but say so — silently degrading is what made a missing
        # IAM permission look like the model choosing not to help.
        return {"reply": _IDENTITY_ERROR.format(err=s["identity_error"]),
                "needs_auth": False, "identity_error": s["identity_error"]}
    s["tool_texts"] = []
    state = _run(s["graph"].ainvoke({"messages": [HumanMessage(message)]},
                                    config=s["config"]))
    for m in state.get("messages", []):
        if isinstance(m, ToolMessage):
            s["tool_texts"].append(_message_text(m))
    reply = _message_text(state["messages"][-1]) if state.get("messages") else ""
    if _hit_auth_wall(s):
        return {"reply": _AUTH_PROMPT.format(url=s["auth_url"]),
                "needs_auth": True, "auth_url": s["auth_url"]}
    return {"reply": reply, "needs_auth": False}


def run_chat(actor_id: str, message: str, email: str = "",
             mem_sid: str = "") -> str:
    """Back-compat: assistant's final text (or the consent prompt)."""
    return chat_result(actor_id, message, email, mem_sid)["reply"]


def chat_async(actor_id: str, message: str, chat_id: str, email: str = "",
               mem_sid: str = "", message_id: str = "", reaction_id: str = "",
               fresh_session: bool = False, workload_token: str = "") -> dict:
    """Accept the work and answer later.

    A real task can outlast any request/response window (InvokeAgentRuntime caps at
    15 min, the calling Lambda at less), and a turn that times out mid-way is the worst
    outcome: the work often completed, but the user was told it failed. So we return an
    acknowledgement now and push the result to the chat when it's ready.

    An unauthorized user is not stopped here: the Lark tools are listed even without a
    token, so the turn runs and consent is only raised if the model actually calls
    one. By then the router has returned, so the prompt is pushed to the chat like any
    other answer and the user re-sends after approving."""
    s = _get_session(actor_id, email, mem_sid or _session_id_for(actor_id),
                     fresh=fresh_session, workload_token=workload_token)
    if s.get("identity_error"):
        return {"reply": _IDENTITY_ERROR.format(err=s["identity_error"]),
                "needs_auth": False, "identity_error": s["identity_error"]}

    def _run_turn() -> None:
        try:
            answer = _stream_to_chat(s, message, chat_id)
            if _hit_auth_wall(s):
                # A clickable "点击授权" link, matching the router's synchronous path,
                # instead of a raw URL. The message was parked before this turn, so the
                # shim's /return replays it once consent lands — no re-send.
                lark_notify.send_link(
                    chat_id, "需要访问你的 Lark 账号，授权后我会自动继续：",
                    "点击授权", s["auth_url"])
        except Exception as e:  # noqa: BLE001 — the caller is already gone
            log.exception("async turn failed for %s", actor_id)
            # This session stays cached, so a turn that died mid tool call would make every
            # later turn fail too. Repair here as well as at build time.
            try:
                _run(_arepair_interrupted_turn(s["graph"], s["config"]), timeout=30)
            except Exception:  # noqa: BLE001
                log.warning("could not repair the interrupted turn", exc_info=True)
            lark_notify.send_text(chat_id, f"Sorry, that didn't work out ({type(e).__name__}).")
        finally:
            # Clear the router's marker whatever happened — leaving it on a failed turn
            # would read as "still working". If the container dies outright nobody
            # clears it, which is cosmetic only.
            lark_notify.remove_reaction(message_id, reaction_id)
            _track(-1)

    _track(+1)
    threading.Thread(target=_run_turn, name=f"turn-{actor_id[:16]}", daemon=True).start()
    return {"accepted": True, "needs_auth": False}


# No throttle on the card write on purpose. The write is queued, not performed — the
# card's own worker coalesces to the newest text and is paced by Lark's round trip
# (~470 ms measured), so handing over every delta costs a lock and sets no rate. An
# earlier throttle guarded a blocking write from this loop, and at 0.4 s it was tuned
# below the write's real cost: the loop lost over half its time to HTTP and the text
# jerked.


def _message_text(msg) -> str:
    """Text of a LangChain message or chunk. Bedrock Converse returns content as a
    list of typed blocks, not a string, so both shapes have to be handled."""
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content or "")


_STREAM_END = object()


def _iter_deltas(graph, message: str, config: dict, on_tool_use=None,
                 on_tool_result=None):
    """Yield text chunks from the graph's async stream, as a synchronous generator.

    The stream runs on the agent loop (where the MCP sessions live) and hands chunks
    over through a queue, so the caller — a plain worker thread — can drive the card
    without touching asyncio.

    `on_tool_use(tool_name) -> bool` is consulted when the model starts a tool call;
    returning True abandons the stream. Used to cut a turn short the moment an
    unauthorized session reaches for a Lark tool. `on_tool_result(text)` receives each
    tool result, which is how the auth-wall fallback sees lark-mcp's refusal."""
    q: queue.Queue = queue.Queue()

    async def _pump() -> None:
        try:
            stream = graph.astream({"messages": [HumanMessage(message)]},
                                   config=config, stream_mode="messages")
            async for chunk, _meta in stream:
                if isinstance(chunk, ToolMessage):
                    if on_tool_result is not None:
                        on_tool_result(_message_text(chunk))
                    continue
                # Strands emitted no tool-result event and LangGraph does; what both
                # give us is the tool call as it *starts*, which is what matters here:
                # in an unauthorized session any Lark tool call is certain to be
                # refused, so seeing one begin means the turn is already lost — stop
                # rather than let the model narrate the refusal at length.
                for tc in getattr(chunk, "tool_call_chunks", None) or []:
                    name = tc.get("name")
                    if name and on_tool_use is not None and on_tool_use(name):
                        return
                text = _message_text(chunk)
                if text:
                    q.put(text)
        except Exception as e:  # noqa: BLE001 — re-raised in the consumer
            q.put(e)
        finally:
            q.put(_STREAM_END)

    asyncio.run_coroutine_threadsafe(_pump(), _LOOP)
    while True:
        item = q.get()
        if item is _STREAM_END:
            return
        if isinstance(item, Exception):
            raise item
        yield item


def _stream_to_chat(session: dict, message: str, chat_id: str) -> str:
    """Run the turn, typing the answer into a streaming card as it is produced, and
    return the full text. If the card can't be created or an update fails (e.g. the
    cardkit:card:write scope isn't granted), fall back to posting the final text — the
    answer must survive regardless. Consent replies are handled by the caller."""
    session["tool_texts"] = []
    card = lark_notify.StreamingCard(chat_id)
    streaming = card.open()

    # Unauthorized: the first Lark tool the model reaches for is certain to be refused,
    # and letting the turn run on means it narrates that refusal at length before the
    # consent card arrives. Stop at the tool call instead — the caller sees `walled`
    # and posts the card as the only reply.
    unauthorized = bool(session.get("auth_url"))

    def _abort_on_lark_tool(tool_name: str) -> bool:
        if not unauthorized or tool_name.startswith("WebSearch"):
            return False
        session["walled_tool"] = tool_name
        log.info("aborting turn: %s needs consent", tool_name)
        return True

    acc = []
    for delta in _iter_deltas(session["graph"], message, session["config"],
                              on_tool_use=_abort_on_lark_tool,
                              on_tool_result=session["tool_texts"].append):
        acc.append(delta)
        if streaming and not card.update("".join(acc)):
            streaming = False  # a failed write drops us to the fallback below
    text = "".join(acc)

    # Aborted for consent: whatever the model had started saying is a half-sentence
    # about a failure the user is about to be asked to fix, so replace it rather than
    # leave it on the card.
    if session.get("walled_tool"):
        text = "🔐 这一步需要访问你的 Lark 账号"

    if streaming:
        if not card.close(text):
            streaming = False
    if not streaming:
        # Either CardKit was never available or it failed mid-stream. Post the whole
        # answer as plain text so the user still gets it.
        if not lark_notify.send_text(chat_id, text):
            log.error("could not deliver reply to %s: %s", chat_id, text[:200])
    return text


def _thread_config(actor_id: str, mem_sid: str) -> dict:
    return {"configurable": {"thread_id": mem_sid or _session_id_for(actor_id),
                             "actor_id": actor_id}}


def history_stats(actor_id: str, mem_sid: str = "") -> dict:
    """How many messages this thread holds → {"messages": n}.

    The router used to count `conversational` events itself, which it can no longer do:
    the conversation is graph state in DynamoDB now, and decoding it means the whole
    LangGraph stack. Answering here instead keeps agent_core the only framework-coupled
    module (the router stays a thin channel adapter) and costs one checkpoint read rather
    than the paged ListEvents walk it replaces.

    Tool messages are excluded: the old count was of user/assistant exchanges, and that
    is what /status reports."""
    try:
        tup = _run(_checkpointer().aget_tuple(_thread_config(actor_id, mem_sid)),
                   timeout=30)
    except Exception:  # noqa: BLE001 — /status must answer even if this part cannot
        log.warning("could not read thread stats", exc_info=True)
        return {"messages": 0, "unavailable": True}
    values = (tup.checkpoint.get("channel_values") if tup else None) or {}
    return {"messages": sum(
        1 for m in (values.get("messages") or [])
        if isinstance(m, (HumanMessage, AIMessage)))}


def clear_history(actor_id: str, mem_sid: str = "") -> dict:
    """Delete this thread's stored state → {"deleted": bool}.

    Really deletes, unlike /reset which rotates to a new thread and leaves the old one
    readable. Also drops the cached session, because it holds a compiled graph whose next
    turn would otherwise write on top of a thread the user believes is gone.

    Long-term memory is NOT touched yet: no Memory resource exists, so there is nothing
    to delete. When one does, its records belong in here too — a user asking to clear
    history does not mean "except the parts extracted from it"."""
    config = _thread_config(actor_id, mem_sid)
    thread_id = config["configurable"]["thread_id"]
    with _lock:
        for key in [k for k in _sessions if k.endswith(f"|{thread_id}")]:
            s = _sessions.pop(key, None)
            if s:
                _close_session(s)
    try:
        _run(_checkpointer().adelete_thread(thread_id), timeout=60)
    except Exception as e:  # noqa: BLE001
        log.warning("could not clear thread %s", thread_id, exc_info=True)
        return {"deleted": False, "error": type(e).__name__}
    return {"deleted": True}


def reauth(actor_id: str, idp: str = "lark", workload_token: str = "") -> dict:
    """Start a fresh 3LO flow for `idp` even when a token is already vaulted →
    {auth_url}. Authorization is per-IdP; only "lark" is wired up so far (add a module
    like lark_3lo for each new downstream system)."""
    if idp not in ("", "lark"):
        return {"reply": f"IdP 尚未接入：{idp}", "needs_auth": False}
    # Drop every cached session for this user so the next turn re-reads the vault.
    with _lock:
        for key in [k for k in _sessions if k.startswith(f"{actor_id}|")]:
            s = _sessions.pop(key, None)
            if s:
                _close_session(s)
    kind, value = lark_3lo.get_user_lark_token(actor_id, force=True,
                                               workload_token=workload_token)
    if kind == "auth_url":
        return {"reply": _AUTH_PROMPT.format(url=value),
                "needs_auth": True, "auth_url": value}
    return {"reply": "Already authorized.", "needs_auth": False}
