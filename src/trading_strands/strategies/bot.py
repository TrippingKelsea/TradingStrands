"""Strategy bot — Strands agent wrapper for LLM-driven trade decisions (§5.2).

Each strategy bot:
- Holds its strategy prompt and compiled IR schema
- Owns its ledger
- Emits trade intents when woken by the orchestrator
- Can be halted by the risk manager
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import structlog
from pydantic import BaseModel
from strands import Agent

from trading_strands.coordinator.types import IntentAction, TradeIntent
from trading_strands.emf.emitter import emit_metric, timed_metric
from trading_strands.ir.tta import Predicate
from trading_strands.ledger.models import Ledger
from trading_strands.token_telemetry.record import record_from_result

logger = structlog.get_logger()


class BotDecision(BaseModel):
    """Structured output from the strategy bot's LLM decision."""

    action: str  # buy | sell | close | hold (maintain) | noop (stand_down)
    symbol: str
    quantity: str  # string to avoid float precision issues
    rationale: str


_DECISION_PROMPT_TEMPLATE = """\
You are a trading strategy bot. Your job is to analyze the current market \
conditions and your portfolio state, then decide what action to take.

## Your Strategy
{strategy_prompt}

## Current Market Data
{market_data}

## Calendar (today + tomorrow)
{calendar_context}

## Technical Indicators
{ta_context}

## Your Portfolio
{portfolio_state}

## Recent Decisions
{recent_decisions}

## Instructions
Based on your strategy rules and the current conditions, decide your next action.
- If conditions warrant a trade, specify the action (buy/sell/close), symbol, and quantity.
- If you have an open position and are deliberately choosing to keep it as-is
  (maintain), respond with action "hold". Hold is an ACTIVE decision: you looked
  at the position, the market, and the signal, and concluded the current shape
  is still right. A rationale is required.
- If you have no position to act on AND no signal worth trading, respond with
  action "noop" (stand down). Noop is the correct answer when nothing is
  actionable — do not use "hold" in that case, it misleads downstream review.
- Always provide a clear rationale explaining your reasoning, even for hold/noop.
- Quantity should be a whole number of shares or options.
- If the strategy contains a list of trade symbols you are only permitted to trade those symbols.
- If the strategy does not contain a list of trade symbols, you will need to select them yourself.

Action→signal mapping (for reference — use whichever action matches your
intent; the signal framing is how downstream review interprets it):
  BUY  → open_long, open_short (via long puts), add_to_long, add_to_short
  SELL → close_long, close_short, open_short
         (a bearish thesis opened via long puts is BUY, not SELL —
          SELL is for closing existing longs or opening shorts)
  HOLD → maintain
  NOOP → stand_down
"""


def _format_market_data(prices: dict[str, Decimal]) -> str:
    if not prices:
        return "No market data available."
    lines = [f"  {symbol}: ${price}" for symbol, price in sorted(prices.items())]
    return "\n".join(lines)


def _format_portfolio(ledger: Ledger) -> str:
    lines = [
        f"Starting capital: ${ledger.starting_capital}",
        f"Equity: ${ledger.equity}",
        f"Realized PnL: ${ledger.realized_pnl}",
        f"High water mark: ${ledger.high_water_mark}",
        f"Drawdown: {ledger.drawdown_pct:.2%}",
    ]
    if ledger.open_positions:
        lines.append("Open positions:")
        for pos in ledger.open_positions:
            lines.append(
                f"  {pos.symbol}: {pos.quantity} shares @ "
                f"${pos.burdened_cost_basis} cost basis"
            )
    else:
        lines.append("No open positions.")
    return "\n".join(lines)


def _map_action(action_str: str) -> IntentAction:
    """Parse the LLM's action string into an IntentAction.

    Unknown / garbled / empty strings default to NOOP, not HOLD. A
    bot that can't make itself understood shouldn't be inferred as
    "deliberately maintaining a position"; it should be treated as
    stand-down. This is the safer posture for the memory + self-
    critique review path — NOOP correctly flags "no actionable
    state this tick", whereas a silent HOLD would misrepresent
    confused output as deliberate holding.
    """

    mapping = {
        "buy": IntentAction.BUY,
        "sell": IntentAction.SELL,
        "close": IntentAction.CLOSE,
        "hold": IntentAction.HOLD,
        "noop": IntentAction.NOOP,
    }
    return mapping.get(action_str.lower(), IntentAction.NOOP)


