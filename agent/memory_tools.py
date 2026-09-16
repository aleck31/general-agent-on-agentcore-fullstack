"""Long-term memory as two explicit tools, not as a by-product of every turn.

The conversation itself is graph state in DynamoDB and needs nothing from here (see
.dev/adr/0008). What this adds is the ability to carry a fact *across* threads — the kind
of thing a user says once and expects to hold next week.

Why tools rather than automatic extraction:

  - Cost sits on retrieval, not writes. AgentCore Memory bills ~$0.50 per 1,000 record
    retrievals against ~$0.25 per 1,000 event writes, so a blind topK on every turn is the
    expensive pattern. As a tool, the model queries only when the question warrants it.
  - Automatic extraction would require feeding whole transcripts into Memory to be mined,
    which is the coupling ADR 0008 removed: it makes "what gets stored" a by-product
    instead of a decision.
  - Direct writes need no strategy at all. Measured: BatchCreateMemoryRecords accepts a
    record with `memoryStrategyId` omitted, and RetrieveMemoryRecords still scores it
    semantically — the same score as the identical record under a semantic or summary
    strategy. So the Memory resource carries no strategy, no extraction model, and no
    execution role.

`actor_id` is bound when the tools are built, never taken as an argument. The namespace is
`/facts/{actor_id}`, so one user's records are unreachable from another's session even if
the model is talked into asking for them.
"""

from __future__ import annotations

import datetime
import logging
import os
import uuid

import boto3
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

log = logging.getLogger("agent.memory")

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_MEMORY_ID = os.environ.get("BEDROCK_AGENTCORE_MEMORY_ID", "")
# Retrieval is the priced operation, so the ceiling is deliberately low.
_TOP_K = int(os.environ.get("MEMORY_RECALL_TOP_K", "5"))


def available() -> bool:
    return bool(_MEMORY_ID)


class _RememberArgs(BaseModel):
    fact: str = Field(description="One self-contained fact about the user, in their "
                                 "language, phrased so it still makes sense weeks later.")


class _RecallArgs(BaseModel):
    query: str = Field(description="What to look up, phrased as the question you want "
                                   "answered rather than as keywords.")


def tools_for(actor_id: str) -> list:
    """The remember/recall pair bound to this user, or [] when no Memory is deployed.

    Absent-not-broken: without a Memory resource the agent simply has no long-term recall,
    the same way it has no Lark tools when the vault is empty."""
    if not available():
        return []

    namespace = f"/facts/{actor_id}"
    client = boto3.client("bedrock-agentcore", region_name=_REGION)

    def remember(fact: str) -> str:
        try:
            r = client.batch_create_memory_records(
                memoryId=_MEMORY_ID,
                records=[{
                    "content": {"text": fact},
                    "namespaces": [namespace],
                    # Per-record idempotency key; the service correlates successes and
                    # failures by it.
                    "requestIdentifier": str(uuid.uuid4()),
                    "timestamp": datetime.datetime.now(datetime.timezone.utc),
                }])
            failed = r.get("failedRecords") or []
            if failed:
                log.warning("remember rejected for %s: %s", actor_id, failed[:1])
                return "I could not save that."
            return "Saved."
        except Exception as e:  # noqa: BLE001 — a failed write must not kill the turn
            log.exception("remember failed for %s", actor_id)
            return f"I could not save that ({type(e).__name__})."

    def recall(query: str) -> str:
        try:
            r = client.retrieve_memory_records(
                memoryId=_MEMORY_ID, namespace=namespace,
                searchCriteria={"searchQuery": query, "topK": _TOP_K})
        except Exception as e:  # noqa: BLE001
            log.exception("recall failed for %s", actor_id)
            return f"I could not check my notes ({type(e).__name__})."
        hits = r.get("memoryRecordSummaries") or []
        if not hits:
            return "Nothing on record about that."
        return "\n".join(
            f"- {(h.get('content') or {}).get('text', '')}" for h in hits)

    return [
        StructuredTool.from_function(
            func=remember, name="remember",
            description=(
                "Store one durable fact about this user for future conversations. Use it "
                "when they tell you something that should outlast this chat — a "
                "preference, a name, an ongoing project. Do not use it for things only "
                "relevant to the current exchange; the conversation already remembers "
                "itself."),
            args_schema=_RememberArgs),
        StructuredTool.from_function(
            func=recall, name="recall",
            description=(
                "Look up what you previously stored about this user. Use it when the "
                "answer may depend on something said in an earlier conversation, not for "
                "anything visible in the messages above."),
            args_schema=_RecallArgs),
    ]
