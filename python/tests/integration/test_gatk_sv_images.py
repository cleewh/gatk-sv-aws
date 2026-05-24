"""Integration test: every GATK-SV container image is in ECR and HealthOmics-accessible.

Reads the image list from the container registry map's ``imageMappings``
and HEADs every ``destinationImage`` via ECR's ``DescribeImages`` to verify
presence, then confirms the repository policy grants HealthOmics access.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REGISTRY_MAP_PATH = (
    Path(__file__).resolve().parents[4]
    / "gatk-sv-healthomics"
    / "container-registry-map"
    / "container-registry-map.json"
)


@pytest.mark.aws_integration
def test_every_mapped_image_is_in_ecr_and_accessible() -> None:
    import boto3

    if not REGISTRY_MAP_PATH.exists():
        pytest.skip(f"no registry map at {REGISTRY_MAP_PATH}")

    data = json.loads(REGISTRY_MAP_PATH.read_text())
    mappings = data.get("imageMappings", [])
    if not mappings:
        pytest.skip("no imageMappings in registry map")

    ecr = boto3.client("ecr", region_name="ap-southeast-1")  # type: ignore[attr-defined]

    missing: list[str] = []
    inaccessible: list[str] = []

    for m in mappings:
        dest = m["destinationImage"]
        # Format: <account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>
        _, path = dest.split("/", 1)
        repo, _, tag = path.partition(":")
        try:
            ecr.describe_images(
                repositoryName=repo, imageIds=[{"imageTag": tag}]
            )
        except ecr.exceptions.ImageNotFoundException:
            missing.append(dest)
            continue
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{dest} ({type(exc).__name__}: {exc})")
            continue

        try:
            policy = ecr.get_repository_policy(repositoryName=repo)
            if "omics.amazonaws.com" not in policy["policyText"]:
                inaccessible.append(dest)
        except ecr.exceptions.RepositoryPolicyNotFoundException:
            inaccessible.append(f"{dest} (no policy)")

    assert not missing, "images not in ECR:\n  " + "\n  ".join(missing)
    assert not inaccessible, (
        "images without HealthOmics access grant:\n  " + "\n  ".join(inaccessible)
    )
