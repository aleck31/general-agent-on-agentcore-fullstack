#!/usr/bin/env bash
# Deploy the agent. Override the target with PROFILE=... REGION=... env vars.
#
# Steps (idempotent — re-runnable independently):
#   --base      CDK base stacks (security, agentcore, router, gateway, observability)
#   --memory    AgentCore Memory for long-term records (implied by --runtime)
#   --runtime   create/update the AgentCore Runtime from the built image (CLI)
#   --gateway   Web Search gateway in us-east-1 (only when WEB_SEARCH=true)
#   --webui     static web chat on S3 + CloudFront (only when WEBUI=true)
#   (no arg)    run all steps in order
#
# Step implementation — normally invoked through ./deploy.sh in the repo root,
# which owns the ordering. Callable directly when iterating on one phase:
# Usage: [PROFILE=p REGION=r] scripts/provision.sh [--base|--memory|--runtime|--gateway|--a2a|--webui]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Deployment target: command-line env vars win over .env, which wins over defaults.
_CLI_PROFILE="${PROFILE:-}" _CLI_REGION="${REGION:-}" _CLI_WEB_SEARCH="${WEB_SEARCH:-}"
_CLI_FILES="${FILES_STORAGE:-}"
[ -f .env ] && { set -a; . ./.env; set +a; }
PROFILE="${_CLI_PROFILE:-${PROFILE:-}}"   # empty -> ambient creds (instance role / env)
REGION="${_CLI_REGION:-${REGION:-us-west-2}}"
WEB_SEARCH="${_CLI_WEB_SEARCH:-${WEB_SEARCH:-false}}"
# Persistent per-user files. Off by default because it is the only part of this project
# with a fixed monthly cost: the mount is NFS, so the Runtime has to sit in a VPC and
# needs a NAT to keep reaching Bedrock and Lark. See .dev/adr/0007.
FILES_STORAGE="${_CLI_FILES:-${FILES_STORAGE:-false}}"
PREFIX="agentcore-fullstack"
export AWS_REGION="$REGION" UV_LINK_MODE=copy
# Credentials already in the environment outrank .env's profile.
[ -n "${AWS_ACCESS_KEY_ID:-}" ] || { [ -n "$PROFILE" ] && export AWS_PROFILE="$PROFILE"; } || true

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
export CDK_DEFAULT_ACCOUNT="$ACCOUNT" CDK_DEFAULT_REGION="$REGION"
CDK="npx --yes aws-cdk@2"

# Control-plane calls whose parameters are newer than the AWS CLI's bundled service model.
# Verified against CLI 2.34: it rejects requireServiceS3Endpoint, sessionConfiguration and
# clientAuthenticationMethod as unknown parameters even though the service accepts them.
# uv resolves a current botocore, so these calls do not depend on how fresh the operator's
# CLI happens to be. Usage: acp <operation_name> <json params>  → prints the JSON result.
acp() {
  ACP_OP="$1" ACP_PARAMS="$2" ACP_REGION="${3:-$REGION}" \
  uv run --with 'boto3>=1.43.92' python - <<'PYEOF'
import json, os, sys
import boto3
c = boto3.client("bedrock-agentcore-control", region_name=os.environ["ACP_REGION"])
try:
    r = getattr(c, os.environ["ACP_OP"])(**json.loads(os.environ["ACP_PARAMS"]))
except Exception as e:
    sys.stderr.write(f"{type(e).__name__}: {e}\n")
    sys.exit(1)
print(json.dumps({k: v for k, v in r.items() if k != "ResponseMetadata"}, default=str))
PYEOF
}

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

cfn_out() { # stack, output-key
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null
}

ctx_set() { # key value  — persist a deployment id into .cdk-state.json (gitignored)
  uv run python - "$1" "$2" <<'PY'
import json, os, sys
k, v = sys.argv[1], sys.argv[2]
f = ".cdk-state.json"
d = json.load(open(f)) if os.path.isfile(f) else {}
d[k] = v
with open(f, "w") as fh: json.dump(d, fh, indent=2); fh.write("\n")
print(f".cdk-state.json: {k} = {v}")
PY
}

