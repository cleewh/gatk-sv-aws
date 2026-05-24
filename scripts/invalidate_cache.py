"""Invalidate the cohort Run_Cache without deleting prior outputs.

Creates a fresh Run_Cache via ``CreateAHORunCache``, returns its id, and
leaves the old cache intact until ``--delete-old`` is passed (Req 10.5,
17.6; Design §Deployment → Rollback).

Usage:

    python gatk-sv-healthomics/scripts/invalidate_cache.py \\
        --new-cache-s3 s3://bucket/path \\
        [--old-cache-id <id> --delete-old]
"""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--new-cache-s3", required=True)
    parser.add_argument("--old-cache-id")
    parser.add_argument("--delete-old", action="store_true")
    parser.add_argument("--region", default="ap-southeast-1")
    args = parser.parse_args()

    import boto3

    client = boto3.client("omics", region_name=args.region)  # type: ignore[attr-defined]
    response = client.create_run_cache(
        name="gatk-sv-run-cache",
        cacheS3Location=args.new_cache_s3,
        cacheBehavior="CACHE_ALWAYS",
        tags={"gatk-sv:environment": "prod"},
    )
    new_id = response["id"]
    logger.info("new run-cache id=%s", new_id)

    if args.old_cache_id:
        if args.delete_old:
            client.delete_run_cache(id=args.old_cache_id)
            logger.info("deleted old cache id=%s", args.old_cache_id)
        else:
            logger.info(
                "old cache id=%s left intact (pass --delete-old to remove)",
                args.old_cache_id,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
