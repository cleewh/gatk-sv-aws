"""Shared fixtures for GATK-SV HealthOmics integration tests.

Tests marked ``@pytest.mark.aws_integration`` are auto-skipped when AWS
credentials for ``ap-southeast-1`` are not reachable. Other integration
tests (e.g. local bundle-lint) run without AWS.

Opt out explicitly with ``SKIP_AWS_INTEGRATION=1 pytest ...``.
"""

from __future__ import annotations

import os

import pytest


def _aws_reachable() -> bool:
    if os.environ.get("SKIP_AWS_INTEGRATION", "").lower() in {"1", "true", "yes"}:
        return False
    try:
        import boto3

        sts = boto3.client("sts", region_name="ap-southeast-1")  # type: ignore[attr-defined]
        sts.get_caller_identity()
        return True
    except Exception:  # noqa: BLE001
        return False


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "aws_integration: test requires AWS credentials for ap-southeast-1",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if _aws_reachable():
        return
    skip_marker = pytest.mark.skip(
        reason="AWS credentials for ap-southeast-1 not available"
    )
    for item in items:
        if "aws_integration" in item.keywords:
            item.add_marker(skip_marker)
