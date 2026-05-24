"""Unit tests for the GATK-SV Reference Bundle Stager (Design §Components.d).

Uses a fake S3 client to exercise success, checksum-mismatch, and
scheme-handling paths of :func:`stage_reference_bundle` (Req 5.3, 5.4).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from gatk_sv_aws.reference import (
    ReferenceBundleManifest,
    ReferenceFile,
    load_manifest,
    stage_reference_bundle,
)


class FakeS3Client:
    def __init__(self, *, raise_on_copy: bool = False) -> None:
        self.copy_calls: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []
        self.raise_on_copy = raise_on_copy

    def copy(
        self,
        CopySource: dict[str, str],  # noqa: N803
        Bucket: str,  # noqa: N803
        Key: str,  # noqa: N803
    ) -> None:
        if self.raise_on_copy:
            raise RuntimeError("forced failure")
        self.copy_calls.append(
            {"CopySource": dict(CopySource), "Bucket": Bucket, "Key": Key}
        )

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict[str, Any]:  # noqa: N803
        self.put_calls.append({"Bucket": Bucket, "Key": Key, "Body": Body})
        return {"ETag": hashlib.md5(Body, usedforsecurity=False).hexdigest()}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"ETag": "dummy"}


def test_load_manifest_round_trip(tmp_path: Path) -> None:
    manifest_path = tmp_path / "GRCh38.json"
    manifest_path.write_text(
        json.dumps(
            {
                "build": "GRCh38",
                "files": [
                    {
                        "logical_name": "ref_fasta",
                        "source_uri": "s3://broad-references/hg38/v0/ref.fasta",
                        "expected_md5": "abc123",
                        "expected_sha256": None,
                        "size_bytes": 42,
                    }
                ],
            }
        )
    )

    manifest = load_manifest(manifest_path)

    assert manifest.build == "GRCh38"
    assert len(manifest.files) == 1
    assert manifest.files[0].logical_name == "ref_fasta"
    assert manifest.files[0].scheme == "s3"


def test_s3_source_uses_copy(tmp_path: Path) -> None:
    manifest = ReferenceBundleManifest(
        build="GRCh38",
        files=(
            ReferenceFile(
                logical_name="ref.fasta",
                source_uri="s3://broad-ref/hg38/v0/ref.fasta",
                expected_md5=None,
                expected_sha256=None,
            ),
        ),
    )
    client = FakeS3Client()

    report = stage_reference_bundle(
        manifest,
        destination_bucket="omics-ref-ap-southeast-1",
        destination_prefix="gatk-sv/GRCh38",
        s3_client=client,
    )

    assert report.all_succeeded is True
    assert len(report.succeeded) == 1
    assert client.copy_calls == [
        {
            "CopySource": {"Bucket": "broad-ref", "Key": "hg38/v0/ref.fasta"},
            "Bucket": "omics-ref-ap-southeast-1",
            "Key": "gatk-sv/GRCh38/ref.fasta",
        }
    ]


def test_https_source_streams_and_hashes() -> None:
    payload = b"genome data"
    expected_md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    expected_sha256 = hashlib.sha256(payload).hexdigest()

    manifest = ReferenceBundleManifest(
        build="GRCh38",
        files=(
            ReferenceFile(
                logical_name="ref.fasta",
                source_uri="https://example.com/ref.fasta",
                expected_md5=expected_md5,
                expected_sha256=expected_sha256,
            ),
        ),
    )
    client = FakeS3Client()

    report = stage_reference_bundle(
        manifest,
        destination_bucket="omics-ref",
        destination_prefix="gatk-sv/GRCh38",
        s3_client=client,
        http_fetcher=lambda url: payload,
    )

    assert report.all_succeeded is True
    assert len(client.put_calls) == 1
    assert client.put_calls[0]["Body"] == payload
    assert report.succeeded[0].md5_actual == expected_md5
    assert report.succeeded[0].sha256_actual == expected_sha256


def test_checksum_mismatch_reports_failure_with_file_name() -> None:
    payload = b"wrong bytes"
    manifest = ReferenceBundleManifest(
        build="GRCh38",
        files=(
            ReferenceFile(
                logical_name="ref.fasta",
                source_uri="https://example.com/ref.fasta",
                expected_md5="deadbeef" * 4,  # wrong
                expected_sha256=None,
            ),
        ),
    )
    client = FakeS3Client()

    report = stage_reference_bundle(
        manifest,
        destination_bucket="omics-ref",
        destination_prefix="gatk-sv/GRCh38",
        s3_client=client,
        http_fetcher=lambda url: payload,
    )

    assert report.all_succeeded is False
    assert len(report.failed) == 1
    failed = report.failed[0]
    assert failed.logical_name == "ref.fasta"
    assert failed.reason is not None
    assert "md5 mismatch" in failed.reason


def test_partial_failure_records_both_outcomes(tmp_path: Path) -> None:
    payload_good = b"good"
    payload_bad = b"bad"
    manifest = ReferenceBundleManifest(
        build="GRCh38",
        files=(
            ReferenceFile(
                logical_name="good",
                source_uri="https://example.com/good",
                expected_md5=hashlib.md5(
                    payload_good, usedforsecurity=False
                ).hexdigest(),
                expected_sha256=None,
            ),
            ReferenceFile(
                logical_name="bad",
                source_uri="https://example.com/bad",
                expected_md5="0" * 32,  # wrong
                expected_sha256=None,
            ),
        ),
    )
    client = FakeS3Client()

    payloads = {
        "https://example.com/good": payload_good,
        "https://example.com/bad": payload_bad,
    }

    report = stage_reference_bundle(
        manifest,
        destination_bucket="omics-ref",
        destination_prefix="gatk-sv/GRCh38",
        s3_client=client,
        http_fetcher=lambda url: payloads[url],
    )

    assert not report.all_succeeded
    assert [r.logical_name for r in report.succeeded] == ["good"]
    assert [r.logical_name for r in report.failed] == ["bad"]


def test_gs_without_streamer_fails_gracefully() -> None:
    manifest = ReferenceBundleManifest(
        build="GRCh38",
        files=(
            ReferenceFile(
                logical_name="ref.fasta",
                source_uri="gs://broad-references/hg38/v0/ref.fasta",
                expected_md5=None,
                expected_sha256=None,
            ),
        ),
    )
    client = FakeS3Client()

    report = stage_reference_bundle(
        manifest,
        destination_bucket="omics-ref",
        destination_prefix="gatk-sv/GRCh38",
        s3_client=client,
    )

    assert not report.all_succeeded
    assert "gs://" in report.failed[0].reason  # type: ignore[operator]


def test_s3_copy_failure_is_reported(tmp_path: Path) -> None:
    manifest = ReferenceBundleManifest(
        build="GRCh38",
        files=(
            ReferenceFile(
                logical_name="ref.fasta",
                source_uri="s3://broad-ref/hg38/v0/ref.fasta",
                expected_md5=None,
                expected_sha256=None,
            ),
        ),
    )
    client = FakeS3Client(raise_on_copy=True)

    report = stage_reference_bundle(
        manifest,
        destination_bucket="omics-ref",
        destination_prefix="gatk-sv/GRCh38",
        s3_client=client,
    )

    assert not report.all_succeeded
    assert "RuntimeError" in report.failed[0].reason  # type: ignore[operator]
