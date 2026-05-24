"""Integration test: Run_Cache is provisioned with CACHE_ALWAYS (Req 10.1, 10.2).

Checks that a CACHE_ALWAYS run cache exists in ``ap-southeast-1`` so cohort
runs inherit caching automatically.

Skipped unless AWS credentials are active.
"""

from __future__ import annotations

import pytest


@pytest.mark.aws_integration
def test_cache_always_run_cache_exists() -> None:
    import boto3

    omics = boto3.client("omics", region_name="ap-southeast-1")  # type: ignore[attr-defined]
    response = omics.list_run_caches()
    caches = response.get("items", [])

    assert caches, (
        "no HealthOmics run caches in ap-southeast-1; "
        "run `scripts/deploy.py --step 9` to create one."
    )

    cache_always = [c for c in caches if c.get("cacheBehavior") == "CACHE_ALWAYS"]
    assert cache_always, (
        f"no CACHE_ALWAYS run caches among {len(caches)} caches. "
        "The project standard (Req 10.2) is CACHE_ALWAYS for cohort reruns."
    )

    # Pick the first CACHE_ALWAYS cache and confirm it's ACTIVE.
    cache = cache_always[0]
    detail = omics.get_run_cache(id=cache["id"])
    assert detail["status"] == "ACTIVE"
    assert detail["cacheS3Uri"].startswith("s3://")
