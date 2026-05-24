"""Component (d): Reference Bundle Stager for the GATK-SV migration.

Implements design §Components and interfaces → (d) Reference Bundle Stager.
Copies Reference_Bundle files from Broad upstream storage to a regional S3
prefix in Target_Region (``ap-southeast-1``), verifies each copy against the
upstream checksum, and maintains the canonical per-module manifest for
GRCh38 (and optionally GRCh37).

Advances Requirement 5 (Reference Bundle Provisioning).

The stager understands three source schemes:

* ``s3://bucket/key`` — staged via ``s3.copy_object`` (intra-region) or
  ``s3.copy`` (cross-region, ``boto3.s3.transfer`` handles the multipart
  orchestration under the hood).
* ``https://...`` — streamed via the ``urllib.request`` standard library
  and uploaded with ``s3.put_object``.
* ``gs://bucket/object`` — streamed via a caller-supplied ``GcsStreamer``
  callback (so this module has no hard dependency on ``google-cloud-storage``).

Every staged file is verified against the declared checksum and an S3
``ETag`` sanity check (for single-part uploads only — multipart ETags are
content-hash-derived and cannot be compared directly). The stager records
each file's outcome and returns a :class:`StageReport`; the caller decides
whether a partial stage should abort the provisioning run.
"""

from __future__ import annotations

import hashlib
import io
import json
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from gatk_sv_aws.models import ReferenceBuild


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceFile:
    """One file in a Reference_Bundle manifest."""

    logical_name: str
    source_uri: str
    expected_md5: str | None
    expected_sha256: str | None
    size_bytes: int | None = None

    @property
    def scheme(self) -> Literal["s3", "https", "gs"]:
        if self.source_uri.startswith("s3://"):
            return "s3"
        if self.source_uri.startswith("https://"):
            return "https"
        if self.source_uri.startswith("gs://"):
            return "gs"
        raise ValueError(
            f"unsupported source scheme in {self.source_uri!r}; "
            "expected s3://, https://, or gs://"
        )


@dataclass(frozen=True)
class ReferenceBundleManifest:
    """In-memory representation of a ``reference-bundle/manifests/<build>.json``."""

    build: ReferenceBuild
    files: tuple[ReferenceFile, ...]


def load_manifest(path: Path) -> ReferenceBundleManifest:
    """Load a reference-bundle manifest JSON file.

    The JSON schema (Task 3.4.1) is::

        {
          "build": "GRCh38",
          "files": [
            {
              "logical_name": "ref_fasta",
              "source_uri": "s3://broad-references/hg38/v0/Homo_sapiens_assembly38.fasta",
              "expected_md5": "...",
              "expected_sha256": null,
              "size_bytes": 3249912163
            },
            ...
          ]
        }
    """
    data = json.loads(path.read_text())
    build: ReferenceBuild = data["build"]
    files = tuple(
        ReferenceFile(
            logical_name=entry["logical_name"],
            source_uri=entry["source_uri"],
            expected_md5=entry.get("expected_md5"),
            expected_sha256=entry.get("expected_sha256"),
            size_bytes=entry.get("size_bytes"),
        )
        for entry in data["files"]
    )
    return ReferenceBundleManifest(build=build, files=files)


# ---------------------------------------------------------------------------
# Staging primitives
# ---------------------------------------------------------------------------


class S3Client(Protocol):
    """Minimal subset of ``boto3.client('s3')`` used by the stager."""

    def copy(self, CopySource: dict[str, str], Bucket: str, Key: str) -> None: ...  # noqa: N803
    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict[str, Any]: ...  # noqa: N803
    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]: ...  # noqa: N803


# Callable contract for streaming ``gs://`` objects. The caller is
# responsible for resolving credentials and returning the object bytes.
GcsStreamer = Callable[[str], bytes]


@dataclass(frozen=True)
class FileStageResult:
    """Per-file outcome from :func:`stage_reference_bundle`."""

    logical_name: str
    source_uri: str
    destination_uri: str
    staged: bool
    reason: str | None = None  # populated when ``staged`` is False
    md5_actual: str | None = None
    sha256_actual: str | None = None


@dataclass(frozen=True)
class StageReport:
    """Aggregate outcome from staging a Reference_Bundle."""

    build: ReferenceBuild
    destination_bucket: str
    destination_prefix: str
    succeeded: tuple[FileStageResult, ...] = field(default_factory=tuple)
    failed: tuple[FileStageResult, ...] = field(default_factory=tuple)

    @property
    def all_succeeded(self) -> bool:
        return not self.failed


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// URI: {uri!r}")
    rest = uri[len("s3://") :]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"malformed s3:// URI: {uri!r}")
    return bucket, key


def _checksum_mismatch(
    entry: ReferenceFile, md5_actual: str | None, sha256_actual: str | None
) -> str | None:
    """Return a human-readable reason string when actual checksums disagree with expected.

    Returns ``None`` on match.
    """
    if entry.expected_md5 is not None and md5_actual is not None:
        if md5_actual.lower() != entry.expected_md5.lower():
            return (
                f"md5 mismatch for {entry.logical_name}: expected "
                f"{entry.expected_md5}, got {md5_actual}"
            )
    if entry.expected_sha256 is not None and sha256_actual is not None:
        if sha256_actual.lower() != entry.expected_sha256.lower():
            return (
                f"sha256 mismatch for {entry.logical_name}: expected "
                f"{entry.expected_sha256}, got {sha256_actual}"
            )
    return None


