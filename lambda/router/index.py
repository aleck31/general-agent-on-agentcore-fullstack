"""Router Lambda — Lark webhook ingestion.

Sync path (API Gateway): handle url_verification challenge, verify signature,
then self-invoke asynchronously and return 200 immediately (avoids webhook
timeout). Async path: decrypt + parse the event, resolve the user, invoke the
AgentCore Runtime, and send the reply back to the Lark chat.

Identity: lark:{open_id} — the same identity the web UI resolves to.
"""

from __future__ import annotations

import datetime
import base64
import json
import logging
import os
import re
import time

import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.config import Config
from botocore.exceptions import ReadTimeoutError

import cognito
import files
import lark
import identity

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]
QUALIFIER = os.environ.get("AGENTCORE_QUALIFIER", "DEFAULT")
SELF_FUNCTION_NAME = os.environ.get("SELF_FUNCTION_NAME", os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""))
LAMBDA_TIMEOUT = int(os.environ.get("LAMBDA_TIMEOUT_SECONDS", "60"))

_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_\-.]{1,200}$")

# Leave a margin so the Lambda can still send a reply after a timeout.
READ_TIMEOUT = max(LAMBDA_TIMEOUT - 10, 30)
# `standard` rather than no retries at all. What must never be retried is a read
# timeout — the turn may well have succeeded, and replaying it would duplicate both
# the work and the pushed answer — and standard mode does not retry those (only
# connection errors, throttling and 5xx). Disabling retries outright also gave up on
# those, which are safe and worth retrying.
_RETRIES = {"mode": "standard", "max_attempts": 3}
agentcore = boto3.client(
    "bedrock-agentcore", region_name=AWS_REGION,
    config=Config(read_timeout=READ_TIMEOUT, connect_timeout=10, retries=_RETRIES),
)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)

# The Runtime is invoked over plain HTTPS with the user's own JWT, not through the SDK
# with SigV4 — a CUSTOM_JWT runtime rejects SigV4 outright ("Authorization method
# mismatch"), so the two are alternatives, never a fallback pair.
_RUNTIME_URL = (f"https://bedrock-agentcore.{AWS_REGION}.amazonaws.com/runtimes/"
                f"{urllib.parse.quote(RUNTIME_ARN, safe='')}/invocations"
                f"?qualifier={urllib.parse.quote(QUALIFIER)}")


# ------------------------------- invoke agent -------------------------------

def thread_stats(session_id: str, user_id: str, actor_id: str, mem_sid: str) -> tuple[int, bool]:
    """(messages, capped) for this thread, answered by the agent.

    The conversation is LangGraph state in DynamoDB now, so counting it means decoding a
    checkpoint — the agent owns that. `capped` is kept in the signature the callers already
    use, but is always False: a checkpoint read is exact, unlike the paged ListEvents walk
    this replaces, which gave up after a few pages."""
    r = invoke_agent(session_id, user_id, actor_id, "", action="history_stats",
                     mem_sid=mem_sid)
    return int(r.get("messages") or 0), False


def invoke_agent(session_id: str, user_id: str, actor_id: str, message: str,
                 action: str = "chat", mem_sid: str = "",
                 budget: float | None = None, chat_id: str = "",
                 message_id: str = "", reaction_id: str = "",
                 fresh_session: bool = False) -> dict:
    """Invoke the agent once. Returns the parsed response dict
    {reply, needs_auth, auth_url?} (or {reply:<raw>} on non-JSON).

    `budget` overrides the read timeout for this call — the consent path spends
    part of the Lambda's time waiting for the user, so the retry afterwards must
    fit in what is left, not assume a full budget."""
    payload = json.dumps({
        "action": action, "userId": user_id, "actorId": actor_id,
        "channel": "lark", "message": message,
        # The router owns the Memory thread id (see identity.get_or_create_memory_session).
        "memorySessionId": mem_sid,
        # Where the agent pushes the result when it answers asynchronously.
        "chatId": chat_id,
        # The in-progress reaction this router added, for the agent to remove when
        # the turn ends — only the identity that added one may delete it.
        "messageId": message_id,
        "reactionId": reaction_id,
        # Consent-resume: force a rebuilt session so the new token is used.
        "freshSession": fresh_session,
    }).encode()
    req = urllib.request.Request(
        _RUNTIME_URL, data=payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            # The identity of this turn, signed. The Runtime validates it and hands the
            # agent a workload access token derived from it — the agent cannot name a
            # different user, because it never gets to state one.
            "Authorization": f"Bearer {cognito.user_jwt(actor_id)}",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
        },
    )
    timeout = max(int(budget), 10) if budget is not None else READ_TIMEOUT
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:400]
        logger.error("runtime HTTP %s for %s: %s", e.code, actor_id, body)
        raise
    except TimeoutError as e:  # urllib surfaces a socket timeout as this
        raise ReadTimeoutError(endpoint_url=_RUNTIME_URL) from e
    try:
        data = json.loads(raw)
    except Exception:
        return {"reply": raw or ""}
    if "reply" not in data and "error" in data:
        data["reply"] = data["error"]
    return data


