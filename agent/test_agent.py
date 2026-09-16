"""Unit tests for agent logic that doesn't require live AWS.

Run: tests/run.sh (which supplies the deps), or from this directory:
  uv run --with langchain --with langchain-aws --with langgraph \
         --with langchain-mcp-adapters --with boto3 --with httpx --with pytest \
         python -m pytest test_agent.py -v

`agent_core` is imported for real. That was impossible under Strands, whose wheels
target the ARM64 runtime, so the streaming tests used to exec slices of the source with
a faked `_iter_deltas` — asserting against a copy of the code rather than the code. The
LangGraph stack is pure Python, so the turn loop is now driven through an actual
compiled graph with a scripted model. Constructing the model touches no AWS
credentials; nothing here calls Bedrock.
"""

import base64
import hashlib
import json
import os
import re
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import agent_core

# Message classes are used throughout; agent_core already pulls them in.
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


# ------------------------------- identity -----------------------------------

def test_derive_password_deterministic_and_complex():
    import identity
    with mock.patch.object(identity, "_get_salt", return_value="test-salt"):
        p1 = identity._derive_password("lark:ou_abc")
        p2 = identity._derive_password("lark:ou_abc")
        p3 = identity._derive_password("lark:ou_xyz")
    assert p1 == p2                       # deterministic
    assert p1 != p3                       # per-user
    assert p1.endswith("Aa1!")            # complexity suffix
    assert len(p1) == 36


def test_jwt_exp_parses_unverified():
    import identity
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1234567890}).encode()).decode().rstrip("=")
    token = f"header.{payload}.sig"
    assert identity._jwt_exp(token) == 1234567890.0


def test_jwt_exp_bad_token_returns_zero():
    import identity
    assert identity._jwt_exp("not-a-jwt") == 0.0


def test_ensure_user_email_sanitizes_colon():
    """open_id-based username 'lark:ou_x' must not produce an invalid email."""
    import identity
    captured = {}

    def fake_create(**kw):
        captured["email"] = next(a["Value"] for a in kw["UserAttributes"] if a["Name"] == "email")

    with mock.patch.object(identity, "_cognito") as c:
        from botocore.exceptions import ClientError
        c.admin_get_user.side_effect = ClientError(
            {"Error": {"Code": "UserNotFoundException"}}, "AdminGetUser")
        c.admin_create_user.side_effect = fake_create
        c.admin_set_user_password.return_value = {}
        with mock.patch.object(identity, "_get_salt", return_value="s"):
            identity._ensure_user("lark:ou_abc", "")
    assert ":" not in captured["email"]           # colon replaced
    assert captured["email"] == "lark-ou_abc@lark.local"


# ------------------------------- agent_core session id ----------------------

def test_session_id_deterministic_per_user():
    """Load just the _session_id_for function without importing the heavy deps."""
    src = open(os.path.join(os.path.dirname(__file__), "agent_core.py"), encoding="utf-8").read()
    ns = {"hashlib": hashlib}
    # exec only the function definition we care about
    start = src.index("def _session_id_for")
    end = src.index("\n\ndef ", start)
    exec(src[start:end], ns)
    sid = ns["_session_id_for"]
    assert sid("lark:ou_abc") == sid("lark:ou_abc")          # stable
    assert sid("lark:ou_abc") != sid("lark:ou_xyz")          # per-user
    assert sid("lark:ou_abc").startswith("sess-")


# ------------------------------- checkpointer -------------------------------
# The conversation lives in the checkpointer, so the two ways this can go wrong are both
# silent: picking the fallback in a real deployment (history dies with the microVM, which
# reads as "the bot forgot everything") or mis-wiring the offload (a long conversation
# hits DynamoDB's 400 KB item cap mid-turn).

def test_checkpointer_falls_back_to_one_shared_in_process_saver():
    """No table configured — a session must still work, and the fallback has to be shared:
    a fresh InMemorySaver per session build would drop history every cache expiry."""
    with mock.patch.object(agent_core, "_CHECKPOINT_TABLE", ""), \
         mock.patch.object(agent_core, "_fallback_saver", None):
        first = agent_core._checkpointer()
        second = agent_core._checkpointer()
    from langgraph.checkpoint.memory import InMemorySaver
    assert isinstance(first, InMemorySaver)
    assert first is second


def test_checkpointer_uses_dynamodb_with_offload_when_configured():
    with mock.patch.object(agent_core, "_CHECKPOINT_TABLE", "tbl"), \
         mock.patch.object(agent_core, "_CHECKPOINT_BUCKET", "bkt"), \
         mock.patch("langgraph_checkpoint_aws.DynamoDBSaver") as saver:
        agent_core._checkpointer()
    kwargs = saver.call_args.kwargs
    assert kwargs["table_name"] == "tbl"
    assert kwargs["ttl_seconds"] == agent_core._CHECKPOINT_TTL
    # Compression keeps the spill rare; the spill keeps a long thread from failing hard.
    assert kwargs["enable_checkpoint_compression"] is True
    assert kwargs["s3_offload_config"]["bucket_name"] == "bkt"


