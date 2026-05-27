#!/usr/bin/env python3
"""Stage the 156-sample GATK-SV open-data CRAM cohort into the customer's
ap-southeast-1 cohorts bucket.

Source: ``s3://gatk-sv-data-us-east-1/cram/`` (Registry of Open Data on AWS,
managed by Loka Inc. — see https://registry.opendata.aws/gatk-sv-data/).
312 objects = 156 final.cram + 156 final.cram.crai, ~2.36 TiB total.

Destination: ``s3://omics-cohorts-ap-southeast-1-<account>/cohorts/gatk-sv-156/``

What it does:
  1. List the source bucket and group keys by sample (HG/NA prefix).
  2. Fan out parallel server-side ``s3 cp`` per sample (CRAM + CRAI). Each
     copy is bucket-to-bucket — the laptop only orchestrates.
  3. Verify every destination object's size matches the source.
  4. Write a HealthOmics-ready manifest to
     ``validation-cohort/inputs/manifest-gatk-sv-156.json`` so it can be fed
     directly to ``scripts/run_cohort_e2e.py --manifest``.

Cross-region egress (us-east-1 -> ap-southeast-1) is billed to the
destination AWS account; expect roughly $0.085 / GiB * 2.54 TB ~= $215.

Idempotent: re-running checks each destination object's ContentLength
against the source and only re-copies the ones that don't match.

Usage:
    AWS_ACCOUNT_ID=<account> .venv/bin/python \\
        scripts/stage_gatk_sv_open_data.py \\
        [--max-concurrency 16] [--samples HG00096,NA12878]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
import botocore

SRC_BUCKET = "gatk-sv-data-us-east-1"
SRC_REGION = "us-east-1"
SRC_PREFIX = "cram/"
DEST_REGION = "ap-southeast-1"
DEST_PREFIX = "cohorts/gatk-sv-156/"
ROOT = Path(__file__).resolve().parent.parent


def list_source_objects() -> list[dict]:
    """Return [{Key, Size, Sample, Kind}] for every CRAM/CRAI in source."""
    s3 = boto3.client("s3", region_name=SRC_REGION)
    objects = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=SRC_BUCKET, Prefix=SRC_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            base = Path(key).name
            if base.endswith(".final.cram"):
                sample = base[: -len(".final.cram")]
                kind = "cram"
            elif base.endswith(".final.cram.crai"):
                sample = base[: -len(".final.cram.crai")]
                kind = "crai"
            else:
                continue
            objects.append({
                "Key": key,
                "Size": obj["Size"],
                "Sample": sample,
                "Kind": kind,
            })
    return objects


def head_dest_size(s3_dest, dest_bucket: str, dest_key: str) -> int | None:
    try:
        r = s3_dest.head_object(Bucket=dest_bucket, Key=dest_key)
        return r["ContentLength"]
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def copy_one(src_obj: dict, dest_bucket: str) -> tuple[str, str, str]:
    """Server-side copy a single object via the AWS CLI. Returns (key, status, msg)."""
    src_key = src_obj["Key"]
    src_size = src_obj["Size"]
    base = Path(src_key).name
    dest_key = f"{DEST_PREFIX}{base}"

    s3_dest = boto3.client("s3", region_name=DEST_REGION)
    existing_size = head_dest_size(s3_dest, dest_bucket, dest_key)
    if existing_size == src_size:
        return (src_key, "skip", f"already at destination ({src_size} bytes)")

    src_uri = f"s3://{SRC_BUCKET}/{src_key}"
    dest_uri = f"s3://{dest_bucket}/{dest_key}"
    cmd = [
        "aws", "s3", "cp", src_uri, dest_uri,
        "--source-region", SRC_REGION,
        "--region", DEST_REGION,
        "--no-progress",
    ]
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - started
    if proc.returncode != 0:
        return (src_key, "fail", f"rc={proc.returncode} {proc.stderr.strip()[:300]}")

    final_size = head_dest_size(s3_dest, dest_bucket, dest_key)
    if final_size != src_size:
        return (
            src_key,
            "fail",
            f"size mismatch: src {src_size} vs dest {final_size}",
        )
    rate = src_size / max(elapsed, 0.001) / 1024 / 1024
    return (src_key, "ok", f"{src_size/2**30:.1f} GiB in {elapsed:.0f}s ({rate:.0f} MB/s)")


def write_manifest(samples: list[str], dest_bucket: str) -> Path:
    """Write a manifest compatible with run_cohort_e2e.py / run_gse_cohort.py."""
    manifest_path = ROOT / "validation-cohort" / "inputs" / "manifest-gatk-sv-156.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    base = f"s3://{dest_bucket}/{DEST_PREFIX.rstrip('/')}"
    manifest = {
        "cohort_id": "gatk-sv-156",
        "source": "s3://gatk-sv-data-us-east-1/cram/ (Registry of Open Data on AWS)",
        "destination_base": base,
        "sample_count": len(samples),
        "samples": [
            {
                "sample_id": sid,
                "cram": f"{base}/{sid}.final.cram",
                "crai": f"{base}/{sid}.final.cram.crai",
            }
            for sid in samples
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--max-concurrency", type=int, default=16,
                    help="Max parallel s3 cp processes (default 16). "
                         "Each cp is server-side so the laptop is just orchestrating.")
    ap.add_argument("--samples", default=None,
                    help="Optional comma-separated subset of sample IDs (default: all 156).")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be copied, but don't copy.")
    args = ap.parse_args()

    account = os.environ.get("AWS_ACCOUNT_ID")
    if not account:
        print("ERROR: AWS_ACCOUNT_ID env var required", file=sys.stderr)
        return 1
    dest_bucket = f"omics-cohorts-{DEST_REGION}-{account}"

    print(f"Source:      s3://{SRC_BUCKET}/{SRC_PREFIX}  ({SRC_REGION})")
    print(f"Destination: s3://{dest_bucket}/{DEST_PREFIX}  ({DEST_REGION})")
    print()
    print("Listing source objects...")
    src_objects = list_source_objects()
    samples = sorted({o["Sample"] for o in src_objects})
    if args.samples:
        wanted = set(args.samples.split(","))
        samples = [s for s in samples if s in wanted]
        src_objects = [o for o in src_objects if o["Sample"] in wanted]
    total_size = sum(o["Size"] for o in src_objects)
    print(f"  Samples:        {len(samples)}")
    print(f"  Objects:        {len(src_objects)}  ({sum(1 for o in src_objects if o['Kind'] == 'cram')} CRAM + {sum(1 for o in src_objects if o['Kind'] == 'crai')} CRAI)")
    print(f"  Total size:     {total_size/2**40:.2f} TiB ({total_size/2**30:.0f} GiB)")
    print(f"  Est. egress:    ${total_size/2**30 * 0.085:.0f}  (us-east-1 -> {DEST_REGION} @ $0.085/GiB)")
    print()

    if args.dry_run:
        print("Dry run; not copying.")
        for o in src_objects[:10]:
            print(f"  {o['Sample']}/{o['Kind']:5}: s3://{SRC_BUCKET}/{o['Key']}  ({o['Size']/2**30:.1f} GiB)")
        if len(src_objects) > 10:
            print(f"  ...and {len(src_objects) - 10} more")
        return 0

    print(f"Starting parallel copy with concurrency={args.max_concurrency}...")
    print()
    started = time.time()
    n_ok = n_skip = n_fail = 0
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.max_concurrency) as pool:
        futures = {pool.submit(copy_one, o, dest_bucket): o for o in src_objects}
        for i, future in enumerate(as_completed(futures), 1):
            o = futures[future]
            key, status, msg = future.result()
            if status == "ok":
                n_ok += 1
            elif status == "skip":
                n_skip += 1
            else:
                n_fail += 1
                failures.append((key, msg))
            tag = {"ok": "[OK]", "skip": "[--]", "fail": "[FAIL]"}[status]
            print(f"  {i:3}/{len(src_objects)} {tag} {Path(key).name:35} {msg}")

    elapsed = time.time() - started
    print()
    print(f"Done in {elapsed/60:.1f} min")
    print(f"  ok:   {n_ok}")
    print(f"  skip: {n_skip}")
    print(f"  fail: {n_fail}")
    if failures:
        print()
        print("Failures:")
        for key, msg in failures:
            print(f"  {key}: {msg}")
        return 2

    manifest_path = write_manifest(samples, dest_bucket)
    print()
    print(f"Manifest written: {manifest_path}")
    print(f"  Use with: scripts/run_cohort_e2e.py --cohort-id gatk-sv-156 --manifest {manifest_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
