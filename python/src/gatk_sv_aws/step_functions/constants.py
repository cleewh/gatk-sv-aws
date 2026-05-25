"""Shared constants for the GATK-SV Step Functions orchestrator.

Centralises module execution order, retry parameters, backoff
configuration, and default deployment values used across Lambda handlers
and the CDK stack.

Requirements: 1.1, 5.1, 5.2, 6.1.
"""

from __future__ import annotations

import os

from gatk_sv_aws.models import ModuleName

# ---------------------------------------------------------------------------
# Module execution order (Req 1.1)
# ---------------------------------------------------------------------------

MODULE_EXECUTION_ORDER: tuple[ModuleName, ...] = (
    "GatherSampleEvidence",
    "GatherBatchEvidence",
    "ClusterBatch",
    "GenerateBatchMetrics",
    "FilterBatch",
    "MergeBatchSites",
    "GenotypeBatch",
    "RegenotypeCNVs",
    "MakeCohortVcf",
    "AnnotateVcf",
)

# ---------------------------------------------------------------------------
# GatherSampleEvidence parallel tasks (Req 3.2)
# ---------------------------------------------------------------------------

GATHER_SAMPLE_EVIDENCE_TASKS: tuple[str, ...] = (
    "CollectCounts",
    "CollectSVEvidence",
    "Manta",
    "Wham",
    "Scramble",
)

# ---------------------------------------------------------------------------
# Retry and backoff parameters (Req 5.1, 5.2)
# ---------------------------------------------------------------------------

RETRYABLE_ERROR_CODES: frozenset[str] = frozenset(
    {"InternalServerError", "Throttling", "ServiceUnavailable"}
)

BACKOFF_BASE_SECONDS: int = 30
BACKOFF_FACTOR: int = 2
BACKOFF_CAP_SECONDS: int = 480
MAX_RETRY_ATTEMPTS: int = 3

# ---------------------------------------------------------------------------
# Polling configuration (Req 4.2)
# ---------------------------------------------------------------------------

POLLING_INTERVAL_SECONDS: int = 60

# ---------------------------------------------------------------------------
# Module timeout (Req 11.2)
# ---------------------------------------------------------------------------

MODULE_TIMEOUT_SECONDS: int = 86400  # 24 hours

# ---------------------------------------------------------------------------
# Default deployment configuration
# ---------------------------------------------------------------------------

DEFAULT_REGION: str = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
DEFAULT_ROLE_ARN: str = (
    "arn:aws:iam::__ACCOUNT_ID__:role/gatk-sv-healthomics-run-role"
)
DEFAULT_CACHE_ID: str = "__RUN_CACHE_ID__"
DEFAULT_OUTPUT_BUCKET: str = "healthomics-outputs-__ACCOUNT_ID__-apse1"

# ---------------------------------------------------------------------------
# Optimized workflow IDs for GatherSampleEvidence (empirically validated)
# ---------------------------------------------------------------------------

# Sequential scanners — no pre-localization needed, FUSE is fine
WORKFLOW_ID_REINDEX: str = "8437840"
WORKFLOW_ID_COLLECT_COUNTS: str = "3901751"  # 4 CPU, 7.5 GB
WORKFLOW_ID_COLLECT_SV_EVIDENCE: str = "7038412"  # 4 CPU, 7.5 GB

# Random-access tools — pre-localize CRAM to local disk
WORKFLOW_ID_MANTA: str = "4091926"  # 16 CPU, 32 GB, pre-localize
WORKFLOW_ID_SCRAMBLE: str = "3817166"  # 12 CPU, 32 GB, STATIC, 12 parallel cluster_identifier
WORKFLOW_ID_WHAM_PARALLEL: str = "5369691"  # 48 CPU, 64 GB, pre-localize + 24 parallel
