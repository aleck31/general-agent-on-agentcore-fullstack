"""Router unit tests — Lark webhook crypto + event handling.

Run: uv run --with cryptography --with boto3 python -m pytest lambda/router/test_router.py -v

Env is set before importing modules so boto3 clients construct without error;
AWS calls are mocked.
"""

import hashlib
import json
import os
import sys
from unittest import mock

import pytest

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("IDENTITY_TABLE_NAME", "test-identity")
os.environ.setdefault("LARK_SECRET_ID", "test/lark")
os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/x")

sys.path.insert(0, os.path.dirname(__file__))

ENCRYPT_KEY = "test-encrypt-key"

# Golden ciphertext: Lark's AES-256-CBC webhook framing (key=sha256(ENCRYPT_KEY),
# IV "0123456789abcdef" prepended, PKCS#7) of the payload below. Precomputed so the
# test never constructs a cipher itself — it exercises the production decrypt path
# against a fixed, real-format payload.
_GOLDEN_EVENT = {"type": "url_verification", "challenge": "abc123"}
_GOLDEN_CIPHERTEXT = (
    "MDEyMzQ1Njc4OWFiY2RlZjTCy6xCTjl4LV6d/+dDtlSdzPmqcTkTl1iyYaHzzRk+H4NOv2+gZhM3z9/EBitHGNigsYuEJZ1EgAioJmTx0ps="
)


@pytest.fixture
def lark_mod():
    import lark
    lark._creds_cache = {"appId": "cli_x", "appSecret": "s",
                         "verificationToken": "v", "encryptKey": ENCRYPT_KEY}
    lark._token_cache = {"token": "", "expires_at": 0.0}
    return lark


def test_decrypt_event_roundtrip(lark_mod):
    assert lark_mod.decrypt_event(_GOLDEN_CIPHERTEXT) == _GOLDEN_EVENT


def test_verify_signature_valid(lark_mod):
    import time
    body = b'{"hello":"world"}'
    ts = str(int(time.time()))
    nonce = "n1"
    sig = hashlib.sha256(f"{ts}{nonce}{ENCRYPT_KEY}".encode() + body).hexdigest()
    headers = {"X-Lark-Request-Timestamp": ts, "X-Lark-Request-Nonce": nonce,
               "X-Lark-Signature": sig}
    assert lark_mod.verify_signature(headers, body) is True


def test_verify_signature_wrong_sig_fails(lark_mod):
    import time
    ts = str(int(time.time()))
    headers = {"X-Lark-Request-Timestamp": ts, "X-Lark-Request-Nonce": "n",
               "X-Lark-Signature": "deadbeef"}
    assert lark_mod.verify_signature(headers, b"body") is False


def test_verify_signature_replay_window(lark_mod):
    old_ts = "1000000000"  # year 2001 — well outside window
    body = b"body"
    sig = hashlib.sha256(f"{old_ts}n{ENCRYPT_KEY}".encode() + body).hexdigest()
    headers = {"X-Lark-Request-Timestamp": old_ts, "X-Lark-Request-Nonce": "n",
               "X-Lark-Signature": sig}
    assert lark_mod.verify_signature(headers, body) is False


def test_verify_signature_fail_closed_without_key(lark_mod):
    lark_mod._creds_cache = {"encryptKey": ""}
    assert lark_mod.verify_signature({"x-lark-signature": "x"}, b"b") is False


def test_send_message_chunks_long_text(lark_mod):
    calls = []

    class FakeResp:
        def __init__(self, d): self._d = d
        def read(self): return json.dumps(self._d).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        calls.append(req.data)
        return FakeResp({"code": 0})

    lark_mod._token_cache = {"token": "t", "expires_at": 9e18}
    with mock.patch.object(lark_mod.urllib.request, "urlopen", fake_urlopen):
        ok = lark_mod.send_message("oc_x", "A" * 45000)
    assert ok is True
    assert len(calls) == 3  # 45000 / 20000 -> 3 chunks


def test_challenge_regex_rejects_xss():
    import index
    assert index._CHALLENGE_RE.match("safe_challenge-1.2") is not None
    assert index._CHALLENGE_RE.match("<script>") is None