def test_checkpointer_omits_offload_when_no_bucket():
    """Passing a config with an empty bucket name would fail at write time, deep inside
    the saver, instead of simply not offloading."""
    with mock.patch.object(agent_core, "_CHECKPOINT_TABLE", "tbl"), \
         mock.patch.object(agent_core, "_CHECKPOINT_BUCKET", ""), \
         mock.patch("langgraph_checkpoint_aws.DynamoDBSaver") as saver:
        agent_core._checkpointer()
    assert saver.call_args.kwargs["s3_offload_config"] is None


def _repair(messages):
    """Drive the real repair against a graph whose state is `messages`, returning the
    count and whatever it appended."""
    import asyncio
    appended = []

    class _Graph:
        """Mimics the real graph's contract, including the part a looser fake hid: a
        multi-node graph rejects aupdate_state without as_node."""

        def get_graph(self):
            return type("G", (), {"nodes": ["__start__", "model", "tools", "__end__"]})()

        async def aget_state(self, config):
            return type("S", (), {"values": {"messages": messages}})()

        async def aupdate_state(self, config, values, as_node=None):
            if as_node is None:
                raise RuntimeError("Ambiguous update, specify as_node")
            appended.append(as_node)
            appended.extend(values["messages"])

    n = asyncio.run(agent_core._arepair_interrupted_turn(_Graph(), {}))
    return n, appended


def test_interrupted_tool_call_is_answered_so_the_thread_stays_usable():
    """Bedrock rejects a toolUse with no toolResult, so without this the whole thread
    becomes permanently unusable after one killed turn — history intact, unreachable."""
    from langchain_core.messages import AIMessage as AI, HumanMessage
    dangling = AI(content="", tool_calls=[
        {"name": "lark_list_my_docs", "args": {}, "id": "call-1"}])
    n, appended = _repair([HumanMessage("hi"), dangling])
    assert n == 1
    # The write must be attributed to the tools node or LangGraph refuses it outright.
    assert appended[0] == "tools"
    assert appended[1].tool_call_id == "call-1"


def test_repair_is_a_no_op_on_a_completed_turn():
    from langchain_core.messages import AIMessage as AI, HumanMessage
    n, appended = _repair([HumanMessage("hi"), AI(content="done")])
    assert (n, appended) == (0, [])


def test_repair_ignores_a_dangling_call_that_is_not_trailing():
    """A toolResult must follow its toolUse, so appending cannot fix a call buried in the
    history — silently 'repairing' it would corrupt the order instead."""
    from langchain_core.messages import AIMessage as AI, HumanMessage
    buried = AI(content="", tool_calls=[{"name": "t", "args": {}, "id": "old"}])
    n, appended = _repair([buried, HumanMessage("hi"), AI(content="done")])
    assert (n, appended) == (0, [])


def _load_busy_helpers():
    """The real functions, with the counter reset so tests don't inherit each other."""
    agent_core._in_flight = 0
    return {"busy": agent_core.busy, "_track": agent_core._track}


def test_busy_reflects_in_flight_turns():
    ns = _load_busy_helpers()
    busy, track = ns["busy"], ns["_track"]
    assert busy() is False
    track(+1)
    assert busy() is True
    track(+1)
    track(-1)
    assert busy() is True          # still one turn running
    track(-1)
    assert busy() is False         # idle again, so the container may be reclaimed


def test_track_is_usable_from_a_thread():
    """The decrement happens on the worker thread; a scoping bug shows up there."""
    import threading
    ns = _load_busy_helpers()
    busy, track = ns["busy"], ns["_track"]
    track(+1)
    err = []

    def worker():
        try:
            track(-1)
        except Exception as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert not err, f"_track failed off the main thread: {err}"
    assert busy() is False


# ------------------------------- web search ---------------------------------
# Search is optional; with no gateway configured the agent must simply run
# without the tool rather than failing to build a session.

def test_websearch_gateway_contract():
    """websearch imports mcp (not installable here), so assert the two things that
    silently break the Gateway call: the protocol-version header, without which it
    negotiates 2025-03-26 and rejects with -32022, and an access token rather than
    an ID token, since the Gateway checks the client_id claim."""
    src = open(os.path.join(os.path.dirname(__file__), "websearch.py"), encoding="utf-8").read()
    assert "MCP-Protocol-Version" in src
    assert "2025-11-25" in src
    assert "get_user_jwt" in src        # identity.py returns the ACCESS token
    # available() must be a pure config check, so an unset gateway just means no tool
    assert 'os.environ.get("WEBSEARCH_GATEWAY_URL", "")' in src


