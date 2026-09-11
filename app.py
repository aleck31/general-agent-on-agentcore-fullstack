#!/usr/bin/env python3
"""CDK application entry point.

A general-purpose agent on Bedrock AgentCore Runtime, reachable from Lark bot chat, with
Lark as the identity provider. Per-user Lark tokens live in the AgentCore Identity Token
Vault (3LO); the agent fetches the calling user's token and passes it to the tool servers
in a custom header, holding no downstream credential of its own.

Deployment is hybrid:
  CDK:  security, agentcore base (role/ECR/S3), shim, router, gateway, observability,
        and storage when files_storage=true
  CLI:  the AgentCore Runtimes, Memory, the OAuth provider and the Web Search gateway,
        created by deploy.sh through control-plane APIs; ids fed back via .cdk-state.json
"""

import json
import os
import pathlib

import aws_cdk as cdk

from stacks.security_stack import SecurityStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.router_stack import RouterStack
from stacks.gateway_stack import GatewayStack
from stacks.shim_stack import ShimStack
from stacks.observability_stack import ObservabilityStack
from stacks.storage_stack import StorageStack

app = cdk.App()

# Per-deployment state (runtime/gateway ids) lives outside version control —
# deploy.sh writes it, we inject it so stacks read it via try_get_context as usual.
_state = pathlib.Path(__file__).parent / ".cdk-state.json"
if _state.is_file():
    for k, v in json.loads(_state.read_text()).items():
        if v:
            app.node.set_context(k, v)

ctx = app.node.try_get_context

env = cdk.Environment(
    account=ctx("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=ctx("region") or os.environ.get("CDK_DEFAULT_REGION") or "us-west-2",
)

prefix = ctx("resource_prefix") or "agentcore-fullstack"

# Context values arrive as strings when passed with -c, and the string "false" is truthy,
# so compare rather than test.
def _flag(name: str) -> bool:
    return str(ctx(name)).strip().lower() == "true"

# --- Security: Cognito user pool + Secrets Manager slots ---
security = SecurityStack(app, f"{prefix}-security", env=env)

# --- AgentCore base: execution role, ECR image, S3 user files ---
# Runtime itself is created out-of-band by deploy.sh; runtime_arn is derived
# from cdk.json context (runtime_id) once it exists.
agentcore = AgentCoreStack(
    app,
    f"{prefix}-agentcore",
    cognito_user_pool_id=security.user_pool_id,
    cognito_client_id=security.user_pool_client_id,
    cognito_issuer_url=security.cognito_issuer_url,
    cognito_password_secret_name=security.cognito_password_secret.secret_name,
    lark_secret_name=security.lark_secret.secret_name,
    env=env,
)

# --- Shim: Lark OAuth RFC-6749 façade + 3LO return-url ---
# Created before the router so the router can consume its return_url for the
# consent-wait vault check.
shim = ShimStack(
    app,
    f"{prefix}-shim",
    lark_api_domain=ctx("lark_api_domain") or "https://open.larksuite.com",
    lark_secret_name=security.lark_secret.secret_name,
    env=env,
)

# --- Storage: S3 Files + per-user mount broker (opt-in) ---
# Off unless files_storage=true, because this is the one stack with a fixed monthly cost
# (a NAT Gateway, which the NFS mount forces — see .dev/adr/0007). Everything else here
# is consumption-priced, so creating this by accident would be the expensive mistake.
# Declared before the router, which needs its key and file system id.
storage = None
if _flag("files_storage"):
    storage = StorageStack(
        app,
        f"{prefix}-storage",
        user_files_bucket=agentcore.user_files_bucket,
        env=env,
    )

# --- Router: Lark webhook ingestion (HTTP API + Lambda + DynamoDB identity) ---
router = RouterStack(
    app,
    f"{prefix}-router",
    mount_ticket_key_arn=storage.ticket_key.key_arn if storage else "",
    # From .cdk-state.json rather than a cross-stack export: the id is assigned by AWS,
    # so provision.sh reads it back after the storage stack exists and re-deploys the
    # router — the same path runtime_id and the gateway URL already take. A CDK export
    # would also deadlock on removal, since the producer cannot drop an export the router
    # still imports.
    s3files_file_system_id=ctx("files_file_system_id") or "",
    runtime_arn=agentcore.runtime_arn,
    runtime_endpoint_qualifier=ctx("runtime_endpoint_id") or "DEFAULT",
    lark_secret_name=security.lark_secret.secret_name,
    shim_return_url=shim.return_url,
    user_pool_id=security.user_pool_id,
    user_pool_arn=security.user_pool_arn,
    user_pool_client_id=security.user_pool_client_id,
    cognito_password_secret_name=security.cognito_password_secret.secret_name,
    env=env,
)

# --- Gateway: demo tool Lambda + Gateway IAM (mcpServer target wired in Phase 3) ---
gateway = GatewayStack(app, f"{prefix}-gateway", env=env)

# --- Observability: dashboard + alarms ---
observability = ObservabilityStack(app, f"{prefix}-observability", env=env)

app.synth()
