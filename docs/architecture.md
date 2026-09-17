# Architecture

A general-purpose agent on Amazon Bedrock AgentCore, integrated with **Lark (Feishu)** as its interaction channel. This document covers how the pieces fit: how a message becomes a turn, how a turn's answer gets back to the user, what the agent remembers, and how its tools are reached.

> **The Lark identity integration is documented in depth elsewhere, not here.** Two samples cover exactly that subject — [sample-lark-identity-on-agentcore-native](https://github.com/aws-samples/sample-lark-identity-on-agentcore-native) (the native Token Vault path this repo uses) and [sample-lark-identity-on-agentcore-interceptor](https://github.com/aws-samples/sample-lark-identity-on-agentcore-interceptor) (same guarantees via a Gateway Request Interceptor and self-managed vaulting). Go there for the per-hop reasoning, the measured 3LO findings, the permission matrices and the full OAuth dance. [Identity, in brief](#identity-in-brief) below carries only what you need to work on *this* codebase.

The ten layers this is built from, and what each is implemented with, are in [README.md → What we build](../README.md#what-we-build). This document is the other half: how a message becomes a turn, what each hop is authorised with, and why the awkward parts are shaped the way they are.

## The whole system

```
                    ┌──────────────────────────────┐        ┌──────────────────────────┐
  Lark message ───▶ │  Router Lambda               │◀──────▶│  DynamoDB identity table │
   (webhook)        │  verify / AES-decrypt        │        │  SESSION    → runtime id │
                    │  resolve → lark:{open_id}    │        │  MEMSESSION → thread id  │
                    │  chat commands (/auth …)     │        │  ALLOW      → allowlist  │
                    └──────────────┬───────────────┘        └──────────────────────────┘
                     POST /invocations, Authorization: Bearer <user's JWT>
                     — returns "accepted" at once; carries runtimeSessionId +
                       memorySessionId + actorId + chatId
                                   ▼
        ┌────────────────────────────────────────────────────────┐
        │  Agent container (ARM64, AgentCore Runtime)            │     AgentCore Identity
        │   LangGraph agent, conversation checkpointed to DynamoDB│     Token Vault (3LO)
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
| Agent container | LangGraph agent on Bedrock; HTTP contract (8080); DynamoDB checkpoints for continuity; agent-side 3LO; MCP sessions to the lark-cli server, the approval server when deployed, and optionally web search; runs turns in the background and posts answers to the chat itself | `agent/` |
| Lark OAuth shim | RFC-6749 façade over Lark's non-standard token endpoint, plus the 3LO return endpoint | `lambda/shim/` |
| Lark MCP server | lark-cli engine on AgentCore Runtime; calls Lark with the per-user token from a custom passthrough header | `mcp-servers/lark-cli/` |
| Approval MCP server | Lark approvals on AgentCore Runtime — the case where the user's identity *cannot* be forwarded. Limits enforced in code, not by the model | `mcp-servers/approval/` |
| AgentCore Identity | Token Vault: stores, refreshes and returns each user's Lark token (`USER_FEDERATION`), one OAuth provider per downstream system | provider `agentcore-fullstack-3lo`, workload `agentcore-fullstack-wl` |
| Checkpoint table | Per-user conversation state, partition key derived from `thread_id` | `agentcore-fullstack-checkpoints` (+ an S3 bucket for state over ~350 KB) |
| Cognito user pool | Token factory: mints a standard OIDC JWT for a Lark-authenticated user (Lark is not standard OIDC) | `stacks/security_stack.py` |
| AgentCore Gateway | Fronts the built-in **Web Search** connector (us-east-1 only, so it's cross-region) | `stacks/gateway_stack.py`, `deploy.sh gateway` |

## How an answer gets back

Answers come back asynchronously. A turn that researches something and writes it into a document outlasts any request/response window — `InvokeAgentRuntime` and the router's Lambda both cap out — and being cut off mid-way is the worst case, since the work often finished while the user was told it failed. So `chat_async` accepts the turn, returns at once, and runs it on a background thread.

The reply is streamed rather than posted in one go: a CardKit card with `streaming_mode` goes out immediately as a placeholder, then the accumulated text is written into it so it types out. The write happens on the card's own thread and coalesces to the newest text — a CardKit write costs ~470 ms (measured, 361–606 ms), so doing it inline stalled the loop for longer than the interval it was throttled to, and the text arrived in jerks. Now the token loop is paced by the model (~40 chars/s measured for Sonnet 4.6) and the visible cadence by Lark's round trip, instead of the two throttling each other.

What the placeholder actually covers is session assembly, not model latency: raw Bedrock returns a first token in 1.0–1.5 s, while a first turn spends ~7 s before that — ~4 s of MCP handshakes across two servers and ~2 s loading the checkpoint. Subsequent turns in the same session skip the handshake (the agent and its MCP clients are cached). All of this uses the app's tenant token: it is the bot speaking. A CardKit failure (missing `cardkit:card:write`, an update rejected mid-stream) degrades to a single plain-text post.

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

## Code execution, and the workspace it writes to

Optional (`FILES_STORAGE=true`) and off by default. The Runtime is stateless in the way that matters: a session's filesystem dies with its microVM, and `maxLifetime` ends that microVM within 8 h regardless of activity. Conversation state is covered by checkpoints; **bytes** are what this adds — and the thing that consumes them is generated code.

```
agent Runtime (PUBLIC, no VPC)
  │  StartCodeInterpreterSession(filesystemConfigurations=[ this user's Access Point ])
  ▼
Code Interpreter session — own microVM, in the storage VPC
  │  /mnt/workspace  ← S3 Files, Access Point rooted at /users/lark_<open_id>
  ▼  executeCode / executeCommand
S3 (write-through, asynchronous)
```

**Only the sandbox is in the VPC, and there is no NAT.** A mount is NFS, so it needs a mount target, so something must be inside a VPC — but that something is the sandbox, not the Runtime. The sandbox only has to reach S3, which a **free gateway endpoint** does. The Runtime stays PUBLIC and keeps reaching Bedrock, Lark and a cross-region Gateway directly; putting *it* in the VPC is what used to make a NAT unavoidable.

**Isolation is the platform's boundary, not our code's.** `filesystemConfigurations` is accepted per **session**, so one shared Code Interpreter serves every user while each session mounts only that user's Access Point — measured: a session mounting user B's Access Point sees an empty directory while user A's files exist, and the Access Point's root is the top of the visible tree. The Access Point's `rootDirectory` is fixed server-side to `/users/lark_<open_id>`, and the file system's own resource policy refuses any mount that names no Access Point. The agent's execution role has no S3 permission on the bucket at all.

What that leaves as the one thing to get right is **which Access Point a session mounts** — a bug there is a cross-user leak, and no amount of platform isolation helps. It is the same trust shape as the broker this replaced, with far fewer moving parts: no signed ticket, no vended credentials, no `credential_process`, no watchdog.

**What the model sees is a working directory, not a mount.** Three tools — `run_code`, `run_command`, `list_files` — and every call is prefixed with a `cd` into the workspace, because the sandbox's own working directory is elsewhere and anything written there dies with the session. The tools take no path outside the workspace and no actor id: which Access Point gets mounted is decided from the turn's verified identity, never from an argument. The session starts lazily, on the first tool call rather than while the tool list is built, and is stopped when the cached session is discarded — a code session left running is billed until it times out.

**Known edges.** `mountPath` must match `/mnt/[a-zA-Z0-9._-]+/?`, so the path is `/mnt/workspace` rather than a friendlier `/workspace`. Write-through to S3 is asynchronous (~40 s measured for a small file), so reading an artifact out of the bucket needs a retry while reading it back through the sandbox does not. `readFiles`/`writeFiles` are scoped to the sandbox's own workspace and cannot touch the mount — use `executeCommand`/`executeCode`. Access Points are limited (EFS allows 1000 per file system; s3files assumed similar, unconfirmed), which bounds the user count. `/clear` deletes conversation state, never files. Measured details in `docs/agentcore-behavior.md`, decisions in `.dev/adr/0007`.

## Conversation memory

The conversation is LangGraph graph state in a DynamoDB table, addressed by `thread_id` alone — and `thread_id` is derived from the user, never from the container. So a turn cut off at the 15-minute invoke cap resumes in a brand-new microVM. Checkpoints are written **per superstep**, so the durable record advances *during* a turn: a microVM gets no shutdown hook, and any design that flushes at the end loses the turn that was interrupted. The compiled graph and its MCP sessions are cached per session and reused across messages — rebuilding per message re-handshakes every MCP server and re-lists tools, ~15–20s of avoidable latency.

**Two backends, two jobs** (see `.dev/adr/0008` for why each):

| | Holds | Read by |
|---|---|---|
| `DynamoDBSaver` (checkpointer) | full graph state — messages with their tool calls and results, channels, pending writes | the agent, to resume a thread; `/status` and `/clear` via the agent |
| AgentCore Memory (`AgentCoreMemoryStore`) | long-term records only | long-term recall, when a Memory resource exists |

Not `AgentCoreMemorySaver` for the checkpoint: fidelity is identical (both serialize through `JsonPlusSerializer` and produce the same `CheckpointTuple`), but it carries a mandatory 3–365 day event expiry, bills per write, and cannot be reached at all unless the Memory resource exists. DynamoDB sets its own TTL, needs no extra resource, and spills state over ~350 KB to S3 so one unbounded thread per user cannot hit the 400 KB item cap.

A turn killed between "the model emitted `tool_calls`" and "the tool returned" leaves a trailing `AIMessage` whose calls have no matching `ToolMessage`, which Bedrock then rejects on every later request — history intact but unreachable. The agent repairs that on session build and after a failed turn, appending a synthetic result for each unanswered call. Only the trailing message is repaired: a `toolResult` has to follow its `toolUse`, so appending cannot fix a dangling call buried deeper in the history.

### Prompt caching

Bedrock's prompt cache is explicit: with no `cachePoint` block in the request, nothing is cached. `agent_core._CachingChatBedrockConverse` injects `cache_control` into every request, and langchain-aws then places the checkpoints where AWS documents them — after the system prompt, after the tool definitions, and a rolling pair at the end of the message list, within Bedrock's limit of four.

Injection has to happen at the request: `model_kwargs` is rerouted to `additional_model_request_fields`, and `.bind(cache_control=…)` is dropped when `create_agent` calls `bind_tools` (both measured against langchain-aws 1.7).

The TTL is 1h rather than the 5m default, matching `_SESSION_TTL` so the cached prefix lives as long as the session reusing it. A 1h write costs 2.0x base input against 1.25x for 5m, while reads are 0.1x either way, so caching wins once reads exceed ~1.11x writes. Measured on a three-turn session: 7,064 cache-read tokens against 3,840 written, a ratio of 1.84 — already past break-even, and the ratio improves as a conversation lengthens.

What to know before tuning: Sonnet 5's 1,024-token minimum per checkpoint is cumulative over tools + system + messages **in that order**. Measured against the deployed servers, the tool definitions are ~1,618 tokens with the approval server (the approval tools alone are ~1,331) and ~400 without it, and the system prompt is ~32 — so on a minimal deployment the tools checkpoint earns nothing until the conversation itself grows past the minimum. `PROMPT_CACHE_TTL=""` disables caching, which matters because a checkpoint that always misses still costs the write premium.

### Two session ids, deliberately separate

| Id | Decides | Owned by | Stored |
|---|---|---|---|
| **runtime session id** | which microVM serves the request (AgentCore binds them 1:1, but the binding is not permanent — the microVM is replaced on idle/lifetime limits while the id lives on) | router | DynamoDB `USER#{id} / SESSION` |
| **checkpoint thread id** | which conversation thread the agent reads and appends to (still sent as `memorySessionId`) | router, passed to the agent as `memorySessionId` | DynamoDB `USER#{id} / MEMSESSION` |

Keeping them apart is what makes the chat commands possible — rotating an id is instant and non-destructive, so each command just switches ids rather than deleting anything:

- `/reset` → new thread, same instance (start over, old state kept)
- `/reconnect` → new instance, same thread (proves the conversation outlives the container)
- `/new` → both (a genuinely fresh start)
- `/clear` → deletes the current thread's checkpoints (the only destructive one)

Earlier the agent derived the thread id from `actor_id` itself and ignored what the router sent, which welded the two dimensions together: switching instances could never start a new thread, and the router's own reads of the thread always missed.

`/status`'s counts and `/clear` are answered by the **agent**, not the router (`history_stats` / `clear_history` actions). Reading or deleting the conversation now means decoding a checkpoint, and `agent/agent_core.py` is the only module allowed to know about the framework — so the router stayed a channel adapter instead of gaining the whole LangGraph stack, and dropped its Memory permissions with it.

`/status` reports **turns**, not messages: a turn is an `AIMessage` with no pending `tool_calls` — an answer actually delivered. A raw message count is not a conversation length (one exchange can be 5+ messages, and summarization rewrites the list), and the tool-calling `AIMessage` is work in progress rather than a turn. Tool calls are counted alongside rather than folded in, because a plausible-looking answer is not evidence a tool ran. The model it reports comes from the container serving the request, not from the router's config: `UpdateAgentRuntime` replaces the environment wholesale, so the two can disagree and only the container's answer is true.

Authorization is a third, orthogonal dimension: the vaulted Lark token is keyed to `lark:{open_id}`, not to either session, so rotating sessions never forces a re-consent.

## The web entrypoint, over AG-UI

The page is one static file on CloudFront and reaches the agent in two hops, split deliberately:

```
  browser (inside Lark)            router Lambda                 AgentCore Runtime
        │  tt.requestAuthCode           │                              │
        │──── POST /web/session ───────▶│  code → open_id (authen v2)  │
        │◀──── {token, actorId} ────────│  mints the user's JWT        │
        │                                                              │
        │──── POST /invocations  (Bearer JWT, AG-UI RunAgentInput) ────▶│
        │◀──── text/event-stream: canonical AG-UI events ──────────────│
```

The first hop exists so identity is **established, not claimed**: the h5 code is single-use and issued by Lark to this app inside the Lark client, so a browser can prove who it is without being allowed to say who it is. The router remains the only component that mints user JWTs. The second hop has no backend at all — the Runtime's endpoint answers CORS itself and streams SSE straight to the tab.

`serverProtocol` accepts `AGUI`, but a plain `HTTP` runtime already streams SSE (measured), so AG-UI needs no second Runtime — `agent/agui.py` is a route on the existing one, dispatched before the chat path and sharing the same session build, thread and vaulted token. The page's `actorId` and `threadId` are therefore claims: `threadId` is overwritten from the session's `mem_sid`, and the actor is checked against the vaulted token's real owner.

What AG-UI buys over an ad-hoc frame format is the event vocabulary. The page renders the tool lifecycle (`TOOL_CALL_START` → `ARGS` → `RESULT` → `END`) as one card per `toolCallId`, and the **result** line is the point: it is the only thing distinguishing a tool that ran from a model narrating one. `THINKING_*`/`REASONING_*` are handled but never fire — this deployment does not enable extended thinking.

Model output is markdown, re-rendered from the accumulated source on each delta (parsing a partial stream incrementally yields broken trees mid-token) and sanitised on the way in — a tool result is untrusted input that reaches the page through the model. Images go the other way as `ImageInputContent` with an inline base64 source, which `ag-ui-langgraph` converts to LangChain content blocks; inlining avoids a storage bucket and a second credential, at the cost of a 4 MB cap matching the Lark path.

A `/command` does **not** go to the Runtime. `run_command` in the router returns text instead of sending it, so one implementation answers both surfaces; the page posts to `/web/command` with a fresh h5 code. They belong to the router because they rotate session ids it owns and read the identity table, which the agent cannot reach by design. An auth link travels as data (`{"link": {"text", "url"}}`) so Lark can render a rich-text post and the page can render markdown.

Lark-side setup has three separate fields with three different matching rules (see README step 5); the one that costs an afternoon is Redirect URLs, which is an exact page URL and needs the trailing slash.

## A2A: delegation that has to be attributable

What this agent has that a peer does not is the identity chain — it can act in Lark as the calling human, with that person's own vaulted token, and Lark adjudicates. So the capability worth exposing over A2A is not reasoning; it is "do this as that user".

It runs no turn of its own. A vaulted consent is scoped to the Runtime that obtained it — measured: the same user, same workload, same provider and scopes, seen from a second Runtime, reads as "never consented". So `agent/a2a_server.py` is a protocol adapter: it captures the caller's bearer, forwards it to the agent Runtime's `chat` action along with the claimed `actorId`, and returns the reply. A second Runtime is unavoidable here (unlike AG-UI) because the A2A contract binds port 9000 at the root while the HTTP contract is 8080 under `/invocations` — same image, `entrypoint.sh` branches on `SERVER_MODE`.

This keeps the trust boundary honest rather than widening it: A2A carries no end-user identity of its own, so a caller wanting us to act as someone must already hold that person's token — and a caller able to do that is already trusted to speak for them. A request with no bearer is refused with an explanation, not served anonymously (though through AgentCore the authorizer rejects it first, so that branch only guards a direct-to-container call).

`scripts/a2a-demo.sh` drives all of it as a peer would, and its third step is the one that matters: the same bearer naming a *different* actor does not return that person's data — it returns a consent prompt, because the agent checks the claim against the vaulted token's owner. Step two ends by reading the MCP server's own log for `tools/call token=yes`, since a plausible answer is not evidence that a tool ran as anyone. The Agent Card sits behind the Runtime's authorizer, which means a peer must already be a known identity in this tenant before it can even discover the agent — see docs/agentcore-behavior.md.

## Deploy shape

CDK stacks: security, agentcore, router, shim, gateway, observability, plus storage when `FILES_STORAGE=true` (that one carries the VPC, so it is off by default) and webui when `WEBUI=true`. The webui stack owns no compute — a bucket, a distribution, and the two values injected into `config.js`. Its domain only exists after deploy and the router needs it for CORS, so `phase_webui` writes it to `.cdk-state.json` and re-deploys the router; routing it through state rather than a CloudFormation export is deliberate, since importing the router's URL would freeze that export. The tool path is agent-side 3LO, so the gateway stack is reduced to its service role (no mcpServer target); the shim stack is what the 3LO flow actually uses. Everything AgentCore-side is created outside CloudFormation: the **Runtimes** (agent, lark-cli MCP server, and the approval MCP server when deployed), Memory, the OAuth2 credential provider, the workload identity, and the Web Search gateway. `deploy.sh` builds them — ARM64 images via CodeBuild, resources via the AgentCore CLI / control-plane — and feeds ids back through `.cdk-state.json`.

`AWS::BedrockAgentCore::*` types do now exist, so this is a choice rather than a limitation: the agent Runtime and Memory are created implicitly by `agentcore deploy`, which also builds the image, and replacing that official tool to move them into a stack costs more than it returns. Two consequences worth knowing: `destroy.sh` needs an explicit delete for each of these (nothing errors if one is missed — only a real teardown catches it), and the ordering `3lo`/`gateway` → `runtime` has to be maintained by hand, since the Runtime bakes in the provider name and gateway URL. See `README.md` for the deploy commands and Lark console setup.
