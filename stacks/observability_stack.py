"""Observability stack — CloudWatch dashboard + alarms.

Two metric sources, and knowing which is which matters. **AgentCore emits**
`gen_ai.client.token.usage` and `gen_ai.client.operation.duration` into the
`bedrock-agentcore` namespace — cost and model latency, and nothing else. **The agent emits**
its own counters through the same OTel pipeline (`agent/telemetry.py`): whether a turn
succeeded, which tool ran, whether a user hit a consent wall. Those are not platform facts
and nothing would measure them otherwise, which is why every failure in this project used to
be found by a human reading a bad reply.

Ignore the `strands.*` series still in the account: they are from the previous framework and
carry `tool_use_id` as a dimension, i.e. one time series per call forever.

Deployed last and depends on nothing — metrics are referenced by namespace, not by resource.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    aws_cloudwatch as cw,
)
from constructs import Construct


class ObservabilityStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prefix = self.node.try_get_context("resource_prefix") or "agentcore-fullstack"

        def lambda_metric(fn_name: str, metric: str, stat: str = "Sum") -> cw.Metric:
            return cw.Metric(
                namespace="AWS/Lambda", metric_name=metric,
                dimensions_map={"FunctionName": fn_name},
                statistic=stat, period=Duration.minutes(5),
            )

        functions = [f"{prefix}-router"]

        def agent_metric(name: str, stat: str = "Sum", **dims) -> cw.Metric:
            """One of the agent's own counters, published with PutMetricData — measured: the
            container's OTel metric pipeline exports nothing, so `gen_ai.*` has had no
            datapoints since the agent moved off Strands."""
            return cw.Metric(
                namespace=f"{prefix}/agent", metric_name=name,
                dimensions_map={k: v for k, v in dims.items()},
                statistic=stat, period=Duration.minutes(5),
            )

        def platform(name: str, stat: str = "Sum", **dims) -> cw.Metric:
            """AgentCore's own. These are the ones with data, and they cover what the agent
            cannot see about itself: invocations, throttles, session counts, vault fetches."""
            return cw.Metric(
                namespace="AWS/Bedrock-AgentCore", metric_name=name,
                dimensions_map={k: v for k, v in dims.items()},
                statistic=stat, period=Duration.minutes(5),
            )

        dashboard = cw.Dashboard(self, "Dashboard", dashboard_name=f"{prefix}-ops")
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Lambda errors",
                left=[lambda_metric(f, "Errors") for f in functions],
                width=12,
            ),
            cw.GraphWidget(
                title="Lambda duration (p95, ms)",
                left=[lambda_metric(f, "Duration", "p95") for f in functions],
                width=12,
            ),
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Lambda invocations",
                left=[lambda_metric(f, "Invocations") for f in functions],
                width=24,
            ),
        )
        # The agent's own outcomes. "ok" is not the interesting series — the others are.
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Turns by outcome (agent.turn)",
                left=[agent_metric("agent.turn", outcome=o)
                      for o in ("ok", "ValidationException", "ReadTimeoutError")],
                width=12,
            ),
            cw.GraphWidget(
                title="Turn duration p95 (ms)",
                left=[agent_metric("agent.turn.duration", stat="p95", outcome="ok")],
                width=12,
            ),
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                # Whether a tool actually ran is the one thing a plausible reply cannot show.
                title="Tool calls (agent.tool_call)",
                left=[agent_metric("agent.tool_call", tool=t)
                      for t in ("run_code", "run_command", "list_files")],
                width=8,
            ),
            cw.GraphWidget(
                title="Identity & recovery",
                left=[agent_metric("agent.auth_wall"),
                      agent_metric("agent.identity_refused"),
                      agent_metric("agent.interrupted_turn_repaired"),
                      agent_metric("agent.code_session_restarted"),
                      agent_metric("agent.steer_injected")],
                width=8,
            ),
            cw.GraphWidget(
                # The platform's view of the 3LO vault, with the exception type — the exact
                # signal that was missing while consent silently stopped working here.
                title="Token vault fetches (platform)",
                left=[platform("ResourceAccessTokenFetchSuccess"),
                      platform("ResourceAccessTokenFetchFailures")],
                width=8,
            ),
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Runtime invocations & errors (platform)",
                left=[platform("Invocations"), platform("UserErrors"),
                      platform("SystemErrors"), platform("Throttles")],
                width=12,
            ),
            cw.GraphWidget(
                title="Sessions in flight (platform)",
                left=[platform("ActiveSessionCount", stat="Maximum")],
                right=[platform("Latency", stat="p95")],
                width=12,
            ),
        )

        # Router errors alarm (webhook path) — the one most worth paging on.
        self.router_error_alarm = cw.Alarm(
            self, "RouterErrorAlarm",
            alarm_name=f"{prefix}-router-errors",
            metric=lambda_metric(f"{prefix}-router", "Errors"),
            threshold=5,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )

        # A turn that fails is invisible to the user until they read the reply, and the
        # failure that wedged a thread here (Bedrock rejecting a dangling toolUse) repeats on
        # every later turn — so one breach is worth knowing about, not five.
        self.turn_failure_alarm = cw.Alarm(
            self, "TurnFailureAlarm",
            alarm_name=f"{prefix}-agent-turn-failures",
            metric=cw.MathExpression(
                expression="FILL(failed, 0)",
                using_metrics={"failed": agent_metric(
                    "agent.turn", outcome="ValidationException")},
                period=Duration.minutes(5), label="failed turns"),
            threshold=1, evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        # A user being asked to authorise is normal; a burst is the 3LO path breaking, which
        # went unnoticed for hours here. The platform's own counter is the better signal —
        # it sees the vault call itself, not the agent's interpretation of it.
        self.vault_failure_alarm = cw.Alarm(
            self, "VaultFetchFailureAlarm",
            alarm_name=f"{prefix}-vault-fetch-failures",
            metric=platform("ResourceAccessTokenFetchFailures"),
            threshold=5, evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )

        CfnOutput(self, "DashboardName", value=dashboard.dashboard_name)
