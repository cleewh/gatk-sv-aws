"""Integration test: HealthOmics availability in ``ap-southeast-1`` (Req 1.1, 1.2).

Skipped unless AWS credentials are active. See ``conftest.py`` for the
skip guard.
"""

from __future__ import annotations

import pytest


@pytest.mark.aws_integration
def test_ap_southeast_1_is_in_supported_regions() -> None:
    import boto3

    omics = boto3.client("omics", region_name="ap-southeast-1")  # type: ignore[attr-defined]
    # The boto3 client itself must resolve an endpoint for ap-southeast-1.
    # A successful .list_workflows() call (even with empty results)
    # demonstrates HealthOmics is reachable from this region.
    response = omics.list_workflows(maxResults=1)
    assert "items" in response
