#!/usr/bin/env python3
import aws_cdk as cdk
from stacks.trading_stack import TradingStrandsStack

app = cdk.App()
TradingStrandsStack(app, "TradingStrandsStack")
app.synth()
