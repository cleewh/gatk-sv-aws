"""Shared Property-10 cost-tag helper.

Used by every script that calls boto3.omics.start_run / start_workflow
so a single cohort-id ties an entire end-to-end pipeline run together
in Cost Explorer.

Property-10 (design.md §Correctness Properties → Property 10):

    For any cohort run submission with cohort_id C and workflow version V,
    every AWS resource-creating API call issued by the Run Orchestrator
    SHALL carry both a `gatk-sv:cohort-id = C` tag and a
    `gatk-sv:workflow-version = V` tag.

This helper extends that contract with the additional tags from the
spec's Cost Explorer tag taxonomy (`gatk-sv:module`, `gatk-sv:sample-count`,
`gatk-sv:environment`).
"""

from __future__ import annotations

import os
from typing import Dict


def cost_tags(
    cohort_id: str,
    workflow_version: str,
    module: str,
    sample_count: int,
    environment: str = "validation",
) -> Dict[str, str]:
    """Produce the Property-10 tag set for a single resource-creating call.

    Args:
        cohort_id: Stable identifier for the cohort run, e.g.
            "gatk-sv-validation-2026q2-rerun-2026-05-25". This is the join key
            for Cost Explorer aggregation.
        workflow_version: Identifier of the specific workflow version being
            invoked. For per-tool GSE sub-modules this is "gse-<tool>-<id>"
            (e.g. "gse-cc-8771956"); for cohort modules it's typically
            "<module>-<id>" (e.g. "gbe-1575165").
        module: Logical pipeline stage. For per-tool GSE sub-runs this is
            "GatherSampleEvidence:<tool>"; for cohort modules it matches the
            module name as it appears in the spec (Migrated_Modules).
        sample_count: Number of samples in the cohort. Used by Cost Explorer
            to compute per-sample cost.
        environment: "validation" or "prod". Validation cohort reruns use
            "validation" so they don't pollute the production cost budget.

    Returns:
        Dict with five string keys ready to pass as `tags=` to any
        boto3.omics call (start_run, start_workflow, create_run_cache, etc.).
    """
    return {
        "gatk-sv:cohort-id": cohort_id,
        "gatk-sv:workflow-version": workflow_version,
        "gatk-sv:module": module,
        "gatk-sv:sample-count": str(int(sample_count)),
        "gatk-sv:environment": environment,
    }


def cohort_id_from_env(default: str | None = None) -> str:
    """Resolve cohort id from $GATK_SV_COHORT_ID or fall back to a default.

    Lets every script accept `--cohort-id` as a kwarg or pick it up from
    the env without needing per-script flag plumbing.
    """
    val = os.environ.get("GATK_SV_COHORT_ID")
    if val:
        return val
    if default is not None:
        return default
    raise ValueError(
        "GATK_SV_COHORT_ID env var is not set and no default was provided. "
        "Set it to a stable string like "
        "'gatk-sv-validation-2026q2-rerun-2026-05-25'."
    )