def _app_identity() -> str:
    """The Lark app the bot speaks as. Shown next to the user identity because this
    sample's whole point is that the two are different: tools reach Lark as the
    *user* (their vaulted token), while replies, reactions and cards go out as the
    *app* (its tenant token). Masked — an appId is not a secret, but there is no
    reason to paste a full identifier into a chat."""
    app_id = lark.get_credentials()[0]
    if not app_id:
        return "未配置"
    return f"{app_id[:8]}…{app_id[-4:]}" if len(app_id) > 14 else app_id


# ------------------------- execution environment probe -----------------------

# Measured: probing a session id with no live microVM provisions one (a fresh id
# answered in 1.4 s), so this is NOT a passive read — /status materialises what it
# reports. That is a fair trade for making turnover visible, but it does mean the
# id shown may have been created by the command itself. Budget is kept short so a
# slow cold start degrades to "unknown" instead of stalling the command.
_PROBE_SECONDS = int(os.environ.get("STATUS_PROBE_SECONDS", "8"))


def _microvm_line(session_id: str, user_id: str, actor_id: str) -> str:
    """One line describing the microVM currently bound to this session id.

    AgentCore's terms: a session (keyed by runtimeSessionId) is served by a
    dedicated execution environment, realised as a microVM. The mapping is 1:1 but
    not permanent — once the microVM is terminated (idleRuntimeSessionTimeout,
    default 15 min; maxLifetime, default 8 h; both configurable — or
    StopRuntimeSession; or a failed health check) the same session id gets a brand
    new microVM with sanitized memory, not the old one back. The session id does not
    change then, which is why it alone can't show this and the microVM reports its
    own id (agent/server.py:_INSTANCE)."""
    if not session_id:
        return "无（尚未建立会话）"
    data = None
    for attempt in range(2):
        try:
            data = invoke_agent(session_id, user_id, actor_id, "",
                                action="status", budget=_PROBE_SECONDS)
            break
        except Exception as e:  # noqa: BLE001 — diagnostics must not fail the command
            # A 409 RetryableConflictException means the service is mid-provision for
            # this session; AWS documents a short backoff. Retried only here: the chat
            # path deliberately disables retries so a timeout can't replay a turn.
            retryable = "RetryableConflict" in type(e).__name__ or "409" in str(e)
            if retryable and attempt == 0:
                logger.info("microVM probe conflicted, retrying once")
                time.sleep(1)
                continue
            logger.info("microVM probe failed for %s: %s", actor_id, type(e).__name__)
            return "未知（探测未返回；下条消息仍会正常处理）"
    inst = data.get("instance")
    if not inst:
        return "运行中（旧镜像，未上报实例信息）"
    # Two distinct figures, both meaningful; the process's own uptime is neither, and
    # is deliberately not shown. kernelUptime is the microVM's age (from the kernel,
    # the only trustworthy source); sessionAge is how long it has served this session.
    parts = []
    kup = data.get("kernelUptime")
    if kup is not None:
        parts.append(f"已运行 {_secs(kup)}")
    age = data.get("sessionAge")
    if age is not None:
        parts.append(f"服务本会话 {_secs(age)}")
    return f"{inst}（{'，'.join(parts)}）" if parts else f"{inst}（运行中）"


def _secs(seconds) -> str:
    """Always seconds, never minutes. The two figures shown together are meant to be
    compared (microVM age vs. how long it has served this session), and mixed units
    make that arithmetic awkward — this is a lifecycle demo, not a status page."""
    return f"{int(float(seconds))} 秒"


