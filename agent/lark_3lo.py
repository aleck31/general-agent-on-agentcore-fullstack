"""Agent-side per-user 3LO for Lark + direct connection to the lark-mcp Runtime.

Why this exists: the agent drives 3LO itself against AgentCore Identity and delivers
the user's vaulted Lark token to the lark-mcp Runtime in a custom passthrough header.

Required by the topology, not a workaround for a missing feature. The Gateway does do
per-user 3LO — verified @2026-08-19 against an OpenAPI target — but it cannot deliver
the token to an MCP server hosted on AgentCore Runtime: that endpoint authenticates
the transport with SigV4 and owns the Authorization header, so there is no slot left
for a per-user Bearer. Hence the custom passthrough header here. Moving the MCP server
to an addressable HTTPS endpoint is what would make the managed path applicable; see
docs/agentcore-behavior.md.

Flow per user (actor_id = "lark:{open_id}"):
  1. get_user_lark_token(actor_id):
       GetWorkloadAccessTokenForUserId → GetResourceOauth2Token(USER_FEDERATION)
       - token vaulted  → return ("token", <lark_user_access_token>)
       - not yet        → return ("auth_url", <url to send to the user in chat>)
  2. with a token, mcp_connection_for(token) describes an MCP connection to the
     lark-mcp Runtime over SigV4, passing the token in the custom header that
     server reads and calls Lark with.

Non-blocking: we never poll waiting for consent. First turn returns an auth_url
(the agent posts it to Lark chat and ends the turn); a later turn finds the
token vaulted and proceeds.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from datetime import timedelta

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import httpx

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_PROVIDER = os.environ.get("LARK_OAUTH_PROVIDER", "agentcore-fullstack-3lo")
_WORKLOAD = os.environ.get("AGENT_WORKLOAD_NAME", "agentcore-fullstack-wl")
_SHIM_RETURN_URL = os.environ.get("SHIM_RETURN_URL", "")  # bare, allowlisted on the workload
_LARK_MCP_URL = os.environ.get("LARK_MCP_URL", "")        # SigV4 Runtime MCP invocations URL
_SCOPES = os.environ.get("LARK_SCOPES", "drive:drive docx:document offline_access").split()
_CUSTOM_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Lark-Token"
_API_DOMAIN = os.environ.get("LARK_API_DOMAIN", "https://open.larksuite.com").rstrip("/")

log = logging.getLogger("agent.lark_3lo")

_agentcore = boto3.client("bedrock-agentcore", region_name=_REGION)


def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


# Which tokens have been confirmed to belong to the actor they are vaulted under.
# Keyed by a digest, never the token itself, and bounded — this is a cache, and a
# token that is already known good does not need re-checking every turn.
_VERIFIED: dict[str, str] = {}
_VERIFIED_MAX = 256


def _token_owner(token: str) -> str:
    """The open_id this Lark token actually belongs to, or "" if it can't be read.
    Needs no extra scope — the lark-cli server reads the same endpoint for whoami."""
    try:
        r = httpx.get(f"{_API_DOMAIN}/open-apis/authen/v1/user_info",
                      headers={"Authorization": f"Bearer {token}"}, timeout=10)
        return ((r.json().get("data") or {}).get("open_id") or "")
    except Exception as e:  # noqa: BLE001
        log.warning("could not read the token's owner: %s", e)
        return ""


def _belongs_to(token: str, actor_id: str) -> bool:
    """Whether this token is really the actor's own.

    Consent completion binds the vaulted token to whatever userId the return-url was
    told (`state`), NOT to the Lark account that actually signed in. So forwarding a
    consent link is enough to get someone else's token vaulted under your name, and
    every later turn would act as them. Checking at the point of use closes that
    regardless of how the token got there. Fails closed: an owner we cannot establish
    is not accepted."""
    digest = hashlib.sha256(token.encode()).hexdigest()[:32]
    if _VERIFIED.get(digest) == actor_id:
        return True
    owner = _token_owner(token)
    expected = actor_id.split(":", 1)[1] if ":" in actor_id else actor_id
    if not owner or owner != expected:
        log.error("vaulted token does not belong to %s (owner=%s) — refusing it",
                  actor_id, owner[:12] + "…" if owner else "unknown")
        return False
    if len(_VERIFIED) >= _VERIFIED_MAX:
        _VERIFIED.pop(next(iter(_VERIFIED)))
    _VERIFIED[digest] = actor_id
    return True


def _claims(token: str) -> dict:
    """Non-secret claims of an unverified JWT, for identifying which principal it is."""
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        c = json.loads(base64.urlsafe_b64decode(body))
        return {k: c[k] for k in ("sub", "aud", "iss", "wid", "workloadId", "username")
                if k in c}
    except Exception:  # noqa: BLE001
        return {"undecodable": True}


def _fetch_vaulted(workload_token: str, state_actor: str, force: bool = False) -> dict:
    """The one GetResourceOauth2Token call, so the two callers cannot drift apart.

    They did: an earlier separate copy omitted `forceAuthentication` and reported "never
    consented" for a user whose token the other copy fetched fine."""
    kwargs = dict(
        workloadIdentityToken=workload_token,
        resourceCredentialProviderName=_PROVIDER,
        scopes=_SCOPES,
        oauth2Flow="USER_FEDERATION",
        customState=_b64url(state_actor),
        forceAuthentication=force,
    )
    if _SHIM_RETURN_URL:
        kwargs["resourceOauth2ReturnUrl"] = _SHIM_RETURN_URL
    resp = _agentcore.get_resource_oauth2_token(**kwargs)
    # Whether a grant came back, which is the difference between "acts as the user" and
    # "asks for consent again" — and the two callers disagree on it, see the open item in
    # docs/agentcore-behavior.md.
    log.info("vault fetch: state=%r -> %s", state_actor,
             "token" if resp.get("accessToken") else "authorizationUrl")
    return resp


