"""Code generation and execution, with a workspace that belongs to one user.

Three tools — run_code, run_command, list_files — over an AgentCore Code Interpreter
session that mounts that user's own S3 Files Access Point at /mnt/workspace. So generated
code has somewhere durable to write, and a script from last week still runs against the
files it produced.

`actor_id` is bound when the tools are built, never taken as an argument: it decides which
Access Point gets mounted, and that is the only thing standing between two users' files.
The isolation itself is the platform's — one session mounts exactly one Access Point, at
the microVM boundary — but *choosing* it is ours, so nothing here accepts it from a caller.

Measured constraints that shape this file, all in docs/agentcore-behavior.md: the mount
needs VPC network mode (SANDBOX refuses it); `mountPath` must match
`/mnt/[a-zA-Z0-9._-]+/?`; `readFiles`/`writeFiles` cannot reach the mount, so files are
read and written by running code; write-through to S3 is asynchronous.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re

import boto3
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

log = logging.getLogger("agent.code")

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_INTERPRETER_ID = os.environ.get("CODE_INTERPRETER_ID", "")
_FS_ARN = os.environ.get("FILES_FS_ARN", "")
_WORKSPACE = os.environ.get("WORKSPACE_PATH", "/mnt/workspace")
# Long enough to outlive a turn that thinks, short enough that an abandoned session is not
# billed for hours. The platform caps at 28,800.
_SESSION_TIMEOUT = int(os.environ.get("CODE_SESSION_TIMEOUT_SECONDS", "1800"))
# One tool result goes into the model's context, so a runaway print must not fill it.
_MAX_OUTPUT = int(os.environ.get("CODE_MAX_OUTPUT_CHARS", "4000"))


def available() -> bool:
    return bool(_INTERPRETER_ID and _FS_ARN)


def _fs_id() -> str:
    return _FS_ARN.rsplit("/", 1)[-1]


def _ap_path(actor_id: str) -> str:
    """The Access Point's server-side root for this actor.

    Sanitised because it becomes a path: an actor id is `lark:ou_…` and the colon has no
    business in one. Same shape the previous implementation used, so existing files stay
    reachable."""
    return "/users/" + re.sub(r"[^A-Za-z0-9_.-]", "_", actor_id)


def _access_point_for(actor_id: str) -> str:
    """Get or create this user's Access Point, returning its ARN.

    Idempotent by `clientToken`: a stable hash of the actor, so a second call returns the
    same Access Point instead of creating a rival one. `rootDirectory` is fixed here and
    enforced server-side, which is what keeps a mount from seeing another user's prefix
    however the sandbox behaves."""
    s3files = boto3.client("s3files", region_name=_REGION)
    path = _ap_path(actor_id)
    for ap in s3files.list_access_points(fileSystemId=_fs_id()).get("accessPoints", []):
        if (ap.get("rootDirectory") or {}).get("path") == path:
            return ap["accessPointArn"]
    r = s3files.create_access_point(
        fileSystemId=_fs_id(),
        clientToken=hashlib.sha256(actor_id.encode()).hexdigest()[:64],
        tags=[{"key": "actor", "value": actor_id}],
        # 1000/1000 rather than root: files land owned by an unprivileged uid, and the
        # sandbox sees only this subtree anyway.
        posixUser={"uid": 1000, "gid": 1000},
        rootDirectory={"path": path,
                       "creationPermissions": {"ownerUid": 1000, "ownerGid": 1000,
                                               "permissions": "0700"}})
    log.info("created workspace access point for %s at %s", actor_id, path)
    return r["accessPointArn"]


class _Sandbox:
    """One Code Interpreter session, started on first use and reused for the rest of it.

    Held by the caller (agent_core's session cache) rather than module state, so its
    lifetime matches the conversation's and two users can never share one."""

    def __init__(self, actor_id: str) -> None:
        self.actor_id = actor_id
        self._client = boto3.client("bedrock-agentcore", region_name=_REGION)
        self._session_id = ""

    def session_id(self) -> str:
        if self._session_id:
            return self._session_id
        mount = {"s3FilesConfiguration": {
            "accessPointArn": _access_point_for(self.actor_id),
            "fileSystemArn": _FS_ARN,
            "mountPath": _WORKSPACE,
        }}
        r = self._client.start_code_interpreter_session(
            codeInterpreterIdentifier=_INTERPRETER_ID,
            name="agent-" + hashlib.sha256(self.actor_id.encode()).hexdigest()[:16],
            sessionTimeoutSeconds=_SESSION_TIMEOUT,
            filesystemConfigurations=[mount])
        self._session_id = r["sessionId"]
        log.info("code session %s for %s", self._session_id, self.actor_id)
        return self._session_id

    def close(self) -> None:
        if not self._session_id:
            return
        try:
            self._client.stop_code_interpreter_session(
                codeInterpreterIdentifier=_INTERPRETER_ID, sessionId=self._session_id)
        except Exception:  # noqa: BLE001 — the session expires on its own anyway
            log.warning("could not stop code session %s", self._session_id, exc_info=True)
        self._session_id = ""

    def invoke(self, name: str, arguments: dict) -> str:
        """One InvokeCodeInterpreter call, flattened to what the model should read."""
        try:
            r = self._client.invoke_code_interpreter(
                codeInterpreterIdentifier=_INTERPRETER_ID, sessionId=self.session_id(),
                name=name, arguments=arguments)
        except Exception as e:  # noqa: BLE001 — narrowed by the message check below
            # A code session times out (30 min) well before the cached agent session does
            # (50), so a held id goes stale mid-conversation and every later call fails.
            # Safe to retry with a fresh session: the error is raised before the code runs
            # (`ValidationException: … session … is not active`), so nothing ran twice.
            if "is not active" not in str(e):
                raise
            log.info("code session %s expired; starting a new one", self._session_id)
            self._session_id = ""
            r = self._client.invoke_code_interpreter(
                codeInterpreterIdentifier=_INTERPRETER_ID, sessionId=self.session_id(),
                name=name, arguments=arguments)
        chunks, failed = [], False
        for event in r["stream"]:
            result = event.get("result") or {}
            failed = failed or bool(result.get("isError"))
            structured = result.get("structuredContent") or {}
            text = (structured.get("stdout", "") + structured.get("stderr", "")).strip()
            if not text:
                text = " ".join(c.get("text", "") for c in result.get("content", [])).strip()
            if text:
                chunks.append(text)
            if structured.get("exitCode"):
                chunks.append(f"[exit {structured['exitCode']}]")
        out = "\n".join(chunks).strip() or ("(failed, no output)" if failed else "(no output)")
        return out[:_MAX_OUTPUT] + ("\n…(truncated)" if len(out) > _MAX_OUTPUT else "")


class _RunCodeArgs(BaseModel):
    code: str = Field(description="The complete program to run. It starts in the "
                                  "workspace, so plain relative paths land there.")
    language: str = Field(default="python",
                          description="python, javascript or typescript.")


class _RunCommandArgs(BaseModel):
    command: str = Field(description="A shell command, run in the workspace.")


class _ListFilesArgs(BaseModel):
    path: str = Field(default=".", description="A path relative to the workspace root.")


def tools_for(actor_id: str, sandbox: _Sandbox | None = None) -> list:
    """The three code tools bound to this user, or [] when code execution is not deployed.

    Absent-not-broken, like search and long-term memory: without an interpreter the agent
    simply cannot run code."""
    if not available():
        return []
    box = sandbox or _Sandbox(actor_id)

    def _guard(fn):
        def run(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception as e:  # noqa: BLE001 — a broken sandbox must not kill the turn
                log.exception("code tool failed for %s", actor_id)
                return f"The sandbox could not run that ({type(e).__name__})."
        return run

    # Every call is prefixed with a cd: the sandbox's own working directory is elsewhere
    # (/opt/amazon/genesis1p-tools/var) and anything written there dies with the session.
    # The model is not told about the mount — it just has a working directory that persists.
    def run_code(code: str, language: str = "python") -> str:
        if language == "python":
            code = f"import os\nos.chdir({_WORKSPACE!r})\n" + code
        else:
            code = f"process.chdir({_WORKSPACE!r});\n" + code
        return box.invoke("executeCode", {"language": language, "code": code})

    def run_command(command: str) -> str:
        return box.invoke("executeCommand", {"command": f"cd {_WORKSPACE} && {command}"})

    def list_files(path: str = ".") -> str:
        return box.invoke("executeCommand",
                          {"command": f"cd {_WORKSPACE} && ls -la -- {json.dumps(path)}"})

    return [
        StructuredTool.from_function(
            func=_guard(run_code), name="run_code",
            description=(
                "Run code and get its output. Use this for anything computed rather than "
                "recalled — data work, file conversion, checking your own arithmetic. "
                "Files you write persist between turns and across days, so build on them "
                "instead of regenerating them. Python has pandas available."),
            args_schema=_RunCodeArgs),
        StructuredTool.from_function(
            func=_guard(run_command), name="run_command",
            description=(
                "Run one shell command in the workspace. Use it for the things a shell is "
                "better at than a program — inspecting files, moving them, checking sizes. "
                "There is no internet access here."),
            args_schema=_RunCommandArgs),
        StructuredTool.from_function(
            func=_guard(list_files), name="list_files",
            description=(
                "List what is already in the workspace. Worth doing before you claim a "
                "file does or does not exist, since it persists from earlier conversations."),
            args_schema=_ListFilesArgs),
    ]
