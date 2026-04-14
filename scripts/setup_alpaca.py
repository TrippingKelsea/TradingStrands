"""Create an Alpaca paper trading account and retrieve API keys using Nova Act.

This script uses AWS Nova Act to automate browser interaction with Alpaca's
signup flow. It will:
1. Navigate to Alpaca's signup page
2. Create a paper trading account (you provide email/password)
3. Navigate to the API keys page
4. Extract and save the API key and secret to .env

Usage:
    uv run python scripts/setup_alpaca.py

Requirements:
    - AWS credentials configured (~/.aws/credentials or environment variables)
    - nova-act package installed (uv pip install nova-act)
    - Playwright Chromium installed (uv run playwright install chromium)
    - Display available (runs a visible browser)

The browser is visible (not headless) so you can:
    - Complete any CAPTCHAs
    - Verify email if required
    - Confirm the actions being taken

AWS Setup:
    This script uses a Nova Act workflow definition named 'trading-strands-setup'.
    On first run, it will create the workflow definition automatically if it
    doesn't exist. The workflow definition is created in us-east-1 by default.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

import boto3


def _ensure_workflow_definition(region: str = "us-east-1") -> str:
    """Ensure the Nova Act workflow definition exists, creating it if needed."""
    name = "trading-strands-setup"
    client = boto3.client("nova-act", region_name=region)

    existing = client.list_workflow_definitions()
    for defn in existing.get("workflowDefinitionSummaries", []):
        if defn.get("workflowDefinitionName") == name:
            return name

    client.create_workflow_definition(name=name)
    return name


def main() -> None:
    from nova_act import NovaAct, Workflow

    print("=" * 60)
    print("  Alpaca Paper Trading Account Setup")
    print("=" * 60)
    print()
    print("This will open a browser and create an Alpaca paper")
    print("trading account. You'll need to provide an email and")
    print("password. The browser is visible so you can intervene")
    print("if needed (CAPTCHAs, email verification, etc).")
    print()

    email = input("Email for Alpaca account: ").strip()
    if not email:
        print("Email is required.")
        sys.exit(1)

    password = getpass.getpass("Password for Alpaca account: ").strip()
    if not password:
        print("Password is required.")
        sys.exit(1)

    print()
    print("Ensuring Nova Act workflow definition exists...")
    workflow_name = _ensure_workflow_definition()
    print(f"  Using workflow: {workflow_name}")
    print()
    print("Starting browser... (do not close the browser window)")
    print()

    workflow = Workflow(
        model_id="nova-act-latest",
        workflow_definition_name=workflow_name,
    )

    with NovaAct(
        starting_page="https://app.alpaca.markets/signup",
        headless=False,
        workflow=workflow,
    ) as nova:
        # Step 1: Fill in signup form
        print("[1/5] Filling in signup form...")
        result = nova.act(
            f"Fill in the email field with '{email}' and the password field "
            f"with '{password}'. Then click the Sign Up or Create Account button. "
            "If there's a terms/conditions checkbox, check it first.",
            timeout=60,
        )
        print(f"  Signup form: {result.status}")

        if result.status != "COMPLETED":
            print()
            print("Signup form step did not complete automatically.")
            print("Please complete the signup in the browser window.")
            input("Press Enter when you've finished signing up...")

        # Step 2: Handle email verification if needed
        print()
        print("[2/5] Checking for email verification...")
        print("  If Alpaca sent a verification email, please verify it now.")
        input("  Press Enter when your account is verified and you can log in...")

        # Step 3: Navigate to paper trading dashboard
        print("[3/5] Navigating to paper trading dashboard...")
        result = nova.act(
            "Navigate to the Alpaca dashboard. Look for a 'Paper Trading' or "
            "'Paper' environment option and select it. If there's an environment "
            "switcher or toggle between 'Live' and 'Paper', select 'Paper'.",
            timeout=60,
        )
        print(f"  Dashboard navigation: {result.status}")

        # Step 4: Navigate to API keys
        print("[4/5] Finding API keys...")
        result = nova.act(
            "Find the API Keys section. This might be under 'API Keys', "
            "'Overview', or in the sidebar/menu. Look for the API Key ID "
            "and Secret Key. If you need to generate new keys, click the "
            "'Generate' or 'Regenerate' or 'View' button.",
            timeout=60,
        )
        print(f"  API keys page: {result.status}")

        # Step 5: Extract API keys
        print("[5/5] Extracting API keys...")
        result = nova.act(
            "Read the API Key ID and Secret Key from the page. "
            "The API Key ID is a shorter alphanumeric string (like PKXXXXXXXX). "
            "The Secret Key is a longer alphanumeric string. "
            "Return both values.",
            timeout=60,
            schema={
                "type": "object",
                "properties": {
                    "api_key": {
                        "type": "string",
                        "description": "The Alpaca API Key ID",
                    },
                    "secret_key": {
                        "type": "string",
                        "description": "The Alpaca Secret Key",
                    },
                },
                "required": ["api_key", "secret_key"],
            },
        )
        print(f"  Key extraction: {result.status}")

        api_key = None
        secret_key = None

        if result.status == "COMPLETED" and result.parsed_response:
            api_key = result.parsed_response.get("api_key")
            secret_key = result.parsed_response.get("secret_key")

        if not api_key or not secret_key:
            print()
            print("Could not automatically extract API keys.")
            print("Please copy them from the browser window:")
            api_key = input("  API Key ID: ").strip()
            secret_key = input("  Secret Key: ").strip()

        if not api_key or not secret_key:
            print("API keys are required. Exiting.")
            sys.exit(1)

    # Write to .env
    env_path = Path(__file__).parent.parent / ".env"
    env_content = f"""# Alpaca Paper Trading API credentials
ALPACA_API_KEY={api_key}
ALPACA_SECRET_KEY={secret_key}
ALPACA_PAPER=true

# AWS credentials (inherited from environment / ~/.aws/credentials)
# AWS_ACCESS_KEY_ID=
# AWS_SECRET_ACCESS_KEY=
# AWS_DEFAULT_REGION=us-east-1
"""

    env_path.write_text(env_content)
    print()
    print("=" * 60)
    print("  Setup complete!")
    print("=" * 60)
    print()
    print(f"  API Key:    {api_key[:8]}...")
    print(f"  Secret Key: {secret_key[:8]}...")
    print(f"  Written to: {env_path}")
    print()
    print("  Test with:")
    print("    uv run python -c \"from trading_strands.broker.alpaca import AlpacaAdapter\"")
    print()
    print("  Run TradingStrands:")
    print("    uv run python -m trading_strands.app \\")
    print("      --strategy examples/strategies/turtle-trading.md \\")
    print("      --capital 1000 --symbols AAPL")


if __name__ == "__main__":
    main()