# ------------------------------- consent wait -------------------------------

# How long the async Lambda holds, waiting for the user to finish 3LO consent.
# Bounded by the Lambda timeout (see the agentcore read_timeout above); on
# timeout we fall back to "send your message again".
LARK_OAUTH_PROVIDER = os.environ.get("LARK_OAUTH_PROVIDER", "agentcore-fullstack-3lo")
AGENT_WORKLOAD_NAME = os.environ.get("AGENT_WORKLOAD_NAME", "agentcore-fullstack-wl")
LARK_SCOPES = os.environ.get("LARK_SCOPES", "drive:drive docx:document offline_access").split()
SHIM_RETURN_URL = os.environ.get("SHIM_RETURN_URL", "")  # required by GetResourceOauth2Token

# One runtime can front several IdPs — one OAuth provider per downstream system.
# IDP_REGISTRY is a JSON list of {key, provider, scopes, label}; `key` is what the
# user types (/auth lark). Falls back to the single-provider env vars.
def _load_idps() -> dict:
    raw = os.environ.get("IDP_REGISTRY", "").strip()
    if raw:
        try:
            return {i["key"]: i for i in json.loads(raw)}
        except Exception:
            logger.exception("bad IDP_REGISTRY, falling back to single provider")
    return {"lark": {"key": "lark", "provider": LARK_OAUTH_PROVIDER,
                     "scopes": LARK_SCOPES, "label": "Lark"}}


IDPS = _load_idps()


def user_authorized(session_id: str, user_id: str, actor_id: str,
                    idp_key: str = "lark") -> bool:
    """Whether this user has authorised `idp_key`, answered by the agent.

    The router cannot answer it: a consent is vaulted against the workload identity the
    Runtime derives from the inbound JWT, and re-deriving one here reads a different
    namespace and always reports "no" (see docs/agentcore-behavior.md)."""
    r = invoke_agent(session_id, user_id, actor_id, "", action="auth_status")
    return bool(r.get(idp_key))


# --------------------------- consent completion -----------------------------

def _jwt_claim(token: str, name: str) -> str:
    """One claim out of an unverified JWT, for logging which identity was used."""
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        return str(json.loads(base64.urlsafe_b64decode(body)).get(name, ""))
    except Exception:  # noqa: BLE001
        return "?"


def complete_consent(actor_id: str, session_uri: str) -> None:
    """Bind a finished 3LO consent to this user's signed identity.

    Called by the shim's /return, which owns the browser redirect but deliberately not
    the identity: minting user JWTs stays in one place. `userToken`, not `userId` — the
    consent session belongs to the JWT-derived vault namespace, and naming the user by
    string instead fails with `AccessDeniedException: Invalid or expired session`, which
    reads like a timing problem and is really a namespace mismatch (measured)."""
    jwt = cognito.user_jwt(actor_id)
    resp = agentcore.complete_resource_token_auth(
        sessionUri=session_uri,
        userIdentifier={"userToken": jwt},
    )
    # This call returning without raising is NOT evidence that a grant was stored: a
    # consent completed here has been observed to leave the vault empty for every reader
    # (both namespaces, every scope set), so log what identity and session it actually
    # bound. Claims only — never the token.
    logger.info("consent completed for %s: session=%s jwt_sub=%s jwt_username=%s resp=%s",
                actor_id, session_uri[-24:], _jwt_claim(jwt, "sub"),
                _jwt_claim(jwt, "username"),
                {k: v for k, v in resp.items() if k != "ResponseMetadata"})


# ----------------------------- consent resume -------------------------------

