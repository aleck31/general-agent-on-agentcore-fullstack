#!/usr/bin/env bash
# Drive the A2A surface as a peer agent would, and prove what it does.
#
# Three steps, and the third is the point:
#   1. discover  — read the Agent Card, which is how a peer learns this agent exists
#   2. delegate  — send a task as a real user, then check the MCP server's own log for
#                  `tools/call token=yes`: the only evidence the work happened as that person
#   3. impersonate — send the same bearer while naming somebody else, and watch it fail
#
# Step 3 is the claim worth testing. Step 2 passing proves nothing on its own: a plausible
# answer is not evidence of an identity chain, which is a lesson this project paid for.
#
# Usage: [PROFILE=p REGION=r] scripts/a2a-demo.sh [question]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

[ -f .env ] && { set -a; . ./.env; set +a; }
REGION="${REGION:-us-west-2}"
PREFIX="agentcore-fullstack"
export AWS_REGION="$REGION"
[ -n "${AWS_ACCESS_KEY_ID:-}" ] || { [ -n "${PROFILE:-}" ] && export AWS_PROFILE="$PROFILE"; } || true

QUESTION="${1:-列出我最近的文档}"
ACTOR="lark:${LARK_ADMIN_OPEN_ID:?LARK_ADMIN_OPEN_ID must be set in .env}"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
dim() { printf '\033[2m%s\033[0m\n' "$*"; }

runtime_arn() {  # runtime name suffix -> arn
  aws bedrock-agentcore-control list-agent-runtimes \
    --query "agentRuntimes[?agentRuntimeName=='${PREFIX//-/_}_$1'].agentRuntimeArn" \
    --output text | head -1
}

A2A_ARN="$(runtime_arn a2a)"
[ -n "$A2A_ARN" ] && [ "$A2A_ARN" != "None" ] || {
  echo "no A2A runtime — set A2A=true in .env and run ./deploy.sh a2a"; exit 1; }

# AgentCore serves the container's own root paths under /invocations, so the card sits at
# /invocations/.well-known/agent-card.json — not at the domain root (measured).
ENC="$(uv run python -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$A2A_ARN")"
BASE="https://bedrock-agentcore.$REGION.amazonaws.com/runtimes/$ENC/invocations"

# The same mint the router uses. A peer agent would be given this token by whoever it is
# acting for; here the operator's AWS credentials stand in for that.
mint_jwt() { # actor_id
  sec_out() { aws cloudformation describe-stacks --stack-name "$PREFIX-security" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
  COGNITO_USER_POOL_ID="$(sec_out UserPoolId)" \
  COGNITO_CLIENT_ID="$(sec_out UserPoolClientId)" \
  COGNITO_PASSWORD_SECRET_ID="$(aws cloudformation describe-stack-resources \
      --stack-name "$PREFIX-security" \
      --query "StackResources[?starts_with(LogicalResourceId,'CognitoPasswordSecret')].PhysicalResourceId" \
      --output text | head -1)" \
  uv run --with boto3 python -c "
import sys; sys.path.insert(0, 'lambda/router')
import cognito; print(cognito.user_jwt(sys.argv[1]))" "$1"
}

rpc() { # bearer, actorId, text  -> the reply text, or the raw envelope on failure
  local sid
  sid="a2a-$(uuidgen | tr -d - | tr 'A-Z' 'a-z')$(uuidgen | tr -d - | tr 'A-Z' 'a-z' | head -c 8)"
  curl -s -X POST "$BASE?qualifier=DEFAULT" \
    -H "Authorization: Bearer $1" -H 'Content-Type: application/json' \
    -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: $sid" \
    -d "$(ACTOR="$2" TEXT="$3" uv run python -c '
import json, os, uuid
print(json.dumps({"jsonrpc": "2.0", "id": "1", "method": "message/send",
                  "params": {"message": {"messageId": str(uuid.uuid4()), "role": "user",
                                         "parts": [{"text": os.environ["TEXT"]}]},
                             "metadata": {"actorId": os.environ["ACTOR"]}}}))')" \
  | uv run python -c '
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except ValueError:
    print(raw[:400]); raise SystemExit
parts = (d.get("result") or {}).get("parts") or []
text = "".join(p.get("text", "") for p in parts)
print(text or json.dumps(d)[:400])'
}

# ---------------------------------------------------------------- 1. discover
log "1. Discover — what a peer agent learns before delegating anything"
BEARER="$(mint_jwt "$ACTOR")"
curl -s -H "Authorization: Bearer $BEARER" "$BASE/.well-known/agent-card.json?qualifier=DEFAULT" \
  | uv run python -c '
import json, sys
c = json.load(sys.stdin)
print("  name:  " + c.get("name", "?"))
print("  says:  " + c.get("description", "")[:120] + "…")
for s in c.get("skills", []):
    tags = ",".join(s.get("tags", []))
    print("  skill: {} — {}  tags={}".format(s.get("id"), s.get("name"), tags))
print("  streaming: {}".format((c.get("capabilities") or {}).get("streaming")))'

# ---------------------------------------------------------------- 2. delegate
log "2. Delegate — a task that only means anything as a specific person"
dim "   actorId: $ACTOR"
dim "   task:    $QUESTION"
SINCE=$(( $(date +%s) * 1000 ))
echo
REPLY_TEXT="$(rpc "$BEARER" "$ACTOR" "$QUESTION")"
printf '%s\n' "$REPLY_TEXT" | sed 's/^/  /'

# A first run — or one after the vaulted grant lapses — answers with a consent link instead
# of doing the work. Exactly one link is minted and then we wait: each request mints a new
# pending session and invalidates the one before it, so a second link on screen makes the
# first fail with "Invalid or expired session" the moment the human clicks the older one.
NEEDS_CONSENT=0
case "$REPLY_TEXT" in *"/identities/oauth2/authorize"*) NEEDS_CONSENT=1 ;; esac
if [ "$NEEDS_CONSENT" = 1 ]; then
  log "   Consent needed before this task can run as that person"
  if [ -t 0 ]; then
    dim "   Open the link above — the newest one only — approve, then press Enter (5 min)."
    read -r -t 300 _ || true
    echo
    REPLY_TEXT="$(rpc "$BEARER" "$ACTOR" "$QUESTION")"
    printf '%s\n' "$REPLY_TEXT" | sed 's/^/  /'
    case "$REPLY_TEXT" in *"/identities/oauth2/authorize"*)
      dim "   Still not consented. Clicking an older link fails with \"Invalid or expired"
      dim "   session\" — only the last one printed is live." ;;
    esac
  else
    dim "   No terminal to wait on, so no retry: re-run interactively to finish consent."
    dim "   Not minting a second link — it would invalidate the one above."
  fi
