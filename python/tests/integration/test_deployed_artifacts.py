"""Integration test: every workflow bundle + template is present at the URI
``workflow-versions.json`` says (Req 16.3).
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


def _s3_exists(s3_client, uri: str) -> bool:
    assert uri.startswith("s3://")
    bucket, _, key = uri[len("s3://") :].partition("/")
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.aws_integration
def test_every_registered_workflow_has_its_artifacts_uploaded() -> None:
    """Every record in workflow-versions.json points at a real S3 object."""
    if not WORKFLOW_VERSIONS_PATH.exists():
        pytest.skip(f"no workflow-versions.json at {WORKFLOW_VERSIONS_PATH}")

    import boto3

    s3 = boto3.client("s3", region_name="ap-southeast-1")  # type: ignore[attr-defined]
    data = json.loads(WORKFLOW_VERSIONS_PATH.read_text())
    records = data.get("records", [])
    assert records, "workflow-versions.json is empty"

    missing: list[str] = []
    for record in records:
        for field_name in ("container_registry_map_uri", "parameter_template_uri"):
            uri = record.get(field_name)
            if not uri:
                continue
            if not _s3_exists(s3, uri):
                missing.append(f"{record['module']}.{field_name}: {uri}")

    assert not missing, "missing artifacts:\n  " + "\n  ".join(missing)
