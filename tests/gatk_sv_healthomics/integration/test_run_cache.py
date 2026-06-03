"""Integration test: production HealthOmics Run_Cache (Reqs 10.1, 10.2).

Verifies the cache lifecycle row in Design §Testing Strategy → integration
table:

    | Run cache lifecycle | CreateAHORunCache, GetAHORunCache |
    | Cache present in ap-southeast-1; status ACTIVE;            |
    | cache_behavior = CACHE_ALWAYS                              |

Concretely this test asserts that the production cache provisioned by
``scripts/deploy.py --step 9`` (Design §Deployment Step 9, which calls
``CreateAHORunCache(cache_behavior="CACHE_ALWAYS", ...)``) is reachable
via ``GetAHORunCache`` and is configured with:

* ``cacheBehavior == "CACHE_ALWAYS"`` (Req 10.2)
* ``status == "ACTIVE"``
* ARN bound to the configured account (``GATK_SV_AWS_ACCOUNT_ID``) and
  region ``ap-southeast-1``
  (Req 10.1: cache exists in Target_Region for production cohort runs)

The cache is created idempotently as part of deployment and re-used by
every cohort submission, so this test is a safe non-mutating probe.

Skipped unless ``RUN_INTEGRATION_TESTS=1`` and AWS credentials for
``ap-southeast-1`` are reachable (see ``conftest.py``).
"""

from __future__ import annotations

import os

import pytest

# Production HealthOmics Run_Cache, provisioned by deploy.py --step 9.
# Documented in docs/phase-status.md and runtime/runtime-environment.json.
PROD_CACHE_ID = "9564200"
# Account is environment-specific; keep the repo customer-agnostic.
PROD_ACCOUNT_ID = os.environ.get("GATK_SV_AWS_ACCOUNT_ID", "")
TARGET_REGION = "ap-southeast-1"
EXPECTED_CACHE_BEHAVIOR = "CACHE_ALWAYS"
EXPECTED_STATUS = "ACTIVE"


@pytest.mark.integration
def test_production_run_cache_is_cache_always() -> None:
    """Production Run_Cache is ACTIVE in ap-southeast-1 with CACHE_ALWAYS.

    Validates: Requirements 10.1, 10.2.
    """
    import boto3

    omics = boto3.client("omics", region_name=TARGET_REGION)  # type: ignore[attr-defined]

    # GetAHORunCache → boto3 omics.get_run_cache.
    detail = omics.get_run_cache(id=PROD_CACHE_ID)

    # Req 10.1 — cache exists in Target_Region.
    assert detail.get("status") == EXPECTED_STATUS, (
        f"run cache {PROD_CACHE_ID} status is {detail.get('status')!r}, "
        f"expected {EXPECTED_STATUS!r}"
    )

    # Req 10.2 — default behavior is CACHE_ALWAYS.
    assert detail.get("cacheBehavior") == EXPECTED_CACHE_BEHAVIOR, (
        f"run cache {PROD_CACHE_ID} cacheBehavior is "
        f"{detail.get('cacheBehavior')!r}, expected {EXPECTED_CACHE_BEHAVIOR!r}"
    )

    # Account + region binding: ARN is shaped
    #   arn:aws:omics:<region>:<account>:runCache/<id>
    arn = detail.get("arn", "")
    assert f":{TARGET_REGION}:" in arn, (
        f"run cache ARN {arn!r} is not in {TARGET_REGION}"
    )
    if PROD_ACCOUNT_ID:
        assert f":{PROD_ACCOUNT_ID}:" in arn, (
            f"run cache ARN {arn!r} does not belong to account {PROD_ACCOUNT_ID}"
        )
    assert arn.endswith(f"runCache/{PROD_CACHE_ID}"), (
        f"run cache ARN {arn!r} does not end with runCache/{PROD_CACHE_ID}"
    )

    # Sanity: cache must be S3-backed for cross-run persistence.
    s3_uri = detail.get("cacheS3Uri") or detail.get("cacheS3Location")
    assert s3_uri and s3_uri.startswith("s3://"), (
        f"run cache cacheS3Uri not configured: {s3_uri!r}"
    )


@pytest.mark.integration
def test_production_run_cache_listed_among_account_caches() -> None:
    """The production cache shows up in ``list_run_caches`` for the account.

    Validates: Requirement 10.1 (cache present in ap-southeast-1).
    """
    import boto3

    omics = boto3.client("omics", region_name=TARGET_REGION)  # type: ignore[attr-defined]

    response = omics.list_run_caches()
    items = response.get("items", [])

    assert items, (
        f"no HealthOmics run caches in {TARGET_REGION}; "
        "run `scripts/deploy.py --step 9` to create one."
    )

    matching = [c for c in items if str(c.get("id")) == PROD_CACHE_ID]
    assert matching, (
        f"production cache id {PROD_CACHE_ID!r} not found among "
        f"{[c.get('id') for c in items]} in {TARGET_REGION}"
    )

    cache = matching[0]
    assert cache.get("cacheBehavior") == EXPECTED_CACHE_BEHAVIOR, (
        f"listed cache {PROD_CACHE_ID} cacheBehavior is "
        f"{cache.get('cacheBehavior')!r}, expected {EXPECTED_CACHE_BEHAVIOR!r}"
    )
    assert cache.get("status") == EXPECTED_STATUS, (
        f"listed cache {PROD_CACHE_ID} status is {cache.get('status')!r}, "
        f"expected {EXPECTED_STATUS!r}"
    )
