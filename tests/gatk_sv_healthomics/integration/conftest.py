"""Shared fixtures for GATK-SV HealthOmics integration tests.

Tests marked ``@pytest.mark.integration`` require AWS credentials for
``ap-southeast-1`` and an explicit opt-in via ``RUN_INTEGRATION_TESTS=1``.
This double gate avoids accidentally hitting AWS during local
unit-test runs while still enabling the suite to run unattended in CI
when the env var is exported and credentials are available.

Design reference: §Testing Strategy → integration table (Reqs 10, 16).
"""

from __future__ import annotations

import os

import pytest

TARGET_REGION = "ap-southeast-1"


def _integration_enabled() -> tuple[bool, str]:
    """Return ``(enabled, reason)``; ``reason`` is used when skipping."""
    flag = os.environ.get("RUN_INTEGRATION_TESTS", "").lower()
    if flag not in {"1", "true", "yes"}:
        return False, "RUN_INTEGRATION_TESTS!=1; skipping AWS integration tests"

    try:
        import boto3
    except ImportError:  # pragma: no cover - boto3 is a runtime dep
        return False, "boto3 is not installed"

    try:
        sts = boto3.client("sts", region_name=TARGET_REGION)  # type: ignore[attr-defined]
        sts.get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        return False, f"AWS credentials for {TARGET_REGION} not reachable: {exc}"

    return True, ""


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: integration test requiring AWS credentials in "
        "ap-southeast-1 and RUN_INTEGRATION_TESTS=1",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    enabled, reason = _integration_enabled()
    if enabled:
        return
    skip_marker = pytest.mark.skip(reason=reason)
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)
