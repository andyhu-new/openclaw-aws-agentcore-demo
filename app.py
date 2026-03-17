#!/usr/bin/env python3
import os
import aws_cdk as cdk
from stacks.app_stack import AppStack

app = cdk.App()

AppStack(app, "AppStack",
    env=cdk.Environment(
        account=app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION") or "us-west-2",
    ),
)

app.synth()