def actor_from_workload_token(workload_token: str) -> tuple[str, str]:
    """Who this request is, derived rather than declared → ("actor", "lark:{open_id}").

    A browser on the AG-UI path and a peer on the A2A path cannot be trusted to name a user:
    that would be naming whose conversation and whose files to open. The workload token the
    platform derived from the verified JWT already scopes the vault lookup, and Lark itself
    says who the resulting token belongs to.

    ("needs_consent", "") when nothing is vaulted — the actor is unknowable then, and a web
    or peer turn cannot start a consent flow because completing one needs the actor in
    `state`. Authorising once in Lark chat is the way in."""
    try:
        resp = _fetch_vaulted(workload_token, "resolve")
    except Exception:  # noqa: BLE001
        log.exception("could not resolve the caller from the workload token")
        return "error", ""
    token = resp.get("accessToken")
    if not token:
        return "needs_consent", ""
    owner = _token_owner(token)
    if not owner:
        log.error("vaulted token has no establishable owner — refusing it")
        return "error", ""
    return "actor", f"lark:{owner}"


def get_user_lark_token(actor_id: str, force: bool = False,
                        workload_token: str = "") -> tuple[str, str]:
    """Return ("token", <lark token>) if vaulted, else ("auth_url", <url>).

    actor_id is "lark:{open_id}" — carried through as customState so the return-url can
    complete the consent. force=True always starts a fresh 3LO flow, ignoring any
    vaulted token.

    `workload_token` is the workload access token the Runtime delivered with this
    request, derived from the caller's verified JWT. Passing it is what makes the
    identity unforgeable: this code never gets to *name* a user, it can only use the one
    the platform already authenticated. Absent it (no CUSTOM_JWT inbound) we fall back to
    deriving one from actor_id, which is a trusted-caller assumption — and a different
    vault namespace, so the two are not interchangeable for an existing deployment.

    A vaulted token is used only after it is confirmed to be this actor's own; one
    that isn't gets discarded in favour of a fresh consent flow (see _belongs_to).
    """
    if workload_token:
        wat = workload_token
        # Which identity the platform's token actually carries. A consent opened under this
        # identity has been observed to complete successfully and then be unreadable by
        # every namespace we query, so the open question is whether this differs from the
        # identity GetWorkloadAccessTokenForJWT derives from the same JWT. Claims only.
        pass  # the platform's token; opaque, so nothing useful to log about it
    else:
        log.warning("no platform workload token for %s — falling back to ForUserId",
                    actor_id)
        wat = _agentcore.get_workload_access_token_for_user_id(
            workloadName=_WORKLOAD, userId=actor_id
        )["workloadAccessToken"]
    resp = _fetch_vaulted(wat, actor_id, force)
    token = resp.get("accessToken")
    if token and _belongs_to(token, actor_id):
        return "token", token
    if token:
        # Someone else's grant is sitting under this actor. Re-consent is the only way
        # out: the wrong token stays in the vault, so without forcing a fresh flow the
        # next call would fetch it right back. Guarded against recursion by `force`.
        if not force:
            return get_user_lark_token(actor_id, force=True,
                                       workload_token=workload_token)
        log.error("fresh authorization still produced a token for another account")
    return "auth_url", resp["authorizationUrl"]


class _SigV4HTTPXAuth(httpx.Auth):
    """Sign every httpx request (incl. SSE polls) with SigV4 for bedrock-agentcore.

    Only `auth_flow` (sync) is defined on purpose: httpx's default `async_auth_flow`
    iterates it directly, so the same object serves the async MCP transport — signing
    is pure CPU and blocks nothing."""

    def __init__(self, creds, service: str, region: str):
        self._signer = SigV4Auth(creds, service, region)

    def auth_flow(self, request: httpx.Request):
        headers = dict(request.headers)
        headers.pop("connection", None)  # or the server rejects on signature mismatch
        aws_req = AWSRequest(method=request.method, url=str(request.url),
                             data=request.content, headers=headers)
        self._signer.add_auth(aws_req)
        request.headers.update(dict(aws_req.headers))
        yield request


def mcp_connection_for(lark_token: str, url: str = "") -> dict:
    """A langchain-mcp-adapters `StreamableHttpConnection` for an MCP-server Runtime:
    SigV4 transport + the Lark token in the custom passthrough header. Defaults to the
    lark-cli server; pass `url` for another one (the approval server, say). Same
    transport either way — what differs is which tools the server exposes and which
    identity each of them uses.

    Credentials are resolved per call rather than cached: a Runtime's role credentials
    are refreshed under us, and a signer holding an expired set fails every request."""
    creds = boto3.Session(region_name=_REGION).get_credentials()
    return {
        "transport": "streamable_http",
        "url": url or _LARK_MCP_URL,
        "headers": {_CUSTOM_HEADER: lark_token},
        "auth": _SigV4HTTPXAuth(creds, "bedrock-agentcore", _REGION),
        "timeout": timedelta(seconds=60),
    }
