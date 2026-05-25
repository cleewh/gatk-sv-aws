#!/usr/bin/env python3
"""Pull GatherSampleEvidence outputs from S3 for the divergence test.

Layout produced:
    gatk-sv-healthomics/divergence/<sample-id>/healthomics/<artifact>
    gatk-sv-healthomics/divergence/<sample-id>/ec2/<artifact>

Healthomics outputs come from the prior production GSE run.
EC2 outputs are produced by ``run_gse_one_sample_ec2.sh`` (separate
script — runs the same sample on EC2 via miniwdl using the registered
HealthOmics WDL bundle).

Usage:
    python3 gatk-sv-healthomics/scripts/divergence_pull.py \\
      --sample NA12878 \\
      --healthomics-prefix s3://.../runs/.../gather-sample-evidence/<run-id>/out/

Both prefixes are inferred from ``gse-cohort-runs.json`` when not given.
"""
from __future__ import annotations

import os

import argparse
import json
import sys
from pathlib import Path

import boto3

REGION = "ap-southeast-1"
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "__ACCOUNT_ID__")
OUTPUT_BUCKET = f"healthomics-outputs-{ACCOUNT}-apse1"
DIVERGENCE_ROOT = Path("gatk-sv-healthomics/divergence")

# Mapping from local filename to the HealthOmics output key suffix
# (under <run-id>/out/<glob>).
HEALTHOMICS_KEY_PATTERNS: dict[str, str] = {
    "pe.txt.gz": "merged_PE/{sample}.pe.txt.gz",
    "sr.txt.gz": "merged_SR/{sample}.sr.txt.gz",
    "rd.txt.gz": "merged_bincov/{sample}.RD.txt.gz",
    "manta.vcf.gz": "manta_vcf/{sample}.manta.vcf.gz",
    "wham.vcf.gz": "wham_vcf/{sample}.wham.vcf.gz",
    "scramble.vcf.gz": "scramble_vcf/{sample}.scramble.vcf.gz",
}


def _existing_run_root_for_sample(sample: str) -> str:
    """Find the GSE run root for a given sample using gse-cohort-runs.json."""
    runs_path = Path("gatk-sv-healthomics/gse-cohort-runs.json")
    if not runs_path.exists():
        raise SystemExit(
            f"Cannot infer GSE run root: {runs_path} not found. "
            f"Pass --healthomics-prefix explicitly."
        )
    runs = json.loads(runs_path.read_text())
    for run in runs.get("runs", []):
        if run.get("sample") == sample and run.get("status") == "COMPLETED":
            return run["output_uri"].rstrip("/") + "/"
    raise SystemExit(f"No COMPLETED GSE run found for sample {sample!r}")


def _download(s3_uri: str, dest: Path) -> bool:
    """Best-effort copy. Returns False if the object doesn't exist."""
    s3 = boto3.client("s3", region_name=REGION)
    if not s3_uri.startswith("s3://"):
        raise ValueError(f"not an s3 URI: {s3_uri}")
    bucket, _, key = s3_uri[len("s3://"):].partition("/")
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except s3.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"NoSuchKey", "404"}:
            print(f"  (missing) {s3_uri}")
            return False
        raise
    dest.parent.mkdir(parents=True, exist_ok=True)
    s3.download_file(bucket, key, str(dest))
    print(f"  ok       {dest.name}  <- {s3_uri}")
    return True


def pull_healthomics(sample: str, prefix: str | None) -> int:
    if prefix is None:
        prefix = _existing_run_root_for_sample(sample)
    sample_dir = DIVERGENCE_ROOT / sample / "healthomics"
    pulled = 0
    for filename, pattern in HEALTHOMICS_KEY_PATTERNS.items():
        rel = pattern.format(sample=sample)
        if _download(f"{prefix}{rel}", sample_dir / filename):
            pulled += 1
    return pulled


def pull_ec2(sample: str, ec2_prefix: str) -> int:
    """Pull EC2-produced outputs from a user-supplied S3 prefix."""
    sample_dir = DIVERGENCE_ROOT / sample / "ec2"
    pulled = 0
    for filename in HEALTHOMICS_KEY_PATTERNS.keys():
        # EC2 producer script lays artifacts flat under the sample prefix.
        if _download(
            f"{ec2_prefix.rstrip('/')}/{filename}", sample_dir / filename
        ):
            pulled += 1
    return pulled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample", required=True, help="Sample id (e.g. NA12878)"
    )
    parser.add_argument(
        "--healthomics-prefix",
        help="HealthOmics GSE run output prefix (defaults to "
        "the COMPLETED run from gse-cohort-runs.json).",
    )
    parser.add_argument(
        "--ec2-prefix",
        required=True,
        help="S3 prefix where the EC2 producer uploaded the same sample's "
        "GSE artifacts (one folder containing all files in "
        "HEALTHOMICS_KEY_PATTERNS).",
    )
    args = parser.parse_args(argv)

    print(f"Pulling HealthOmics outputs for {args.sample}…")
    n_ho = pull_healthomics(args.sample, args.healthomics_prefix)
    print(f"  {n_ho} artifact(s) pulled.\n")

    print(f"Pulling EC2 outputs for {args.sample}…")
    n_ec2 = pull_ec2(args.sample, args.ec2_prefix)
    print(f"  {n_ec2} artifact(s) pulled.\n")

    if n_ho == 0 or n_ec2 == 0:
        print("No common artifacts; the divergence test will skip this sample.")
        return 0
    print(
        "Run the test with:\n"
        f"  RUN_ACCEPTANCE_TESTS=1 pytest "
        f"tests/gatk_sv_healthomics/acceptance/test_engine_divergence.py "
        f"-k {args.sample}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