def resume_consented_turn(actor_id: str) -> None:
    """Replay the message that hit an auth wall, now that the user has consented.
    Invoked by the shim's /return after 3LO completes — this is the callback-driven
    resume that lets the task continue without the user re-sending.

    No-op if nothing was parked (the user may have run /auth directly, with no turn
    to resume) or it expired."""
    if not actor_id.startswith("lark:"):
        logger.info("resume: unexpected actor_id %r", actor_id)
        return
    open_id = actor_id.split(":", 1)[1]
    user_id, _ = identity.resolve_user("lark", open_id)
    if not user_id:
        logger.info("resume: no user for %s", actor_id)
        return
    parked = identity.take_pending_auth(user_id)
    if not parked:
        logger.info("resume: nothing parked for %s", actor_id)
        return
    message, chat_id = parked["message"], parked["chatId"]
    logger.info("resume: replaying for %s: %r", actor_id, message[:80])
    session_id = identity.get_or_create_session(user_id)
    mem_sid = identity.get_or_create_memory_session(user_id, actor_id)
    # A fresh reaction on the resumed turn is not possible (the original message id
    # isn't parked), so none is passed — the answer arrives without a marker.
    invoke_agent(session_id, user_id, actor_id, message,
                 action="chat_async", mem_sid=mem_sid, chat_id=chat_id,
                 fresh_session=True)


# ---------------------------- approval events -------------------------------

# Both arrive in the legacy 1.0 schema. `approval_task` is the actionable one: it names
# an approver (open_id) and the task, which is exactly what a decision needs.
# `approval_instance` reports the instance's own status and is accepted only so the log
# shows it was seen — deciding from it would take another call to learn whose task it is.
_APPROVAL_EVENT_TYPES = {"approval_task", "approval_instance"}


def _approval_prompt(instance_code: str, task_id: str, open_id: str,
                     approval_code: str) -> str:
    """The turn the agent wakes up to. The ids are handed over rather than left to be
    discovered: an event-driven turn has no user to ask, and `user_id` decides whose
    name the decision is recorded under — too consequential to let the model guess."""
    return "\n".join([
        "【审批事件】有一条待审批任务分配给了你，请代为处理。",
        f"approval_code: {approval_code}",
        f"instance_code: {instance_code}",
        f"task_id: {task_id}",
        f"user_id（审批归属人，就是你）: {open_id}",
        "",
        "请先查看审批详情，判断这件事本身是否成立：内容是否完整、是否与申报事由相符、有无异常。",
        "然后直接做出批准或拒绝，并说明理由。",
        "是否允许自动决定由审批工具在代码里判定 —— 不要自己揣测权限范围而放弃处理；",
        "如果工具拒绝，把它给出的原因转述给我即可。",
    ])


def process_approval_event(ev: dict, context=None) -> None:
    """Event-driven approval: a task lands, the agent decides it with nobody present.

    Which definitions reach here is already decided by what we subscribed to
    (scripts/subscribe-approvals.sh), and whether a decision is *allowed* is enforced
    in the approval MCP server. So this function deliberately re-checks neither — it
    only establishes that there is a real pending task, for a known user, once."""
    # Logged whole: the payload shape for these events is thinly documented, so this is
    # the ground truth for whoever extends it next.
    logger.info("approval event: %s", json.dumps(ev, ensure_ascii=False)[:900])

    status = str(ev.get("status", "")).upper()
    # `open_id` appears in the doc's sample payload but not in its field table, which
    # documents only `user_id` ("operator id", and empty on auto-approve tasks) — in the
    # tenant user_id format, not the open_id this project keys identity on. So open_id
    # is what we need and the less documented of the two. Guessing wrong fails closed:
    # an id that isn't the approver's resolves to no allowlisted user, or to someone
    # with no vaulted grant, and the approval server refuses either way.
    open_id = str(ev.get("open_id", "") or "")
    task_id = str(ev.get("task_id", "") or "")
    instance_code = str(ev.get("instance_code", "") or "")
    approval_code = str(ev.get("approval_code", "") or "")

    # Only a task still awaiting a decision is actionable. This is also what stops the
    # obvious loop: the agent's own approve emits another event, with a settled status.
    if status != "PENDING":
        logger.info("approval: status=%s — nothing to decide", status or "(none)")
        return
    if not (open_id and task_id and instance_code):
        logger.info("approval: no per-approver task in this event, skipping")
        return
    # Lark redelivers until acked, and the ack goes out long before the agent decides.
    if not identity.claim_approval_task(task_id):
        logger.info("approval: task %s already claimed — redelivery", task_id)
        return

    actor_id = f"lark:{open_id}"
    user_id, _ = identity.resolve_user("lark", open_id)
    if not user_id:
        # Someone outside the demo's allowlist. Their approval is their own business —
        # staying silent is the right move, not an error.
        logger.info("approval: %s not in the allowlist, leaving it alone", actor_id)
        return

    message = _approval_prompt(instance_code, task_id, open_id, approval_code)
    # No chat here — an approval event carries none — so the approver's open_id is the
    # delivery address, which the senders read as "DM this person".
    # Park unconditionally: only the agent can tell whether this person has a grant, and
    # asking costs an invocation. An unnecessary park expires by TTL.
    identity.park_pending_auth(user_id, message, open_id)
    logger.info("approval: dispatching task %s for %s", task_id, actor_id)
    _dispatch_turn(user_id, actor_id, message, open_id, context=context)