# --- IdP registry ---------------------------------------------------------
# A malformed registry must degrade to the single-provider defaults rather than
# leaving the bot with no way to authorize anyone.

def _load_idps_with(raw: str) -> dict:
    import index
    with mock.patch.dict(os.environ, {"IDP_REGISTRY": raw}):
        return index._load_idps()


def test_idp_registry_parsed():
    idps = _load_idps_with(json.dumps([
        {"key": "lark", "provider": "p-lark", "scopes": ["a"], "label": "Lark"},
        {"key": "google", "provider": "p-goog", "scopes": ["b"], "label": "Google"},
    ]))
    assert set(idps) == {"lark", "google"}
    assert idps["google"]["provider"] == "p-goog"


def test_idp_registry_falls_back_when_malformed():
    idps = _load_idps_with("{not json")
    assert list(idps) == ["lark"]          # still usable
    assert idps["lark"]["provider"]        # and points somewhere


def test_idp_registry_falls_back_when_empty():
    assert list(_load_idps_with("")) == ["lark"]


# --- Memory thread rotation -----------------------------------------------
# Resetting a conversation rotates the thread id instead of deleting events:
# instant, and the old events stay put. So a rotation must yield a NEW id, and
# an unrotated lookup must be stable.

def test_memory_session_is_stable_then_rotates():
    import identity
    stored = {}

    def fake_get_item(Key):
        item = stored.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}

    def fake_put_item(Item):
        stored[(Item["PK"], Item["SK"])] = Item

    with mock.patch.object(identity._table, "get_item", side_effect=fake_get_item), \
         mock.patch.object(identity._table, "put_item", side_effect=fake_put_item):
        first = identity.get_or_create_memory_session("u1", "lark:ou_x")
        again = identity.get_or_create_memory_session("u1", "lark:ou_x")
        assert again == first                      # stable while not rotated
        rotated = identity.rotate_memory_session("u1")
        assert rotated != first
        assert identity.get_or_create_memory_session("u1", "lark:ou_x") == rotated


# --- The status probe ------------------------------------------------------
# One probe, several lines: probe_agent asks the serving container what it is, and the
# _*_line helpers only format. They must never invoke anything themselves.
def _microvm_line_with(response, session_id="ses_x"):
    import index
    with mock.patch.object(index, "invoke_agent", return_value=response) as inv:
        data = index.probe_agent(session_id, "user_1", "lark:ou_x")
        return index._microvm_line(session_id, data), inv


def test_microvm_line_reports_kernel_age_and_session_age():
    import index
    line, inv = _microvm_line_with(
        {"instance": "abc12345", "kernelUptime": 1830.4, "sessionAge": 65.9, "uptime": 777.0})
    assert "abc12345" in line
    assert "1830 秒" in line and "65 秒" in line
    # The process's own uptime is untrustworthy (wall clock inherits the image's
    # base), so it must not appear.
    assert "777" not in line
    # Probing must not spend the whole Lambda budget on a cold start.
    assert inv.call_args.kwargs["action"] == "status"
    assert inv.call_args.kwargs["budget"] == index._PROBE_SECONDS


def test_microvm_line_uses_one_unit_so_the_two_are_comparable():
    """Both durations in seconds: the point is to subtract them (equal → this microVM
    started for you; age >> sessionAge → an existing one took over)."""
    line, _ = _microvm_line_with(
        {"instance": "abc12345", "kernelUptime": 7200, "sessionAge": 30})
    assert "7200 秒" in line and "30 秒" in line
    assert "分钟" not in line and "小时" not in line


def test_microvm_line_without_a_session_does_not_probe():
    """No session id means no microVM to ask about — and probing would create one."""
    import index
    with mock.patch.object(index, "invoke_agent") as inv:
        line = index._microvm_line("", index.probe_agent("", "user_1", "lark:ou_x"))
    assert inv.call_count == 0
    assert "尚未建立会话" in line


