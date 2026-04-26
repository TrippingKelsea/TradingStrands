"""Self-Critique Agent — weekend reflection on recorded live data.

This is the "weekend mode" referenced in CLAUDE.md. Distinct from
backtesting: the agent reads what actually happened in the past week
(memory files + ledger event log + recorded market data) and produces a
reflection. It does NOT simulate phantom trades.

Output: a Markdown report written to the Strategy Agent's own
lessons.md via append. Orgadmins and auditors read it the same way they
read any other lesson entry. The agent never mutates its own strategy
prompt — that's a human decision.

Deployed as a weekly Lambda (Saturday early morning UTC), one invocation
per active strategy. Each invocation is scoped to one bot_id and runs
in that bot's IAM context (STS assume-role pattern).

In v0, the Lambda is driven from a local CLI / test harness; the
EventBridge schedule and the assume-role operator lambda come in Gate B.
"""

from trading_strands.self_critique.runner import (
    SelfCritiqueReport,
    run_self_critique,
)

__all__ = ["SelfCritiqueReport", "run_self_critique"]