# --------------------------- deferred authorization -------------------------
# A user without a vaulted Lark token still gets a working session: lark-mcp lists
# its tools without one and only rejects tools/call. Consent is raised when a tool
# is actually reached, so plain chat ("hello") is never gated on it.

def test_auth_marker_matches_the_mcp_server():
    """The client detects the auth wall by matching lark-mcp's rejection text. It is
    a cross-process string contract: if the server's wording changes and this does
    not, the user silently stops being offered a consent link."""
    here = os.path.dirname(__file__)
    core = open(os.path.join(here, "agent_core.py"), encoding="utf-8").read()
    server = open(os.path.join(here, "..", "mcp-servers", "lark-cli", "server.js"), encoding="utf-8").read()
    marker = re.search(r'_NEEDS_TOKEN_MARKER = "([^"]+)"', core).group(1)
    assert marker in server, (
        f"agent_core expects {marker!r} but mcp-servers/lark-cli/server.js no longer says it"
    )


def test_hit_auth_wall_reads_tool_results_not_the_final_reply():
    """The check must inspect tool results, not the model's text answer: the model
    paraphrases errors, so 'no user token is available' in the reply slips past a string
    check on the reply — verified end-to-end before this fix."""
    hit = agent_core._hit_auth_wall_from_tool_results

    # 1. Nothing collected → nothing to inspect, so no auth wall.
    assert hit({"auth_url": "https://consent"}) is False
    assert hit({"auth_url": "https://consent", "tool_texts": []}) is False
    # 2. A paraphrase of the error, not the tool's own words → NOT a wall. This is
    #    exactly the case that fooled the first version of this function.
    assert hit({"auth_url": "https://consent", "tool_texts": [
        "It looks like no user token is available — please authorize"]}) is False
    # 3. The tool result carrying the marker verbatim → wall.
    walled = {"auth_url": "https://consent",
              "tool_texts": ["no user token (authorize first)"]}
    assert hit(walled) is True
    # 4. Same tool result but the session is authorized → no wall (auth_url absent),
    #    because then the refusal means something else and consent won't fix it.
    assert hit({"tool_texts": ["no user token (authorize first)"]}) is False


def test_unauthorized_session_passes_an_empty_token_and_still_gets_tools():
    """The token is passed as an empty header rather than skipping the connection —
    verified against the deployed Runtime, which returns the full tool list for an
    empty token. Skipping it would leave the model unaware of its Lark tools."""
    opened = []

    async def fake_open(stack, connection):
        opened.append(connection["headers"])
        return [_dummy_tool]

    with mock.patch.object(agent_core.lark_3lo, "get_user_lark_token",
                           return_value=("auth_url", "https://consent")), \
         mock.patch.object(agent_core.lark_3lo, "mcp_connection_for",
                           side_effect=lambda tok, url="": {"headers": {"tok": tok}}), \
         mock.patch.object(agent_core, "_aopen_tools", fake_open), \
         mock.patch.object(agent_core.websearch, "available", return_value=False):
        s = agent_core._build_session("lark:ou_x", "", "mem1")

    assert s["auth_url"] == "https://consent"      # consent is remembered, not raised
    assert opened == [{"tok": ""}], "the server must still be connected, with no token"
    assert s["graph"] is not None, "the model must see the Lark tools regardless"


# ----------------------------- streaming to a card --------------------------
# chat_async types the answer into a CardKit streaming card so the user isn't left
# staring at silence for the ~7.5 s before the first token. Two things must hold:
# updates are throttled (not one call per token), and any CardKit failure falls back
# to send_text so the answer is never lost.

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool
from langchain_core.language_models import BaseChatModel


@tool
def _dummy_tool() -> str:
    """A tool the scripted model can call. Returns lark-mcp's refusal text."""
    return "no user token (authorize first)"


class _ScriptedModel(BaseChatModel):
    """Streams the turns it is handed. Content arrives as a list of typed blocks, the
    way Bedrock Converse sends it, so the block-flattening path is exercised too."""

    turns: list = []
    calls: int = 0

    @property
    def _llm_type(self):
        return "scripted"

    def bind_tools(self, tools, **kw):
        return self

    async def _astream(self, messages, stop=None, run_manager=None, **kw):
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.__dict__["calls"] = self.calls + 1
        for text in turn.get("text", []):
            yield ChatGenerationChunk(message=AIMessageChunk(
                content=[{"type": "text", "text": text, "index": 0}]))
        for name in turn.get("tool_calls", []):
            yield ChatGenerationChunk(message=AIMessageChunk(
                content=[], tool_call_chunks=[
                    {"name": name, "args": "{}", "id": "c1", "index": 0}]))

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        return ChatResult(generations=[ChatGeneration(message=AIMessageChunk(content=""))])