base_cdk_stacks() {
  # First deploy in a region needs the CDK bootstrap stack; it creates an assets
  # bucket/ECR repo and deploy roles, so this step needs broader IAM permissions.
  aws cloudformation describe-stacks --stack-name CDKToolkit >/dev/null 2>&1 || {
    log "CDK bootstrap — first deploy in $REGION"
    $CDK bootstrap "aws://$ACCOUNT/$REGION"
  }

  log "Base — CDK stacks"
  local stacks=("$PREFIX-security" "$PREFIX-agentcore" "$PREFIX-router"
                "$PREFIX-gateway" "$PREFIX-shim" "$PREFIX-observability")
  # The storage stack only exists when the flag is on (app.py skips it otherwise), so
  # naming it unconditionally would fail the deploy rather than skip it.
  if [ "$FILES_STORAGE" = "true" ]; then
    stacks+=("$PREFIX-storage")
    echo "  files storage: on (VPC + NAT will be created)"
  else
    echo "  files storage: off"
  fi
  $CDK deploy "${stacks[@]}" -c "files_storage=$FILES_STORAGE" \
             --require-approval never --outputs-file cdk.out/outputs.json
  # The web page needs this and CDK assigns it, so it travels through state rather than a
  # cross-stack export — importing it would freeze the router's own exports.
  ctx_set router_api_url "$(cfn_out "$PREFIX-router" ApiUrl)"
}

