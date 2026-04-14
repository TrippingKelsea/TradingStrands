# Deployment Guide

## Quick Start: Alpaca Paper Trading

TradingStrands uses Alpaca as its v0 broker. This guide walks through setting up a paper trading account and connecting it to TradingStrands.

### Prerequisites

- **AWS credentials** configured (`~/.aws/credentials` or environment variables)
- **Python 3.12+** with `uv` installed
- **A display** (the setup script opens a visible browser)

### Option A: Automated Setup (Nova Act)

The `scripts/setup_alpaca.py` script uses AWS Nova Act to automate the Alpaca signup flow in a visible browser. You can intervene at any point (CAPTCHAs, email verification, etc).

```bash
# Install dependencies
uv sync --extra dev
uv pip install nova-act
uv run playwright install chromium

# Run the setup script
uv run python scripts/setup_alpaca.py
```

The script will:
1. Create a Nova Act workflow definition in your AWS account (first run only)
2. Open a browser to Alpaca's signup page
3. Fill in your email/password and submit the form
4. Pause for email verification (you verify manually)
5. Navigate to the paper trading API keys page
6. Extract API keys and write them to `.env`

**How it works:** Nova Act is an AWS browser automation agent. It uses your existing AWS credentials (IAM) to authenticate — no separate API key needed. The workflow definition (`trading-strands-setup`) is created automatically in `us-east-1` on first run.

### Option B: Manual Setup

1. Go to [app.alpaca.markets/signup](https://app.alpaca.markets/signup)
2. Create an account and verify your email
3. Switch to the **Paper Trading** environment
4. Go to **API Keys** and generate a new key pair
5. Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

```env
ALPACA_API_KEY=PKXXXXXXXXXXXXXXXXXX
ALPACA_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ALPACA_PAPER=true
```

### Verify Setup

```bash
# Check the adapter imports and credentials load
uv run python -c "from trading_strands.broker.alpaca import AlpacaAdapter; print('OK')"
```

### Run TradingStrands

```bash
uv run python -m trading_strands.app \
  --strategy examples/strategies/turtle-trading.md \
  --capital 1000 --symbols AAPL
```

## AWS Requirements

TradingStrands uses AWS for two things:

1. **Strands Agents** (Bedrock) — LLM-powered strategy compilation and bot decisions
2. **Nova Act** (optional) — browser automation for account setup

Your AWS IAM user/role needs permissions for:
- `bedrock:InvokeModel` (for Strands agents)
- `nova-act:*` (only if using the automated setup script)

Credentials are loaded from the standard AWS credential chain (`~/.aws/credentials`, environment variables, or instance role).