_thread_seq = iter(range(1, 10_000))


def _make_tool(name):
    """A no-op tool under an arbitrary name, so the scripted model can call the real
    thing (lark_list_my_docs, WebSearch___WebSearch) rather than a name the graph has
    never heard of."""
    from langchain_core.tools import StructuredTool
    return StructuredTool.from_function(
        func=lambda: "no user token (authorize first)", name=name,
        description=f"test double for {name}")


def _session_for(deltas, tool_calls=None, follow_up="", **extra):
    """A session dict backed by a real compiled graph. `tool_calls` are requested after
    the deltas, so the abort path runs exactly where it does in production."""
    turns = [{"text": list(deltas), "tool_calls": list(tool_calls or [])}]
    if tool_calls:
        turns.append({"text": [follow_up] if follow_up else []})
    tools = [_dummy_tool] + [_make_tool(n) for n in (tool_calls or [])]
    graph = agent_core.create_agent(model=_ScriptedModel(turns=turns), tools=tools)
    s = {"graph": graph, "tool_texts": [],
         "config": {"configurable": {"thread_id": f"t{next(_thread_seq)}"}}}
    s.update(extra)
    return s


def _patch_notify(card, notify_sent):
    return mock.patch.object(
        agent_core, "lark_notify",
        mock.Mock(StreamingCard=lambda chat_id: card,
                  send_text=lambda cid, t: notify_sent.append(t) or True))


class FakeCard:
    def __init__(self, open_ok=True, update_ok=True, close_ok=True):
        self._open_ok, self._update_ok, self._close_ok = open_ok, update_ok, close_ok
        self.ok = False
        self.updates = []
        self.closed_with = None

    def open(self):
        self.ok = self._open_ok
        return self._open_ok

    def update(self, full_text):
        if not self.ok:
            return False
        self.updates.append(full_text)
        self.ok = self._update_ok
        return self._update_ok

    def close(self, final_text):
        self.closed_with = final_text
        return self._close_ok



# ------------------- vaulted token ownership (consent hijack) ------------------

def _load_3lo_guard(owner_lookup):
    """Exec the ownership check in isolation, so the HTTP lookup can be replaced without
    touching the module's boto3 client at import time."""
    src = open(os.path.join(os.path.dirname(__file__), "lark_3lo.py"), encoding="utf-8").read()
    start = src.index("_VERIFIED: dict[str, str] = {}")
    end = src.index("def get_user_lark_token(")
    ns = {"hashlib": hashlib, "log": mock.Mock(), "httpx": mock.Mock()}
    exec(src[start:end], ns)
    ns["_token_owner"] = owner_lookup          # replace the HTTP call
    return ns


def test_vaulted_token_must_belong_to_the_actor():
    """Consent completion binds the token to whatever userId the return-url was told
    (`state`), not to the account that actually signed in — so forwarding a consent
    link vaults someone else's token under your name. Checked at the point of use."""
    ns = _load_3lo_guard(lambda t: "ou_alice")
    assert ns["_belongs_to"]("tok", "lark:ou_alice") is True
    assert ns["_belongs_to"]("tok2", "lark:ou_bob") is False     # Bob's grant, Alice's slot


def test_unverifiable_token_owner_fails_closed():
    """An owner we cannot establish is not "probably fine"."""
    ns = _load_3lo_guard(lambda t: "")
    assert ns["_belongs_to"]("tok", "lark:ou_alice") is False


def test_verified_token_is_not_rechecked_every_turn():
    """One whoami per token, not per turn — the check is on the hot path."""
    calls = []
    def owner(t):
        calls.append(t)
        return "ou_alice"
    ns = _load_3lo_guard(owner)
    for _ in range(4):
        assert ns["_belongs_to"]("tok", "lark:ou_alice") is True
    assert len(calls) == 1


def test_the_ownership_cache_is_bounded():
    """A long-lived container sees many users; the cache must not grow without limit."""
    ns = _load_3lo_guard(lambda t: t.replace("tok-", "ou_"))
    for i in range(ns["_VERIFIED_MAX"] + 20):
        ns["_belongs_to"](f"tok-{i}", f"lark:ou_{i}")
    assert len(ns["_VERIFIED"]) <= ns["_VERIFIED_MAX"]



# ------------------------ CardKit worker (coalescing) ------------------------