def test_microvm_line_retries_a_provisioning_conflict():
    """AgentCore returns a retryable 409 while it provisions a session. The chat path
    disables retries on purpose (a timeout must not replay a turn), so this path has
    to handle it itself or /status reports "unknown" during a cold start."""
    import index

    class RetryableConflictException(Exception):
        pass

    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RetryableConflictException("Session operation in progress")
        return {"instance": "abc12345", "kernelUptime": 30, "sessionAge": 5}

    with mock.patch.object(index, "invoke_agent", side_effect=flaky), \
         mock.patch.object(index.time, "sleep"):
        line = index._microvm_line("ses_x", index.probe_agent("ses_x", "user_1", "lark:ou_x"))
    assert len(calls) == 2
    assert "abc12345" in line


def test_microvm_line_gives_up_after_one_retry():
    """Bounded: /status must not stall on a session stuck mid-provision."""
    import index

    class RetryableConflictException(Exception):
        pass

    with mock.patch.object(index, "invoke_agent",
                           side_effect=RetryableConflictException("busy")) as inv, \
         mock.patch.object(index.time, "sleep"):
        line = index._microvm_line("ses_x", index.probe_agent("ses_x", "user_1", "lark:ou_x"))
    assert inv.call_count == 2
    assert "未知" in line


def test_microvm_line_survives_a_probe_failure():
    """Diagnostics must never break the command that carries them."""
    import index
    with mock.patch.object(index, "invoke_agent", side_effect=RuntimeError("timeout")):
        line = index._microvm_line("ses_x", index.probe_agent("ses_x", "user_1", "lark:ou_x"))
    assert "未知" in line


def test_microvm_line_tolerates_an_older_image():
    """A microVM running a previous image reports no instance field."""
    line, _ = _microvm_line_with({"reply": "pong"})
    assert "旧镜像" in line


def test_model_line_reports_what_the_container_says_not_the_routers_config():
    """UpdateAgentRuntime replaces the environment, so the router's own MODEL_ID can name
    a model nobody is calling. Only the serving container's answer is shown."""
    import index
    assert index._model_line({"model": "global.anthropic.claude-sonnet-5",
                              "cacheTtl": "1h"}) == \
        "global.anthropic.claude-sonnet-5（prompt cache 1h）"
    assert "关闭" in index._model_line({"model": "m", "cacheTtl": ""})
    # An older image reports no model, and a failed probe reports nothing at all.
    assert "探测未返回" in index._model_line({"instance": "abc"})
    assert "探测未返回" in index._model_line(None)


def test_add_reaction_returns_id_and_never_raises():
    """The progress marker is best-effort: the reaction_id is needed to remove it
    later, but a failure must not stop the turn."""
    import lark
    ok = {"code": 0, "data": {"reaction_id": "RE_1"}}

    class FakeResp:
        def __init__(self, d): self._d = d
        def read(self): return json.dumps(self._d).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    with mock.patch.object(lark, "get_tenant_token", return_value="t"), \
         mock.patch.object(lark.urllib.request, "urlopen", lambda *a, **k: FakeResp(ok)):
        assert lark.add_reaction("om_1") == "RE_1"

    # Lark rejecting it, and the call blowing up, both degrade to "" rather than raise.
    with mock.patch.object(lark, "get_tenant_token", return_value="t"), \
         mock.patch.object(lark.urllib.request, "urlopen",
                           lambda *a, **k: FakeResp({"code": 99, "msg": "nope"})):
        assert lark.add_reaction("om_1") == ""
    with mock.patch.object(lark, "get_tenant_token", return_value="t"), \
         mock.patch.object(lark.urllib.request, "urlopen",
                           mock.Mock(side_effect=OSError("boom"))):
        assert lark.add_reaction("om_1") == ""
    # No message id (or no token) means nothing to react to.
    with mock.patch.object(lark, "get_tenant_token", return_value="t"):
        assert lark.add_reaction("") == ""


def test_status_shows_both_identities_and_masks_the_app_id():
    """/status must name both identities — the whole point of this sample is that
    tools act as the *user* while replies go out as the *app*. The appId is not a
    secret but is masked anyway: no reason to paste a full identifier into a chat."""
    import index
    with mock.patch.object(index.lark, "get_credentials",
                           return_value=("cli_a1b2c3d4e5f6g7h8", "s", "v", "k")):
        line = index._app_identity()
    assert line == "cli_a1b2…g7h8"
    assert "c3d4e5f6" not in line          # middle is not exposed

    # Unconfigured must not blow up or print an empty parenthesis.
    with mock.patch.object(index.lark, "get_credentials", return_value=("", "", "", "")):
        assert index._app_identity() == "未配置"