phase_memory() {
  log "Memory — long-term store"

  # Long-term records only; the conversation lives in DynamoDB. No memoryStrategies on
  # purpose — direct writes are embedded and scored without one, and a custom strategy is
  # rejected without an execution role. eventExpiryDuration is required. See .dev/adr/0008.
  local mname="${PREFIX//-/_}_ltm" mid
  # list-memories returns no `name` — only `id`, which is "<name>-<suffix>". Matching on
  # name would never hit, and this step would create a new memory on every run.
  mid="$(aws bedrock-agentcore-control list-memories --max-results 100 \
        --query "memories[?starts_with(id, '${mname}-')].id" --output text 2>/dev/null | head -1)"

  if [ -n "$mid" ] && [ "$mid" != "None" ]; then
    echo "  reusing $mid"
  else
    mid="$(acp create_memory "$(MNAME="$mname" uv run python -c '
import json, os
print(json.dumps({
    "name": os.environ["MNAME"],
    "description": "Long-term records for the agent; the conversation lives in DynamoDB",
    "eventExpiryDuration": 365,
}))')" | uv run python -c 'import json,sys;print(json.load(sys.stdin)["memory"]["id"])')"
    echo "  created $mid"
  fi

  # Wait for ACTIVE: creation is asynchronous and a write to a CREATING memory fails
  # rather than queueing.
  for _ in $(seq 1 40); do
    [ "$(aws bedrock-agentcore-control get-memory --memory-id "$mid" \
         --query 'memory.status' --output text 2>/dev/null)" = "ACTIVE" ] && break
    sleep 5
  done
  ctx_set memory_id "$mid"
}


phase2_runtime() {
  # The agent Runtime is created straight from the control plane, using the ARM64 image
  # CDK already published to ECR (AgentImageUri).
  #
  # The AgentCore CLI used to do this (`agentcore configure` + `agentcore deploy`), but
  # 0.28 removed `configure` and turned `deploy` into "deploy project infrastructure via
  # CDK", which wants to own the CDK app this repo already has. Driving the API directly
  # removes the dependency on a fast-moving CLI and matches what build-mcp.sh has always
  # done for the MCP server runtimes. It also lets the authorizer and network mode be set
  # at creation time instead of patched in afterwards.
  log "Runtime — create/update the agent Runtime from the CDK-published image"

  local role image model memory shim mcp_arn mcp_url pool client pwsecret ws_url
  local approval_url approval_arn issuer ckpt_table ckpt_bucket
  role="$(cfn_out "$PREFIX-agentcore" ExecutionRoleArn)"
  image="$(cfn_out "$PREFIX-agentcore" AgentImageUri)"
  ckpt_table="$(cfn_out "$PREFIX-agentcore" CheckpointTableName)"
  ckpt_bucket="$(cfn_out "$PREFIX-agentcore" CheckpointBucketName)"
  pool="$(cfn_out "$PREFIX-security" UserPoolId)"
  client="$(cfn_out "$PREFIX-security" UserPoolClientId)"
  issuer="$(cfn_out "$PREFIX-security" CognitoIssuerUrl)"
  pwsecret="$PREFIX/cognito-password-secret"
  ws_url="$(uv run python -c "import json,os;f='.cdk-state.json';print((json.load(open(f)) if os.path.isfile(f) else {}).get('websearch_gateway_url',''))" 2>/dev/null)"
  model="${MODEL_ID:-$(uv run python -c "import json;print(json.load(open('cdk.json'))['context']['default_model_id'])")}"
  memory="$(uv run python -c "import json,os;f='.cdk-state.json';print((json.load(open(f)) if os.path.isfile(f) else {}).get('memory_id',''))" 2>/dev/null)"
  shim="$(cfn_out "$PREFIX-shim" ShimReturnUrl)"

  mcp_arn="$(aws bedrock-agentcore-control list-agent-runtimes \
    --query "agentRuntimes[?agentRuntimeName=='${PREFIX//-/_}_mcp'].agentRuntimeArn" --output text 2>/dev/null | head -1)"
  mcp_url="https://bedrock-agentcore.$REGION.amazonaws.com/runtimes/$(uv run python -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$mcp_arn")/invocations?qualifier=DEFAULT"
  approval_arn="$(aws bedrock-agentcore-control list-agent-runtimes \
    --query "agentRuntimes[?agentRuntimeName=='${PREFIX//-/_}_approval'].agentRuntimeArn" --output text 2>/dev/null | head -1)"
  if [ -n "$approval_arn" ] && [ "$approval_arn" != "None" ]; then
    approval_url="https://bedrock-agentcore.$REGION.amazonaws.com/runtimes/$(uv run python -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$approval_arn")/invocations?qualifier=DEFAULT"
    echo "  approval tools: enabled"
  else
    approval_url=""
    echo "  approval tools: not deployed (./deploy.sh mcp approval to add them)"
  fi

  [ -n "$role" ] || { echo "missing execution role output — run --base first"; exit 1; }
  [ -n "$image" ] || { echo "missing agent image output — run --base first"; exit 1; }
  # Not fatal: the agent falls back to in-process state and keeps answering. Loud, because
  # the symptom is "the bot forgot everything after a timeout", which reads as a bug.
  [ -n "$ckpt_table" ] && [ "$ckpt_table" != "None" ] || \
    echo "  WARNING: no checkpoint table output — conversations will not survive a new microVM"
  [ -n "$mcp_arn" ] && [ "$mcp_arn" != "None" ] || {
    echo "${PREFIX//-/_}_mcp runtime not found — run ./deploy.sh mcp first"; exit 1; }

  # Files storage: the mount is NFS, so the Runtime has to join the storage stack's VPC.
  local subnets="" runtime_sg="" fs_id="" broker_fn=""
  if [ "$FILES_STORAGE" = "true" ]; then
    subnets="$(cfn_out "$PREFIX-storage" RuntimeSubnetIds)"
    runtime_sg="$(cfn_out "$PREFIX-storage" RuntimeSecurityGroupId)"
    fs_id="$(cfn_out "$PREFIX-storage" FileSystemId)"
    broker_fn="$(cfn_out "$PREFIX-storage" BrokerFunctionName)"
    [ -n "$subnets" ] && [ "$subnets" != "None" ] || {
      echo "storage stack outputs missing — run --base with FILES_STORAGE=true first"; exit 1; }
    echo "  files storage: VPC mode, subnets $subnets"
    # The router needs the file system id too, and it is AWS-assigned — so it travels
    # through .cdk-state.json and reaches the router on the re-deploy below.
    ctx_set files_file_system_id "$fs_id"
  fi

  local rname="${PREFIX//-/_}_agent" params rid
  params="$(RNAME="$rname" IMAGE="$image" ROLE="$role" ISSUER="$issuer" CLIENT="$client" \
    SUBNETS="$subnets" SG="$runtime_sg" MODEL="$model" MEMORY="$memory" MCP_URL="$mcp_url" \
    SHIM="$shim" POOL="$pool" PWSECRET="$pwsecret" WS_URL="$ws_url" \
    APPROVAL_URL="$approval_url" FS_ID="$fs_id" BROKER_FN="$broker_fn" PREFIX="$PREFIX" \
    CKPT_TABLE="$ckpt_table" CKPT_BUCKET="$ckpt_bucket" \
    LARK_DOMAIN="$(uv run python -c "import json;print(json.load(open('cdk.json'))['context']['lark_api_domain'])")" \
    uv run python - <<'PYEOF'
