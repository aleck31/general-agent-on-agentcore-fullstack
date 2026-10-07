# Console setup — Lark and Google

What to configure in the vendors' consoles; the deploy itself is in the [README](../README.md#deploy).

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

## Optional: the approval demo

Off by default. It shows what an agent must do when a downstream API *refuses* to accept the user's identity — Lark's approval endpoints take only an app token, so a decision is made by the app with a `user_id` saying whose name to record it under. Read [architecture.md](architecture.md#a-third-path-approvals-where-the-users-identity-cannot-be-passed-through) before switching it on: the limits are self-imposed, and what they can and cannot prevent is the point of the demo.

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

## Optional: Google as a second downstream system

Off unless `GOOGLE_CLIENT_ID` is set. It exists to show the identity chain is not Lark-specific: Google is a built-in AgentCore vendor, so there is no shim — the `google` MCP server calls Google's APIs with the user's own token and offers `google_whoami` and a read-only `google_calendar_upcoming`.

In [Google Auth Platform](https://console.cloud.google.com/auth/overview) for your project:

1. **APIs & Services → Library**: enable **Google Calendar API**.
2. **Audience**: *Internal* (Google Workspace — no verification needed, any account in your organisation can consent). *External* also works, but while in testing only the listed test users can consent.
3. **Data Access**: add `openid`, `.../auth/userinfo.email`, `.../auth/userinfo.profile`, `.../auth/calendar.readonly` — the scopes `GOOGLE_SCOPES` requests.
4. **Clients → Create client → Web application**. Put the ID and secret in `.env` as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`, then run `./deploy.sh` (idempotent; it creates the `agentcore-fullstack-google` provider, the `google` MCP Runtime, and adds `google` to the router's `/auth` list).
5. Back on the client: **Authorized redirect URIs** = the `callbackUrl` that `./deploy.sh 3lo` prints for the Google provider (`https://bedrock-agentcore.<region>.amazonaws.com/identities/oauth2/callback/<uuid>` — a different uuid from Lark's). Saving it adds `amazonaws.com` to **Branding → Authorized domains** automatically.

Then `/auth google` in the bot chat. Google shows two screens — sign-in (name and picture), then the Calendar permission — both are expected.

> If consent ends in Google's `400 error.` page, retry with only one Google account signed in (or in an incognito window).
