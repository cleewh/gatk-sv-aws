"""Integration test: ECR pull-through caches and HealthOmics access (Req 3.2, 3.3).

Skipped unless AWS credentials are active.
"""

from __future__ import annotations

import pytest


@pytest.mark.aws_integration
def test_pull_through_cache_rules_exist_for_required_upstreams() -> None:
    import boto3

    ecr = boto3.client("ecr", region_name="ap-southeast-1")  # type: ignore[attr-defined]
    response = ecr.describe_pull_through_cache_rules()
    rules = response.get("pullThroughCacheRules", [])

    upstream_urls = {rule["upstreamRegistryUrl"] for rule in rules}

    # Project created at least quay.io and public.ecr.aws PTCs
    # (see gatk-sv-healthomics/docs/phase-status.md).
    assert "quay.io" in upstream_urls or "public.ecr.aws" in upstream_urls, (
        f"expected quay.io or public.ecr.aws among {upstream_urls}; "
        "run scripts/deploy.py step 3 to provision them."
    )


@pytest.mark.aws_integration
def test_repositories_are_healthomics_accessible() -> None:
    """Repositories fronting pull-through caches must grant omics.amazonaws.com."""
    import boto3

    ecr = boto3.client("ecr", region_name="ap-southeast-1")  # type: ignore[attr-defined]
    repos = ecr.describe_repositories().get("repositories", [])

    # Only check repos that front a HealthOmics-usable PTC prefix. Other
    # repos in the account belong to unrelated projects and don't need
    # to grant HealthOmics access.
    ptc_prefixes = ("quay/", "ecr-public/")
    ptc_repos = [
        r for r in repos if r["repositoryName"].startswith(ptc_prefixes)
    ]
    if not ptc_repos:
        pytest.skip(
            "no PTC-backed repositories have been populated yet; "
            "HealthOmics will create them on first pull."
        )

    for repo in ptc_repos:
        try:
            policy = ecr.get_repository_policy(repositoryName=repo["repositoryName"])
            assert "omics.amazonaws.com" in policy["policyText"], (
                f"repository {repo['repositoryName']} lacks HealthOmics grant"
            )
        except ecr.exceptions.RepositoryPolicyNotFoundException:
            raise AssertionError(
                f"PTC-backed repository {repo['repositoryName']} has no policy; "
                "re-run GrantHealthOmicsRepository."
            )
