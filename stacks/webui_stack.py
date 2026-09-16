"""WebUI stack — the static web chat page on S3 behind CloudFront.

Nothing dynamic lives here. The page's two hops go straight to the router (to trade a Lark
h5 code for a user JWT) and to the AgentCore Runtime (AG-UI over SSE), so this stack owns
no compute and no state — only the bucket, the distribution, and the two values the page
needs injected at deploy time.

Off by default: Lark requires an h5 app registered against this distribution's domain, so
deploying it without doing that in the console produces a page nobody can use.
"""

from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    Stack,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
)
from constructs import Construct


class WebUiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        router_api_url: str,
        lark_app_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = self.node.try_get_context("resource_prefix") or "agentcore-fullstack"

        bucket = s3.Bucket(
            self,
            "WebUiBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,  # PoC
            auto_delete_objects=True,
        )

        # No public bucket policy: CloudFront reaches it through an origin access identity,
        # so the only way in is the distribution.
        self.distribution = cloudfront.Distribution(
            self,
            "WebUiDistribution",
            comment=f"{prefix} web chat",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                # The page is one file that carries deploy-time values, so caching it would
                # serve a stale router URL after a redeploy.
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            ),
        )

        # Injected rather than built: the page needs the router's URL and the Lark app id,
        # and a build step for two constants is not worth a toolchain.
        config = (f"window.ROUTER_BASE={router_api_url.rstrip('/')!r};"
                  f"window.LARK_APP_ID={lark_app_id!r};")
        s3deploy.BucketDeployment(
            self,
            "WebUiContent",
            sources=[
                s3deploy.Source.asset("webui"),
                s3deploy.Source.data("config.js", config),
            ],
            destination_bucket=bucket,
            distribution=self.distribution,
            distribution_paths=["/*"],
        )

        self.url = f"https://{self.distribution.distribution_domain_name}"
        CfnOutput(self, "WebUiUrl", value=self.url)
