"""CloudWatch Embedded Metric Format (EMF) emitter.

CloudWatch Logs interprets specially-formatted JSON log lines as metrics,
extracting the named fields into the metrics namespace. This gives us a
metrics pipeline with no new infrastructure — emitted as regular stdout
log lines and CloudWatch handles the rest.

Spec reference: docs/SPEC/observability.md picks this over Prometheus
because ephemeral Lambdas + per-Agent Fargate tasks don't have scrape
targets that Prometheus can discover, and EMF preserves metrics even
after the emitter dies.

Usage:

    from trading_strands.emf import emit_metric

    emit_metric(
        namespace="TradingStrands",
        name="agent.decision.latency_ms",
        value=123.4,
        unit="Milliseconds",
        dimensions={"agent_id": "bot-1", "org_id": "org-a"},
    )

The emitter writes to stdout. In Fargate + Lambda, stdout goes to the
task's CloudWatch log group, where EMF parsing happens automatically.
Locally, it just prints structured JSON.
"""

from trading_strands.emf.emitter import emit_metric, timed_metric

__all__ = ["emit_metric", "timed_metric"]
