"""Integration test: every registered workflow reaches ACTIVE (Req 16.1).

Loads ``gatk-sv-healthomics/workflow-versions.json`` and, for every
recorded ``workflow_id``, asserts ``omics.get_workflow`` returns
``status == ACTIVE``.

Skipped unless AWS credentials are active.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

WORKFLOW_VERSIONS_PATH = (
    Path(__file__).resolve().parents[4]
    / "gatk-sv-healthomics"
    / "workflow-versions.json"
)


@pytest.mark.aws_integration
def test_every_registered_workflow_is_active() -> None:
    if not WORKFLOW_VERSIONS_PATH.exists():
        pytest.skip(
            f"no workflow-versions.json at {WORKFLOW_VERSIONS_PATH}; "
            "run scripts/deploy.py step 8 to populate."
        )

    import boto3

    data = json.loads(WORKFLOW_VERSIONS_PATH.read_text())
    records = data.get("records", [])
    if not records:
        pytest.skip("workflow-versions.json contains no records yet")

    client = boto3.client("omics", region_name="ap-southeast-1")  # type: ignore[attr-defined]
    for record in records:
        response = client.get_workflow(id=record["workflow_id"])
        assert response["status"] == "ACTIVE", (
            f"workflow {record['workflow_id']} ({record['module']}) "
            f"has status {response['status']!r}, expected ACTIVE"
        )
