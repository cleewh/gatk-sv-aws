"""Integration test: stage a couple lightweight reference files to S3 (Req 5.2).

Copies the Broad ``.fai`` and ``.dict`` from ``s3://broad-references/hg38/v0/``
to ``s3://omics-ref-ap-southeast-1-__ACCOUNT_ID__/gatk-sv/reference/GRCh38/``.
Cross-region copy through ``boto3.s3.copy``.

Skipped unless AWS credentials are active (see ``conftest.py``).
"""

from __future__ import annotations

import pytest

from gatk_sv_aws.reference import (
    ReferenceBundleManifest,
    ReferenceFile,
    stage_reference_bundle,
)

DESTINATION_BUCKET = "omics-ref-ap-southeast-1-__ACCOUNT_ID__"
DESTINATION_PREFIX = "gatk-sv/reference/GRCh38"


@pytest.mark.aws_integration
def test_stage_reference_lightweight_files() -> None:
    import boto3

    manifest = ReferenceBundleManifest(
        build="GRCh38",
        files=(
            ReferenceFile(
                logical_name="Homo_sapiens_assembly38.fasta.fai",
                source_uri="s3://broad-references/hg38/v0/Homo_sapiens_assembly38.fasta.fai",
                expected_md5=None,
                expected_sha256=None,
                size_bytes=160928,
            ),
            ReferenceFile(
                logical_name="Homo_sapiens_assembly38.dict",
                source_uri="s3://broad-references/hg38/v0/Homo_sapiens_assembly38.dict",
                expected_md5=None,
                expected_sha256=None,
                size_bytes=581244,
            ),
        ),
    )
    s3 = boto3.client("s3", region_name="ap-southeast-1")  # type: ignore[attr-defined]

    report = stage_reference_bundle(
        manifest,
        destination_bucket=DESTINATION_BUCKET,
        destination_prefix=DESTINATION_PREFIX,
        s3_client=s3,
    )

    assert report.all_succeeded, f"staging failed: {[f.reason for f in report.failed]}"
    assert len(report.succeeded) == 2

    # Verify the objects landed.
    for entry in report.succeeded:
        key = entry.destination_uri.removeprefix(f"s3://{DESTINATION_BUCKET}/")
        head = s3.head_object(Bucket=DESTINATION_BUCKET, Key=key)
        assert head["ContentLength"] > 0