def _format_memory_line(
    decision: BotDecision,
    prices: dict[str, Decimal],
    ledger: Ledger,
    now_gmtime: Any,
) -> str:
    """Format a single memory-append line.

    Module-level so tests can verify the line format + the anti-
    confabulation MARKETDATA# pointer without constructing a full
    StrategyBot (which pulls in Strands / Bedrock). `now_gmtime` is
    injected so tests can fix the timestamp.
    """

    ts_str = (
        f"{now_gmtime.tm_hour:02d}:{now_gmtime.tm_min:02d}:"
        f"{now_gmtime.tm_sec:02d}Z"
    )
    hourly_bucket = (
        f"{now_gmtime.tm_year:04d}{now_gmtime.tm_mon:02d}"
        f"{now_gmtime.tm_mday:02d}{now_gmtime.tm_hour:02d}"
    )
    price_ref = prices.get(decision.symbol, "?")
    marketdata_ref = f"[MARKETDATA#{decision.symbol}#{hourly_bucket}]"
    return (
        f"- {ts_str} {decision.action.upper()} {decision.symbol} "
        f"x{decision.quantity} @ ~{price_ref} {marketdata_ref} "
        f"(equity ${ledger.equity}). {decision.rationale}"
    )


def _build_ta_context(
    ta_store: Any | None,
    ta_enabled: bool,
    symbols: set[str],
    bot_id: str,
) -> str:
    """Render the TA indicator section for the decision prompt.

    Mirrors _build_calendar_context four-case logic: not wired /
    disabled / read-failed / enabled-and-producing. Same rationale:
    graceful degradation, module-level for testability without
    pulling in Strands.
    """

    if ta_store is None:
        return "(no TA wired)"
    if not ta_enabled:
        return "(TA disabled for this strategy)"
    try:
        from trading_strands.ta_snapshot.store import summarize_for_symbols
        return summarize_for_symbols(symbols=symbols, store=ta_store)
    except Exception:
        logger.exception("ta.read_failed", bot_id=bot_id)
        return "TA read failed."


def _build_calendar_context(
    calendar_store: Any | None,
    calendar_enabled: bool,
    symbols: set[str],
    bot_id: str,
) -> str:
    """Render the calendar section for the decision prompt.

    Four cases worth distinguishing so the LLM reasons correctly:
      - not wired at all → "(no calendar)"  (local dev / legacy)
      - wired but strategy opts out → "(calendar disabled)"
      - wired and enabled but store read fails → error placeholder
      - wired and enabled → today + tomorrow summary via the
        formatter (symbols filtered to the strategy's watch list)

    A store read failure never interrupts the decision — the bot
    falls back to a marker that tells the LLM the calendar is
    unavailable this tick. Same posture as every other optional
    context path.

    Module-level rather than a method so the logic is unit-testable
    without constructing a StrategyBot (which pulls in Strands/Bedrock).
    """

    if calendar_store is None:
        return "(no calendar wired)"
    if not calendar_enabled:
        return "(calendar disabled for this strategy)"

    try:
        import time as _time

        from trading_strands.calendar_store.store import (
            summarize_for_symbols,
        )

        today_str = _time.strftime("%Y-%m-%d", _time.gmtime())
        tomorrow_str = _time.strftime(
            "%Y-%m-%d", _time.gmtime(_time.time() + 86400),
        )
        today = calendar_store.get_day(today_str)
        tomorrow = calendar_store.get_day(tomorrow_str)
        return summarize_for_symbols(
            symbols=symbols, today=today, tomorrow=tomorrow,
        )
    except Exception:
        logger.exception("calendar.read_failed", bot_id=bot_id)
        return "Calendar read failed."