# --------------------------- consent resume ---------------------------------
# When a Lark tool hits an auth wall in the async run, the message is parked; the
# shim's /return replays it after consent. These pin the parts that must not drift:
# take is once-only, expiry is honoured, and resume actually re-invokes the agent.

def test_take_pending_auth_claims_atomically_and_honours_ttl():
    """The claim IS the delete. Two paths race to replay a parked turn — the router
    polling the vault and the shim's post-consent callback — so a get-then-delete lets
    both read the same item and run the turn twice, with duplicated side effects."""
    import identity, time as _t
    store = {}
    def fake_put(Item): store[Item["SK"]] = Item
    def fake_del(Key, ReturnValues=None):
        old = store.pop(Key["SK"], None)
        return {"Attributes": old} if (old and ReturnValues == "ALL_OLD") else {}
    with mock.patch.object(identity._table, "put_item", side_effect=fake_put), \
         mock.patch.object(identity._table, "delete_item", side_effect=fake_del):
        identity.park_pending_auth("u1", "查待审批", "oc_1")
        assert identity.take_pending_auth("u1") == {"message": "查待审批", "chatId": "oc_1"}
        # The loser of the race gets nothing — replay must not happen twice.
        assert identity.take_pending_auth("u1") is None

        # An item past its ttl is treated as absent, even before DynamoDB sweeps it.
        store["PENDING_AUTH"] = {"message": "old", "chatId": "oc_1",
                                 "ttl": int(_t.time()) - 1}
        assert identity.take_pending_auth("u1") is None


def test_take_pending_auth_reads_the_item_from_the_delete_itself():
    """Guards the mechanism, not just the outcome: without ReturnValues=ALL_OLD the
    claim degrades to a blind delete and nothing is ever replayed."""
    import identity
    with mock.patch.object(identity._table, "delete_item",
                           return_value={"Attributes": {"message": "m", "chatId": "c",
                                                        "ttl": 9999999999}}) as dele:
        assert identity.take_pending_auth("u1") == {"message": "m", "chatId": "c"}
    assert dele.call_args.kwargs.get("ReturnValues") == "ALL_OLD"


# The synchronous consent-wait is gone (it polled a vault the router cannot read), so the
# shim's /return is the only replay path. The atomic claim above is what still stops a
# double replay if anything else ever races it.

def test_resume_replays_the_parked_message():
    import index, identity
    with mock.patch.object(identity, "resolve_user", return_value=("u1", False)), \
         mock.patch.object(identity, "take_pending_auth",
                           return_value={"message": "查待审批", "chatId": "oc_1"}), \
         mock.patch.object(identity, "get_or_create_session", return_value="ses_x"), \
         mock.patch.object(identity, "get_or_create_memory_session", return_value="mem_x"), \
         mock.patch.object(index, "invoke_agent") as inv:
        index.resume_consented_turn("lark:ou_abc")
    assert inv.call_count == 1
    kw = inv.call_args.kwargs
    assert kw["action"] == "chat_async"
    assert kw["chat_id"] == "oc_1"
    assert inv.call_args.args[3] == "查待审批"   # message positional


def test_resume_is_a_noop_when_nothing_parked():
    """A user who ran /auth directly has no turn to resume — must not invoke."""
    import index, identity
    with mock.patch.object(identity, "resolve_user", return_value=("u1", False)), \
         mock.patch.object(identity, "take_pending_auth", return_value=None), \
         mock.patch.object(index, "invoke_agent") as inv:
        index.resume_consented_turn("lark:ou_abc")
    assert inv.call_count == 0


def test_resume_ignores_malformed_actor():
    import index
    with mock.patch.object(index, "invoke_agent") as inv:
        index.resume_consented_turn("not-a-lark-id")
    assert inv.call_count == 0



# --------------------------- event-driven approval ---------------------------

