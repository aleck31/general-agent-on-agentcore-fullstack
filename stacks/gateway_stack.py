"""Gateway stack — IAM service role for the Web Search Gateway.

The Gateway itself is created by scripts/provision.sh (`create-gateway`, us-east-1) with
the Web Search connector as its only target. Search carries no user identity, so the
target uses GATEWAY_IAM_ROLE; per-user tools bypass the Gateway (see docs/agentcore-behavior.md).
"""

from aws_cdk import (
    CfnOutput,
    Stack,
    aws_iam as iam,
)
from constructs import Construct


class GatewayStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        prefix = self.node.try_get_context("resource_prefix") or "agentcore-fullstack"
        agentcore_principal = iam.ServicePrincipal("bedrock-agentcore.amazonaws.com")

        # Gateway service role — what the Web Search target calls the connector as.
        self.gateway_role = iam.Role(
            self, "GatewayRole",
            role_name=f"{prefix}-gateway-role-{region}",
            assumed_by=agentcore_principal,
        )

        # Web Search connector. Its gateway lives in us-east-1 — the only region
        # offering the connector — while this role is global, so both regions are
        # allowed here. InvokeWebSearch targets a service-owned ARN: the account
        # segment is the literal "aws", not this account.
        account = Stack.of(self).account
        ws_region = "us-east-1"
        self.gateway_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeGateway"],
                resources=[
                    f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/*",
                    f"arn:aws:bedrock-agentcore:{ws_region}:{account}:gateway/*",
                ],
            )
        )
        self.gateway_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeWebSearch"],
                resources=[f"arn:aws:bedrock-agentcore:{ws_region}:aws:tool/web-search.v1"],
            )
        )

        CfnOutput(self, "GatewayRoleArn", value=self.gateway_role.role_arn)