class StrategyBot:
    """A strategy bot backed by a Strands agent.

    The bot is registered with the orchestrator and called on each tick
    where its TTA predicate fires. It uses an LLM to decide whether
    to trade, based on the strategy prompt and current conditions.
    """

    def __init__(
        self,
        bot_id: str,
        org_id: str,
        strategy_prompt: str,
        symbols: list[str],
        tta: Predicate | None = None,
        model: str | None = None,
        token_store: Any | None = None,
        memory_store: Any | None = None,
        heartbeat_store: Any | None = None,
        tools: list[Any] | None = None,
        calendar_store: Any | None = None,
        calendar_enabled: bool = False,
        ta_store: Any | None = None,
        ta_enabled: bool = False,
        skills: list[Any] | None = None,
        strategy_name: str = "",
        prompt_snapshot_store: Any | None = None,
    ) -> None:
        self.bot_id = bot_id
        self.org_id = org_id
        self.strategy_prompt = strategy_prompt
        self.symbols = symbols
        self.tta = tta
        self.model = model or ""
        self._token_store = token_store
        self._memory_store = memory_store
        self._heartbeat_store = heartbeat_store
        # Calendar is context-injected (not a tool call). Store handle
        # is always optional — when absent, no calendar section is
        # rendered. `calendar_enabled` is the per-strategy opt-in; a
        # strategy with the store wired but enabled=False gets a
        # "(disabled)" placeholder so the prompt template doesn't
        # blow up on missing keys.
        self._calendar_store = calendar_store
        self._calendar_enabled = calendar_enabled
        # TA snapshots follow the same wiring pattern as calendar.
        self._ta_store = ta_store
        self._ta_enabled = ta_enabled
        self._recent_decisions: list[str] = []
        self._max_history = 10
        # Health payload state (docs/SPEC/observability.md §"Health
        # checks"). last_decision_at is set after every successful
        # decide(). _error_ts tracks the timestamps of the last hour
        # of errors so the supervisor can see a degrading trend
        # without needing CloudWatch.
        self._last_decision_at: int = 0
        self._error_ts: list[int] = []

        # Base system-prompt framing. Skills (if any) get composed
        # into this at construction time — per SPEC §8.4 they appear
        # between the base and the strategy body. When no skills are
        # supplied, the bot falls back to the v0 behavior of using
        # the strategy_prompt as the whole prompt body.
        # Anti-confabulation clause (docs/SPEC/agent_memory.md §"Anti-
        # confabulation rule"): every concrete market-state claim the
        # agent makes in its rationale or memory must carry a data
        # pointer (MARKETDATA#/LEDGER#/DECISION#). Keeps weekly review
        # + chat trustworthy instead of plausible-sounding fiction.
        base_prompt = (
            "You are a disciplined trading bot. Follow your strategy rules "
            "precisely. Never deviate from the strategy. Be conservative "
            "when uncertain — prefer to hold rather than make a bad trade.\n\n"
            "ANTI-CONFABULATION: when your rationale references a concrete "
            "market state (a price, a level, a range, a fill), you must "
            "either (a) cite the data pointer it came from (e.g. "
            "[MARKETDATA#SPY#2026042615] for an hourly bucket, "
            "[LEDGER#<bot>#<yyyymmddhhmm>], [DECISION#<bot>#<ts>]) or "
            "(b) say explicitly you don't have the data. Never invent "
            "specifics you don't see in the inputs above. 'Looks choppy' "
            "without a reference is worse than 'I don't have the data'."
        )
        if skills:
            from trading_strands.skills_store.store import (
                compose_system_prompt,
            )
            system_prompt = compose_system_prompt(
                base_prompt=base_prompt,
                skills=skills,
                strategy_name=strategy_name or bot_id,
                strategy_markdown=strategy_prompt,
            )
        else:
            system_prompt = base_prompt

        # Stashed for the Prompt tab on the detail page — the rendered
        # system prompt is what the LLM actually sees each tick.
        self._system_prompt = system_prompt
        self._prompt_snapshot_store = prompt_snapshot_store
        self._tick_counter = 0

        # Tools are bound by the caller (app.py) via
        # tools.base.bind_tools_for_strategy. We pass them straight
        # through to the Strands Agent — None/empty means the agent
        # has no tools, which is the default v0 behavior.
        agent_kwargs: dict[str, Any] = {
            "model": model,
            "system_prompt": system_prompt,
        }
        if tools:
            agent_kwargs["tools"] = tools
        self._agent = Agent(**agent_kwargs)

    async def decide(
        self,
        bot_id: str,
        prices: dict[str, Decimal],
        ledger: Ledger,
    ) -> TradeIntent | None:
        """Make a trading decision based on current conditions.

        This is called by the orchestrator on each tick where the TTA fires.
        Returns a TradeIntent or None (hold).
        """
        # Heartbeat BEFORE the LLM call so a slow/failing Bedrock still
        # registers as "this bot is alive" for the supervisor. Write
        # failures must never interrupt the trade path — swallow them
        # via contextlib.suppress. Bedrock observability already gives
        # us a louder signal if decisions start failing.
        #
        # Payload (docs/SPEC/observability.md §"Health checks"):
        # current_activity captures where in the loop we are so a
        # stuck bot reveals *what* it's stuck on, not just that it
        # stopped beating. errors_last_hour is a rolling count over
        # the last 3600s, pruned inline on each beat.
        if self._heartbeat_store is not None:
            import contextlib as _contextlib
            import time as _time
            cutoff = int(_time.time()) - 3600
            self._error_ts = [t for t in self._error_ts if t > cutoff]
            with _contextlib.suppress(Exception):
                self._heartbeat_store.beat(
                    agent_type="strategy", agent_id=self.bot_id,
                    status="healthy",
                    current_activity="reasoning",
                    last_decision_at=self._last_decision_at,
                    errors_last_hour=len(self._error_ts),
                )

        prompt = _DECISION_PROMPT_TEMPLATE.format(
            strategy_prompt=self.strategy_prompt,
            market_data=_format_market_data(prices),
            calendar_context=self._calendar_context(),
            ta_context=self._ta_context(),
            portfolio_state=_format_portfolio(ledger),
            recent_decisions=self._format_recent() or "No recent decisions.",
        )

        # Persist the last-rendered prompt for the detail page's Prompt
        # tab. Writes happen BEFORE the LLM call so the snapshot is
        # available even on LLM failure (makes debugging "why didn't
        # this tick decide?" tractable). Best-effort — a snapshot-
        # write failure must never interrupt the trade path.
        self._tick_counter += 1
        if self._prompt_snapshot_store is not None:
            import contextlib as _contextlib
            with _contextlib.suppress(Exception):
                self._prompt_snapshot_store.write(
                    bot_id=self.bot_id,
                    org_id=self.org_id,
                    system_prompt=self._system_prompt,
                    user_prompt=prompt,
                    tick=self._tick_counter,
                )

        dims = {
            "agent_id": self.bot_id,
            "org_id": self.org_id,
            "agent_type": "strategy",
        }
        try:
            with timed_metric("agent.decision.latency_ms", dims):
                result = await self._agent.invoke_async(
                    prompt,
                    structured_output_model=BotDecision,
                )
        except Exception:
            import time as _time
            self._error_ts.append(int(_time.time()))
            emit_metric(
                "agent.error.count", 1, unit="Count",
                dimensions={**dims, "error_type": "llm_invoke"},
            )
            await logger.aexception("bot.llm.error", bot_id=self.bot_id)
            return None

        import time as _time2
        self._last_decision_at = int(_time2.time())

        # Record token usage for the cost dashboard. No-op when
        # token_store is None (tests / local dev).
        record_from_result(
            self._token_store,
            result,
            org_id=self.org_id,
            agent_id=self.bot_id,
            agent_type="strategy",
            model=self.model,
        )

        raw_decision = result.structured_output
        if raw_decision is None:
            await logger.awarn("bot.no_decision", bot_id=self.bot_id)
            return None

        decision = cast(BotDecision, raw_decision)

        # EMF decision counter, dimensioned by decision_type so dashboard
        # can split 'buys vs sells vs holds per bot over time'.
        emit_metric(
            "agent.decision.count", 1, unit="Count",
            dimensions={**dims, "decision_type": decision.action.lower()},
        )

        # Record decision for short-term in-process history (context seed
        # on next invocation's prompt).
        self._recent_decisions.append(
            f"{decision.action} {decision.symbol} x{decision.quantity}: "
            f"{decision.rationale}"
        )
        if len(self._recent_decisions) > self._max_history:
            self._recent_decisions = self._recent_decisions[-self._max_history :]

        # Durable memory: append a structured line to today's markdown file.
        # v0 format is minimal (timestamp + action + rationale). v1 will
        # expand this with MARKETDATA# pointers per the anti-confabulation
        # rule in docs/SPEC/agent_memory.md.
        self._append_memory_line(decision, prices, ledger)

        action = _map_action(decision.action)
        # HOLD = maintain (position exists, kept deliberately);
        # NOOP = stand_down (no position, no signal, nothing to do).
        # Both produce no TradeIntent — the distinction is preserved
        # in memory + recent_decisions above, not in the trade pipeline.
        if action in (IntentAction.HOLD, IntentAction.NOOP):
            return None

        return TradeIntent(
            bot_id=self.bot_id,
            org_id=self.org_id,
            symbol=decision.symbol,
            action=action,
            quantity=Decimal(decision.quantity),
            rationale=decision.rationale,
        )

    def _append_memory_line(
        self,
        decision: BotDecision,
        prices: dict[str, Decimal],
        ledger: Ledger,
    ) -> None:
        """Append one line about this decision to the agent's daily memory.

        Failures are swallowed — memory is useful but the tick loop must
        never crash on S3 issues. If writes fail repeatedly that'll
        surface in the health check (v1).

        Each line includes a [MARKETDATA#<symbol>#<yyyymmddhh>] pointer
        per docs/SPEC/agent_memory.md §"Anti-confabulation rule" — the
        hourly bucket that contains this tick's observed price. A weekly
        reviewer can dereference it to audit the claim instead of
        trusting the agent's prose.
        """

        if self._memory_store is None:
            return
        import time as _time

        line = _format_memory_line(decision, prices, ledger, _time.gmtime())
        try:
            self._memory_store.append_to_today(line)
        except Exception:
            logger.exception("memory.append_failed", bot_id=self.bot_id)

    def _format_recent(self) -> str:
        if not self._recent_decisions:
            return ""
        return "\n".join(
            f"  {i + 1}. {d}" for i, d in enumerate(self._recent_decisions)
        )

    def _calendar_context(self) -> str:
        return _build_calendar_context(
            calendar_store=self._calendar_store,
            calendar_enabled=self._calendar_enabled,
            symbols=set(self.symbols),
            bot_id=self.bot_id,
        )

    def _ta_context(self) -> str:
        return _build_ta_context(
            ta_store=self._ta_store,
            ta_enabled=self._ta_enabled,
            symbols=set(self.symbols),
            bot_id=self.bot_id,
        )