def _approval_event(**over) -> dict:
    """A Lark `approval_task` event. Legacy 1.0 schema: no `header`, type inside
    `event`. Fields per the official doc, plus the `open_id` its sample payload shows
    but its field table omits."""
    ev = {"app_id": "cli_x", "type": "approval_task", "open_id": "ou_alice",
          "user_id": "b613t51g", "task_id": "t1", "instance_code": "i1",
          "approval_code": "AC1", "status": "PENDING", "operate_time": "1700000000000"}
    ev.update(over)
    return ev


def test_approval_event_dispatches_a_turn_addressed_to_the_approver():
    """An approval event carries no chat, so the approver's open_id is the delivery
    address — the senders read an `ou_` prefix as "DM this person"."""
    import index, identity
    with mock.patch.object(identity, "claim_approval_task", return_value=True), \
         mock.patch.object(identity, "resolve_user", return_value=("u1", False)), \
         mock.patch.object(identity, "park_pending_auth"), \
         mock.patch.object(index, "_dispatch_turn") as disp:
        index.process_approval_event(_approval_event())
    assert disp.call_count == 1
    user_id, actor_id, message, chat_id = disp.call_args.args[:4]
    assert actor_id == "lark:ou_alice"
    assert chat_id == "ou_alice"
    # The ids are handed over rather than left to be discovered: an unattended turn has
    # nobody to ask, and user_id decides whose name the decision is recorded under.
    for token in ("i1", "t1", "AC1", "ou_alice"):
        assert token in message


def test_approval_event_ignores_a_settled_task():
    """The loop-breaker: the agent's own approve emits another event, and acting on it
    would decide the same instance again."""
    import index, identity
    for status in ("APPROVED", "REJECTED", "TRANSFERRED", "DONE"):
        with mock.patch.object(identity, "claim_approval_task", return_value=True), \
             mock.patch.object(index, "_dispatch_turn") as disp:
            index.process_approval_event(_approval_event(status=status))
        assert disp.call_count == 0, status


def test_approval_event_without_an_approver_is_skipped():
    """`approval_instance` events, and auto-approve tasks (documented as having an
    empty user), name nobody — there is no identity to act as."""
    import index, identity
    for missing in ({"open_id": ""}, {"task_id": ""}, {"instance_code": ""}):
        with mock.patch.object(identity, "claim_approval_task", return_value=True), \
             mock.patch.object(index, "_dispatch_turn") as disp:
            index.process_approval_event(_approval_event(**missing))
        assert disp.call_count == 0, missing


def test_approval_event_redelivery_decides_once():
    """Lark redelivers until acked, and the ack goes out long before the agent has
    decided. Without the claim the same task gets approved twice."""
    import index, identity
    with mock.patch.object(identity, "claim_approval_task", return_value=False), \
         mock.patch.object(index, "_dispatch_turn") as disp:
        index.process_approval_event(_approval_event())
    assert disp.call_count == 0


def test_approval_event_for_an_unknown_user_stays_silent():
    """Someone outside the allowlist: their approval is their own business."""
    import index, identity
    with mock.patch.object(identity, "claim_approval_task", return_value=True), \
         mock.patch.object(identity, "resolve_user", return_value=(None, False)), \
         mock.patch.object(index, "_dispatch_turn") as disp:
        index.process_approval_event(_approval_event())
    assert disp.call_count == 0


def test_approval_event_parks_the_turn_when_the_approver_has_not_consented():
    """The approval server refuses to decide without that person's own grant, so the turn
    may wall. Parking is what lets consent-resume replay it — otherwise an unattended turn
    is simply lost. Unconditional: only the agent knows whether a grant exists, and an
    unnecessary park expires by TTL."""
    import index, identity
    with mock.patch.object(identity, "claim_approval_task", return_value=True), \
         mock.patch.object(identity, "resolve_user", return_value=("u1", False)), \
         mock.patch.object(identity, "park_pending_auth") as park, \
         mock.patch.object(index, "_dispatch_turn"):
        index.process_approval_event(_approval_event())
    assert park.call_count == 1
    assert park.call_args.args[2] == "ou_alice"      # DM target, not a chat


