# Adding another downstream system (per-user 3LO)

Lark is the system this repo wires up, but the pattern isn't Lark-specific: **one OAuth credential provider per downstream system, one vaulted token per (user, provider)**, and the agent fetches whichever one a tool needs. This page is the checklist. For the reasoning, the measured evidence and the full OAuth dance, read the two samples that exist for that subject: [sample-lark-identity-on-agentcore-native](https://github.com/aws-samples/sample-lark-identity-on-agentcore-native) and [sample-lark-identity-on-agentcore-interceptor](https://github.com/aws-samples/sample-lark-identity-on-agentcore-interceptor).

> **3LO = 3-Legged OAuth**: the end user consents on the IdP's own page, and the app receives a token that acts **on behalf of that user**, inheriting that user's permissions (`authorization_code`; AgentCore calls it `oauth2Flow=USER_FEDERATION`). Contrast 2LO (`client_credentials`), where the app uses its own identity and no user is involved — which cannot distinguish per-user access, hence 3LO here.

## Which category is your system?

| | Registration | Shim needed |
|---|---|---|
| **Built-in vendor** — Google, Github, Slack, Salesforce, Microsoft, Atlassian, Linkedin | vendor sub-key, client id + secret only | no |
| **Other built-in** — Okta, PingOne, Auth0, Zoom, Notion, HubSpot, Cognito, … (24 total) | `includedOauth2ProviderConfig`; you also supply authorize/token/issuer endpoints | no |
| **`CustomOauth2`** — Lark/Feishu, most China-region and in-house IdPs | fully self-described (`authorizationServerMetadata` or a discovery URL) | only if its OAuth is non-standard |

`aws bedrock-agentcore-control create-oauth2-credential-provider --credential-provider-vendor` lists the exact set. A non-standard token endpoint (JSON request body, a `code:"0"` success envelope, HTTP 200 on error — all true of Lark) needs an RFC-6749 façade in front of it to register at all; `lambda/shim/` is a working one to copy, and its `/token` must forward PKCE `code_verifier` upstream.

## Steps

1. **Create the OAuth client at the IdP.** Leave the redirect URI as a placeholder; you fill it in at step 3.
2. **Register the credential provider.**
   ```bash
   aws bedrock-agentcore-control create-oauth2-credential-provider \
     --name google-xyz --credential-provider-vendor GoogleOauth2 \
     --oauth2-provider-config-input '{"googleOauth2ProviderConfig":{"clientId":"...","clientSecret":"..."}}'
   ```
   `grantType` is a **top-level** field of `oauthCredentialProvider`, not something inside `customParameters` — misplacing it silently falls back to `CLIENT_CREDENTIALS`, and the resulting failure reads like "3LO is unsupported".
3. **Register the provider's callback URL at the IdP.**
   ```bash
   aws bedrock-agentcore-control get-oauth2-credential-provider --name google-xyz --query callbackUrl
   # → https://bedrock-agentcore.<region>.amazonaws.com/identities/oauth2/callback/<uuid>
   ```
   That exact URL goes in the IdP client's authorized redirect URIs — not your own return endpoint.
4. **Allowlist your return URL on the workload identity.** Exact-match, byte for byte, so keep it bare:
   ```bash
   aws bedrock-agentcore-control update-workload-identity --name lark-agent-wl \
     --allowed-resource-oauth2-return-urls "https://<your-return-endpoint>/return"
   ```
5. **Append an `IDP_REGISTRY` entry** (`scripts/setup-3lo.sh`) — `{key, provider, scopes, label}`. `/auth` then reports the new system's status and `/auth <key>` consents to it, with no router or agent change.
6. **Point a tool at it.** The agent fetches the token for `(provider, user)` and passes it to the tool server; `agent/lark_3lo.py` is the reference for both halves.

## The flow the agent drives

```
GetResourceOauth2Token(workloadIdentityToken, provider, scopes,
                       oauth2Flow=USER_FEDERATION,
                       resourceOauth2ReturnUrl=<bare, allowlisted>,
                       customState=<base64url(userId)>)
   → vaulted already?  {accessToken}   → use it
   → otherwise         {authorizationUrl, sessionUri}
        ↓ surface the URL to the user (chat message / link)
        ↓ user consents → IdP → AgentCore's own callback → 302 to your return URL
        ↓ your return endpoint: CompleteResourceTokenAuth(sessionUri, userIdentifier)
   next turn → {accessToken}
```

`userIdentifier` is `{userId}` when the workload token came from `GetWorkloadAccessTokenForUserId`, and `{userToken}` (the original JWT) when it came from `GetWorkloadAccessTokenForJWT` — which is this repo's case, since inbound auth is `CUSTOM_JWT`. The two are **separate vault namespaces**: mixing them silently reports "not consented" forever. Never pass the `workloadIdentityToken` there.

## Gotchas, each of which cost a failed consent

- **`request_uri` is single-use and short-lived (~10 min).** Hand the URL straight to the user and let the browser open it first — **a server-side curl to "verify" it consumes it**, after which the user gets `{"message":"Invalid request"}`.
- **`customState` must be colon-free.** A raw `lark:ou_x` makes AgentCore reject the request with a misleading `Value at 'requestUri' failed to satisfy … pattern`. Base64url-encode it; decode at the return endpoint.
- **That same "requestUri regex" error is also what a stale or consumed `request_uri` returns** — it does not mean the URL is malformed. Mint a fresh one.
- **Don't let the URL get mangled in transit.** Double-encoded `%3A`→`%253A`, or a line-wrapped copy-paste, both fail. One unbroken line.
- **The return-URL allowlist is exact-match** — carry variable data in `customState`, never as query params on the return URL.
- **The authorization code is ~5 min.** Completion has to happen automatically on the return-URL callback; a delayed manual `CompleteResourceTokenAuth` gets `AccessDeniedException: Invalid or expired session`.

## One structural limit

A per-user token **cannot** be delivered by the Gateway to a tool server hosted on **AgentCore Runtime** — `/invocations` owns the `Authorization` header for its own transport auth. So a Runtime-hosted server must receive the token another way (this repo: a custom passthrough header the agent sets), while an addressable HTTPS target (ALB / API Gateway / Fargate) can use the managed Gateway path with `OAUTH` outbound. Verified as a single-variable A/B with permissions ruled out; see `docs/agentcore-behavior.md`.