# ------------------------------- async processing ---------------------------

def process_lark_event(body: str, headers: dict, context=None) -> None:
    """Runs in the async self-invocation. Decrypt (if needed), handle message."""
    try:
        event_data = json.loads(body)
    except Exception:
        logger.error("async: invalid body")
        return

    # decrypt if encrypted
    if "encrypt" in event_data and "header" not in event_data:
        decrypted = lark.decrypt_event(event_data["encrypt"])
        if decrypted is None:
            logger.error("async: decryption failed")
            return
        event_data = decrypted

    header = event_data.get("header", {})
    event = event_data.get("event", {})
    # Message events use schema 2.0 (type in `header`); approval events still use 1.0,
    # where the type sits inside `event`. Reading both is what lets one webhook URL
    # serve both kinds.
    event_type = header.get("event_type") or event.get("type", "")
    logger.info("event_type=%s", event_type)
    if event_type in _APPROVAL_EVENT_TYPES:
        process_approval_event(event, context)
        return
    if event_type != "im.message.receive_v1":
        logger.info("ignoring non-message event")
        return

    sender = event.get("sender", {})
    if sender.get("sender_type") != "user":
        logger.info("ignoring non-user sender")
        return
    open_id = sender.get("sender_id", {}).get("open_id")
    message = event.get("message", {})
    chat_id = message.get("chat_id")
    message_id = message.get("message_id", "")
    msg_type = message.get("message_type")
    content_str = message.get("content", "{}")
    logger.info("message from open_id=%s chat_id=%s type=%s", open_id, chat_id, msg_type)
    if not (open_id and chat_id):
        return

    # extract text
    try:
        content = json.loads(content_str)
    except Exception:
        content = {}
    text = content.get("text", "") if msg_type == "text" else content.get("text", "")

    # strip @mentions in group chats
    if message.get("chat_type") == "group":
        for m in message.get("mentions", []) or []:
            text = text.replace(m.get("key", ""), "").strip()

    actor_id = f"lark:{open_id}"
    user_id, is_new = identity.resolve_user("lark", open_id)
    logger.info("resolve_user -> user_id=%s is_new=%s", user_id, is_new)
    if user_id is None:
        logger.info("user not allowed: %s", actor_id)
        lark.send_message(
            chat_id,
            f"You are not authorized yet. Your ID: {actor_id}. "
            f"Share it with the admin to request access.",
        )
        return

    agent_message = text.strip() or "hi"

    cmd = agent_message.lower()
    if cmd in ("/help", "/?"):
        lark.send_message(chat_id, "\n".join([
            "可用命令：",
            "  /auth        查看各 IdP 的授权状态",
            "  /auth <idp>  对该 IdP 授权或重新授权",
            "  /status      当前身份、会话与对话记录",
            "  /new         开启新的对话（切换运行实例）",
            "  /reset       重置对话记录（运行实例不变）",
            "  /clear       清除对话记录（运行实例不变）",
            "  /reconnect   切换运行实例（对话记录保留）",
        ]))
        return
    # The runtime session (which microVM serves you) and the Memory thread (your
    # conversation history) are independent ids.
    #
    # /reset — same runtime instance, new Memory thread (history starts over).
    if cmd == "/reset":
        mem_sid = identity.rotate_memory_session(user_id)
        logger.info("memory session rotated for %s -> %s", actor_id, mem_sid)
        lark.send_message(chat_id, "已开始新的对话记录（运行实例不变）。")
        return
    # /new — new runtime instance AND new Memory thread: a fully fresh start.
    if cmd == "/new":
        identity.drop_session(user_id)
        mem_sid = identity.rotate_memory_session(user_id)
        logger.info("runtime + memory session rotated for %s -> %s", actor_id, mem_sid)
        lark.send_message(chat_id, "已开启新会话：新的对话记录，且由新的运行实例处理。")
        return
    # /clear — actually delete this thread's events. Different from /reset, which
    # just starts a new thread and leaves the old data in place.
    if cmd == "/clear":
        mem_sid = identity.get_or_create_memory_session(user_id, actor_id)
        rt_sid = identity.get_or_create_session(user_id)
        n, _ = thread_stats(rt_sid, user_id, actor_id, mem_sid)
        r = invoke_agent(rt_sid, user_id, actor_id, "", action="clear_history",
                         mem_sid=mem_sid)
        ok = bool(r.get("deleted"))
        logger.info("history cleared for %s: deleted=%s (%d messages)", actor_id, ok, n)
        lark.send_message(chat_id, f"已删除对话记录（{n} 条消息）。" if ok
                          else "删除对话记录失败，请稍后再试。")
        return
    # /reconnect — new runtime instance, same checkpoint thread. Demonstrates that the
    # conversation outlives the container: a fresh microVM still remembers, because the
    # thread is addressed by user identity, not by which microVM served it.
    if cmd == "/reconnect":
        identity.drop_session(user_id)
        mem_sid = identity.get_or_create_memory_session(user_id, actor_id)
        n, capped = thread_stats(identity.get_or_create_session(user_id),
                                 user_id, actor_id, mem_sid)
        logger.info("runtime session dropped (memory kept) for %s", actor_id)
        lark.send_message(
            chat_id, f"已切换运行实例，对话记录保留（{n}{'+' if capped else ''} 条）"
                     "——对话状态存放在 DynamoDB，不随容器生命周期消失。")
        return
    # /auth [idp] — authorization is per-IdP (one OAuth provider per downstream
    # system). Bare /auth lists each IdP's status; /auth <idp> starts a fresh 3LO
    # flow for that one (idempotent — each run hands out a new consent link).
    if cmd == "/auth" or cmd.startswith("/auth "):
        arg = agent_message[5:].strip().lower()
        if not arg:
            lines = ["各 IdP 的授权状态："]
            session_id = identity.get_or_create_session(user_id)
            for key, idp in IDPS.items():
                ok = user_authorized(session_id, user_id, actor_id, key)
                lines.append(f"  {'✅' if ok else '❌'} {key} ({idp.get('label', key)})"
                             f"{'' if ok else ' — 发送 /auth ' + key + ' 授权'}")
            lark.send_message(chat_id, "\n".join(lines))
            return
        if arg not in IDPS:
            lark.send_message(
                chat_id, f"未知的 IdP：{arg}。可用：{', '.join(IDPS) or '（未配置）'}")
            return
        session_id = identity.get_or_create_session(user_id)
        result = invoke_agent(session_id, user_id, actor_id, arg, action="reauth")
        auth_url = result.get("auth_url")
        if auth_url:
            label = IDPS[arg].get("label", arg)
            lark.send_link_message(
                chat_id, f"请授权访问你的 {label} 账号：", "点击授权", auth_url)
            logger.info("forced re-auth for %s (idp=%s)", actor_id, arg)
        else:
            lark.send_message(chat_id, result.get("reply") or result.get("error", "无法发起授权"))
        return
    # /status — read-only diagnostics over the three independent dimensions: the
    # session id that routes you, the container currently serving that id, and the
    # Memory thread holding your history.
    if cmd == "/status":
        info = identity.session_info(user_id)
        rt_sid = info.get("sessionId", "")
        mem_sid = identity.get_or_create_memory_session(user_id, actor_id)
        events, capped = thread_stats(rt_sid, user_id, actor_id, mem_sid)
        last = info.get("lastActivity", 0)
        last_str = (datetime.datetime.fromtimestamp(last, datetime.timezone.utc)
                    .strftime("%Y-%m-%d %H:%M UTC") if last else "—")
        lines = [
            f"应用身份：{_app_identity()}",
            f"用户身份：{actor_id}",
            f"会话路由键：{rt_sid or '尚未建立（发一条普通消息后创建）'}",
            f"当前 microVM：{_microvm_line(rt_sid, user_id, actor_id)}",
            f"记忆线程：{mem_sid}",
            f"该线程对话记录：{events}{'+' if capped else ''} 条",
            f"最近活跃：{last_str}",
            "授权状态：发送 /auth 查看",
        ]
        lark.send_message(chat_id, "\n".join(lines))
        logger.info("status for %s: events=%d", actor_id, events)
        return

    # If the user isn't authorized yet, a Lark tool this turn may hit an auth wall
    # deep in the async run — past where the router can see it. Park the message now
    # so the shim's /return can replay it once consent lands. Only for unauthorized
    # users: an authorized turn won't wall, and parking every message would be waste.
    # Left to expire by TTL if this turn needs no Lark tool after all.
    identity.park_pending_auth(user_id, agent_message, chat_id)
    _dispatch_turn(user_id, actor_id, agent_message, chat_id, message_id, context)