def test_legacy_schema_event_is_routed_to_the_approval_path():
    """Message events put the type in `header` (schema 2.0); approval events put it in
    `event` (1.0). Reading only `header` drops every approval event on the floor."""
    import index
    body = json.dumps({"uuid": "u", "type": "event_callback",
                       "event": _approval_event()})
    with mock.patch.object(index, "process_approval_event") as appr:
        index.process_lark_event(body, {})
    assert appr.call_count == 1


def test_message_events_still_take_the_message_path():
    """The 2.0 header must keep winning — a regression here breaks the whole bot."""
    import index
    body = json.dumps({"header": {"event_type": "im.message.receive_v1"},
                       "event": {"sender": {"sender_type": "bot"}}})
    with mock.patch.object(index, "process_approval_event") as appr:
        index.process_lark_event(body, {})
    assert appr.call_count == 0


def test_dm_target_is_inferred_from_the_id_shape(lark_mod):
    """`ou_` is a person, anything else a chat. This is what lets the same senders
    serve an event-driven turn, which only ever knows the person."""
    assert lark_mod._receive_id_type("ou_alice") == "open_id"
    assert lark_mod._receive_id_type("oc_room") == "chat_id"
    assert lark_mod._receive_id_type("ou_alice", "chat_id") == "chat_id"   # explicit wins


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ---------------- signed inbound identity (CUSTOM_JWT cutover) ----------------

def test_invoke_agent_carries_the_users_signed_identity_not_a_string():
    """The whole point of the cutover: the runtime hop is authorised by the user's own
    JWT. A payload string naming a user would be forgeable; a signature is not — and the
    agent never receives an actorId it could substitute for someone else's."""
    import index
    captured = {}

    class FakeResp:
        def read(self): return b'{"reply": "ok"}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["body"] = json.loads(req.data)
        return FakeResp()

    with mock.patch.object(index.cognito, "user_jwt", return_value="JWT-FOR-U") as mint, \
         mock.patch.object(index.urllib.request, "urlopen", fake_urlopen), \
         mock.patch.object(index, "agentcore") as ac:
        out = index.invoke_agent("sess-0123456789abcdef0123456789abcdef",
                                 "user_1", "lark:ou_u", "hello")

    assert out == {"reply": "ok"}
    mint.assert_called_once_with("lark:ou_u")
    assert captured["headers"]["authorization"] == "Bearer JWT-FOR-U"
    # SigV4 must not be used at all — a CUSTOM_JWT runtime rejects it outright.
    ac.invoke_agent_runtime.assert_not_called()
    assert "/invocations" in captured["url"]
    assert captured["body"]["actorId"] == "lark:ou_u"


def test_complete_consent_names_the_user_by_token_not_by_id():
    """A consent session started from a JWT-derived workload token can only be completed
    with that identity. Passing userId fails as `Invalid or expired session`, which reads
    like a timing bug and is really a namespace mismatch (measured)."""
    import index
    with mock.patch.object(index.cognito, "user_jwt", return_value="JWT-FOR-U"), \
         mock.patch.object(index, "agentcore") as ac:
        index.complete_consent("lark:ou_u", "sess-uri")
    ac.complete_resource_token_auth.assert_called_once_with(
        sessionUri="sess-uri", userIdentifier={"userToken": "JWT-FOR-U"})


def test_the_router_never_queries_the_vault_itself():
    """It cannot: a grant is vaulted against the workload identity the Runtime derives from
    the inbound JWT, and one re-derived here reads a different namespace and reports "no"
    forever — an endless consent loop while the agent's own calls succeed. Measured, see
    docs/agentcore-behavior.md. An earlier test asserted the opposite as correct."""
    import index, inspect
    src = inspect.getsource(index)
    assert "get_resource_oauth2_token" not in src
    assert "get_workload_access_token_for_jwt" not in src


def test_credential_load_failure_is_not_cached():
    """A failed secret read must not disable the container for its whole life: seeding or
    rotating the Lark secret would then need a cold start, presenting as
    "no encryptKey configured" long after the secret is correct."""
    import lark
    lark._creds_cache = None
    with mock.patch.object(lark, "_SECRET_ID", "s"), \
         mock.patch.object(lark, "_secrets") as sm:
        sm.get_secret_value.side_effect = ValueError("not json")
        assert lark.get_credentials() == ("", "", "", "")
        assert lark._creds_cache is None          # nothing poisoned
        sm.get_secret_value.side_effect = None
        sm.get_secret_value.return_value = {"SecretString": '{"encryptKey":"k"}'}
        assert lark.get_credentials()[3] == "k"   # next call recovers
    lark._creds_cache = None


