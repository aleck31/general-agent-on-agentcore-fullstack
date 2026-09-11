# Architecture

A general-purpose agent on Amazon Bedrock AgentCore, integrated with **Lark (Feishu)** as its interaction channel. This document covers how the pieces fit: how a message becomes a turn, how a turn's answer gets back to the user, what the agent remembers, and how its tools are reached.

> **The Lark identity integration is documented in depth elsewhere, not here.** Two samples cover exactly that subject — [sample-lark-identity-on-agentcore-native](https://github.com/aws-samples/sample-lark-identity-on-agentcore-native) (the native Token Vault path this repo uses) and [sample-lark-identity-on-agentcore-interceptor](https://github.com/aws-samples/sample-lark-identity-on-agentcore-interceptor) (same guarantees via a Gateway Request Interceptor and self-managed vaulting). Go there for the per-hop reasoning, the measured 3LO findings, the permission matrices and the full OAuth dance. [Identity, in brief](#identity-in-brief) below carries only what you need to work on *this* codebase.

## The layers

| Layer | What it decides | Where |
|---|---|---|
| **Channel** | how a user reaches the agent and how answers get back | `lambda/router/`, `agent/lark_notify.py` |
| **Identity** | who this turn is, and whose credentials the tools may use | `lambda/router/cognito.py`, `agent/lark_3lo.py`, `lambda/shim/` |
| **Reasoning** | the model, the system prompt, the turn loop | `agent/agent_core.py` (Strands today; a LangGraph migration is planned — see `README.md → Roadmap`) |
| **Memory** | what the agent remembers, and for how long | AgentCore Memory (STM), thread id owned by the router |
| **Tools** | what the agent can actually do, and as whom | `mcp-servers/*` (one Runtime each), plus the Web Search Gateway |

Each layer is separable, and the seams are deliberate: adding a tool server touches only the last row, adding a channel only the first. `README.md → Extending the agent` lists what each addition actually costs.

## The whole system

```
                    ┌──────────────────────────────┐        ┌──────────────────────────┐
  Lark message ───▶ │  Router Lambda               │◀──────▶│  DynamoDB identity table │
   (webhook)        │  verify / AES-decrypt        │        │  SESSION    → runtime id │
                    │  resolve → lark:{open_id}    │        │  MEMSESSION → memory id  │
                    │  chat commands (/auth …)     │        │  ALLOW      → allowlist  │
                    └──────────────┬───────────────┘        └──────────────────────────┘
                     POST /invocations, Authorization: Bearer <user's JWT>
                     — returns "accepted" at once; carries runtimeSessionId +
                       memorySessionId + actorId + chatId
                                   ▼
        ┌────────────────────────────────────────────────────────┐
        │  Agent container (ARM64, AgentCore Runtime)            │     AgentCore Identity
        │   Strands agent, history in AgentCore Memory           │     Token Vault (3LO)
        │   lark_3lo: platform WAT → GetResourceOauth2Token      │◀───▶ stores / refreshes
        │   the turn runs in the background; /ping = HealthyBusy │     THIS user's token
        └───┬───────────────────────────────────────────┬────────┘            ▲
            │ MCP over SigV4, user's Lark token         │ answer, when ready  │ RFC-6749
            │ in X-Amzn-…-Custom-Lark-Token             │ (tenant token)      │
            ▼                                           │         ┌───────────┴───────────┐
    ┌──────────────────────────────┐                    │         │  Lark OAuth shim      │
    │  Lark MCP server (lark-cli)  │                    │         │  form ⇄ JSON,         │
    │  runs lark-cli AS the user   │                    │         │  code≠0 → 4xx         │
    └──────────────┬───────────────┘                    │         │  /return: complete +  │
         Authorization: Bearer                          │         │  DM the user          │
                   ▼                                    ▼         └───────────┬───────────┘
              Lark REST API ◀─────────────────────────────────────────────────┘
              → only what THIS user can see. Lark adjudicates.       consent
```

## Components

| Component | What it is | Where |
|---|---|---|
| Router Lambda | Lark webhook ingestion (verify + AES decrypt, resolve user, invoke runtime); mints the per-user JWT; owns both session ids; handles the chat commands | `lambda/router/` |
| Agent container | Strands agent on Bedrock; HTTP contract (8080); AgentCore Memory for continuity; agent-side 3LO; MCP clients to the lark-cli server, the approval server when deployed, and optionally web search; runs turns in the background and posts answers to the chat itself | `agent/` |
| Lark OAuth shim | RFC-6749 façade over Lark's non-standard token endpoint, plus the 3LO return endpoint | `lambda/shim/` |
| Lark MCP server | lark-cli engine on AgentCore Runtime; calls Lark with the per-user token from a custom passthrough header | `mcp-servers/lark-cli/` |
| Approval MCP server | Lark approvals on AgentCore Runtime — the case where the user's identity *cannot* be forwarded. Limits enforced in code, not by the model | `mcp-servers/approval/` |
| AgentCore Identity | Token Vault: stores, refreshes and returns each user's Lark token (`USER_FEDERATION`), one OAuth provider per downstream system | provider `lark-agent-3lo`, workload `lark-agent-wl` |
| AgentCore Memory | Per-user conversation history, keyed by `(actor_id, memory_session_id)` | `lark_agent_agent_mem` (STM) |
| Cognito user pool | Token factory: mints a standard OIDC JWT for a Lark-authenticated user (Lark is not standard OIDC) | `stacks/security_stack.py` |
| AgentCore Gateway | Fronts the built-in **Web Search** connector (us-east-1 only, so it's cross-region) | `stacks/gateway_stack.py`, `deploy.sh gateway` |

## How an answer gets back

Answers come back asynchronously. A turn that researches something and writes it into a document outlasts any request/response window — `InvokeAgentRuntime` and the router's Lambda both cap out — and being cut off mid-way is the worst case, since the work often finished while the user was told it failed. So `chat_async` accepts the turn, returns at once, and runs it on a background thread.

The reply is streamed rather than posted in one go: a CardKit card with `streaming_mode` goes out immediately as a placeholder, then the accumulated text is written into it so it types out. The write happens on the card's own thread and coalesces to the newest text — a CardKit write costs ~470 ms (measured, 361–606 ms), so doing it inline stalled the loop for longer than the interval it was throttled to, and the text arrived in jerks. Now the token loop is paced by the model (~40 chars/s measured for Sonnet 4.6) and the visible cadence by Lark's round trip, instead of the two throttling each other.

What the placeholder actually covers is session assembly, not model latency: raw Bedrock returns a first token in 1.0–1.5 s, while a first turn spends ~7 s before that — ~4 s of MCP handshakes across two servers and ~2 s loading Memory history. Subsequent turns in the same session skip the handshake (the agent and its MCP clients are cached). All of this uses the app's tenant token: it is the bot speaking. A CardKit failure (missing `cardkit:card:write`, an update rejected mid-stream) degrades to a single plain-text post.

`/ping` reports `HealthyBusy` for the duration, which is what stops AgentCore from reclaiming the container mid-turn; that defers idle reclamation (the session-inactivity timer `idleRuntimeSessionTimeout`) but not `maxLifetime` — the microVM's wall-clock age cap (default 8 h, configurable 60–28800 s) which never resets on activity, so it is the hard ceiling on one background turn. The router's async self-invocation also disables Lambda's default retries — a timeout counts as a function error there, so retries would replay the whole turn and duplicate both the work and the reply.

## Identity, in brief

The chain, in one pass: the router resolves every message to `lark:{open_id}` and mints a per-user Cognito **access** token for it (`lambda/router/cognito.py` — the only token factory; Lark is not standard OIDC, so the pool is the translation layer). The Runtime is configured `CUSTOM_JWT`, verifies that token, and hands the container a workload access token derived from it — so the agent never *names* a user. The agent exchanges that for the user's Lark token from the Token Vault (`agent/lark_3lo.py`) and passes it to the tool servers in a custom header. `mcp-servers/lark-cli/` calls Lark with it, so **Lark** decides what the tools may reach, and the agent holds no downstream credential of its own.

The property that follows: **replies go out as the app, tool calls go out as the user.** Both directions happen in the same turn, on purpose.

**Per hop.** Inbound and outbound are independent axes; conflating them is where most AgentCore auth confusion comes from.

| Direction | Hop | Credential |
|---|---|---|
| in | Lark → Router (webhook) | `X-Lark-Signature` + AES (encryptKey), verified fail-closed |
| in | Router → agent Runtime | the user's Cognito access token (Bearer), verified by `customJWTAuthorizer` |
| in | Agent → tool Runtimes | IAM SigV4 (transport only) |
| out | Agent → AgentCore Identity | the workload token the Runtime delivered |
| out | Agent → lark-cli Runtime | the user's Lark token in `X-Amzn-…-Custom-Lark-Token` |
| out | lark-cli → Lark REST | the user's `user_access_token` — **Lark adjudicates** |
| out | approval server → Lark REST | the **app's** tenant token + a `user_id` argument (see [approvals](#approvals-where-the-users-identity-cannot-be-passed-through)) |
| out | Agent → Web Search Gateway | the user's Cognito access token; Gateway → connector uses `GATEWAY_IAM_ROLE` |
| out | Agent / Router → Lark chat | the app's tenant token — it is the bot speaking |

**Five things that bite, and are load-bearing in this codebase.** The reasoning behind each is in the sibling repos; these are the ones you can break by editing this one:

1. **`CUSTOM_JWT` and SigV4 invocation are mutually exclusive** — SigV4 against this Runtime returns `AccessDeniedException: Authorization method mismatch`. The router's Bearer call and the authorizer config are one cutover, not two.
2. **The by-name token APIs are denied in IAM, deliberately.** `stacks/agentcore_stack.py` puts an explicit `Deny` on `GetWorkloadAccessTokenForUserId` *and* `ForJWT` (holding any user's JWT would otherwise be enough to exchange for their vaulted token). Not calling them is not the constraint; the Deny is.
3. **`ForUserId` and JWT-derived vault keys are separate namespaces.** Cognito's `sub` is a UUID while the username is `lark:{open_id}`, so consents do not transfer between them and nothing errors — the only symptom is users being asked to consent again. Consequently `CompleteResourceTokenAuth` must name the user with `userIdentifier={"userToken": …}`; the string form fails as `Invalid or expired session`, which reads like expiry and is a namespace mismatch.
4. **A vaulted token is verified against its actor at point of use.** Consent binds a token to whatever userId the return-url was told, not to the account that signed in — so a forwarded consent link would vault someone else's grant under your name. `lark_3lo._belongs_to` resolves each token's real owner and fails closed.
5. **The workload token arrives in three header aliases** (`x-amzn-bedrock-agentcore-runtime-workload-accesstoken`, `x-amz-bedrock-agentcore-identity-wat`, `workloadaccesstoken`), same value. `agent/server.py` reads them in that order — don't assume one name.

**What this does and does not buy.** It closes credential theft and impersonation: the agent cannot act as a user it wasn't handed. It does **not** stop a prompt-injected agent from misusing the tools it legitimately has, within that user's own permissions — that needs action-layer limits in code, which is what the approval server demonstrates. And in an event-driven turn nobody is present, so the router signs a JWT for an absent person: trust is relocated to a small component that runs no model, not eliminated.

## Consent, in this repo

First use has no vaulted token. What happens then is the one identity flow worth keeping here, because two components race to finish it:

```
turn arrives, user has never consented
   ↓  router parks the message (DynamoDB PENDING_AUTH) before dispatch
   ↓  agent reaches a Lark tool → aborts the turn at the tool call, returns needs_auth + auth_url
   ↓  router posts 点击授权, then polls the vault ≤ AUTH_WAIT_SECONDS (45 s)
user consents in the browser  →  AgentCore Identity → shim /return
   ↓  shim asks the router to complete the consent (only the router mints user JWTs)
   ↓  shim pokes the router's resume path        ┐ these two race
   ↓  the router's poll loop also sees the token ┘
   ↓  whoever claims PENDING_AUTH first (atomic take) replays the message with freshSession
the user gets their answer without re-sending
```

Three details that are easy to undo by accident. The claim is **atomic** (`identity.take_pending_auth`) precisely because both paths can win — turning it into a get-then-put duplicates the turn, in two different sessions, with side effects twice. `freshSession` is required on the replay, or the cached unauthorized session is reused and the turn walls again. And the turn is aborted at the *tool call* rather than after the model narrates the refusal, which is why the abort hook in the stream loop matters (`_iter_deltas`, `current_tool_use`).

The full OAuth leg — AgentCore `/authorize`, the shim's form⇄JSON translation, Lark's `code:"0"` envelope, PKCE, the single-use `request_uri`, the colon-free `customState` — is the sibling repos' subject; `docs/native-3lo-builtin-vendor.md` carries the short version needed to add another provider.

## Two tool paths, and why

| | Lark tools | Web search |
|---|---|---|
| Needs the end user's identity | yes — it reads *their* documents | no — it queries Amazon's web index |
| How the agent reaches it | direct to the lark-cli Runtime (SigV4 + the user's token in a custom header) | through an AgentCore Gateway (MCP) |
| Outbound credential | the user's vaulted Lark token | `GATEWAY_IAM_ROLE` |

The split is forced by where the tool server runs, not by a missing Gateway feature: **a Runtime-hosted MCP server cannot be handed a per-user token by the Gateway**, because `/invocations` owns the `Authorization` header for its own transport auth. So the agent fetches tokens itself. Moving a tool server onto an addressable HTTPS endpoint (ALB / API Gateway / Fargate) would make the managed Gateway path applicable, at the cost of Runtime's session and scaling model. Measured evidence and the gateway-role permissions this depends on: `docs/agentcore-behavior.md`.

Web search needs none of it — with no user identity to forward, `GATEWAY_IAM_ROLE` is enough. The connector is only offered in **us-east-1**, so its gateway lives there even when the rest of the stack doesn't. Two IAM actions are required: `InvokeGateway` on the gateway, and `InvokeWebSearch` on `…:aws:tool/web-search.v1`, whose account segment is the literal `aws`, not yours. `WEB_SEARCH=false` skips it entirely and the agent runs without the tool.

## Approvals: where the user's identity cannot be passed through

Everything above rests on the downstream call carrying the user's own token. Lark's approval API breaks that, and this demo exists to show what an agent must do when a downstream API refuses an end-user identity — a situation that recurs well beyond Lark.

`tasks/approve`, `reject`, `transfer`, `rollback` and `tasks/query` accept **only a tenant (app) token**. A decision therefore cannot be made *as* the user: it is made by the app, with a `user_id` argument saying whose name to record it under. Lark verifies that `user_id` owns the task, but never that the person agreed — so the record means "an authorised app claims to have decided for X", and cannot be distinguished from "X decided". That is why every automated decision posts an `[AI 自动处理]` comment: it is the only thing an audit can key on. (`add_sign` is the one endpoint taking a user token, and it is unreachable here — Lark offers no user-token *write* scope for approvals.)

**The guards, in code rather than in the prompt** (`mcp-servers/approval/server.js`, tested in `test_guards.mjs`):

- An **allow-list of approval definitions** (`AGENT_DECIDE_APPROVAL_CODES`) and an **amount ceiling** (`AGENT_DECIDE_MAX_AMOUNT`). Empty allow-list decides nothing — fail closed, so the demo is inert until switched on. `0` is a kill switch.
- **The approver's own grant must be on record.** "A token was passed" and "this approver consented" are different questions: the token is resolved to its owner and compared with `user_id`. Mismatch refuses; an owner that can't be established refuses too.

Both are **self-imposed** — Lark would permit every decision they block — and neither survives a leaked `appSecret`, which bypasses this server entirely. The honest description is that they raise the bar from "knows a task_id" to "has compromised the app". A production design would split messaging and approvals into two Lark apps so the agent holds only the former's secret.

**Event-driven: a turn with nobody present.** This is also the general shape of any non-human trigger on this architecture — the router has to synthesize both the identity and the delivery address.

```
approval_task (PENDING, carries open_id + task_id + instance_code)
   ↓  router: three gates, then hand over the address
   │    status must be PENDING      — a settled status is the agent's own decision echoing back
   │    claim the task_id           — conditional put; Lark redelivers until acked, and the
   │                                  ack goes out long before the agent has decided
   │    approver must be allowlisted — otherwise silence; their approval is their own business
   ↓  invoke_agent(chat_async, chatId=open_id)   ← returns at once, address only
agent: reads the instance, applies the guards, decides, and posts its own reply
   ↓  StreamingCard(open_id) → im/v1/messages
the approver's DM
```

Two things that are easy to get wrong. **The router does not deliver the answer** — it only resolves the address; the agent posts the card itself, because the reply streams and a Lambda cannot stay alive for it. And **an approval event carries no chat**, so the address is a person, not a room (both senders read an `ou_` prefix as "DM this person"). If the approver never consented the turn is guaranteed to wall, so it is parked before dispatch and replayed by consent-resume — an unattended turn has no user to re-send it.

Delivery is scoped by subscription: Lark sends approval events only for definitions subscribed through `approvals/{code}/subscribe`, a **separate step from ticking the event in the console** (`./deploy.sh approvals`). Two naming inconsistencies cost an afternoon each: the event calls it `approval_code` while `tasks/query` returns `definition_code`, and `tasks/query` is a **GET** (POST answers `404 page not found`, which reads like a permissions problem).

## Conversation memory

The agent is a Strands agent with an `AgentCoreMemorySessionManager` (STM) keyed by `(actor_id, memory_session_id)`. History lives in AgentCore Memory (30-day retention), so it outlives the microVM: a fresh container still reads the same thread. Per-session `(agent, MCP client)` are cached and reused across messages — rebuilding per message re-handshakes MCP and re-lists tools, ~15–20s of avoidable latency.

### Two session ids, deliberately separate

| Id | Decides | Owned by | Stored |
|---|---|---|---|
| **runtime session id** | which microVM serves the request (AgentCore binds them 1:1, but the binding is not permanent — the microVM is replaced on idle/lifetime limits while the id lives on) | router | DynamoDB `USER#{id} / SESSION` |
| **memory session id** | which conversation thread the agent reads and appends to | router, passed to the agent as `memorySessionId` | DynamoDB `USER#{id} / MEMSESSION` |

Keeping them apart is what makes the chat commands possible — rotating an id is instant and non-destructive, so each command just switches ids rather than deleting anything:

- `/reset` → new memory thread, same instance (start over, old events kept)
- `/reconnect` → new instance, same memory thread (proves memory outlives the container)
- `/new` → both (a genuinely fresh start)
- `/clear` → deletes the current thread's events (the only destructive one)

Earlier the agent derived the memory id from `actor_id` itself and ignored what the router sent, which welded the two dimensions together: switching instances could never start a new thread, and the router's own reads of the thread always missed. Message counts come from `ListEvents` filtered to `conversational` payloads — Strands also writes session/agent state events, which would otherwise inflate the number.

Authorization is a third, orthogonal dimension: the vaulted Lark token is keyed to `lark:{open_id}`, not to either session, so rotating sessions never forces a re-consent.

## Deploy shape

CDK stacks: security, agentcore, router, shim, gateway, observability. The tool path is agent-side 3LO, so the gateway stack is reduced to its service role (no mcpServer target); the shim stack is what the 3LO flow actually uses. Everything AgentCore-side is created outside CloudFormation: the **Runtimes** (agent, lark-cli MCP server, and the approval MCP server when deployed), Memory, the OAuth2 credential provider, the workload identity, and the Web Search gateway. `deploy.sh` builds them — ARM64 images via CodeBuild, resources via the AgentCore CLI / control-plane — and feeds ids back through `.cdk-state.json`.

`AWS::BedrockAgentCore::*` types do now exist, so this is a choice rather than a limitation: the agent Runtime and Memory are created implicitly by `agentcore deploy`, which also builds the image, and replacing that official tool to move them into a stack costs more than it returns. Two consequences worth knowing: `destroy.sh` needs an explicit delete for each of these (nothing errors if one is missed — only a real teardown catches it), and the ordering `3lo`/`gateway` → `runtime` has to be maintained by hand, since the Runtime bakes in the provider name and gateway URL. See `README.md` for the deploy commands and Lark console setup.
