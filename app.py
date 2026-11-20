#!/usr/bin/env python3
import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from gpu_capacity_finder.config import parse_context
from gpu_capacity_finder.stack import FinderStack

app = cdk.App()
cfg = parse_context(app.node.try_get_context)
FinderStack(
    app,
    cfg["stack_name"],
    cfg=cfg,
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION"),
    ),
)
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
app.synth()