def test_auth_status_is_answered_by_the_agent_not_the_vault():
    """The router cannot read the grant: it lives under the workload identity the Runtime
    derives from the inbound JWT, so a locally re-derived one always reports "no"."""
    import index
    with mock.patch.object(index, "invoke_agent",
                           return_value={"lark": True}) as inv:
        assert index.user_authorized("ses_x", "u1", "lark:ou_a", "lark") is True
    assert inv.call_args.kwargs["action"] == "auth_status"


def test_auth_status_treats_a_missing_key_as_unauthorized():
    """An agent error must not read as authorised."""
    import index
    with mock.patch.object(index, "invoke_agent", return_value={"error": "boom"}):
        assert index.user_authorized("ses_x", "u1", "lark:ou_a", "lark") is False


def test_web_session_requires_a_lark_code_and_never_trusts_an_open_id():
    """The browser proves identity with a single-use code Lark issued to this app inside the
    Lark client. Accepting an open_id instead would let anyone name anyone."""
    import index, inspect
    src = inspect.getsource(index._web_identity) + inspect.getsource(index._web_session)
    assert "open_id_from_auth_code" in src
    assert 'payload.get("openId")' not in src and 'payload.get("open_id")' not in src


def test_the_code_exchange_uses_the_v2_endpoint_and_app_credentials():
    """v1 oidc/access_token is deprecated and took a tenant token; v2 takes client
    credentials in the body and returns only a token, so open_id needs user_info."""
    import io
    import lark as larkmod
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append((req.full_url, req.data, dict(req.headers)))
        body = ({"access_token": "u-tok"} if "oauth/token" in req.full_url
                else {"code": 0, "data": {"open_id": "ou_x"}})
        return io.BytesIO(json.dumps(body).encode())

    with mock.patch.object(larkmod, "get_credentials",
                           return_value=("app", "secret", "", "")), \
         mock.patch.object(larkmod.urllib.request, "urlopen",
                           side_effect=lambda req, timeout=None:
                           _ctx(fake_urlopen(req, timeout))):
        assert larkmod.open_id_from_auth_code("c") == "ou_x"
    assert "authen/v2/oauth/token" in seen[0][0]
    assert b'"client_secret": "secret"' in seen[0][1]
    assert "Authorization" not in seen[0][2]      # no tenant token on the redeem call
    assert "authen/v1/user_info" in seen[1][0]
    assert seen[1][2]["Authorization"] == "Bearer u-tok"


class _ctx:
    """urlopen is used as a context manager; wrap a plain BytesIO so the fake matches."""

    def __init__(self, inner):
        self.inner = inner

    def __enter__(self):
        return self.inner

    def __exit__(self, *a):
        return False


def test_web_session_refuses_an_unexchangeable_code():
    import index
    with mock.patch.object(index.lark, "open_id_from_auth_code", return_value=""):
        assert index._web_session('{"code": "bad"}')["statusCode"] == 401


def test_run_command_returns_text_and_never_sends_it_itself():
    """Channel-agnostic on purpose: the same commands answer in Lark and on the web page.
    A branch that sent to Lark directly would work in chat and silently do nothing on the web."""
    import index, inspect
    src = inspect.getsource(index.run_command)
    assert "lark.send" not in src
    with mock.patch.object(index.lark, "send_message") as send:
        assert "/status" in index.run_command("/help", "u1", "lark:ou_a")["text"]
        assert index.run_command("你好", "u1", "lark:ou_a") is None   # not a command
    assert send.call_count == 0


def test_an_auth_link_travels_as_data_so_each_channel_renders_it_its_own_way():
    """Lark wants a rich-text post; the web page wants markdown. Returning the URL keeps
    that choice with the channel."""
    import index, identity
    with mock.patch.dict(index.IDPS, {"lark": {"label": "Lark"}}, clear=True), \
         mock.patch.object(identity, "get_or_create_session", return_value="ses_x"), \
         mock.patch.object(index, "invoke_agent", return_value={"auth_url": "https://c/x"}):
        r = index.run_command("/auth lark", "u1", "lark:ou_a")
    assert r["link"] == {"text": "点击授权", "url": "https://c/x"}