import json, os
e = os.environ
env = {
    "BEDROCK_MODEL_ID": e["MODEL"],
    # Conversation state. Without the table the agent still answers, but history dies
    # with the container — so a missing output here is a real regression, not an option.
    "CHECKPOINT_TABLE": e["CKPT_TABLE"],
    # Tunable without a code change: the right threshold depends on the model's context
    # window and on how much a cache-prefix invalidation costs at your traffic.
    "SUMMARIZE_AT_TOKENS": os.environ.get("SUMMARIZE_AT_TOKENS", ""),
    "PROMPT_CACHE_TTL": os.environ.get("PROMPT_CACHE_TTL", ""),
    "CHECKPOINT_BUCKET": e["CKPT_BUCKET"],
    # Long-term memory only; empty until the Memory resource exists.
    "BEDROCK_AGENTCORE_MEMORY_ID": e["MEMORY"],
    "LARK_MCP_URL": e["MCP_URL"],
    "SHIM_RETURN_URL": e["SHIM"],
    "LARK_OAUTH_PROVIDER": e["PREFIX"] + "-3lo",
    "AGENT_WORKLOAD_NAME": e["PREFIX"] + "-wl",
    "LARK_SCOPES": "drive:drive docx:document offline_access",
    "LARK_SECRET_ID": e["PREFIX"] + "/channels/lark",
    "LARK_API_DOMAIN": e["LARK_DOMAIN"],
    "COGNITO_USER_POOL_ID": e["POOL"],
    "COGNITO_CLIENT_ID": e["CLIENT"],
    "COGNITO_PASSWORD_SECRET_ID": e["PWSECRET"],
    "WEBSEARCH_GATEWAY_URL": e["WS_URL"],
    "APPROVAL_MCP_URL": e["APPROVAL_URL"],
    # Empty unless files storage is on; the agent and cred_helper treat that as "no mount".
    "S3FILES_FS_ID": e["FS_ID"],
    "MOUNT_BROKER_FN": e["BROKER_FN"],
    "MOUNT_PATH": "/mnt/user" if e["FS_ID"] else "",
}
net = {"networkMode": "PUBLIC"}
if e["SUBNETS"]:
    net = {"networkMode": "VPC", "networkModeConfig": {
        "subnets": [x for x in e["SUBNETS"].split(",") if x],
        "securityGroups": [e["SG"]]}}
print(json.dumps({
    "agentRuntimeName": e["RNAME"],
    "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": e["IMAGE"]}},
    "roleArn": e["ROLE"],
    "networkConfiguration": net,
    "protocolConfiguration": {"serverProtocol": "HTTP"},
    "environmentVariables": {k: v for k, v in env.items() if v},
    # Inbound CUSTOM_JWT: the platform verifies the caller and hands the agent a workload
    # token derived from that identity, so the agent never names a user itself.
    "authorizerConfiguration": {"customJWTAuthorizer": {
        "discoveryUrl": e["ISSUER"] + "/.well-known/openid-configuration",
        "allowedClients": [e["CLIENT"]]}},
}))
PYEOF
)"

  rid="$(aws bedrock-agentcore-control list-agent-runtimes \
    --query "agentRuntimes[?agentRuntimeName=='$rname'].agentRuntimeId" --output text 2>/dev/null | head -1)"
  if [ -n "$rid" ] && [ "$rid" != "None" ]; then
    # update takes the id instead of the name, and replaces rather than patches — which is
    # why every field above is sent every time.
    acp update_agent_runtime "$(RID="$rid" P="$params" uv run python -c '
