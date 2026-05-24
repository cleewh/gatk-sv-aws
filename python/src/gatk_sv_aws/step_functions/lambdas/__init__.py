"""Lambda function handlers for the GATK-SV Step Functions orchestrator.

Each module in this package implements a single Lambda handler invoked by
the Step Functions state machine:

- ``validate_manifest`` — validates the sample manifest before any runs
- ``start_run`` — submits a HealthOmics workflow run
- ``poll_status`` — checks run status and emits observability events
- ``gather_cost`` — collects cost data and produces the final cost report
"""

from __future__ import annotations
