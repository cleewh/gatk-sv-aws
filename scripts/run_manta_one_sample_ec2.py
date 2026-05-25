#!/usr/bin/env python3
"""Run Manta on EC2 for one sample, mirroring the HealthOmics run.

This is a focused engine-divergence test: same Docker image, same
CRAM input, same reference, same Manta arguments — just running on
EC2 instead of HealthOmics. The output VCF body (records only,
metadata stripped) should be identical between the two engines.

Used by tests/gatk_sv_healthomics/acceptance/test_engine_divergence.py.
"""
from __future__ import annotations

import os

import argparse
import sys
import time

import boto3

REGION = "ap-southeast-1"
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "__ACCOUNT_ID__")
INSTANCE_ID = os.environ.get("GATK_SV_EC2_INSTANCE_ID", "__EC2_INSTANCE_ID__")
OUTPUT_BUCKET = f"healthomics-outputs-{ACCOUNT}-apse1"

MANTA_DOCKER = (
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
    "gatk-sv/manta:2023-09-14-v0.28.3-beta-3f22f94d"
)


def _ssm_run(commands: list[str], timeout: int = 600) -> tuple[str, str, str]:
    """Run a list of bash commands on EC2; wait for completion."""
    ssm = boto3.client("ssm", region_name=REGION)
    cmd = ssm.send_command(
        InstanceIds=[INSTANCE_ID],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands},
        TimeoutSeconds=timeout,
    )
    cid = cmd["Command"]["CommandId"]
    while True:
        time.sleep(10)
        inv = ssm.get_command_invocation(
            CommandId=cid, InstanceId=INSTANCE_ID
        )
        st = inv["Status"]
        if st in {"Success", "Failed", "TimedOut", "Cancelled"}:
            return (
                st,
                inv.get("StandardOutputContent", ""),
                inv.get("StandardErrorContent", ""),
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", default="NA12878")
    parser.add_argument(
        "--cram",
        default=(
            f"s3://omics-cohorts-ap-southeast-1-{ACCOUNT}/cohorts/"
            f"gatk-sv-validation-2026q2/NA12878.final.cram"
        ),
    )
    parser.add_argument(
        "--crai",
        default=(
            f"s3://healthomics-outputs-{ACCOUNT}-apse1/runs/"
            f"gatk-sv-e2e/NA12878/reindex/9773901/out/new_crai/NA12878.cram.crai"
        ),
    )
    parser.add_argument(
        "--ref-fasta",
        default=(
            f"s3://omics-ref-ap-southeast-1-{ACCOUNT}/gatk-sv/"
            f"reference/GRCh38/Homo_sapiens_assembly38.fasta"
        ),
    )
    parser.add_argument(
        "--ref-fai",
        default=(
            f"s3://omics-ref-ap-southeast-1-{ACCOUNT}/gatk-sv/"
            f"reference/GRCh38/Homo_sapiens_assembly38.fasta.fai"
        ),
    )
    parser.add_argument(
        "--region-bed",
        default=(
            f"s3://omics-ref-ap-southeast-1-{ACCOUNT}/gatk-sv/"
            f"reference/GRCh38/manta_region_bed"
        ),
    )
    parser.add_argument(
        "--region-bed-tbi",
        default=(
            f"s3://omics-ref-ap-southeast-1-{ACCOUNT}/gatk-sv/"
            f"reference/GRCh38/manta_region_bed.tbi"
        ),
    )
    parser.add_argument(
        "--out-prefix",
        default=(
            f"s3://{OUTPUT_BUCKET}/runs/gatk-sv-e2e/divergence"
        ),
    )
    args = parser.parse_args(argv)

    sample = args.sample
    out_dir = f"{args.out_prefix.rstrip('/')}/{sample}/ec2"

    inner_script = "\n".join([
        "#!/bin/bash",
        "set -euxo pipefail",
        "exec > /tmp/manta-divergence-runner.log 2>&1",
        f"WORK=/tmp/manta-divergence/{sample}",
        "mkdir -p $WORK/refs $WORK/work",
        "cd $WORK",
        # Re-auth ECR (token expires ~12h)
        f"aws ecr get-login-password --region {REGION} | docker login --username AWS --password-stdin {ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com >/dev/null 2>&1",
        # Stage all inputs idempotently — skip if already present.
        f'[ -f refs/Homo_sapiens_assembly38.fasta ] || aws s3 cp {args.ref_fasta} refs/Homo_sapiens_assembly38.fasta --quiet',
        f'[ -f refs/Homo_sapiens_assembly38.fasta.fai ] || aws s3 cp {args.ref_fai} refs/Homo_sapiens_assembly38.fasta.fai --quiet',
        f'[ -f refs/manta_region_bed ] || aws s3 cp {args.region_bed} refs/manta_region_bed --quiet',
        f'[ -f refs/manta_region_bed.tbi ] || aws s3 cp {args.region_bed_tbi} refs/manta_region_bed.tbi --quiet',
        f'[ -f refs/{sample}.cram ] || aws s3 cp {args.cram} refs/{sample}.cram --quiet',
        f'[ -f refs/{sample}.cram.crai ] || aws s3 cp {args.crai} refs/{sample}.cram.crai --quiet',
        "ls -la refs/",
        "df -h /tmp",
        f"docker pull {MANTA_DOCKER} 2>&1 | tail -1",
        # Build inner manta command in a separate heredoc-style file so quoting is clean.
        "cat > $WORK/manta_run.sh << 'INNER'",
        "#!/bin/bash",
        "set -euxo pipefail",
        # Clean any prior runDir so configManta.py doesn't error.
        "rm -rf /work/work/manta",
        f"/usr/local/bin/manta/bin/configManta.py --bam /work/refs/{sample}.cram --reference /work/refs/Homo_sapiens_assembly38.fasta --runDir /work/work/manta --callRegions /work/refs/manta_region_bed",
        "/work/work/manta/runWorkflow.py -j 8",
        f"cp /work/work/manta/results/variants/diploidSV.vcf.gz /work/{sample}.manta.vcf.gz",
        f"cp /work/work/manta/results/variants/diploidSV.vcf.gz.tbi /work/{sample}.manta.vcf.gz.tbi",
        "INNER",
        "chmod +x $WORK/manta_run.sh",
        f"docker run --rm -v $WORK:/work -w /work {MANTA_DOCKER} bash /work/manta_run.sh",
        f"ls -la $WORK/{sample}.manta.vcf.gz*",
        # Upload to S3 in the layout divergence_pull.py expects
        f"aws s3 cp $WORK/{sample}.manta.vcf.gz {out_dir}/manta.vcf.gz --quiet",
        f"aws s3 cp $WORK/{sample}.manta.vcf.gz.tbi {out_dir}/manta.vcf.gz.tbi --quiet",
        f"echo Uploaded to: {out_dir}",
        "echo MANTA-DIVERGENCE-DONE",
    ])

    # SSM runs commands via /bin/sh; we drop a bash script onto disk and
    # spawn it under nohup so this SSM call returns in seconds while
    # the actual ~1-2h Manta run keeps going in the background.
    import base64
    encoded = base64.b64encode(inner_script.encode()).decode()
    cmds = [
        f"echo {encoded} | base64 -d > /tmp/manta-divergence-runner.sh",
        "chmod +x /tmp/manta-divergence-runner.sh",
        # Fully detach: setsid creates a new session so the process
        # outlives the SSM session that spawned it.
        "setsid bash /tmp/manta-divergence-runner.sh < /dev/null > /dev/null 2>&1 &",
        "disown -a 2>/dev/null || true",
        "sleep 2",
        "pgrep -af manta-divergence-runner | head -3",
    ]

    print(f"Running Manta on EC2 for {sample}…")
    print(f"  Image:  {MANTA_DOCKER}")
    print(f"  CRAM:   {args.cram}")
    print(f"  Output: {out_dir}/manta.vcf.gz")
    print()

    status, stdout, stderr = _ssm_run(cmds, timeout=300)
    print(f"SSM status: {status}")
    print(stdout[-1500:])
    if status != "Success":
        print("--- STDERR ---")
        print(stderr[-1500:])
        return 1

    print()
    print("Manta runner spawned in background on EC2.")
    print("Poll progress with:")
    print(
        "  python3 gatk-sv-healthomics/scripts/check_manta_divergence.py"
    )
    print()
    print("When MANTA-DIVERGENCE-DONE appears in the log, finalize with:")
    print(
        f"  aws s3 cp s3://healthomics-outputs-{ACCOUNT}-apse1/runs/"
        f"gatk-sv-e2e/{sample}/optimized-v2/manta/8688241/out/manta_vcf/"
        f"{sample}.manta.vcf.gz "
        f"gatk-sv-healthomics/divergence/{sample}/healthomics/manta.vcf.gz"
    )
    print(
        f"  aws s3 cp {out_dir}/manta.vcf.gz "
        f"gatk-sv-healthomics/divergence/{sample}/ec2/manta.vcf.gz"
    )
    print(
        "  RUN_ACCEPTANCE_TESTS=1 "
        "/Users/cleewh/Desktop/KiroLS/.venv/bin/python -m pytest "
        "kiro-life-sciences/tests/gatk_sv_healthomics/acceptance/"
        f"test_engine_divergence.py -k {sample} -v"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