import json, os
p = json.loads(os.environ["P"]); p.pop("agentRuntimeName", None)
p["agentRuntimeId"] = os.environ["RID"]
# No requireServiceS3Endpoint: rejected at creation, and "agents created after
# 2026-06-08 cannot modify requireServiceS3Endpoint" on update — so for anything built
# now it is unreachable, not update-only as an earlier reading of the error suggested.
# Leaving it in broke every runtime update while the first deploy still succeeded,
# because only the update path sent it.
print(json.dumps(p))')" >/dev/null
    echo "  updated $rname ($rid)"
  else
    rid="$(acp create_agent_runtime "$params" | uv run python -c 'import json,sys;print(json.load(sys.stdin)["agentRuntimeId"])')"
    echo "  created $rname ($rid)"
  fi
  ctx_set runtime_id "$rid"

  for _ in $(seq 1 40); do
    [ "$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$rid" \
         --query status --output text 2>/dev/null)" = "READY" ] && break
    sleep 5
  done
  echo "  status: $(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$rid" --query status --output text 2>/dev/null)"

  # The router's AGENTCORE_RUNTIME_ARN was synthesised before the runtime existed (a
  # PLACEHOLDER), so re-deploy it now that the real id is known.
  log "Router — re-deploy with the real runtime ARN"
  $CDK deploy "$PREFIX-router" -c "files_storage=$FILES_STORAGE" --require-approval never

  # AgentCore keeps serving existing sessions from the OLD container, so stored session
  # ids would pin users to the previous image. Drop them: the next message starts a new
  # session on the just-deployed version.
  log "Sessions — drop stored ids so users land on the new version"
  local n=0
  for pk in $(aws dynamodb scan --table-name "$PREFIX-identity" \
      --filter-expression "SK = :s" \
      --expression-attribute-values '{":s":{"S":"SESSION"}}' \
      --projection-expression "PK" --query 'Items[].PK.S' --output text 2>/dev/null); do
    aws dynamodb delete-item --table-name "$PREFIX-identity" \
      --key "{\"PK\":{\"S\":\"$pk\"},\"SK\":{\"S\":\"SESSION\"}}" >/dev/null 2>&1 && n=$((n+1))
  done
  echo "  dropped $n session(s)"
}

phase_a2a() {
  log "A2A — expose the agent to peer agents"
  if [ "${A2A:-false}" != "true" ]; then
    echo "  a2a: off (set A2A=true in .env)"; return 0
  fi
  local role image rname rid params
  role="$(cfn_out "$PREFIX-agentcore" ExecutionRoleArn)"
  image="$(cfn_out "$PREFIX-agentcore" AgentImageUri)"
  [ -n "$image" ] || { echo "missing agent image — run --base first"; exit 1; }
  rname="${PREFIX//-/_}_a2a"

  # Same image as the agent, SERVER_MODE=a2a picks the other entrypoint. Only the identity
  # and model variables are needed: an A2A task runs a normal turn.
  params="$(RNAME="$rname" IMAGE="$image" ROLE="$role" PREFIX="$PREFIX" \
    ISSUER="$(cfn_out "$PREFIX-security" CognitoIssuerUrl)" \
    CLIENT="$(cfn_out "$PREFIX-security" UserPoolClientId)" \
    AGENT_ARN="$(aws bedrock-agentcore-control list-agent-runtimes \
      --query "agentRuntimes[?agentRuntimeName=='${PREFIX//-/_}_agent'].agentRuntimeArn" \
      --output text | head -1)" \
    SRC="$(aws bedrock-agentcore-control get-agent-runtime \
      --agent-runtime-id "$(aws bedrock-agentcore-control list-agent-runtimes \
        --query "agentRuntimes[?agentRuntimeName=='${PREFIX//-/_}_agent'].agentRuntimeId" \
        --output text | head -1)" --query 'environmentVariables' --output json 2>/dev/null)" \
    uv run python - <<'PYEOF'
import json, os
e = os.environ
env = json.loads(e["SRC"] or "{}")
env["SERVER_MODE"] = "a2a"
# It forwards to the agent Runtime rather than running a turn: a vaulted consent belongs to
# the Runtime that obtained it, so only that one can act for a user.
env["AGENT_RUNTIME_ARN"] = e["AGENT_ARN"]
print(json.dumps({
    "agentRuntimeName": e["RNAME"],
    "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": e["IMAGE"]}},
    "roleArn": e["ROLE"],
    "networkConfiguration": {"networkMode": "PUBLIC"},
    "protocolConfiguration": {"serverProtocol": "A2A"},
    # Authorization is stripped at the edge unless listed, and forwarding the caller's own
    # bearer is the whole design — this server adds no identity.
    "requestHeaderConfiguration": {"requestHeaderAllowlist": ["Authorization"]},
    "environmentVariables": {k: v for k, v in env.items() if v},
    "authorizerConfiguration": {"customJWTAuthorizer": {
        "discoveryUrl": e["ISSUER"] + "/.well-known/openid-configuration",
        "allowedClients": [e["CLIENT"]]}},
}))
PYEOF
)"

  rid="$(aws bedrock-agentcore-control list-agent-runtimes \
    --query "agentRuntimes[?agentRuntimeName=='$rname'].agentRuntimeId" --output text 2>/dev/null | head -1)"
  if [ -n "$rid" ] && [ "$rid" != "None" ]; then
    acp update_agent_runtime "$(RID="$rid" P="$params" uv run python -c '