def _dispatch_turn(user_id: str, actor_id: str, agent_message: str, chat_id: str,
                   message_id: str = "", context=None) -> None:
    """Invoke the agent for one turn and deliver the reply. Shared by the webhook
    path and the consent-resume path (shim replays a parked message here)."""
    session_id = identity.get_or_create_session(user_id)
    mem_sid = identity.get_or_create_memory_session(user_id, actor_id)
    logger.info("invoking agent: session=%s mem=%s msg=%r",
                session_id, mem_sid, agent_message[:80])
    # Mount this user's files once per session. It costs a control-plane round trip and a
    # cold start, so it is claimed per session rather than attempted per turn — and it
    # doubles as the warm-up, since InvokeAgentRuntimeCommand starts the microVM. A
    # failure here is not fatal: the turn then runs without file tools.
    if files.enabled() and identity.mount_needs_bootstrap(user_id, session_id):
        files.bootstrap(session_id, actor_id)
    # Acknowledge before doing anything slow: the first token is seconds away
    # (session assembly, MCP handshake, model latency), and until then the user has
    # no way to tell "working on it" from "my message never arrived". The agent
    # removes this when the turn ends.
    reaction_id = lark.add_reaction(message_id)
    try:
        # chat_async: the agent accepts the work, returns at once, and pushes the
        # answer to the chat itself. A synchronous wait cannot cover real tasks —
        # both InvokeAgentRuntime and this Lambda cap out long before they finish.
        result = invoke_agent(session_id, user_id, actor_id, agent_message,
                              action="chat_async", mem_sid=mem_sid, chat_id=chat_id,
                              message_id=message_id, reaction_id=reaction_id)
        # First-use 3LO: post the consent link. The turn is already parked, so the
        # shim's /return replays it when consent lands — the user does not re-send.
        if result.get("needs_auth"):
            auth_url = result.get("auth_url")
            if auth_url:
                lark.send_link_message(
                    chat_id, "需要访问你的 Lark 账号，请先授权：", "点击授权", auth_url)
            else:  # no structured url — fall back to the agent's text
                lark.send_message(chat_id, result.get("reply", ""))
            # No synchronous wait here. It used to poll the vault, which the router
            # cannot read (docs/agentcore-behavior.md), so it only ever timed out. The
            # shim's /return replays the parked turn, claiming it atomically.
            return
        reply = result.get("reply", "")
    except ReadTimeoutError:
        # We stopped waiting; the agent keeps running and may still finish, so
        # don't report failure — the doc it was writing might well exist.
        logger.warning("agent read timeout for %s", actor_id)
        reply = ("That took longer than I can wait for. It may still have "
                 "completed — please check, or try a smaller request.")
    except Exception as e:  # noqa: BLE001
        logger.exception("agent invocation failed")
        reply = f"Sorry, something went wrong ({type(e).__name__})."

    if reply:
        lark.send_message(chat_id, reply)


