"""Emit EMF metric lines to stdout.

EMF spec: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/
CloudWatch_Embedded_Metric_Format_Specification.html

The essential shape:

{
  "_aws": {
    "Timestamp": <ms>,
    "CloudWatchMetrics": [
      {
        "Namespace": "...",
        "Dimensions": [["d1", "d2"]],
        "Metrics": [{"Name": "...", "Unit": "..."}]
      }
    ]
  },
  "d1": "v1",
  "d2": "v2",
  "metric_name": 123.4,
  ...any additional context fields (not metrics, searchable in Logs)
}

Design choices in this emitter:

- Single metric per emission. Could batch multiple metrics in one line,
  but the callsite clarity win of one-metric-one-call outweighs the
  cost of a few extra log lines at our volume.
- Stdout, not the CloudWatch Logs API. Fargate + Lambda route stdout to
  CloudWatch automatically. Calling the PutLogEvents API from every
  metric would add latency + throttling concerns.
- Dimensions are required. CloudWatch indexes by dimension; metrics
  without meaningful dimensions aggregate into one meaningless number.
  We force callers to pass at least one.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from collections.abc import Iterator
from typing import Any

DEFAULT_NAMESPACE = "TradingStrands"


def emit_metric(
    name: str,
    value: float,
    unit: str = "None",
    dimensions: dict[str, str] | None = None,
    namespace: str = DEFAULT_NAMESPACE,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit a single EMF metric line.

    CloudWatch valid units (partial list — full list in EMF spec):
      Seconds | Microseconds | Milliseconds |
      Bytes | Kilobytes | Megabytes |
      Count | Percent | None

    `dimensions` values are strings by spec. Non-string values are
    coerced via str() to avoid surprising log failures on int/bool.
    `extra` contains searchable-but-not-metric context (trace_id,
    correlation ids, error messages etc.).
    """

    if not dimensions:
        msg = "EMF metrics require at least one dimension"
        raise ValueError(msg)

    dim_names = list(dimensions.keys())
    record: dict[str, Any] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": namespace,
                "Dimensions": [dim_names],
                "Metrics": [{"Name": name, "Unit": unit}],
            }],
        },
        name: value,
    }
    for k, v in dimensions.items():
        record[k] = str(v)
    if extra:
        for k, v in extra.items():
            if k in record:
                continue  # never let extra shadow the metric or dimensions
            record[k] = v

    # stdout, one line, no trailing flush wait — CloudWatch picks up on
    # the next log flush anyway.
    print(json.dumps(record, separators=(",", ":")), file=sys.stdout, flush=False)


@contextlib.contextmanager
def timed_metric(
    name: str,
    dimensions: dict[str, str],
    namespace: str = DEFAULT_NAMESPACE,
    extra: dict[str, Any] | None = None,
) -> Iterator[None]:
    """Context manager that emits a Milliseconds metric for elapsed time.

    Example:

        with timed_metric("agent.decision.latency_ms",
                          {"agent_id": bot_id, "org_id": org_id}):
            result = await agent.invoke_async(...)

    Emits regardless of whether the block raises — failures still have
    interesting latency data (a slow failure is different from a fast
    one). If you need to distinguish success vs error, add a status
    field via `extra` (or emit a separate error-count metric).
    """

    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        emit_metric(
            name, elapsed_ms, unit="Milliseconds",
            dimensions=dimensions, namespace=namespace, extra=extra,
        )