import json, os
p = json.loads(os.environ["P"]); p.pop("agentRuntimeName", None)
p["agentRuntimeId"] = os.environ["RID"]
print(json.dumps(p))')" >/dev/null
    echo "  updated $rname ($rid)"
  else
    rid="$(acp create_agent_runtime "$params" | uv run python -c 'import json,sys;print(json.load(sys.stdin)["agentRuntimeId"])')"
    echo "  created $rname ($rid)"
  fi
  for _ in $(seq 1 40); do
    [ "$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$rid" \
         --query status --output text 2>/dev/null)" = "READY" ] && break
    sleep 5
  done
  ctx_set a2a_runtime_id "$rid"
  log "Agent card: .../runtimes/<arn>/invocations/.well-known/agent-card.json"
}


phase_webui() {
  log "WebUI — static web chat on S3 + CloudFront"
  if [ "${WEBUI:-false}" != "true" ]; then
    echo "  webui: off (set WEBUI=true in .env)"; return 0
  fi
  $CDK deploy "$PREFIX-webui" -c webui=true -c "files_storage=$FILES_STORAGE" \
             --require-approval never
  local url
  url="$(cfn_out "$PREFIX-webui" WebUiUrl)"
  # The router must allow this origin to trade an h5 code for a JWT, and CloudFront only
  # assigns the domain now — so it travels through .cdk-state.json and the router is
  # re-deployed, the same route the file system id takes.
  ctx_set web_origin "$url"
  $CDK deploy "$PREFIX-router" -c webui=true -c "files_storage=$FILES_STORAGE" \
             --exclusively --require-approval never
  log "Web chat: $url  (register this domain on the Lark h5 app)"
}


