"""CDK app entry point for the GATK-SV Step Functions orchestrator.

Synthesizes the :class:`~.stack.GatkSvOrchestratorStack` into a
CloudFormation template deployable via ``cdk deploy``.
"""

from __future__ import annotations

import aws_cdk as cdk

from gatk_sv_aws.step_functions.stack import (
    GatkSvOrchestratorStack,
)

app = cdk.App()

GatkSvOrchestratorStack(
    app,
    "GatkSvOrchestratorStack",
    env=cdk.Environment(
        account="__ACCOUNT_ID__",
        region=app.node.try_get_context("target_region") or "ap-southeast-1",
    ),
)

app.synth()
