"""AgentCore stack — execution role + container image for the simple agent.

Scope is deliberately small (this is a from-scratch simple agent, not OpenClaw):
  - Execution role: Bedrock invoke, Cognito admin-auth (mint per-user JWT for
    MCP calls), Secrets read, CloudWatch logs, ECR pull.
  - Container image: built from ./agent via DockerImageAsset (ARM64), pushed to
    the CDK assets ECR repo.

The Runtime itself is NOT a CloudFormation resource in this account/region
(verified: no AWS::BedrockAgentCore::Runtime type). It is created out-of-band by
scripts/provision.sh via `aws bedrock-agentcore-control create-agent-runtime`, using
this stack's execution role ARN and the image URI below. The resulting runtime_id
is written back into cdk.json context, from which runtime_arn is derived here for
dependent stacks (Router, WebUI).
"""

from aws_cdk import (
    CfnOutput,
    Stack,
    RemovalPolicy,
    Duration,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_s3 as s3,
    aws_ecr_assets as ecr_assets,
)
from constructs import Construct


class AgentCoreStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cognito_user_pool_id: str,
        cognito_client_id: str,
        cognito_issuer_url: str,
        cognito_password_secret_name: str,
        lark_secret_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        prefix = self.node.try_get_context("resource_prefix") or "agentcore-fullstack"

        # --- Execution role: what the agent container may do -----------------
        execution_role_name = f"{prefix}-execution-role-{region}"
        self.execution_role = iam.Role(
            self,
            "ExecutionRole",
            role_name=execution_role_name,
            assumed_by=iam.CompositePrincipal(
                iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                iam.ServicePrincipal("bedrock.amazonaws.com"),
            ),
        )

        # Bedrock model invocation (foundation models + inference profiles for
        # the global.* Sonnet 5 cross-region profile).
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                ],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/*",
                    "arn:aws:bedrock:*::inference-profile/*",
                ],
            )
        )

        # Cognito admin auth — mint a per-user JWT (username = lark:{open_id})
        # to attach as Bearer on outbound MCP/Gateway calls.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminSetUserPassword",
                    "cognito-idp:AdminInitiateAuth",
                    "cognito-idp:AdminGetUser",
                ],
                resources=[
                    f"arn:aws:cognito-idp:{region}:{account}:userpool/{cognito_user_pool_id}",
                ],
            )
        )

        # Secrets read — Cognito password salt + Lark creds, plus the OAuth provider secret AgentCore Identity vaults per credential provider.
        # GetResourceOauth2Token reads that managed secret AS the caller, so the execution role needs GetSecretValue on it (name: bedrock-agentcore-identity!default/oauth2/*).
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:{prefix}/*",
                    f"arn:aws:secretsmanager:{region}:{account}:secret:bedrock-agentcore-identity!default/oauth2/*",
                ],
            )
        )

        # CloudWatch logs — AgentCore Runtime writes to /aws/bedrock-agentcore/runtimes/*,
        # so that path must be allowed or the log group is never created.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                ],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:/{prefix}/*",
                    f"arn:aws:logs:{region}:{account}:log-group:/{prefix}/*:*",
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*",
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*",
                    f"arn:aws:logs:{region}:{account}:log-group:*",
                ],
            )
        )

        # Observability — traces and metrics from the runtime.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                resources=["*"],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
            )
        )

        # AgentCore Memory — long-term records only, via the remember/recall tools. No event
        # actions on purpose: that leaves the agent structurally unable to store transcripts
        # here, which is where the conversation would otherwise leak in.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:BatchCreateMemoryRecords",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{region}:{account}:memory/*",
                ],
            )
        )

        # Downstream MCP servers, scoped to this project's runtimes. A name prefix, not exact
        # ARNs: the AWS-assigned suffix only exists after provision.sh runs, and this stack
        # owns the role it runs with. Verified scopable, and why it matters: .dev/adr/0008.
        rt_prefix = prefix.replace("-", "_")
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{rt_prefix}_*",
                    f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{rt_prefix}_*/runtime-endpoint/*",
                ],
            )
        )

        # Agent-side 3LO: the agent fetches THIS user's Lark token from the Identity
        # Token Vault. Left unscoped — unlike the invoke actions above, the resource
        # types for these were not verified, and the impersonation surface is closed by
        # the explicit Deny below rather than by resource scoping. The Gateway is in
        # us-east-1 (the only region offering the search connector), so its ARN is not
        # derivable from this stack's region.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetResourceOauth2Token",
                    "bedrock-agentcore:InvokeGateway",
                ],
                resources=["*"],
            )
        )

        # The impersonation surface, closed. With CUSTOM_JWT inbound the Runtime hands
        # the agent a workload token derived from the caller's verified JWT, so the agent
        # has no reason to mint one for a user it names itself. Not calling those APIs is
        # not enough — anything holding this role could still call them, and the actions
        # take no resource scope, so a Deny is the only way to make "act as an arbitrary
        # consented user" impossible rather than merely unwritten.
        #
        # ForJWT is denied too: possessing any user's JWT is sufficient to exchange it
        # for their vaulted token, so leaving it would keep the surface open to whatever
        # token reaches the agent — verified callable from outside AgentCore entirely.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                actions=[
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                ],
                resources=["*"],
            )
        )

        # Code execution: one shared Code Interpreter, one Access Point per user, and the
        # session mounts that user's Access Point. The agent needs to create the Access
        # Point on first use and to drive sessions — it never gets mount credentials of its
        # own, and never S3 access to the bucket. See .dev/adr/0007.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:StartCodeInterpreterSession",
                         "bedrock-agentcore:InvokeCodeInterpreter",
                         "bedrock-agentcore:GetCodeInterpreterSession",
                         "bedrock-agentcore:StopCodeInterpreterSession"],
                # rt_prefix, not prefix: AgentCore resource names use underscores, so the
                # hyphenated form matches nothing and every call is AccessDenied.
                resources=[f"arn:aws:bedrock-agentcore:{region}:{account}:*code-interpreter*/{rt_prefix}_*"],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                # TagResource because creating an Access Point tags it with its actor, and
                # a missing tag permission fails the create on the tag rather than the create.
                actions=["s3files:CreateAccessPoint", "s3files:GetAccessPoint",
                         "s3files:TagResource", "s3files:ListAccessPoints"],
                # Access Point ids are unknown until created; the file system is not, so the
                # blast radius is this project's file system rather than the account's.
                resources=["*"],
            )
        )
        # Mounting itself. Without these the session fails to start with "S3 Files mount
        # failed: access denied" (measured). Both name sets are needed: S3 Files runs on the
        # EFS control plane, so the elasticfilesystem aliases are what some paths check.
        # The file system's own resource policy refuses any mount that names no Access Point,
        # so these permissions cannot reach the file system root.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3files:ClientMount", "s3files:ClientWrite",
                         "elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite",
                         "elasticfilesystem:DescribeMountTargets"],
                resources=[f"arn:aws:s3files:{region}:{account}:file-system/*",
                           f"arn:aws:elasticfilesystem:{region}:{account}:file-system/*"],
            )
        )

        # ECR pull (agent image lives in the CDK assets repo).
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                    "ecr:BatchCheckLayerAvailability",
                ],
                resources=[
                    f"arn:aws:ecr:{region}:{account}:repository/cdk-*",
                    f"arn:aws:ecr:{region}:{account}:repository/{prefix}-*",
                    # Starter Toolkit pushes to repos named bedrock-agentcore-<agent>
                    f"arn:aws:ecr:{region}:{account}:repository/bedrock-agentcore-*",
                ],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )

        # --- Per-user files. Reached through the sandbox's mount, never with this role:
        # the agent deliberately holds no S3 permission on this bucket. Access is granted
        # only to the S3 Files file system role (scoped to users/*) and, per session, to the
        # one Access Point that session mounts — see .dev/adr/0007. An earlier version
        # granted this role read/write on the whole bucket, which handed every session's
        # code every user's files.
        self.user_files_bucket = s3.Bucket(
            self,
            "UserFilesBucket",
            bucket_name=f"{prefix}-user-files-{account}-{region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            # Required by S3 Files: creating a file system over a bucket without
            # versioning fails with "Your bucket must have versioning enabled".
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,  # PoC
            auto_delete_objects=True,
            # Versioning is mandatory for S3 Files, and it makes capacity grow from four
            # separate directions — a mounted filesystem overwrites constantly (an
            # appended jsonl, a rewritten cache), so each needs its own rule.
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-current-and-old-versions",
                    expiration=Duration.days(365),
                    # 1. Every overwrite and delete retains the previous version.
                    noncurrent_version_expiration=Duration.days(30),
                    # 2. Orphaned multipart parts are billed and do not show up in a
                    #    listing, which is what makes them easy to miss.
                    abort_incomplete_multipart_upload_after=Duration.days(7)),
                # 3. Deleting a versioned object leaves a delete marker behind. AWS
                #    rejects ExpiredObjectDeleteMarker in the same rule as a Days
                #    expiration, hence a second rule.
                s3.LifecycleRule(id="clean-expired-delete-markers",
                                 expired_object_delete_marker=True),
            ],
        )

        # --- Conversation checkpoints (LangGraph graph state) -----------------
        # One table for all users; isolation is the partition key, derived from thread_id.
        # Kept separate from the router's identity table for privilege, not layout.
        # Why not per-user tables, and the quota that rules them out: .dev/adr/0008.
        self.checkpoint_table = dynamodb.Table(
            self,
            "CheckpointTable",
            table_name=f"{prefix}-checkpoints",
            # PK/SK/ttl are all fixed by DynamoDBSaver, which writes those literal names.
            partition_key=dynamodb.Attribute(
                name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(
                name="SK", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="ttl",
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,  # PoC
        )

        # State over ~350 KB spills to S3, leaving only a reference in the item, because
        # DynamoDB caps an item at 400 KB and one thread per user grows without bound.
        # A dedicated bucket, not the user-files one: that bucket grants this role nothing
        # by design (.dev/adr/0007), its mandatory versioning would retain a copy of every
        # superstep overwrite, and its 365-day expiry would strip a payload out from under
        # a live reference.
        self.checkpoint_bucket = s3.Bucket(
            self,
            "CheckpointBucket",
            bucket_name=f"{prefix}-checkpoints-{account}-{region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,  # PoC
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-after-checkpoint-ttl",
                    # Deliberately longer than the table's TTL: a payload outliving its
                    # reference wastes storage, the reverse breaks a checkpoint.
                    expiration=Duration.days(400),
                    abort_incomplete_multipart_upload_after=Duration.days(7)),
            ],
        )

        # No enumeration. Isolation here is the partition key, not IAM — the role can reach
        # every thread — so removing Scan and List is what stops "read everyone" from being
        # one call. thread_id is sha256 of the actor and Lark open_ids live in the router's
        # table, which this role cannot read, so without enumeration an attacker must
        # already know whose conversation to ask for. See .dev/adr/0008.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query",
                    "dynamodb:PutItem", "dynamodb:BatchWriteItem", "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem", "dynamodb:DescribeTable",
                ],
                resources=[self.checkpoint_table.table_arn],
            )
        )
        # Exact keys only, under the one prefix the saver writes. DynamoDBSaver also tries to
        # set a lifecycle policy at startup and logs "Failed to configure S3 lifecycle:
        # AccessDenied" — leave it: the stack's own rule outlives the table TTL on purpose,
        # and a container running model output should not be able to rewrite it.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                resources=[f"{self.checkpoint_bucket.bucket_arn}/state/*"],
            )
        )

        # --- Container image (ARM64) ------------------------------------------
        # Built from ./agent. deploy.sh reads this URI to create/update the runtime.
        self.agent_image = ecr_assets.DockerImageAsset(
            self,
            "AgentImage",
            directory="agent",
            platform=ecr_assets.Platform.LINUX_ARM64,
        )

        # --- Runtime ARN derived from context (populated by deploy.sh) --------
        runtime_id = self.node.try_get_context("runtime_id") or "PLACEHOLDER"
        self.runtime_arn = (
            f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}"
        )

        CfnOutput(self, "ExecutionRoleArn", value=self.execution_role.role_arn)
        CfnOutput(self, "AgentImageUri", value=self.agent_image.image_uri)
        CfnOutput(self, "UserFilesBucketName", value=self.user_files_bucket.bucket_name)
        CfnOutput(self, "CheckpointTableName", value=self.checkpoint_table.table_name)
        CfnOutput(self, "CheckpointBucketName", value=self.checkpoint_bucket.bucket_name)