# ------------------------------- handler ------------------------------------

def _resp(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body)}


# The browser holds this for the token's lifetime, so keep it short: it is authority to act
# as that user against the Runtime. Same authority a Lark turn already carries, now in a tab.
_WEB_ALLOWED_ORIGINS = os.environ.get("WEB_ALLOWED_ORIGINS", "")


def _cors(origin: str) -> dict:
    """CORS for the page's own origin only. The Runtime's endpoint answers `*` itself, but
    this route hands out a credential, so it is allowlisted."""
    allowed = [o.strip() for o in _WEB_ALLOWED_ORIGINS.split(",") if o.strip()]
    if origin and origin in allowed:
        return {"Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Headers": "content-type",
                "Access-Control-Allow-Methods": "POST,OPTIONS"}
    return {}


def _web_session(body: str) -> dict:
    """{code} -> {token, runtimeArn, region, threadId}. The code proves who is asking."""
    try:
        payload = json.loads(body or "{}")
    except Exception:  # noqa: BLE001
        return _resp(400, {"error": "invalid JSON"})
    open_id = lark.open_id_from_auth_code(payload.get("code", ""))
    if not open_id:
        return _resp(401, {"error": "could not establish identity from that code"})
    actor_id = f"lark:{open_id}"
    user_id, _ = identity.resolve_user("lark", open_id)
    if not user_id or not identity.is_user_allowed("lark", open_id):
        logger.info("web session refused for %s (allowlist)", actor_id)
        return _resp(403, {"error": "not allowlisted"})
    logger.info("web session issued for %s", actor_id)
    return _resp(200, {
        "token": cognito.user_jwt(actor_id),
        # Echoed so the page can name itself. Not a credential: the agent verifies the
        # claim against the vaulted token's real owner before using it.
        "actorId": actor_id,
        "runtimeArn": RUNTIME_ARN,
        "region": os.environ.get("AWS_REGION", ""),
        "qualifier": QUALIFIER,
    })