def _load_card(monkeypatch_calls):
    """Import StreamingCard with its HTTP layer replaced. lark_notify imports boto3
    only, so unlike agent_core it loads on the test host."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    import importlib
    import lark_notify
    importlib.reload(lark_notify)
    lark_notify._tenant_token = lambda: "tok"

    def fake_call(method, url, body, bearer=""):
        monkeypatch_calls.append((method, body.get("content"), body.get("sequence")))
        return {"code": 0}
    lark_notify._call = fake_call
    return lark_notify


def test_card_worker_writes_the_newest_text_and_never_loses_the_last():
    """Superseded states may be dropped — CardKit takes the full text every time, so
    the newest write subsumes the earlier ones. The FINAL text must not be dropped,
    which is why close() stops the worker and writes synchronously."""
    calls = []
    ln = _load_card(calls)
    card = ln.StreamingCard("oc_1")
    card.card_id = "c1"
    card.ok = True
    import threading
    card._worker = threading.Thread(target=card._pump, daemon=True)
    card._worker.start()

    for t in ("a", "ab", "abc", "abcd"):
        assert card.update(t) is True
    card.close("abcd-final")

    contents = [c for _, c, _ in calls if c]
    assert "abcd-final" in contents, "the final text must always be written"
    assert contents[-2:][0] == "abcd-final" or contents[-1] == "abcd-final"
    # Every write is one of the accumulated states, never a stale fragment reordered.
    assert all(c in ("a", "ab", "abc", "abcd", "abcd-final") for c in contents)


def test_card_sequence_never_repeats_or_goes_backwards():
    """CardKit rejects an out-of-order sequence, and the worker plus close() both
    write — so the counter has to be shared and monotonic across them."""
    calls = []
    ln = _load_card(calls)
    card = ln.StreamingCard("oc_1")
    card.card_id = "c1"
    card.ok = True
    import threading
    card._worker = threading.Thread(target=card._pump, daemon=True)
    card._worker.start()
    for i in range(6):
        card.update("x" * (i + 1))
    card.close("done")
    seqs = [s for _, _, s in calls if s is not None]
    assert seqs == sorted(set(seqs)), f"sequence not strictly increasing: {seqs}"


def test_card_update_reports_failure_on_the_next_call():
    """A write now fails on the worker, so the caller learns one call late. That is
    enough to stop streaming and fall back — the answer is never lost."""
    calls = []
    ln = _load_card(calls)
    ln._call = lambda *a, **k: {"code": 99991400, "msg": "nope"}
    card = ln.StreamingCard("oc_1")
    card.card_id = "c1"
    card.ok = True
    import threading, time as _t
    card._worker = threading.Thread(target=card._pump, daemon=True)
    card._worker.start()
    assert card.update("a") is True        # queued before any failure is known
    for _ in range(50):                   # let the worker discover it
        if not card.ok:
            break
        _t.sleep(0.02)
    assert card.ok is False
    assert card.update("ab") is False      # now the caller is told


def test_stream_hands_over_every_delta_cumulatively():
    """The loop no longer throttles: handing over text is a lock, not a round trip, so
    every delta goes over and the card's worker decides what to actually write. Each
    hand-over carries the full text so far — CardKit renders the appended tail, so a
    non-prefix would flash the wrong content."""
    card = FakeCard()
    deltas = ["a" * 30, "b" * 30, "c" * 30, "d" * 30]
    session = _session_for(deltas)
    with _patch_notify(card, []):
        text = agent_core._stream_to_chat(session, "msg", "oc_1")
    assert text == "a"*30 + "b"*30 + "c"*30 + "d"*30
    assert len(card.updates) == len(deltas)
    for u in card.updates:
        assert text.startswith(u)          # cumulative, always a prefix of the whole
    assert card.closed_with == text


def test_stream_aborts_when_an_unauthorized_session_reaches_a_lark_tool():
    """The model must not get to narrate a refusal at length before the consent card.
    Aborting at the tool call is what keeps the card the only thing the user reads."""
    card = FakeCard()
    session = _session_for(["让我查一下…"], tool_calls=["approval_list_pending"],
                           follow_up="很抱歉，我没有权限……",
                           auth_url="https://consent")
    with _patch_notify(card, []):
        text = agent_core._stream_to_chat(session, "查待审批", "oc_1")
    assert session.get("walled_tool") == "approval_list_pending"
    # The half-sentence is replaced, not left on the card, and the model's apology for
    # the refusal never runs at all.
    assert "让我查一下" not in text
    assert "很抱歉" not in text
    assert "Lark" in text


def test_stream_does_not_abort_on_websearch_when_unauthorized():
    """Search needs no Lark grant, so an unauthorized user must still get results."""
    card = FakeCard()
    session = _session_for(["天气是…"], tool_calls=["WebSearch___WebSearch"],
                           follow_up="晴天", auth_url="https://consent")
    with _patch_notify(card, []):
        text = agent_core._stream_to_chat(session, "今天天气", "oc_1")
    assert "walled_tool" not in session
    assert text == "天气是…晴天"           # the turn ran to completion


def test_stream_does_not_abort_when_authorized():
    """An authorized session has no auth_url, so tool calls proceed normally."""
    card = FakeCard()
    session = _session_for(["结果…"], tool_calls=["lark_list_my_docs"],
                           follow_up="共 3 个文件")     # no auth_url
    with _patch_notify(card, []):
        text = agent_core._stream_to_chat(session, "查文档", "oc_1")
    assert "walled_tool" not in session
    assert text == "结果…共 3 个文件"


def test_stream_collects_tool_results_for_the_auth_wall_fallback():
    """A turn that runs to completion still has to expose what the tools said, or the
    fallback auth-wall check has nothing to read."""
    card = FakeCard()
    # No auth_url while streaming, so the abort (which normally fires first) stays out
    # of the way and the turn runs to completion — the fallback's actual situation is a
    # session that looked authorized until a tool refused mid-turn.
    session = _session_for(["查询中…"], tool_calls=["lark_list_my_docs"],
                           follow_up="失败了")
    with _patch_notify(card, []):
        agent_core._stream_to_chat(session, "查文档", "oc_1")
    session["auth_url"] = "https://consent"   # the vault lookup that follows finds one
    assert any(agent_core._NEEDS_TOKEN_MARKER in t for t in session["tool_texts"])
    assert agent_core._hit_auth_wall(session) is True


def test_stream_falls_back_to_text_when_card_cannot_open():
    """No cardkit:card:write scope → open() fails → the answer still arrives as text."""
    card = FakeCard(open_ok=False)
    sent = []
    session = _session_for(["hello ", "world"])
    with _patch_notify(card, sent):
        text = agent_core._stream_to_chat(session, "msg", "oc_1")
    assert text == "hello world"
    assert card.updates == []              # never tried to stream
    assert sent == ["hello world"]         # delivered as plain text instead


def test_stream_falls_back_when_an_update_fails_midway():
    """A card that dies mid-stream must still deliver the full answer via text."""
    card = FakeCard(update_ok=False)       # first update flips ok to False
    sent = []
    session = _session_for(["x" * 100, "y" * 100])
    with _patch_notify(card, sent):
        text = agent_core._stream_to_chat(session, "msg", "oc_1")
    assert text == "x"*100 + "y"*100
    assert sent == [text]                  # fell back after the failed update
    assert card.closed_with is None        # never reached a clean close


def test_fresh_session_evicts_a_cached_unauthorized_session():
    """The consent-resume replay must not reuse the cached unauthorized session: its
    MCP sessions hold an empty token, so the turn would wall again. Measured in the
    field — consent completed 31 s after the prompt, inside _UNAUTH_TTL, so waiting
    for expiry is not a fix."""
    closed, built = [], []

    def fake_build(actor_id, email, mem_sid, workload_token=""):
        built.append(mem_sid)
        return {"created": 1e9}                      # authorized: no auth_url

    cached = {"auth_url": "https://consent", "created": 1e9, "stack": object()}
    with mock.patch.dict(agent_core._sessions, {"lark:u|mem1": cached}, clear=True), \
         mock.patch.object(agent_core, "_build_session", fake_build), \
         mock.patch.object(agent_core, "_close_session", lambda s: closed.append(s)), \
         mock.patch.object(agent_core.time, "time", return_value=1e9 + 10):
        # Without fresh, the cached unauthorized session is returned (still inside TTL).
        s = agent_core._get_session("lark:u", "", "mem1")
        assert s.get("auth_url") == "https://consent"
        assert built == []

        # With fresh, it is evicted, its MCP sessions closed, and a new one built.
        s = agent_core._get_session("lark:u", "", "mem1", fresh=True)

    assert closed == [cached], "the stale MCP sessions must be closed, not leaked"
    assert built == ["mem1"]
    assert "auth_url" not in s


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# --------------------------- thread bookkeeping ------------------------------
# /status and /clear moved here from the router when the conversation became LangGraph
# state. Both must answer without building a session — that would re-handshake every MCP
# server for what is a bookkeeping question.

def _fake_saver(messages=None, deleted=None):
    class _S:
        def __init__(self):
            self.deleted = deleted

        async def aget_tuple(self, config):
            if messages is None:
                return None
            return type("T", (), {"checkpoint": {"channel_values": {"messages": messages}}})()

        async def adelete_thread(self, thread_id):
            self.deleted = thread_id
    return _S()


def test_history_stats_counts_only_user_and_assistant_messages():
    from langchain_core.messages import AIMessage as AI, ToolMessage as TM
    msgs = [HumanMessage("q"), AI(content="", tool_calls=[{"name": "t", "args": {}, "id": "1"}]),
            TM(content="r", tool_call_id="1"), AI(content="a")]
    with mock.patch.object(agent_core, "_checkpointer", return_value=_fake_saver(msgs)):
        assert agent_core.history_stats("lark:ou_x", "sess-1") == {"messages": 3}


def test_history_stats_reports_zero_for_an_untouched_thread():
    with mock.patch.object(agent_core, "_checkpointer", return_value=_fake_saver(None)):
        assert agent_core.history_stats("lark:ou_x", "sess-1") == {"messages": 0}


def test_history_stats_degrades_instead_of_failing_status():
    """/status must still render if the checkpoint cannot be read."""
    broken = mock.Mock()
    broken.aget_tuple.side_effect = RuntimeError("no table")
    with mock.patch.object(agent_core, "_checkpointer", return_value=broken):
        assert agent_core.history_stats("lark:ou_x", "sess-1")["unavailable"] is True


def test_clear_history_deletes_the_thread_and_drops_the_cached_session():
    """A surviving cached session holds a compiled graph whose next turn would write on
    top of a thread the user was told is gone."""
    saver = _fake_saver()
    closed = []
    agent_core._sessions["lark:ou_x|sess-1"] = {"stack": None, "created": 0}
    with mock.patch.object(agent_core, "_checkpointer", return_value=saver), \
         mock.patch.object(agent_core, "_close_session", closed.append):
        assert agent_core.clear_history("lark:ou_x", "sess-1") == {"deleted": True}
    assert saver.deleted == "sess-1"
    assert "lark:ou_x|sess-1" not in agent_core._sessions
    assert len(closed) == 1


def test_clear_history_reports_failure_rather_than_claiming_success():
    broken = mock.Mock()
    broken.adelete_thread.side_effect = RuntimeError("boom")
    with mock.patch.object(agent_core, "_checkpointer", return_value=broken):
        r = agent_core.clear_history("lark:ou_x", "sess-1")
    assert r["deleted"] is False and r["error"] == "RuntimeError"


# --------------------------- prompt caching ----------------------------------
# Bedrock caches nothing without a cachePoint block, and the only route that works is a
# per-call kwarg: model_kwargs is rerouted to additional_model_request_fields, and
# .bind(cache_control=...) is dropped when create_agent calls bind_tools. Both measured —
# so these tests exist to catch the day a library change breaks the injection silently.

class _CaptureClient:
    def __init__(self):
        self.params = {}

    def converse(self, **kw):
        self.params = kw
        return {"output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}


def _invoke_capturing(**ctor):
    from langchain_core.messages import SystemMessage
    client = _CaptureClient()
    m = agent_core._CachingChatBedrockConverse(
        model="global.anthropic.claude-sonnet-5", region_name="us-west-2",
        client=client, **ctor)
    m.bind_tools([{"name": "t", "description": "d",
                   "input_schema": {"type": "object", "properties": {}}}]).invoke(
        [SystemMessage("sys"), HumanMessage("a"), HumanMessage("b")])
    p = client.params
    return {
        "system": sum(1 for b in p.get("system", []) if "cachePoint" in b),
        "tools": sum(1 for t in p.get("toolConfig", {}).get("tools", []) if "cachePoint" in t),
        "messages": sum(1 for msg in p.get("messages", [])
                        for b in (msg.get("content") or [])
                        if isinstance(b, dict) and "cachePoint" in b),
        "ttls": [b["cachePoint"].get("ttl") for b in p.get("system", []) if "cachePoint" in b],
    }


def test_cache_points_reach_the_request_on_system_tools_and_messages():
    got = _invoke_capturing(cache_ttl="1h")
    assert got["system"] == 1 and got["tools"] == 1 and got["messages"] >= 1
    assert got["ttls"] == ["1h"]


def test_five_minute_ttl_is_sent_without_an_explicit_ttl_field():
    """Bedrock's default window is 5m, and langchain-aws omits the field for it rather
    than sending a redundant value — so an absent ttl here is correct, not a bug."""
    assert _invoke_capturing(cache_ttl="5m")["ttls"] == [None]


def test_empty_ttl_disables_caching_entirely():
    """An escape hatch that must really disable it: a cachePoint still costs a 1.25-2x
    write on every request that misses."""
    got = _invoke_capturing(cache_ttl="")
    assert (got["system"], got["tools"], got["messages"]) == (0, 0, 0)


def test_an_explicit_per_call_cache_control_is_not_overridden():
    from langchain_core.messages import SystemMessage
    client = _CaptureClient()
    m = agent_core._CachingChatBedrockConverse(
        model="global.anthropic.claude-sonnet-5", region_name="us-west-2",
        client=client, cache_ttl="1h")
    m.invoke([SystemMessage("sys"), HumanMessage("a")], cache_control={"ttl": "5m"})
    assert [b["cachePoint"].get("ttl") for b in client.params["system"]
            if "cachePoint" in b] == [None]      # 5m → field omitted


# --------------------------- long-term memory --------------------------------
# Two tools, not automatic extraction. The isolation that matters is the namespace: it is
# built from the bound actor, never from a model-supplied argument, so no prompt can make
# recall read another user's records.

def test_no_memory_resource_means_no_tools_rather_than_an_error():
    import memory_tools
    with mock.patch.object(memory_tools, "_MEMORY_ID", ""):
        assert memory_tools.tools_for("lark:ou_x") == []


def test_the_namespace_comes_from_the_bound_actor_not_from_arguments():
    import memory_tools
    calls = {}

    class _C:
        def batch_create_memory_records(self, **kw):
            calls["write"] = kw
            return {"successfulRecords": [{"memoryRecordId": "r1"}]}

        def retrieve_memory_records(self, **kw):
            calls["read"] = kw
            return {"memoryRecordSummaries": [{"content": {"text": "code name is X"}}]}

    with mock.patch.object(memory_tools, "_MEMORY_ID", "mem-1"), \
         mock.patch.object(memory_tools.boto3, "client", return_value=_C()):
        remember, recall = memory_tools.tools_for("lark:ou_alice")
        assert remember.invoke({"fact": "likes tea"}) == "Saved."
        assert "code name is X" in recall.invoke({"query": "code name?"})

    assert calls["write"]["records"][0]["namespaces"] == ["/facts/lark:ou_alice"]
    assert calls["read"]["namespace"] == ["/facts/lark:ou_alice"][0]
    # No strategy id: measured that the service embeds and scores directly-written records
    # without one, so declaring a strategy would only add cost and constraints.
    assert "memoryStrategyId" not in calls["write"]["records"][0]
    assert set(remember.args) == {"fact"} and set(recall.args) == {"query"}


def test_a_rejected_record_is_reported_not_swallowed():
    import memory_tools

    class _C:
        def batch_create_memory_records(self, **kw):
            return {"successfulRecords": [], "failedRecords": [{"errorCode": "Throttled"}]}

    with mock.patch.object(memory_tools, "_MEMORY_ID", "mem-1"), \
         mock.patch.object(memory_tools.boto3, "client", return_value=_C()):
        remember, _ = memory_tools.tools_for("lark:ou_x")
        assert "could not save" in remember.invoke({"fact": "x"})


def test_recall_says_so_when_there_is_nothing_stored():
    """An empty result must not read as a tool failure, or the model retries it."""
    import memory_tools

    class _C:
        def retrieve_memory_records(self, **kw):
            return {"memoryRecordSummaries": []}

    with mock.patch.object(memory_tools, "_MEMORY_ID", "mem-1"), \
         mock.patch.object(memory_tools.boto3, "client", return_value=_C()):
        _, recall = memory_tools.tools_for("lark:ou_x")
        assert recall.invoke({"query": "anything?"}) == "Nothing on record about that."


# --------------------------- bounding the thread -----------------------------
# The thread is permanent per user and DynamoDBSaver has no prune, so summarisation is the
# only bound. It must trigger on a threshold, never per turn: it rewrites the prefix, and
# rewriting the prefix every turn would make the prompt cache miss every turn.

def test_summarisation_triggers_on_a_token_threshold_not_every_turn():
    mw = agent_core._middleware()
    assert len(mw) == 1
    clauses = mw[0]._trigger_clauses
    assert clauses == [{"tokens": agent_core._SUMMARIZE_AT_TOKENS}]
    assert agent_core._SUMMARIZE_AT_TOKENS > 0


def test_the_summariser_does_not_pay_for_prompt_caching():
    """The summary call happens once per crossing and is never re-read, so a cachePoint
    would only buy a 1.25-2x write premium."""
    assert agent_core._middleware()[0].model.cache_ttl == ""


def test_summarisation_can_be_disabled_outright():
    with mock.patch.object(agent_core, "_SUMMARIZE_AT_TOKENS", 0):
        assert agent_core._middleware() == []


def test_summarisation_rewrites_state_so_the_checkpoint_shrinks():
    """If it only trimmed what is sent to the model, the stored thread would keep growing
    towards DynamoDB's item cap. Asserted against the real middleware's contract."""
    from langchain_core.messages import RemoveMessage
    from langchain.agents.middleware.summarization import SummarizationMiddleware
    import inspect
    src = inspect.getsource(SummarizationMiddleware.before_model)
    assert "RemoveMessage" in src and "REMOVE_ALL_MESSAGES" in src
