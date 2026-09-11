"""Persistent per-user file storage: S3 Files + the credential broker.

Only deployed when the `files_storage` context flag is true, because it is the one part
of this project with a fixed monthly cost: a NAT Gateway. That is unavoidable rather
than careless — the mount is NFS, its endpoint is a mount target (an ENI with a private
address), so the agent Runtime has to be inside this VPC, and once it is, its outbound
traffic to Bedrock and to Lark's public API needs a route out. Interface endpoints for
the AWS services would not help: seven of them cost more than one NAT.

What isolation rests on (see .dev/adr/0007):
  - one Access Point per user, `rootDirectory` fixed server-side to /users/<actor>
  - credentials scoped by an STS session policy to that one Access Point
Neither is agent code, which is the point — the agent runs model output.

The Access Points themselves are NOT created here. They are per-user and created on
first use by the broker, because `filesystemConfigurations` is Runtime-scoped and
therefore cannot carry a per-user mount.
"""

from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_s3files as s3files,
)
from constructs import Construct

from . import lambda_asset, retention_days

# NFS. The mount target listens here and nowhere else.
_NFS_PORT = 2049


class StorageStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *,
                 user_files_bucket: s3.IBucket, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        prefix = self.node.try_get_context("resource_prefix") or "agentcore-fullstack"
        log_days = int(self.node.try_get_context("cloudwatch_log_retention_days") or 30)

        # --- VPC: two AZs because mount targets are per-AZ, one NAT because two would
        # double the only fixed cost here. The trade is that an AZ outage takes egress
        # with it, which a sample can live with.
        self.vpc = ec2.Vpc(
            self, "Vpc",
            max_azs=2,
            nat_gateways=1,
            ip_addresses=ec2.IpAddresses.cidr("10.20.0.0/16"),
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC,
                                        cidr_mask=24),
                ec2.SubnetConfiguration(name="private", cidr_mask=24,
                                        subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            ],
        )

        # The Runtime's own security group, and the mount targets' — separated so the
        # file system only accepts NFS from the agent, not from anything else in the VPC.
        self.runtime_sg = ec2.SecurityGroup(
            self, "RuntimeSg", vpc=self.vpc, allow_all_outbound=True,
            description="AgentCore Runtime: outbound to Bedrock, Lark and the mount")
        self.mount_sg = ec2.SecurityGroup(
            self, "MountTargetSg", vpc=self.vpc, allow_all_outbound=False,
            description="S3 Files mount targets: NFS from the Runtime only")
        self.mount_sg.add_ingress_rule(
            peer=self.runtime_sg, connection=ec2.Port.tcp(_NFS_PORT),
            description="NFS from the agent Runtime")

        # --- The file system over the user-files bucket, and the role it uses to move
        # data in and out of that bucket.
        # Trusted by elasticfilesystem, NOT s3files: S3 Files runs on the EFS control
        # plane (hence mount.s3files coming from amazon-efs-utils and the
        # elasticfilesystem:Client* aliases), and IAM rejects s3files.amazonaws.com as an
        # unknown principal. The conditions keep the trust to file systems in this
        # account and region, since the principal itself is a whole other service.
        self.fs_role = iam.Role(
            self, "FileSystemRole",
            assumed_by=iam.ServicePrincipal(
                "elasticfilesystem.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {
                        "aws:SourceArn":
                            f"arn:aws:s3files:{region}:{account}:file-system/*"},
                }),
            description="Lets S3 Files move data between the file system and the bucket")
        # Whole bucket, not just users/*: the file system spans the bucket, and each
        # user's confinement comes from their Access Point instead. This role is assumable
        # only by the service, never by the agent.
        #
        # Ported from the reference implementation rather than left to grant_read_write:
        # the service also needs ListBucketVersions, and it manages EventBridge rules for
        # bucket/file-system synchronisation under a fixed name prefix. The ResourceAccount
        # conditions keep the role from reaching a bucket in another account.
        user_files_bucket.grant_read_write(self.fs_role)
        self.fs_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:ListBucket", "s3:ListBucketVersions"],
            resources=[user_files_bucket.bucket_arn],
            conditions={"StringEquals": {"aws:ResourceAccount": account}}))
        self.fs_role.add_to_policy(iam.PolicyStatement(
            actions=["events:PutRule", "events:PutTargets", "events:DeleteRule",
                     "events:DisableRule", "events:EnableRule", "events:RemoveTargets"],
            resources=["arn:aws:events:*:*:rule/DO-NOT-DELETE-S3-Files*"],
            conditions={"StringEquals": {
                "events:ManagedBy": "elasticfilesystem.amazonaws.com"}}))
        self.fs_role.add_to_policy(iam.PolicyStatement(
            actions=["events:DescribeRule", "events:ListRules",
                     "events:ListRuleNamesByTarget", "events:ListTargetsByRule"],
            resources=["arn:aws:events:*:*:rule/*"]))

        # `prefix` is deliberately not set. The layout is decided in exactly one place —
        # each Access Point's rootDirectory (/users/<actor>) — and scoping the file
        # system to a prefix as well would stack the two, with semantics we cannot
        # confirm without deploying. One source of truth is worth more here.
        self.file_system = s3files.CfnFileSystem(
            self, "FileSystem",
            # The bucket ARN, not its name: CloudFormation validates this against
            # ^(arn:aws[a-zA-Z0-9-]*:s3:::.+)$ and rejects a bare name.
            bucket=user_files_bucket.bucket_arn,
            role_arn=self.fs_role.role_arn,
            # The bucket is ours and holds only what this agent wrote, so the warning
            # about pointing a file system at an existing bucket is expected.
            accept_bucket_warning=True,
        )
        # A resource-policy Deny that no identity policy can override: mounting is refused
        # whenever s3files:AccessPointArn is absent, i.e. nobody may mount the file system
        # root. The STS session policy already scopes each session's credentials to one
        # Access Point; this closes the root escape structurally, so a later mistake in an
        # identity policy cannot reopen it. Ported from the reference implementation.
        s3files.CfnFileSystemPolicy(
            self, "FileSystemPolicy",
            file_system_id=self.file_system.ref,
            policy={
                "Version": "2012-10-17",
                "Statement": [{
                    "Sid": "DenyMountWithoutAccessPoint",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": ["s3files:ClientMount", "s3files:ClientWrite",
                               "s3files:ClientRootAccess"],
                    "Condition": {"Null": {"s3files:AccessPointArn": "true"}},
                }],
            },
        )

        for i, subnet in enumerate(self.vpc.select_subnets(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnets):
            s3files.CfnMountTarget(
                self, f"MountTarget{i}",
                # .ref (the ARN) on purpose: the mount target accepts it, and switching
                # to the bare id would replace the resource — which S3 Files rejects,
                # because it allows only one mount target per availability zone and
                # CloudFormation creates the replacement before deleting the original.
                file_system_id=self.file_system.ref,
                subnet_id=subnet.subnet_id,
                security_groups=[self.mount_sg.security_group_id],
            )

        # --- Ticket signing key. Asymmetric on purpose: the router signs and the broker
        # only verifies, so nothing that can verify can also mint.
        self.ticket_key = kms.Key(
            self, "TicketKey",
            key_spec=kms.KeySpec.ECC_NIST_P256,
            key_usage=kms.KeyUsage.SIGN_VERIFY,
            alias=f"{prefix}-mount-ticket",
            description="Signs per-user mount tickets",
            removal_policy=RemovalPolicy.DESTROY,  # PoC
        )

        # --- The broker. Bundles its own boto3: the `s3files` client is new enough that
        # the Lambda runtime's built-in SDK cannot be assumed to know the service.
        # Created before the mount role so that role can trust this function's role
        # specifically; MOUNT_ROLE_ARN is added to the environment afterwards, which is
        # what keeps the two from referring to each other in a cycle.
        self.broker = _lambda.Function(
            self, "BrokerFn",
            function_name=f"{prefix}-mount-broker",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=lambda_asset("lambda/broker"),
            timeout=Duration.seconds(30),
            memory_size=256,
            log_retention=retention_days(log_days),
            environment={
                # attr_file_system_id, not .ref: Ref on this resource returns the ARN,
                # and both the mount command and the access-point ARN need the fs-… id.
                "FILE_SYSTEM_ID": self.file_system.attr_file_system_id,
                "KMS_KEY_ID": self.ticket_key.key_arn,
                "ACCOUNT_ID": account,
            },
        )
        self.ticket_key.grant_verify(self.broker)

        # --- The role the broker vends, always with a session policy narrowing it to one
        # Access Point.
        #
        # Its own permissions are unconditioned, so whoever can assume it without passing
        # a session policy holds mount rights to every Access Point. That makes the trust
        # policy the real gate, which is why it names the broker's role rather than the
        # account: account-root trust would extend this to any principal that happens to
        # carry a wildcard sts:AssumeRole.
        self.mount_role = iam.Role(
            self, "MountRole",
            role_name=f"{prefix}-mount-role-{region}",
            assumed_by=iam.ArnPrincipal(self.broker.role.role_arn),
            max_session_duration=Duration.hours(12),
            description="Assumed by the mount broker only; scoped per call to one AP")
        self.mount_role.add_to_policy(iam.PolicyStatement(
            actions=["s3files:ClientMount", "s3files:ClientWrite",
                     "elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite",
                     "elasticfilesystem:DescribeMountTargets"],
            resources=["*"],
        ))
        self.broker.add_environment("MOUNT_ROLE_ARN", self.mount_role.role_arn)
        self.mount_role.grant_assume_role(self.broker.grant_principal)
        self.broker.add_to_role_policy(iam.PolicyStatement(
            # TagResource is required because create_access_point tags the Access Point
            # with its actor — without it the create fails with AccessDenied on the tag,
            # not on the create (matching the reference implementation's policy).
            actions=["s3files:CreateAccessPoint", "s3files:GetAccessPoint",
                     "s3files:TagResource",
                     "s3files:ListAccessPoints", "s3files:DeleteAccessPoint"],
            resources=["*"],  # Access Point ids are not known until they are created.
        ))

        CfnOutput(self, "FileSystemId", value=self.file_system.attr_file_system_id)
        CfnOutput(self, "TicketKeyArn", value=self.ticket_key.key_arn)
        CfnOutput(self, "BrokerFunctionName", value=self.broker.function_name)
        CfnOutput(self, "RuntimeSecurityGroupId",
                  value=self.runtime_sg.security_group_id)
        CfnOutput(self, "RuntimeSubnetIds", value=",".join(
            s.subnet_id for s in self.vpc.select_subnets(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnets))
