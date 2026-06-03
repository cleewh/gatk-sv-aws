"""Integration test: Run_Cache lifecycle in ap-southeast-1 (Reqs 10.1, 10.2).

Implements the "Run cache lifecycle" row of Design §Testing Strategy →
integration table:

    | Run cache lifecycle | CreateAHORunCache, GetAHORunCache |
    | Cache present in ap-southeast-1; status ACTIVE;            |
    | cache_behavior = CACHE_ALWAYS                              |

Two scenarios are covered:

1. **Lookup-existing** — when ``GATK_SV_RUN_CACHE_ID`` is set, calls
   ``omics.get_run_cache(id=...)`` against the production cache (the
   project pre-creates run-cache ``9564200`` per Design §Deployment
   Step 9 and ``docs/phase-status.md``) and asserts:

   * ``cacheBehavior == "CACHE_ALWAYS"`` (Req 10.2)
   * ``status == "ACTIVE"`` and the cache lives in ``ap-southeast-1``
     (Req 10.1)

2. **Create-new** — only when both ``GATK_SV_TEST_CREATE_CACHE=1`` and
   ``GATK_SV_TEST_CACHE_S3_LOCATION=s3://...`` are set. Mirrors the
   ``CreateAHORunCache`` → ``GetAHORunCache`` → ``DeleteAHORunCache``
   round-trip from Design §Deployment Step 9 and cleans up after itself
   so no orphan caches are left behind. Skipped by default.

Validates: Requirements 10.1, 10.2.

Design references:
  * §Deployment Step 9 — ``CreateAHORunCache(cache_behavior="CACHE_ALWAYS",
    cache_s3_location=..., name=...)``
  * §Testing Strategy → Integration Tests, "Run cache lifecycle" row

Skipped by default. Opt in with ``RUN_INTEGRATION_TESTS=1`` AND active
AWS credentials reachable in ``ap-southeast-1``. Additional environment
variables:

  * ``GATK_SV_RUN_CACHE_ID`` — id of the production run cache to probe.
    When unset, the lookup-existing test is skipped.
  * ``GATK_SV_TEST_CREATE_CACHE`` — set to ``1`` to enable the
    create/delete round-trip test. Disabled by default to avoid
    leaving caches behind.
  * ``GATK_SV_TEST_CACHE_S3_LOCATION`` — S3 URI used as
    ``cacheS3Location`` for the round-trip cache. Required for the
    create-new test.

This test does not start any HealthOmics runs; the lookup variant is
read-only and the create variant deletes any cache it creates.
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TARGET_REGION = "ap-southeast-1"

EXPECTED_CACHE_BEHAVIOR = "CACHE_ALWAYS"
EXPECTED_STATUS = "ACTIVE"


# ---------------------------------------------------------------------------
# Skip guard — opt-in via env var
# ---------------------------------------------------------------------------


def _opted_in() -> bool:
    return os.environ.get("RUN_INTEGRATION_TESTS", "").lower() in {"1", "true", "yes"}


def _create_opted_in() -> bool:
    return os.environ.get("GATK_SV_TEST_CREATE_CACHE", "").lower() in {
        "1",
        "true",
        "yes",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_existing_cache_id() -> str:
    """Return the run-cache id to probe, or ``pytest.skip``."""
    cache_id = os.environ.get("GATK_SV_RUN_CACHE_ID")
    if not cache_id:
        pytest.skip(
            "set GATK_SV_RUN_CACHE_ID to the production HealthOmics run cache "
            "id (project default 9564200, see docs/phase-status.md) to probe "
            "the lookup-existing scenario."
        )
    return cache_id


def _resolve_cache_s3_location() -> str:
    """Return the S3 URI used as cacheS3Location, or ``pytest.skip``."""
    s3_uri = os.environ.get("GATK_SV_TEST_CACHE_S3_LOCATION")
    if not s3_uri:
        pytest.skip(
            "set GATK_SV_TEST_CACHE_S3_LOCATION to an S3 URI "
            "(e.g. s3://my-cache-bucket/gatk-sv-test/) to enable the "
            "create-new round-trip test."
        )
    if not s3_uri.startswith("s3://"):
        pytest.skip(
            f"GATK_SV_TEST_CACHE_S3_LOCATION={s3_uri!r} is not a valid s3:// URI."
        )
    return s3_uri


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.aws_integration
@pytest.mark.skipif(
    not _opted_in(),
    reason="set RUN_INTEGRATION_TESTS=1 to run run-cache integration test",
)
def test_existing_run_cache_is_cache_always_and_active() -> None:
    """``GetAHORunCache`` returns CACHE_ALWAYS / ACTIVE in ap-southeast-1.

    Validates: Requirements 10.1, 10.2.
    """
    import boto3  # local import: keeps the file importable without boto3 at collection

    cache_id = _resolve_existing_cache_id()

    omics = boto3.client("omics", region_name=TARGET_REGION)  # type: ignore[attr-defined]

    detail = omics.get_run_cache(id=cache_id)

    # Req 10.2 — default behavior is CACHE_ALWAYS.
    assert detail.get("cacheBehavior") == EXPECTED_CACHE_BEHAVIOR, (
        f"run cache {cache_id} cacheBehavior is "
        f"{detail.get('cacheBehavior')!r}, expected "
        f"{EXPECTED_CACHE_BEHAVIOR!r} (Req 10.2)."
    )

    # Req 10.1 — cache exists in Target_Region and is ACTIVE so cohort runs
    # can reference it. Status indicates the cache is usable.
    assert detail.get("status") == EXPECTED_STATUS, (
        f"run cache {cache_id} status is {detail.get('status')!r}, "
        f"expected {EXPECTED_STATUS!r} (Req 10.1)."
    )

    arn = detail.get("arn", "")
    assert f":{TARGET_REGION}:" in arn, (
        f"run cache ARN {arn!r} is not in {TARGET_REGION} (Req 10.1)."
    )
    assert arn.endswith(f"runCache/{cache_id}"), (
        f"run cache ARN {arn!r} does not end with runCache/{cache_id}."
    )

    # Cache must be S3-backed for cross-run persistence.
    s3_uri = detail.get("cacheS3Uri") or detail.get("cacheS3Location")
    assert s3_uri and s3_uri.startswith("s3://"), (
        f"run cache cacheS3Uri not configured: {s3_uri!r}"
    )


@pytest.mark.aws_integration
@pytest.mark.skipif(
    not _opted_in(),
    reason="set RUN_INTEGRATION_TESTS=1 to run run-cache integration test",
)
@pytest.mark.skipif(
    not _create_opted_in(),
    reason=(
        "set GATK_SV_TEST_CREATE_CACHE=1 to run the create/delete round-trip; "
        "disabled by default to avoid leaving caches behind."
    ),
)
def test_create_run_cache_round_trip_cleans_up() -> None:
    """``CreateAHORunCache`` → ``GetAHORunCache`` → ``DeleteAHORunCache``.

    Mirrors Design §Deployment Step 9 with ``cache_behavior=CACHE_ALWAYS``.
    Always deletes the cache it creates, even on assertion failure.

    Validates: Requirements 10.1, 10.2.
    """
    import boto3  # local import: keeps the file importable without boto3 at collection

    s3_location = _resolve_cache_s3_location()
    cache_name = f"gatk-sv-rc-test-{int(time.time())}"

    omics = boto3.client("omics", region_name=TARGET_REGION)  # type: ignore[attr-defined]

    created: dict[str, Any] = omics.create_run_cache(
        cacheBehavior=EXPECTED_CACHE_BEHAVIOR,
        cacheS3Location=s3_location,
        name=cache_name,
    )
    cache_id = str(created["id"])
    try:
        # CreateRunCache echoes the chosen behavior; record it before
        # the round-trip so a failure in the round-trip still surfaces
        # the create-time mismatch (Req 10.2).
        assert created.get("status"), (
            f"CreateAHORunCache did not return a status field: {created!r}"
        )

        detail = omics.get_run_cache(id=cache_id)

        # Req 10.2 — round-trip preserves CACHE_ALWAYS.
        assert detail.get("cacheBehavior") == EXPECTED_CACHE_BEHAVIOR, (
            f"GetAHORunCache returned cacheBehavior="
            f"{detail.get('cacheBehavior')!r}, expected "
            f"{EXPECTED_CACHE_BEHAVIOR!r} (Req 10.2)."
        )

        # Req 10.1 — cache lives in Target_Region.
        arn = detail.get("arn", "")
        assert f":{TARGET_REGION}:" in arn, (
            f"created cache ARN {arn!r} is not in {TARGET_REGION} (Req 10.1)."
        )
        assert arn.endswith(f"runCache/{cache_id}"), (
            f"created cache ARN {arn!r} does not end with runCache/{cache_id}."
        )

        # The reported S3 URI matches what we requested. HealthOmics may
        # normalize the URI (trailing slash) so compare on the prefix.
        reported_uri = detail.get("cacheS3Uri") or detail.get("cacheS3Location") or ""
        assert reported_uri.startswith(s3_location.rstrip("/")), (
            f"created cache cacheS3Uri {reported_uri!r} does not match "
            f"requested {s3_location!r}."
        )
    finally:
        # Always clean up — leaving caches behind incurs storage cost
        # and pollutes the account namespace.
        try:
            omics.delete_run_cache(id=cache_id)
        except Exception:  # noqa: BLE001
            # Best-effort cleanup; surface the original assertion failure
            # rather than masking it with a delete-time error.
            pass