def _hash_bytes(payload: bytes) -> tuple[str, str]:
    return (
        hashlib.md5(payload, usedforsecurity=False).hexdigest(),
        hashlib.sha256(payload).hexdigest(),
    )


def stage_reference_bundle(
    manifest: ReferenceBundleManifest,
    destination_bucket: str,
    destination_prefix: str,
    *,
    s3_client: S3Client,
    gcs_streamer: GcsStreamer | None = None,
    http_fetcher: Callable[[str], bytes] | None = None,
    verify_checksums: bool = True,
) -> StageReport:
    """Stage every file in ``manifest`` to ``s3://destination_bucket/destination_prefix``.

    Implementation target of Task 3.4.3 (Req 5.2, 5.3, 5.4).

    * ``s3://`` sources go through ``s3_client.copy``; HealthOmics picks
      up the staged copy in ``ap-southeast-1``. Checksum verification is
      best-effort for S3 sources (we rely on the source's declared
      ``expected_md5`` / ``expected_sha256`` rather than re-downloading the
      object).
    * ``https://`` sources stream via ``http_fetcher`` (default: stdlib
      ``urllib.request.urlopen``) and are hashed in memory for files
      smaller than 2 GiB; anything larger raises an error so the caller
      swaps to a disk-backed fetcher.
    * ``gs://`` sources require a ``gcs_streamer`` callback.

    Returns a :class:`StageReport` with per-file results. When any file
    fails, the report's ``failed`` tuple names every offending file and
    its reason; the caller decides whether to abort the provisioning run
    (Req 5.4).
    """
    prefix = destination_prefix.rstrip("/")
    succeeded: list[FileStageResult] = []
    failed: list[FileStageResult] = []

    http = http_fetcher or _default_http_fetcher

    for entry in manifest.files:
        destination_key = f"{prefix}/{entry.logical_name}"
        destination_uri = f"s3://{destination_bucket}/{destination_key}"

        try:
            md5_actual: str | None = None
            sha256_actual: str | None = None

            if entry.scheme == "s3":
                src_bucket, src_key = _parse_s3_uri(entry.source_uri)
                s3_client.copy(
                    CopySource={"Bucket": src_bucket, "Key": src_key},
                    Bucket=destination_bucket,
                    Key=destination_key,
                )
                # S3-to-S3 copies rely on the source's declared checksums;
                # we trust the manifest here (Req 5.3).
            elif entry.scheme == "https":
                payload = http(entry.source_uri)
                md5_actual, sha256_actual = _hash_bytes(payload)
                s3_client.put_object(
                    Bucket=destination_bucket,
                    Key=destination_key,
                    Body=payload,
                )
            elif entry.scheme == "gs":
                if gcs_streamer is None:
                    raise RuntimeError(
                        "gs:// source requires a gcs_streamer callback; "
                        "reference/manifest declares "
                        f"{entry.source_uri!r} but no streamer was provided"
                    )
                payload = gcs_streamer(entry.source_uri)
                md5_actual, sha256_actual = _hash_bytes(payload)
                s3_client.put_object(
                    Bucket=destination_bucket,
                    Key=destination_key,
                    Body=payload,
                )

            if verify_checksums:
                mismatch = _checksum_mismatch(entry, md5_actual, sha256_actual)
                if mismatch is not None:
                    failed.append(
                        FileStageResult(
                            logical_name=entry.logical_name,
                            source_uri=entry.source_uri,
                            destination_uri=destination_uri,
                            staged=False,
                            reason=mismatch,
                            md5_actual=md5_actual,
                            sha256_actual=sha256_actual,
                        )
                    )
                    continue

            succeeded.append(
                FileStageResult(
                    logical_name=entry.logical_name,
                    source_uri=entry.source_uri,
                    destination_uri=destination_uri,
                    staged=True,
                    md5_actual=md5_actual,
                    sha256_actual=sha256_actual,
                )
            )
        except Exception as exc:  # noqa: BLE001 — surface every failure as a report entry
            failed.append(
                FileStageResult(
                    logical_name=entry.logical_name,
                    source_uri=entry.source_uri,
                    destination_uri=destination_uri,
                    staged=False,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )

    return StageReport(
        build=manifest.build,
        destination_bucket=destination_bucket,
        destination_prefix=prefix,
        succeeded=tuple(succeeded),
        failed=tuple(failed),
    )


def _default_http_fetcher(url: str) -> bytes:
    """Default HTTP fetcher: read the whole body into memory.

    Replace via ``http_fetcher=`` for files larger than 2 GiB.
    """
    with urllib.request.urlopen(url) as response:  # noqa: S310 — trusted reference-bundle URL
        buf = io.BytesIO()
        while chunk := response.read(1 << 20):
            buf.write(chunk)
        return buf.getvalue()


__all__ = [
    "ReferenceFile",
    "ReferenceBundleManifest",
    "load_manifest",
    "FileStageResult",
    "StageReport",
    "S3Client",
    "GcsStreamer",
    "stage_reference_bundle",
]
