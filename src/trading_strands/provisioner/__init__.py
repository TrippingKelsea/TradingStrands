"""BotProvisioner — enumerates active Strategy Agents and fans work out.

The v0 responsibility is narrow: walk the strategies table, find the
ACTIVE bots, and invoke the Self-Critique Lambda asynchronously per
(org_id, bot_id). The Self-Critique Lambda does the reflection work;
the provisioner only fans out.
"""

from trading_strands.provisioner.bot_provisioner import (
    InvocationResult,
    enumerate_active_bots,
    fan_out_self_critique,
    handler,
)

__all__ = [
    "InvocationResult",
    "enumerate_active_bots",
    "fan_out_self_critique",
    "handler",
]