phase3_gateway() {
  # The Web Search connector is only offered in us-east-1, while everything else
  # here runs in $REGION — so this gateway lives there and the agent calls it
  # cross-region. That is fine: web search needs no end-user identity, so
  # it uses GATEWAY_IAM_ROLE outbound auth and never touches the per-user 3LO path
  # that forced the Lark tools off the Gateway in the first place.
  if [ "${WEB_SEARCH:-false}" != "true" ]; then
    log "Gateway — skipped (WEB_SEARCH is not true)"
    return 0
  fi
  # Not configurable: us-east-1 is the only region offering the connector.
  local ws_region="us-east-1"
  log "Gateway — Web Search connector in $ws_region"

  local issuer client grole gid
  issuer="$(cfn_out "$PREFIX-security" CognitoIssuerUrl)"
  client="$(cfn_out "$PREFIX-security" UserPoolClientId)"
  grole="$(cfn_out "$PREFIX-gateway" GatewayRoleArn)"
  [ -n "$issuer" ] && [ "$issuer" != "None" ] || { echo "security stack outputs missing — run --base first"; exit 1; }

  # IAM roles are global, so the role from the main region is reused as-is.
  gid="$(aws bedrock-agentcore-control list-gateways --region "$ws_region" \
    --query "items[?name=='${PREFIX}-websearch-gw'].gatewayId" --output text 2>/dev/null || true)"
  if [ -z "$gid" ] || [ "$gid" = "None" ]; then
    echo "  creating gateway"
    gid="$(acp create_gateway "$(ACP_NAME="${PREFIX}-websearch-gw" ACP_ROLE="$grole" \
        ACP_ISSUER="$issuer" ACP_CLIENT="$client" uv run python -c '
import json, os
e = os.environ
print(json.dumps({
    "name": e["ACP_NAME"],
    "protocolType": "MCP",
    "protocolConfiguration": {"mcp": {
        "supportedVersions": ["2025-11-25"],
        # Without this the gateway issues no Mcp-Session-Id and every tools/call
        # cold-starts a fresh downstream microVM (measured; see docs).
        "sessionConfiguration": {"sessionTimeoutInSeconds": 3600}}},
    "roleArn": e["ACP_ROLE"],
    "authorizerType": "CUSTOM_JWT",
    "authorizerConfiguration": {"customJWTAuthorizer": {
        "discoveryUrl": e["ACP_ISSUER"] + "/.well-known/openid-configuration",
        "allowedClients": [e["ACP_CLIENT"]]}},
}))')" "$ws_region" | uv run python -c 'import json,sys;print(json.load(sys.stdin)["gatewayId"])')"
    # A gateway is briefly CREATING; targets can't be added until it settles.
    for _ in $(seq 1 30); do
      [ "$(aws bedrock-agentcore-control get-gateway --region "$ws_region" \
           --gateway-identifier "$gid" --query status --output text 2>/dev/null)" = "READY" ] && break
      sleep 3
    done
  fi
  echo "  gateway: $gid"

  # The tool name must be WebSearch and parameterValues must be present even when
  # empty ({} = no domain filter); the API rejects a config entry without it.
  local tid
  tid="$(aws bedrock-agentcore-control list-gateway-targets --region "$ws_region" \
    --gateway-identifier "$gid" --query "items[?name=='web-search-tool'].targetId" \
    --output text 2>/dev/null || true)"
  if [ -z "$tid" ] || [ "$tid" = "None" ]; then
    echo "  creating web-search target"
    tid="$(acp create_gateway_target "$(ACP_GID="$gid" uv run python -c '
import json, os
print(json.dumps({
    "gatewayIdentifier": os.environ["ACP_GID"],
    "name": "web-search-tool",
    # The tool name must be WebSearch, and parameterValues has to be present even when
    # empty ({} = no domain filter) — the API rejects a config entry without it.
    "targetConfiguration": {"mcp": {"connector": {
        "source": {"connectorId": "web-search"},
        "configurations": [{"name": "WebSearch", "parameterValues": {}}]}}},
    "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
}))')" "$ws_region" | uv run python -c 'import json,sys;print(json.load(sys.stdin)["targetId"])')"
  fi
  echo "  target: $tid"

  local gurl
  gurl="$(aws bedrock-agentcore-control get-gateway --region "$ws_region" \
    --gateway-identifier "$gid" --query gatewayUrl --output text 2>/dev/null || true)"
  ctx_set websearch_gateway_id "$gid"
  [ -n "$gurl" ] && [ "$gurl" != "None" ] && ctx_set websearch_gateway_url "$gurl"
  echo "  url: $gurl"
  echo "  next: ./deploy.sh runtime  (injects the URL into the agent)"
}

case "${1:-all}" in
  --base|--phase1) base_cdk_stacks ;;  # --phase1 kept as a back-compat alias
  --memory)   phase_memory ;;
  # Memory first: the runtime is created with BEDROCK_AGENTCORE_MEMORY_ID baked into its
  # environment, and UpdateAgentRuntime replaces rather than patches.
  --runtime)  phase_memory; phase2_runtime ;;
  --a2a)      phase_a2a ;;
  --webui)    phase_webui ;;
  --gateway)  phase3_gateway ;;
  all|"")     base_cdk_stacks; phase_memory; phase2_runtime; phase3_gateway; phase_a2a; phase_webui
              log "Webhook URL (register in Lark): $(cfn_out "$PREFIX-router" WebhookLarkUrl)" ;;
  *) echo "usage: [PROFILE=p REGION=r] $0 [--base|--memory|--runtime|--gateway|--a2a|--webui]"; exit 1 ;;
esac
