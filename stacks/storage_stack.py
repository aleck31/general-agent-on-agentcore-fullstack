"""Per-user file storage for code execution: S3 Files, mounted into the sandbox.

Only deployed when the `files_storage` context flag is true. **No NAT**: what sits in this
VPC is the Code Interpreter session, and it only has to reach S3, which a gateway endpoint
does for free. The agent Runtime stays PUBLIC. That is the whole difference from the shape
this replaced, where the Runtime itself had to mount NFS and therefore needed a route out
to Bedrock, Lark and a cross-region Gateway.

What isolation rests on (see .dev/adr/0007):
  - one Access Point per user, `rootDirectory` fixed server-side to /users/<actor>
  - the session mounts exactly one Access Point, enforced at the microVM boundary
Neither is agent code, which is the point — the agent runs model output.

The Access Points themselves are NOT created here: they are per-user, created on first use
and named in `StartCodeInterpreterSession`.
"""
from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_s3 as s3,
    aws_s3files as s3files,
)
from constructs import Construct


# NFS. The mount target listens here and nowhere else.
_NFS_PORT = 2049


class StorageStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *,
                 user_files_bucket: s3.IBucket, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        prefix = self.node.try_get_context("resource_prefix") or "agentcore-fullstack"

        # --- VPC: isolated subnets and no NAT. What lives in here is the Code Interpreter
        # session, and it only has to reach S3 — which a gateway endpoint does for free.
        # The agent Runtime stays PUBLIC and is unaffected; putting *it* in a VPC is what
        # used to force a NAT, because it must keep reaching Bedrock, Lark and a
        # cross-region Gateway. Two AZs because mount targets are per-AZ. See ADR 0007.
        self.vpc = ec2.Vpc(
            self, "Vpc",
            max_azs=2,
            nat_gateways=0,
            ip_addresses=ec2.IpAddresses.cidr("10.20.0.0/16"),
            subnet_configuration=[
                ec2.SubnetConfiguration(name="isolated", cidr_mask=24,
                                        subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            ],
            gateway_endpoints={
                "S3": ec2.GatewayVpcEndpointOptions(
                    service=ec2.GatewayVpcEndpointAwsService.S3),
            },
        )

        # The sandbox's own security group, and the mount targets' — separated so the file
        # system accepts NFS from the sandbox only, not from anything else in the VPC.
        self.sandbox_sg = ec2.SecurityGroup(
            self, "SandboxSg", vpc=self.vpc, allow_all_outbound=True,
            description="Code Interpreter session: S3 via the gateway endpoint, and the mount")
        self.mount_sg = ec2.SecurityGroup(
            self, "MountTargetSg", vpc=self.vpc, allow_all_outbound=False,
            description="S3 Files mount targets: NFS from the sandbox only")
        self.mount_sg.add_ingress_rule(
            peer=self.sandbox_sg, connection=ec2.Port.tcp(_NFS_PORT),
            description="NFS from the Code Interpreter session")

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
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnets):
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

        # The permissions for all of this — creating an Access Point and mounting one —
        # live on the agent's execution role in the agentcore stack, which is also the Code
        # Interpreter's execution role. Nothing here vends credentials. See .dev/adr/0007.

        CfnOutput(self, "FileSystemId", value=self.file_system.attr_file_system_id)
        CfnOutput(self, "FileSystemArn", value=self.file_system.ref)
        CfnOutput(self, "SandboxSecurityGroupId",
                  value=self.sandbox_sg.security_group_id)
        CfnOutput(self, "SandboxSubnetIds", value=",".join(
            s.subnet_id for s in self.vpc.select_subnets(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnets))
