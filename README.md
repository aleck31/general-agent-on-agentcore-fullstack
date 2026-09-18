# General Agent on AgentCore — a full-stack agent sample

A general-purpose agent on **Amazon Bedrock AgentCore**, with **Lark (Feishu) as its interaction channel**. The agent runs on AgentCore Runtime (LangGraph + Bedrock), checkpoints conversation state to DynamoDB, reaches its tools over MCP, and answers in Lark chat — typed into a streaming card as it is produced, because a real task outlasts any request/response window.

Every message resolves to `lark:{open_id}`, and any tool touching a user's data acts **as that user**, with their own vaulted Lark token (3LO through AgentCore Identity). So Lark adjudicates access and there is no parallel permission layer to keep in sync. That identity chain is documented in depth by two sibling samples rather than here: [native](https://github.com/aws-samples/sample-lark-identity-on-agentcore-native) (the Token Vault path this repo uses) and [interceptor](https://github.com/aws-samples/sample-lark-identity-on-agentcore-interceptor) (same guarantees, self-managed vaulting).

## What we build

Ten layers, each with a real implementation rather than a stub — that is the "full-stack" in the name. The interesting part is not any single layer but the seams between them, which is where this repo's design decisions and the measured findings in [docs/agentcore-behavior.md](docs/agentcore-behavior.md) came from.

| Layer | What it decides | How it's built |
|---|---|---|
| **Channel** | how a turn is started, and how the answer gets back | Lark webhook → router Lambda → an answer typed into a streaming CardKit card. Two more surfaces reach the same agent — a page inside Lark (AG-UI over SSE) and A2A for peer agents — and an approval event can start a turn with nobody present at all |
| **Model** | which model answers, and what it costs per turn | **Amazon Bedrock** — Claude Sonnet over the Converse API, with prompt caching on every request (a `cachePoint` is explicit) |
| **Agent loop** | the prompt, and how tool calls iterate | LangGraph `create_agent`, plus threshold-triggered summarisation to bound a thread that is permanent per user |
| **Identity** | who this turn is, and whose credentials its tools may use | every message resolves to `lark:{open_id}`; Cognito mints the signed identity, **AgentCore Identity** vaults that person's Lark grant (3LO), and tools act as them — Lark adjudicates, we add nothing |
| **Session state** | what the agent remembers *within* a conversation | DynamoDB checkpoints — full graph state keyed by the user, written per superstep, so it outlives the microVM and a turn cut off at the 15-minute cap resumes |
| **Long-term memory** | what it remembers *across* conversations | **AgentCore Memory**, reached through `remember`/`recall` — written only when the model decides a fact should outlast the chat, because retrieval is the priced operation |
| **Persistent storage** | what survives the container, and whose it is | **S3 Files** in your own bucket, one Access Point per user, mounted into the sandbox at `/mnt/workspace` — outlives sessions, deploys and image updates, and is isolated at the microVM boundary rather than by agent code |
| **Tools** | what it can do, and as whom | **MCP servers** on their own Runtimes — `lark-cli` as the user, approvals on the app identity; **AgentCore Gateway** for Web Search; **AgentCore Code Interpreter** for generated code |
| **Observability** | what it did, and on which compute | **AgentCore Observability** — OTel traces and container logs in CloudWatch; chat commands report the serving microVM and the model it is really running |
| **Operations** | how it gets deployed and torn down | CDK for what CloudFormation covers, control-plane calls for the rest, both ordered by one `./deploy.sh` |

