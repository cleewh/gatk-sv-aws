"""Step Functions orchestrator for the GATK-SV HealthOmics pipeline.

Implements a CDK-deployed AWS Step Functions Standard Workflow that chains
the 10 GATK-SV modules sequentially, handles the GatherSampleEvidence
parallel fan-out, polls HealthOmics run status, retries transient failures
with exponential backoff, and produces a final cost report.

See ``.kiro/specs/step-functions-orchestrator/design.md`` for the full
architecture and component descriptions.
"""

from __future__ import annotations
