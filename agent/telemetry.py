"""Agent-level metrics — the ones no platform metric can give.

What the platform already measures, and this must not duplicate: `AWS/Bedrock-AgentCore`
carries Invocations, Errors/UserErrors/SystemErrors, Latency, Sessions, ActiveSessionCount,
and — directly useful here — `ResourceAccessTokenFetchSuccess`/`Failures` with the
`ExceptionType`, which is the 3LO vault fetch that failed all through one evening.

What it cannot: a turn's outcome. `chat_async` returns 200 immediately and the turn runs on a
background thread, so a failed turn is a *successful* invocation as far as the platform can
see. Same for which tool ran, whether a user hit a consent wall, and the recoveries this
agent performs on itself.

Why PutMetricData and not OTel: measured — the container's MeterProvider is a real SDK
provider, but nothing exports it. `gen_ai.client.token.usage` has no datapoints at all in
this account since the agent moved off Strands, which configured its own exporter. A counter
that silently goes nowhere is worse than no counter, so this takes the certain path.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import boto3

log = logging.getLogger("agent.telemetry")

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_NAMESPACE = os.environ.get("METRICS_NAMESPACE", "agentcore-fullstack/agent")
# Batched because PutMetricData is billed per call, not per datum, and a turn emits several.
_FLUSH_AFTER = int(os.environ.get("METRICS_FLUSH_AFTER", "20"))
_FLUSH_SECONDS = float(os.environ.get("METRICS_FLUSH_SECONDS", "30"))

_cw = None
_buffer: list[dict] = []
_lock = threading.Lock()
_last_flush = time.monotonic()


def _client():
    global _cw
    if _cw is None:
        _cw = boto3.client("cloudwatch", region_name=_REGION)
    return _cw


def _put(name: str, value: float, unit: str, attrs: dict) -> None:
    """Buffer one datum, flushing on size or age. Never raises: a missing metric must not
    cost a turn, and a metrics outage must not look like an agent outage."""
    datum = {
        "MetricName": name, "Value": value, "Unit": unit,
        # Low cardinality on purpose. The `strands.*` series still in this account carry
        # `tool_use_id`, i.e. one time series per call, forever — useless for a dashboard.
        "Dimensions": [{"Name": k, "Value": str(v)[:200]}
                       for k, v in attrs.items() if v is not None],
    }
    with _lock:
        _buffer.append(datum)
        due = len(_buffer) >= _FLUSH_AFTER or (time.monotonic() - _last_flush) > _FLUSH_SECONDS
        batch = _buffer[:] if due else []
        if due:
            _buffer.clear()
    if batch:
        _flush(batch)


def _flush(batch: list[dict]) -> None:
    global _last_flush
    _last_flush = time.monotonic()
    try:
        _client().put_metric_data(Namespace=_NAMESPACE, MetricData=batch)
    except Exception:  # noqa: BLE001
        log.warning("could not publish %d metric datum(s)", len(batch), exc_info=True)


def flush() -> None:
    """Publish whatever is buffered. Called at the end of a turn so a container reclaimed
    while idle does not take the turn's own metrics with it."""
    with _lock:
        batch, _buffer[:] = _buffer[:], []
    if batch:
        _flush(batch)


def count(name: str, **attrs) -> None:
    _put(name, 1, "Count", attrs)


def observe_ms(name: str, value: float, **attrs) -> None:
    _put(name, value, "Milliseconds", attrs)


class timed:
    """Times a turn and records its outcome, including when it raises.

    `outcome` is the dimension that matters — "ok" is not the interesting value. An
    exception becomes its type name, so a ValidationException (the failure that wedged a live
    thread here) is a visible time series rather than a line in a log nobody reads."""

    def __init__(self, name: str, **attrs) -> None:
        self.name, self.attrs = name, attrs

    def __enter__(self):
        self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        outcome = "ok" if exc_type is None else exc_type.__name__
        observe_ms(self.name + ".duration", (time.monotonic() - self._t0) * 1000,
                   outcome=outcome, **self.attrs)
        count(self.name, outcome=outcome, **self.attrs)
        flush()
        return False        # never swallow
