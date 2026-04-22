#!/usr/bin/env python3
import os

import aws_cdk as cdk
from stacks.trading_stack import TradingStrandsStack

app = cdk.App()

# TLS config via context:
#   cdk deploy -c domain=app.tradingstrands.xyz -c zone_name=tradingstrands.xyz -c zone_id=Z0XXX
domain_name = app.node.try_get_context("domain") or ""
zone_name = app.node.try_get_context("zone_name") or ""
hosted_zone_id = app.node.try_get_context("zone_id") or ""

TradingStrandsStack(
    app,
    "TradingStrandsStack",
    domain_name=domain_name,
    zone_name=zone_name,
    hosted_zone_id=hosted_zone_id,
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-west-2"),
    ),
)
app.synth()
