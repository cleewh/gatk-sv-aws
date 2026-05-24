"""Stage reference files into the ap-southeast-1 reference bucket.

Drives ``stage_reference_bundle`` with two transports wired:

* ``s3://`` — via ``boto3.s3.copy`` (cross-region copy).
* ``gs://`` — via anonymous ``google-cloud-storage`` client (public buckets
  only; extend with credentials if staging private data).

Usage:

    # Stage every file in a manifest JSON:
    python gatk-sv-healthomics/scripts/stage_reference.py \\
        --manifest gatk-sv-healthomics/reference-bundle/manifests/GRCh38.json \\
        --bucket omics-ref-ap-southeast-1-__ACCOUNT_ID__ \\
        --prefix gatk-sv/reference/GRCh38

    # Stage only one logical name:
    python gatk-sv-healthomics/scripts/stage_reference.py \\
        --manifest gatk-sv-healthomics/reference-bundle/manifests/GRCh38.json \\
        --bucket omics-ref-ap-southeast-1-__ACCOUNT_ID__ \\
        --prefix gatk-sv/reference/GRCh38 \\
        --only Homo_sapiens_assembly38.fasta

The script reports per-file outcomes and exits 0 on full success, 4 on any
partial failure (matching the CLI stage-reference subcommand).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Stage only the named logical_name (repeatable).",
    )
    parser.add_argument(
        "--skip-checksums",
        action="store_true",
        help="Do not verify md5/sha256 even when present in the manifest.",
    )
    args = parser.parse_args()

    import boto3

    from kiro_life_sciences.gatk_sv_healthomics.reference import (
        ReferenceBundleManifest,
        ReferenceFile,
        load_manifest,
        stage_reference_bundle,
    )

    try:
        from google.cloud import storage as gcs_storage

        gcs_client = gcs_storage.Client.create_anonymous_client()

        def gcs_streamer(gs_uri: str) -> bytes:
            assert gs_uri.startswith("gs://")
            bucket_name, _, key = gs_uri[len("gs://") :].partition("/")
            return gcs_client.bucket(bucket_name).blob(key).download_as_bytes()

    except ImportError:
        logger.warning(
            "google-cloud-storage not installed; gs:// sources will fail. "
            "Install with: .venv/bin/pip install google-cloud-storage"
        )
        gcs_streamer = None  # type: ignore[assignment]

    manifest = load_manifest(args.manifest)
    if args.only:
        selected: list[ReferenceFile] = [
            f for f in manifest.files if f.logical_name in set(args.only)
        ]
        missing = set(args.only) - {f.logical_name for f in selected}
        if missing:
            logger.error("logical_name(s) not found in manifest: %s", ", ".join(missing))
            return 2
        manifest = ReferenceBundleManifest(build=manifest.build, files=tuple(selected))

    logger.info(
        "staging %d file(s) from %s to s3://%s/%s",
        len(manifest.files),
        args.manifest,
        args.bucket,
        args.prefix.rstrip("/"),
    )

    s3 = boto3.client("s3", region_name="ap-southeast-1")  # type: ignore[attr-defined]
    report = stage_reference_bundle(
        manifest,
        destination_bucket=args.bucket,
        destination_prefix=args.prefix,
        s3_client=s3,
        gcs_streamer=gcs_streamer,
        verify_checksums=not args.skip_checksums,
    )

    for entry in report.succeeded:
        logger.info("  OK   %s -> %s", entry.logical_name, entry.destination_uri)
    for entry in report.failed:
        logger.error("  FAIL %s: %s", entry.logical_name, entry.reason)

    logger.info(
        "result: %d succeeded, %d failed",
        len(report.succeeded),
        len(report.failed),
    )
    return 0 if report.all_succeeded else 4


if __name__ == "__main__":
    sys.exit(main())