Each layer is separable and the seams are deliberate: adding a tool server touches one row, adding a channel another. Two AgentCore primitives are **not** used: Browser (nothing here needs a headless one) and Evaluations (`CreateEvaluator` / online evaluation configs) — this repo verifies behaviour by measuring a real deployment, and has no automated quality harness. Nor is it production: single-tenant, no CI, removal policies that destroy data. Those trade-offs are stated where they are made, see [Notes & limitations](#notes--limitations).

## Architecture

```
                                        ┌──────────── AgentCore Identity ────────────┐
                                        │  Token Vault: stores / refreshes / returns │
                                        │  THIS user's Lark token (3LO)              │
                                        └──────────────────┬─────────────────────────┘
  Lark bot chat ──webhook──▶ Router Lambda                 │ the user's own token
  Lark h5 page  ──h5 code──▶ (mints the JWT)               │
  Peer agent    ──bearer───▶ A2A Runtime                   ▼
                                  └──────▶  Agent (AgentCore Runtime)  ──▶  Lark MCP server
                                            LangGraph + Bedrock             acts AS the user
                                            DynamoDB checkpoints                    │
                                              │          │                          ▼
   the answer, when the turn finishes  ◀──────┘          │                   Lark REST API
   (chat card · SSE · A2A reply)                         │            returns only what THIS
                                                         ▼            user can see
                                          Code Interpreter session
                                          /mnt/workspace ← this user's S3 Files
```

Three surfaces, one agent, one thread, one vaulted token per person. The `actorId` a page or a peer sends is a **claim**: the agent checks it against the vaulted token's real owner before acting, so naming somebody else yields a consent prompt rather than their data.

Two behaviours worth knowing before reading the code. **Delivery is asynchronous** — the agent accepts the turn, returns at once, and posts the answer when it is ready, because a real task outlasts any request/response window and cutting one off is worse than waiting. **Consent is self-healing** — with no vaulted token the router posts a 点击授权 link, waits, and replays the original message once the grant lands, so the user never re-sends.

Four layers ship switched off, because each adds cost or console work: web search (`WEB_SEARCH`), the web page (`WEBUI`), A2A (`A2A`), and code execution with its storage (`FILES_STORAGE`). The agent runs without any of them.

See **[docs/architecture.md](docs/architecture.md)** for the full flow, per-hop auth, and the consent-wait sequence; **[docs/agentcore-behavior.md](docs/agentcore-behavior.md)** for measured AgentCore Runtime/Gateway behavior (read this before debugging anything platform-level); **[docs/native-3lo-builtin-vendor.md](docs/native-3lo-builtin-vendor.md)** for the reusable recipe to give the agent access to *another* downstream system.

## Layout

| Path | What |
|---|---|
| `app.py`, `cdk.json` | CDK app (uv-managed deps). Deployment state goes to `.cdk-state.json`, not here |
| `.env` | Deployment target (`PROFILE`/`REGION`/`MODEL_ID`) + Lark credentials — gitignored, read by every script |
| `stacks/` | security, agentcore, router, shim, gateway, observability, plus storage and webui when their flags are on |
| `agent/` | the agent container: HTTP contract + DynamoDB checkpoints + per-user 3LO (`lark_3lo`) + MCP clients for the lark-cli server and, optionally, web search (`websearch`); runs turns in the background and streams answers into a card (`lark_notify`). `code_tools.py` runs generated code in a per-user sandbox; `agui.py` serves the browser off the same Runtime; `a2a_server.py` is a second entrypoint on the same image (`SERVER_MODE=a2a`) |
| `webui/` | the web chat page — one static file, no build step; deploy-time values arrive as `config.js` |
| `lambda/router/` | Lark webhook: verify/decrypt/tenant-token/send + 3LO consent-wait + the chat commands. Also the only component that mints per-user JWTs (`cognito.py`) |
| `lambda/shim/` | Lark OAuth RFC-6749 façade + 3LO return endpoint (`CompleteResourceTokenAuth`, then DMs the user) |
| `mcp-servers/` | One directory per MCP server, one Runtime each: `lark-cli/` acts as the user against Lark, `approval/` runs approval decisions on the app identity. Each declares its own build/runtime config in `runtime.env`, so adding a server needs no script change |
| `deploy.sh` | the deploy entry point — orders the steps in `scripts/` |
| `scripts/` | step implementations: preflight / provision (base/memory/code/runtime/gateway/a2a/webui) / build-mcp / setup-3lo / setup-lark / subscribe-approvals / manage-allowlist / destroy |
| `tests/` | `run.sh` (all unit suites) + e2e smoke tests that need a deployed stack |
| `probe/` | a measurement MCP server, not part of the deployed agent — how the findings in `docs/agentcore-behavior.md` were established |

## Deploy

Prereqs: `uv`, Docker, the AgentCore CLI (`npm i -g @aws/agentcore`), and AWS credentials. The deployment target lives in `.env` (`PROFILE`, `REGION`, `MODEL_ID`); command-line env vars override it (`REGION=... ./deploy.sh`). Resources deploy under the `agentcore-fullstack` prefix. The first deploy into a region runs `cdk bootstrap` automatically.

```bash
cp .env.example .env          # deployment target (PROFILE/REGION/MODEL_ID) + Lark appId/appSecret/encryptKey/token + your open_id
./deploy.sh                   # everything, in order — ends by printing the two URLs to register in Lark
```

That's the whole deploy. It runs the steps in dependency order and nothing stops to ask you anything: the two values you must paste into the Lark console don't block the deploy, they only gate the bot at runtime, so they're printed together at the end (`./deploy.sh urls` reprints them).

Individual steps, for iterating — each is idempotent, so re-running any of them is safe:

| Step | What |
|---|---|
| `./deploy.sh base` | CDK stacks (security, agentcore, router, shim, gateway, observability, plus storage when `FILES_STORAGE=true`) |
| `./deploy.sh code` | the Code Interpreter generated code runs in — skipped unless `FILES_STORAGE=true` |
| `./deploy.sh webui` | the web chat page on S3 + CloudFront — skipped unless `WEBUI=true` |
| `./deploy.sh a2a` | the A2A Runtime (same image, `SERVER_MODE=a2a`) — then `scripts/a2a-demo.sh` drives it as a peer would |
| `./deploy.sh mcp` | build every MCP server under `mcp-servers/` (CodeBuild ARM64) + create/update a Runtime each. `./deploy.sh mcp approval` for just one |
| `./deploy.sh 3lo` | workload identity + the `agentcore-fullstack-3lo` OAuth credential provider |
| `./deploy.sh gateway` | Web Search gateway in us-east-1 — skipped unless `WEB_SEARCH=true` |
| `./deploy.sh runtime` | build the agent image + deploy the agent Runtime |
| `./deploy.sh lark` | seed Lark credentials to Secrets Manager + allowlist your `open_id` |
| `./deploy.sh approvals` | subscribe to Lark approval events for each `AGENT_DECIDE_APPROVAL_CODES` definition — no-op when that is empty. Ticking the event in the console is **not** sufficient; Lark delivers approval events only for definitions also subscribed through the API |

Order matters in one place: `3lo` and `gateway` precede `runtime`, because the agent Runtime is created with the provider name and the gateway URL baked into its environment. `deploy.sh` handles that; the underlying implementations are in `scripts/`.

`setup-3lo.sh` registers the OAuth credential provider (Lark behind the RFC-6749 shim) plus the agent's workload identity, and prints the provider `callbackUrl` — register that in the Lark console (step 4 below) before the first 3LO consent.

Per-deployment ids (runtime/gateway) go to `.cdk-state.json` (gitignored), so `cdk.json` stays free of environment state.

Optional web chat: set `WEBUI=true` in `.env`, then register the printed domain on the Lark app (step 5 below). CloudFront only assigns the domain at deploy time and the router must allow that exact origin to trade an h5 code for a JWT, so `./deploy.sh webui` writes it to `.cdk-state.json` and re-deploys the router itself — the same route the file system id takes. Off by default: without the Lark-side registration the page loads but can establish nobody.

Optional web search: set `WEB_SEARCH=true` in `.env` before deploying (or run `./deploy.sh gateway` then `./deploy.sh runtime`). That provisions a Gateway fronting AgentCore's built-in Web Search connector — **in us-east-1, the only region offering it**, so the agent calls it cross-region. Search carries no end-user identity, so it uses `GATEWAY_IAM_ROLE` and never touches the per-user 3LO path the Lark tools have to avoid. Off by default; the agent just runs without the tool.

### Tear down

```bash
scripts/destroy.sh            # delete everything deploy.sh created (asks for confirmation)
# or: scripts/destroy.sh --yes  # skip the prompt
```

Deletes in dependency order — gateway targets → gateways (including the Web Search one in us-east-1) → both CLI-created Runtimes → the CDK stacks → the OAuth credential provider and workload identity. Idempotent: re-running skips already-gone resources.

Two consequences worth knowing: deleting the provider **purges every user's vaulted token**, so everyone consents again after a redeploy — and the new provider gets a **new `callbackUrl`** that must be registered in the Lark console (`scripts/setup-3lo.sh` prints it). Your Lark console app config is otherwise untouched; re-seed credentials from `.env` via `scripts/setup-lark.sh`.

## Lark console setup

1. **Add features**: enable **Bot**.
2. **Permissions & Scopes** — two groups, and the split is what makes the identity model real: the bot speaks with its own identity, while anything touching a user's data acts as that user.

   **Tenant token scopes** (the bot acting as itself — receiving webhooks, replying, reacting):

   | Scope | Used for |
   |---|---|
   | `im:message` | receive events, send replies, and the in-progress emoji reaction (no separate reaction scope needed) |
   | `im:message:readonly` | read message content |
   | `im:message.p2p_msg:readonly` | **required for single (p2p) chats** — without it the bot never sees direct messages |
   | `im:message.group_at_msg:readonly` | see @mentions in group chats (the router strips the mention before passing the text on) |
   | `im:message:send_as_bot` | post as the bot |
   | `im:resource` | download images the user sends |
   | `contact:user.base:readonly` | resolve the sender's basic profile |
   | `cardkit:card:write` | create and update the streaming reply card ("Create and update cards") |

   **User token scopes** (the *user's* identity, via 3LO — this is what the Lark MCP server uses, so tools reach only what that user can). Needs admin approval:

   | Scope | Used for |
   |---|---|
   | `drive:drive` | list and read the user's Drive |
   | `docx:document` | read/write the user's documents |
   | `offline_access` | issue a refresh token, so the vaulted grant survives without re-consent |

   Nothing in the first group can read a user's documents, and nothing in the second is ever used to speak as the bot — `LARKSUITE_CLI_DEFAULT_AS=user` keeps the MCP server on the user's token exclusively. Widening what the agent can *do* for a user means adding user-token scopes here and to `LARK_SCOPES`; every user then re-consents once.
3. **Events & Callbacks**: Request URL = the webhook URL from deploy output; enable Encryption; add `im.message.receive_v1`. For the approval demo also add **审批任务状态变更** (`approval_task`) — and note that ticking it here is not enough on its own, see below.
4. **Security Settings → Redirect URLs**: add the OAuth credential provider's `callbackUrl` (`https://bedrock-agentcore.<region>.amazonaws.com/identities/oauth2/callback/<uuid>`, from `get-oauth2-credential-provider --name agentcore-fullstack-3lo`). This is where AgentCore Identity receives the 3LO code — not the shim URL.
5. **Web chat only** (skip unless `WEBUI=true`) — three separate fields, and they do not match the same way:

   | Field | Value | Why it is easy to get wrong |
   |---|---|---|
   | **Features → Web app** → Desktop + Mobile homepage | the CloudFront URL, "open in Lark" | This is what creates the workspace entry. A trusted domain alone gives you no way in |
   | **Security Settings → H5 trusted domains** | the CloudFront URL, no trailing slash | Matched by origin. Grants JSAPI access — necessary, but by itself produces no entrypoint |
   | **Security Settings → Redirect URLs** | the CloudFront URL **with a trailing `/`** | Matched as an exact page URL; `?query`/`#fragment` are stripped first. A homepage entry without the slash fails `requestAuthCode` with error 10236 ("invalid url"), which is the single most common cause |

   No permission scope is needed for this: `requestAuthCode` is exempt from JSAPI authorization, and the code→`open_id` exchange requires none. Then publish, with your own user in the availability scope.

6. **Publish** a version (re-publish after any scope/event change).

### Optional: the approval demo

Off by default. It shows what an agent must do when a downstream API *refuses* to accept the user's identity — Lark's approval endpoints take only an app token, so a decision is made by the app with a `user_id` saying whose name to record it under. Read [docs/architecture.md](docs/architecture.md#a-third-path-approvals-where-the-users-identity-cannot-be-passed-through) before switching it on: the limits are self-imposed, and what they can and cannot prevent is the point of the demo.

To enable:

1. Add the approval scopes and the `approval_task` event from step 3 above, then re-publish. The console lists these by display name, so both are given here:

   | Scope | Type | Display name | Used for |
   |---|---|---|---|
   | `approval:approval` | tenant | View, create, update, and delete info of Approval app | making decisions (approve/reject/transfer) |
   | `approval:approval:readonly` | tenant | Access Approval | reading instances and queues |

   The `approval_task` event accepts **either** of those two (the console shows "any one suffices"), so nothing extra is needed to receive events.
2. Set the limits in `.env` — the agent decides nothing until you do:
   ```
   AGENT_DECIDE_APPROVAL_CODES="<definitionCode>, ..."   # empty = decide nothing
   AGENT_DECIDE_MAX_AMOUNT=1000                          # 0 = kill switch
   ```
   The definition code is the `definitionCode=` query parameter in the URL of a form's edit page in the Lark approval admin.
3. `./deploy.sh mcp approval` (builds the approval Runtime — gated on that variable so it costs nothing when unused), then `./deploy.sh approvals` to subscribe. **Both the console tick and this API subscription are required**; Lark delivers approval events only for definitions subscribed through the API.
4. Authorize as the approver (`/auth lark` in the bot chat). The server refuses to decide for anyone without their own grant on record, so an approver who never consented gets a 点击授权 card instead — after which the turn resumes on its own.

One tool is deliberately left unusable: `approval_add_sign` (加签) is the single approval endpoint that takes the *user's* token instead of the app's, but the vaulted token carries only the scopes `LARK_SCOPES` requests (`drive:drive docx:document offline_access`), and the only user-token approval scope on offer is `approval:approval:readonly` — a read scope, while add_sign writes. So it fails on permissions by construction. It stays exposed because that boundary is the lesson: Lark's approval API admits a user identity for exactly one operation, and not one this sample can reach.

Then submit an approval assigned to that approver. Both outcomes are worth trying: within the limits the agent decides and comments `[AI 自动处理]`; over the amount ceiling it refuses to decide and hands the case back.

### Letting more people in

The bot answers only allowlisted users; `./deploy.sh lark` adds you and nobody else. An unlisted user who messages the bot is told their own id, which is the easiest way to collect one:

```bash
PROFILE=... REGION=... scripts/manage-allowlist.sh add lark:ou_...
scripts/manage-allowlist.sh list
scripts/manage-allowlist.sh remove lark:ou_...
```

The allowlist gates *conversations*. It also gates whether an approval event is acted on at all: an approver who isn't listed is left alone silently. Note the asymmetry — deciding for someone needs their own 3LO grant, but DMing them does not (that runs on the app's token), so the allowlist is the only thing standing between an approval event and a stranger's chat window.

## Chat commands

Send these to the bot instead of a question — in Lark chat or on the web page, which has the same set. `/help` lists them in-chat.

| Command | What it does |
|---|---|
| `/auth` | Authorization status per IdP — one OAuth provider per downstream system |
| `/auth <idp>` | Authorize or re-authorize that IdP; always starts a fresh 3LO flow, so it is idempotent |
| `/status` | Identity, both session ids, the microVM serving you (id + age + how long it has served this session), the model that microVM actually calls, turns and tool calls, last activity |
| `/new` | New thread **and** new runtime instance — a fully fresh start |
| `/reset` | New thread, same runtime instance — history starts over, old state kept |
| `/clear` | Actually delete this thread's checkpoints (unlike `/reset`, which just stops reading them) |
| `/reconnect` | New runtime instance, same thread — shows that the conversation outlives the container |

The three session commands exist because **the runtime session and the checkpoint thread are independent ids**: one decides which microVM serves you, the other which history the agent reads. Rotating either is instant; `/clear` is the only command that deletes anything. Authorization is a third, orthogonal dimension — the vaulted token is keyed to `lark:{open_id}`, not to a session, so `/new` never costs a re-consent.

`/status` is for developers evaluating AgentCore, so it deliberately exposes the compute layer: the microVM's own id and two durations, both in seconds so they can be subtracted (equal means it started for you; a much larger age means an existing one took over). The model line comes from that same probe — the container reports what it actually calls Bedrock with, because the router's config can name a model nobody is using. Which signals here are *not* trustworthy, and why, is in [docs/agentcore-behavior.md](docs/agentcore-behavior.md).

## Extending the agent

The four extension points that need no new plumbing:

| To add | Do this |
|---|---|
| A tool server | Create `mcp-servers/<name>/` with a `Dockerfile`, a server speaking MCP on `:8000`, and a `runtime.env` (`RUNTIME_SUFFIX`, `IMAGE_TAG`, `RUNTIME_ENV_MAP`, optional `DEPLOY_IF` gate and `REQUIRE_VARS`), then `./deploy.sh mcp <name>`. `scripts/build-mcp.sh` needs no edit — each server describes itself. The agent must be pointed at the new Runtime's URL, the way `APPROVAL_MCP_URL` is in `scripts/provision.sh` |
| A downstream system with its own login | Register an OAuth credential provider and append an `IDP_REGISTRY` entry — `/auth` then reports it and `/auth <key>` consents to it. Built-in vendors need no shim; anything non-standard needs one like `lambda/shim/`. Recipe: `docs/native-3lo-builtin-vendor.md` |
| A different model | `MODEL_ID` in `.env` (falls back to `default_model_id` in `cdk.json`), then `./deploy.sh runtime` |
| A different system prompt | `AGENT_SYSTEM_PROMPT` in `.env`, passed through to the Runtime by `./deploy.sh runtime`. Blank keeps the default in `agent/agent_core.py`, which is short on purpose |

Two structural constraints shape anything larger. **One Runtime per MCP server** — `protocolConfiguration.serverProtocol` is a single value and a container exposes one MCP endpoint, so tools are grouped by trust boundary rather than packed together, which is why `lark-cli` (acts as the user) and `approval` (acts as the app) are separate servers. And **a Runtime-hosted MCP server cannot receive a per-user token from the Gateway**, so the agent fetches tokens itself and passes them in a custom header; a tool server on an addressable HTTPS endpoint could use the managed Gateway path instead. Both are explained in `docs/agentcore-behavior.md`.

## Test

```bash
tests/run.sh                 # unit suites (agent, router, shim) — no AWS needed
```

Unit tests sit next to the code they cover and mock AWS; `tests/run.sh` walks them, one process per suite (several modules share filenames, so a single pytest session would import the wrong one). The e2e smoke tests in `tests/` exercise a **deployed** stack and stay skipped unless you point them at it — see `tests/README.md`.

## Cost

This deploys billable AWS resources. All the always-on pieces are consumption- or per-unit-priced (no fixed reservation), so an idle single-user demo in us-west-2 is on the order of a couple USD/month before model usage; the variable cost is dominated by the agent's Bedrock calls. Verify current rates on the AWS pricing pages — figures below are as researched, not a quote.

- **Bedrock model invocations** — the main usage-sensitive line; priced per input/output token on the model in `default_model_id`. A chatty demo is cents-to-dollars; a load test is not.
- **AgentCore Runtime ×2–3** — the agent and the lark-cli MCP server always, plus the approval MCP server if `AGENT_DECIDE_APPROVAL_CODES` is set. Each is metered per-second: CPU (`~$0.0895/vCPU-hour`) only during active processing, memory (`~$0.00945/GB-hour`) continuously while the microVM is alive. So each extra MCP server adds idle memory-time even when nothing calls it — which is why the approval one is gated on that variable rather than always deployed, and why grouping tools by trust boundary has a running cost.
- **AgentCore Identity Token Vault (3LO)** — stores/refreshes/injects each user's Lark token natively. No separate per-user Secrets Manager charge (unlike the interceptor variant).
- **DynamoDB + S3 (checkpoints)** — on-demand writes, one item per superstep, with state over ~350 KB spilled to S3. Table TTL expires old threads; nothing is billed for retention beyond storage.

- **AgentCore Memory** — long-term records only. Billed per event *written* (`~$0.25 per 1,000`), per record stored per month, and per *retrieval* (`~$0.50 per 1,000`) — retrieval is the expensive one, so it is queried on demand rather than every turn.
- **Lambda + API Gateway** — router (webhook) + shim (OAuth RFC-6749 façade, a backend web service); effectively free at demo volume.
- **Secrets Manager** — `$0.40/secret/month` each, and only two static secrets: the Lark credentials (`{prefix}/channels/lark`) and the Cognito password salt. No dynamic per-user secrets.
- **Web search (optional)** — only when `WEB_SEARCH=true`: an AgentCore Gateway plus per-query connector charges. The gateway sits in us-east-1, so its traffic is cross-region.
- **Code execution (optional, `FILES_STORAGE=true`)** — **no NAT.** A mount needs a VPC, but only the Code Interpreter session goes in there, and it only has to reach S3, which a free gateway endpoint does. So the fixed cost is a VPC (free), an S3 gateway endpoint (free) and mount targets (free); what is metered is sandbox session time and the S3 storage the workspace uses. Off by default because it still adds resources you should not create by accident.
- **Cognito, DynamoDB (on-demand)** — the identity/state plane; negligible at demo volume.

`scripts/destroy.sh` removes everything `deploy.sh` created, including the OAuth credential provider and both gateways. Costs are usage-driven; an idle deployment still accrues the two microVMs' memory-time and the two static per-secret charges.

## Security considerations

This is a **reference implementation, not production-ready as-is**. Before any real use:

- **Per-user Lark tokens live in the AgentCore Identity Token Vault**, not in application code or a self-managed store. The agent fetches a user's token at call time (agent-side 3LO) and passes it to the lark-cli MCP server in a custom header; it holds no long-lived credential of its own. Treat the account hosting the vault as sensitive.
- **Inbound identity is a signed JWT, and the by-name token APIs are denied in IAM** — so the agent cannot act as a user it wasn't handed. That closes credential theft and impersonation; it does not stop a prompt-injected agent from misusing the tools it legitimately has, within that user's own permissions. Action-layer limits in code are what address that (see the approval server).
- **The MCP server calls Lark strictly as the user.** `LARKSUITE_CLI_DEFAULT_AS=user` — the lark-cli engine always acts with the vaulted `user_access_token`, never the bot identity, so access is scoped to what that user can do in Lark and Lark adjudicates it.
- **A vaulted token is checked against its actor at point of use.** Consent binds a token to whatever the return-url was told, so forwarding a consent link would otherwise vault someone else's grant under your name; the agent resolves each token's real owner before using it and fails closed.
- **Command execution is injection-safe.** The MCP server spawns lark-cli via `execFile` (no shell) with arguments passed as an array, and the user token via an environment variable — never interpolated into a command line.
- **Web search sees no user data.** It runs on Amazon's index with `GATEWAY_IAM_ROLE`, carries no user token, and queries stay inside AWS. It does mean model output can include fetched web content — treat that as untrusted input like any other tool result. `parameterValues.domainFilter` can restrict which domains are searched.
- **Per-user files are isolated by IAM, not by agent code** — one S3 Files Access Point per user with its `rootDirectory` fixed server-side to `users/lark_<open_id>`, and mount credentials that an STS session policy pins to that one Access Point. The agent's execution role holds **no** S3 permission on the bucket, and the router (the only component that establishes who a turn is) is the only thing that can sign a mount ticket — it signs the verified actor and never accepts a subject as input.
- **Enabling files storage makes the agent container run as root**, on an Amazon Linux base, because `mount(2)` needs `CAP_SYS_ADMIN` and the per-user Access Point cannot be declared on the Runtime. That gives up the non-root execution the container otherwise has; nothing else in the image needs root. A mount also means an injected turn can read and write that user's own files — the same blast radius the Lark tools already have, but worth knowing before switching it on.
- **IAM is scoped but a sample.** Re-review least-privilege for your account before production.
- **Webhook verification is fail-closed** — a missing/invalid signature or a timestamp outside the replay window is rejected before decryption. Don't relax this.
- **AES-CBC webhook decryption** is Lark's fixed scheme (not our choice); authenticity is guaranteed by the upstream signature check, not by the cipher mode.
- **No secrets in this repo** — Lark credentials come from `.env` → Secrets Manager via `scripts/setup-lark.sh`; `.env` is git-ignored.

### Notes & limitations

- **A background turn has a hard ceiling of `maxLifetime`.** `/ping` reports `HealthyBusy` while a turn runs, which defers *idle* reclamation (`idleRuntimeSessionTimeout`) but **not** `maxLifetime` — the microVM's wall-clock age cap (default 8 h) that never resets. The first token takes several seconds (session assembly, MCP handshake, model latency), which is what the CardKit placeholder covers; if CardKit is unavailable the answer is posted as plain text rather than lost. Consent is the one synchronous exception, since the router drives the wait-and-retry loop around it.
- **Consent-wait is time-bounded.** On first use the router posts the consent link, then holds and polls the vault up to `AUTH_WAIT_SECONDS` (45s) before falling back to "re-send after approving". A user who takes longer than that to approve just re-sends once; the token is already vaulted by then.
- **A2A runs no turn of its own.** A vaulted consent is scoped to the Runtime that obtained it (measured — the same user, provider and scopes read as "never consented" from a second Runtime), so the A2A adapter can only forward to the agent Runtime.
- **3LO is agent-side, not Gateway-mediated — because the topology requires it.** A tool server hosted on AgentCore Runtime cannot be handed a per-user token by the Gateway: `/invocations` owns the `Authorization` header for its own transport auth. So the agent fetches each user's token and passes it in a custom header. Measured evidence in `docs/agentcore-behavior.md`.
- **One Runtime per MCP server, because `serverProtocol` is a single value.** Each server under `mcp-servers/` declares its own build and runtime config in `runtime.env` — including a gate that skips it when unconfigured — and is built via CodeBuild (ARM64) out-of-band from CDK.
- **A new image doesn't reach existing users by itself.** AgentCore keeps serving stored sessions from the old container, so `./deploy.sh runtime` drops the saved session ids — the next message lands on the new version.
- **`/status` counts turns, not messages.** A turn is an answer actually delivered (an `AIMessage` with no pending `tool_calls`), and tool calls are reported beside it rather than folded in. The count is exact — it decodes the latest checkpoint, unlike the paged event walk it replaces — but it describes only what is *in* the checkpoint: after summarization the trimmed turns are gone from it, by design.
- **Token Vault exposes no metadata.** `GetResourceOauth2Token` returns just the token (or a consent URL) — no issued-at, expiry, or granted scopes — so `/auth` reports presence only.