fi

log "   Evidence — did a tool actually run, and as whom?"
dim "   A reply proves nothing; the MCP server's own log line does."
sleep 6
for name in mcp approval; do
  arn="$(runtime_arn "$name")"
  [ -n "$arn" ] && [ "$arn" != "None" ] || continue
  group="/aws/bedrock-agentcore/runtimes/${arn##*/}-DEFAULT"
  hits="$(aws logs filter-log-events --log-group-name "$group" --start-time "$SINCE" \
            --filter-pattern '"tools/call"' --query 'events[].message' --output text 2>/dev/null \
          | tr '\t' '\n' | grep -o 'tools/call[^ ]* token=[a-z]*' | sort | uniq -c || true)"
  printf '  %-9s %s\n' "$name:" "${hits:-（no tools/call in this window）}"
done

# ---------------------------------------------------------------- 3. impersonate
log "3. Impersonate — the same bearer, naming somebody else"
dim "   A2A carries no end-user identity of its own, so the actorId is only a claim."
dim "   The agent checks it against the vaulted token's real owner and refuses a mismatch."
# Skipped when consent is still outstanding: this call mints its own pending session and
# would invalidate the link the human is in the middle of using.
if [ "$NEEDS_CONSENT" = 1 ] && [ ! -t 0 ]; then
  dim "   Skipped — a link for step 2 is still live and this would invalidate it."
  exit 0
fi
dim "   The link this prints is for a user that does not exist — do not click it."
echo
OTHER="$(rpc "$BEARER" "lark:ou_0000000000000000000000000000dead" "$QUESTION")"
printf '%s\n' "$OTHER" | sed 's/^/  /'
echo
# The pass is a refusal, and the refusal must not be a consent link: minting one for a
# claimed actor destroys the real owner's grant, which is how this used to fail.
case "$OTHER" in
  *"/identities/oauth2/authorize"*)
    printf '  \033[31m✗ offered a consent link — that would burn the real owner'"'"'s grant\033[0m\n' ;;
  *不一致*|*[Aa]uthoriz*|*无法*)
    printf '  \033[32m✓ refused — returned nobody'"'"'s data, and minted no consent link\033[0m\n' ;;
  *) printf '  \033[31m✗ look closely: this should not have produced an answer\033[0m\n' ;;
esac

log "Note"
dim "  A call with no bearer at all cannot be shown from here: the Runtime's CUSTOM_JWT"
dim "  authorizer rejects it before the container is reached, so the executor's own"
dim "  \"present the user's bearer token\" branch only guards a direct-to-container call."