def test_web_command_refuses_anything_that_is_not_a_command():
    """The command endpoint must not become a second way to talk to the agent — that path
    goes straight to the Runtime and is the only one carrying the user's JWT."""
    import index, identity
    with mock.patch.object(index.lark, "open_id_from_auth_code", return_value="ou_x"), \
         mock.patch.object(identity, "resolve_user", return_value=("u1", False)), \
         mock.patch.object(identity, "is_user_allowed", return_value=True):
        assert index._web_command('{"code":"c","command":"你好"}')["statusCode"] == 400
        r = index._web_command('{"code":"c","command":"/help"}')
    assert r["statusCode"] == 200 and "/status" in json.loads(r["body"])["text"]


def test_web_command_establishes_identity_per_request():
    """It rotates session ids, so it cannot trust a token the page is holding."""
    import index
    with mock.patch.object(index.lark, "open_id_from_auth_code", return_value="") as ex:
        assert index._web_command('{"code":"bad","command":"/status"}')["statusCode"] == 401
    assert ex.call_count == 1


def test_cors_is_configured_on_the_api_and_not_duplicated_in_the_handler():
    """The browser preflights a cross-origin JSON POST. With CORS on the HTTP API, API
    Gateway answers OPTIONS itself and ignores the integration's own CORS headers — so
    setting them here too is dead code, which is exactly how this shipped broken."""
    import pathlib
    stack = pathlib.Path(__file__).parents[2] / "stacks" / "router_stack.py"
    src = stack.read_text()
    assert "cors_preflight" in src and "web_allowed_origins.split" in src
    handler_src = pathlib.Path(__file__).with_name("index.py").read_text()
    assert "Access-Control-Allow-Origin" not in handler_src


def test_web_session_honours_the_allowlist():
    import index, identity
    with mock.patch.object(index.lark, "open_id_from_auth_code", return_value="ou_x"), \
         mock.patch.object(identity, "resolve_user", return_value=("u1", False)), \
         mock.patch.object(identity, "is_user_allowed", return_value=False):
        assert index._web_session('{"code": "c"}')["statusCode"] == 403


def test_web_session_returns_a_token_and_where_to_send_it():
    import index, identity
    with mock.patch.object(index.lark, "open_id_from_auth_code", return_value="ou_x"), \
         mock.patch.object(identity, "resolve_user", return_value=("u1", False)), \
         mock.patch.object(identity, "is_user_allowed", return_value=True), \
         mock.patch.object(index.cognito, "user_jwt", return_value="JWT"):
        r = index._web_session('{"code": "c"}')
    body = json.loads(r["body"])
    assert r["statusCode"] == 200 and body["token"] == "JWT"
    assert body["runtimeArn"] and body["qualifier"]


def test_status_survives_a_missing_runtime_session():
    """rt_sid is empty until a turn has run. Passing it through produced a 400 that killed
    the whole handler, so /status silently answered nothing at all."""
    import index
    assert index.thread_stats("", "u1", "lark:ou_a", "mem-1") == {}
    assert index.user_authorized("", "u1", "lark:ou_a", "lark") is False


def test_a_failed_count_does_not_take_down_the_command():
    import index
    with mock.patch.object(index, "invoke_agent", side_effect=RuntimeError("boom")):
        assert index.thread_stats("ses_x", "u1", "lark:ou_a", "mem-1") == {}
        assert index.user_authorized("ses_x", "u1", "lark:ou_a", "lark") is False


def test_status_does_not_manufacture_the_session_it_reports_as_absent():
    """Counting needs a runtime session, so counting eagerly created one — while the line
    above still said "not established". A diagnostic must not cause what it reports."""
    import index, inspect
    src = inspect.getsource(index.run_command)
    i = src.index('cmd == "/status"')
    block = src[i:i + 1400]
    assert "get_or_create_session" not in block
    assert "（发一条普通消息后可见）" in block
