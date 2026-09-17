"""Router stack — Lark webhook ingestion (HTTP API + Lambda + DynamoDB identity).

Owns the DynamoDB identity table (channel→user mapping, allowlist, sessions),
which the WebUI stack also reads/writes. Exposes explicit routes:
  POST /webhook/lark   — Lark event subscription callback
  GET  /health         — health probe
Signature verification + AES event decryption happen inside the Lambda
(fail-closed). Processing is async: the sync path validates + returns 200 fast,
then self-invokes for the actual agent call.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
)
from constructs import Construct

from stacks import retention_days, lambda_asset


class RouterStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        runtime_arn: str,
        runtime_endpoint_qualifier: str,
        lark_secret_name: str,
        shim_return_url: str,
        user_pool_id: str,
        user_pool_arn: str,
        user_pool_client_id: str,
        cognito_password_secret_name: str,
        # Per-user file mounts. Empty unless the storage stack is deployed, and the
        # router treats that as "no file tools" rather than an error.
        mount_ticket_key_arn: str = "",
        s3files_file_system_id: str = "",
        mount_path: str = "/mnt/user",
        web_allowed_origins: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        prefix = self.node.try_get_context("resource_prefix") or "agentcore-fullstack"
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30
        timeout = int(self.node.try_get_context("lambda_timeout_seconds") or "60")
        memory = int(self.node.try_get_context("lambda_memory_mb") or "256")
        registration_open = str(self.node.try_get_context("registration_open") or "false").lower()
        lark_api_domain = self.node.try_get_context("lark_api_domain") or "https://open.larksuite.com"
        fn_name = f"{prefix}-router"

        # --- DynamoDB identity table (shared with WebUI stack) ---
        self.identity_table = dynamodb.Table(
            self,
            "IdentityTable",
            table_name=f"{prefix}-identity",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,  # PoC
        )
        self.identity_table_name = self.identity_table.table_name
        self.identity_table_arn = self.identity_table.table_arn

        log_group = logs.LogGroup(
            self,
            "RouterLogGroup",
            log_group_name=f"/{prefix}/lambda/router",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.router_fn = _lambda.Function(
            self,
            "RouterFn",
            function_name=fn_name,
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,  # matches aarch64 wheels bundled by uv
            handler="index.handler",
            code=lambda_asset("lambda/router"),
            timeout=Duration.seconds(timeout),
            memory_size=memory,
            environment={
                "AGENTCORE_RUNTIME_ARN": runtime_arn,
                "AGENTCORE_QUALIFIER": runtime_endpoint_qualifier,
                "IDENTITY_TABLE_NAME": self.identity_table.table_name,
                "LARK_SECRET_ID": lark_secret_name,
                "LARK_API_DOMAIN": lark_api_domain,
                "REGISTRATION_OPEN": registration_open,
                "SELF_FUNCTION_NAME": fn_name,
                # Without this the code assumed 60s and cut the agent off early,
                # leaving part of the Lambda's budget unused.
                "LAMBDA_TIMEOUT_SECONDS": str(timeout),
                # 3LO consent-wait: poll the vault so the user needn't re-send.
                "LARK_OAUTH_PROVIDER": f"{prefix}-3lo",
                "AGENT_WORKLOAD_NAME": f"{prefix}-wl",
                "LARK_SCOPES": "drive:drive docx:document offline_access",
                # Per-IdP registry for /auth; written by scripts/setup-3lo.sh.
                "IDP_REGISTRY": self.node.try_get_context("idp_registry") or "",
                "SHIM_RETURN_URL": shim_return_url,
                # Token factory: the router signs each turn's identity (see cognito.py).
                "COGNITO_USER_POOL_ID": user_pool_id,
                "COGNITO_CLIENT_ID": user_pool_client_id,
                "COGNITO_PASSWORD_SECRET_ID": cognito_password_secret_name,
                # Per-user file mounts. files.enabled() is false while these are empty.
                "MOUNT_TICKET_KEY_ID": mount_ticket_key_arn,
                "S3FILES_FS_ID": s3files_file_system_id,
                "MOUNT_PATH": mount_path,
            },
            log_group=log_group,
        )

        # The sync phase self-invokes asynchronously, and Lambda counts a timeout as
        # a function error — with the default 2 retries a slow turn would be replayed
        # twice, so the agent would redo the work and the user would get duplicate
        # replies. Deliver once; the user can always ask again.
        self.router_fn.configure_async_invoke(retry_attempts=0)

        integration = apigwv2_integrations.HttpLambdaIntegration("Integration", handler=self.router_fn)
        # The web page's /web/session call is a cross-origin JSON POST, so the browser sends
        # a preflight first. Without this it 404s and the page reports only "Failed to fetch".
        cors = apigwv2.CorsPreflightOptions(
            allow_origins=[o.strip() for o in web_allowed_origins.split(",") if o.strip()],
            allow_methods=[apigwv2.CorsHttpMethod.POST, apigwv2.CorsHttpMethod.OPTIONS],
            allow_headers=["content-type"],
        ) if web_allowed_origins else None
        self.http_api = apigwv2.HttpApi(
            self, "RouterApi", api_name=f"{prefix}-router",
            description="Lark webhook ingestion (explicit routes only)",
            cors_preflight=cors,
        )
        self.http_api.add_routes(
            path="/webhook/lark", methods=[apigwv2.HttpMethod.POST], integration=integration,
        )
        self.http_api.add_routes(
            path="/health", methods=[apigwv2.HttpMethod.GET], integration=integration,
        )
        # The web entrypoint's identity step: an h5 authorization code in, a user JWT out.
        self.http_api.add_routes(
            path="/web/session", methods=[apigwv2.HttpMethod.POST],
            integration=integration,
        )
        # Chat commands for the web page. Here rather than on the Runtime because they
        # rotate session ids the router owns.
        self.http_api.add_routes(
            path="/web/command", methods=[apigwv2.HttpMethod.POST],
            integration=integration,
        )

        # throttling on default stage
        default_stage = self.http_api.default_stage
        if default_stage:
            cfn_stage = default_stage.node.default_child
            cfn_stage.default_route_settings = apigwv2.CfnStage.RouteSettingsProperty(
                throttling_burst_limit=20,
                throttling_rate_limit=50,
                detailed_metrics_enabled=True,
            )

        # --- IAM ---
        # No InvokeAgentRuntime here: the runtime is reached over HTTPS with the user's
        # own JWT, and a CUSTOM_JWT runtime refuses SigV4 anyway. Authorization for that
        # hop is the token, not this role.
        self.identity_table.grant_read_write_data(self.router_fn)

        # Per-user file mounts, only when the storage stack exists. Two permissions, and
        # both are deliberately narrow: signing (never verifying — the router mints
        # tickets and the broker checks them, so neither can do the other's job) and
        # running the bootstrap command in a session.
        if mount_ticket_key_arn:
            self.router_fn.add_to_role_policy(iam.PolicyStatement(
                actions=["kms:Sign"], resources=[mount_ticket_key_arn]))
            self.router_fn.add_to_role_policy(iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntimeCommand"],
                # The runtime id is not known at synth time on a first deploy, which is
                # the same reason AGENTCORE_RUNTIME_ARN starts as a placeholder here.
                resources=[f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/*"]))
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[f"arn:aws:lambda:{region}:{account}:function:{fn_name}"],
            )
        )
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:{prefix}/*",
                    # Identity-managed OAuth provider secret (for the vault check below).
                    f"arn:aws:secretsmanager:{region}:{account}:secret:bedrock-agentcore-identity!default/oauth2/*",
                ],
            )
        )
        # 3LO consent-wait: check the vault for the user's Lark token (same
        # USER_FEDERATION sequence the agent uses) so the router can hold and
        # re-invoke once consent completes, sparing the user a re-send. Also completes
        # the consent itself — see complete_consent in the router.
        #
        # ForJWT, and deliberately NOT ForUserId: the router mints the user's JWT, so it
        # needs no by-name path, and the vault namespace follows the token's `sub`.
        # This is the trust the design relocates here — the router is small, runs no
        # model and never sees untrusted input, which the agent cannot claim.
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetResourceOauth2Token",
                    "bedrock-agentcore:CompleteResourceTokenAuth",
                ],
                resources=["*"],
            )
        )

        # Token factory: mint a per-user Cognito access token, so every downstream hop
        # verifies a signature instead of trusting a string.
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminGetUser",
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminSetUserPassword",
                    "cognito-idp:AdminInitiateAuth",
                ],
                resources=[user_pool_arn],
            )
        )

        # Nothing here for /status or /clear any more: the conversation is LangGraph
        # state, so the agent answers both over InvokeAgentRuntime and the router needs
        # no Memory permissions of its own (dropped ListMemories/ListEvents/DeleteEvent).

        self.api_url = self.http_api.url or ""
        CfnOutput(self, "ApiUrl", value=self.api_url)
        CfnOutput(self, "WebhookLarkUrl", value=(self.http_api.url or "") + "webhook/lark")
        CfnOutput(self, "IdentityTableName", value=self.identity_table.table_name)