def handler(event, context):
    # Async self-invocation path
    if event.get("_async_dispatch"):
        logger.info("async dispatch: processing lark event")
        process_lark_event(event["body"], event.get("headers", {}), context)
        return {"ok": True}

    # Consent-completion path: the shim's /return calls us synchronously, because only
    # the router mints user JWTs and the completion must name the user by token.
    # Answered with ok/error so the shim can render an honest page.
    if event.get("_complete_consent"):
        actor_id = event.get("actorId", "")
        session_uri = event.get("sessionUri", "")
        if not (actor_id and session_uri):
            return {"ok": False, "error": "actorId and sessionUri required"}
        try:
            complete_consent(actor_id, session_uri)
        except Exception as e:  # noqa: BLE001 — reported to the browser, not swallowed
            logger.exception("consent completion failed for %s", actor_id)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True}

    # Consent-resume path: the shim invokes us here after a user finishes 3LO, so the
    # message that hit the auth wall can be replayed with the token now in the vault
    # — the user does not re-send. See .dev/adr and PLAN-consent-resume.
    if event.get("_consent_resumed"):
        actor_id = event.get("actorId", "")
        resume_consented_turn(actor_id)
        return {"ok": True}

    path = event.get("rawPath", event.get("requestContext", {}).get("http", {}).get("path", ""))
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    headers = event.get("headers", {}) or {}
    body = event.get("body", "") or ""
    logger.info("webhook hit: method=%s path=%s bytes=%d", method, path, len(body))

    if path.endswith("/health"):
        return _resp(200, {"status": "ok"})

    # Web entrypoint: trade a Lark h5 authorization code for a token the browser can use to
    # call the Runtime directly. The router stays the only place that mints user JWTs, and
    # the code is what makes the identity verified rather than claimed.
    if path.endswith("/web/session") and method == "POST":
        return _web_session(body)

    if not path.endswith("/webhook/lark"):
        return _resp(404, {"error": "not found"})

    # url_verification challenge (handled synchronously, may be encrypted)
    try:
        parsed = json.loads(body)
    except Exception:
        parsed = {}
    if "encrypt" in parsed and "type" not in parsed:
        decrypted = lark.decrypt_event(parsed["encrypt"])
        parsed = decrypted or {}
    if parsed.get("type") == "url_verification":
        challenge = parsed.get("challenge", "")
        if not _CHALLENGE_RE.match(challenge):
            return _resp(400, {"error": "invalid challenge"})
        return _resp(200, {"challenge": challenge})

    # verify signature (fail-closed)
    if not lark.verify_signature(headers, body.encode()):
        return _resp(401, {"error": "invalid signature"})

    # dispatch async and ack immediately
    try:
        lambda_client.invoke(
            FunctionName=SELF_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps({"_async_dispatch": True, "body": body,
                                "headers": {k: v for k, v in headers.items()
                                            if k.lower().startswith("x-lark-")}}).encode(),
        )
    except Exception:
        logger.exception("async dispatch failed")
    return _resp(200, {"ok": True})
