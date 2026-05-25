#!/usr/bin/env python3
"""Poll the Manta divergence run on EC2."""
from __future__ import annotations

import sys
import time

import os
import boto3

REGION = "ap-southeast-1"
INSTANCE_ID = os.environ.get("GATK_SV_EC2_INSTANCE_ID", "__EC2_INSTANCE_ID__")


def main() -> int:
    ssm = boto3.client("ssm", region_name=REGION)
    cmd = ssm.send_command(
        InstanceIds=[INSTANCE_ID],
        DocumentName="AWS-RunShellScript",
        Parameters={
            "commands": [
                "echo === pgrep manta ===",
                "pgrep -af 'manta-divergence-runner' | head -3 || echo NOT_RUNNING",
                "echo === log tail ===",
                "tail -25 /tmp/manta-divergence-runner.log 2>/dev/null || echo NO_LOG_YET",
                "echo === outputs ===",
                "ls -la /tmp/manta-divergence/*/?*manta.vcf.gz* 2>/dev/null || echo NO_VCF_YET",
            ]
        },
    )
    cid = cmd["Command"]["CommandId"]
    for _ in range(20):
        time.sleep(5)
        inv = ssm.get_command_invocation(
            CommandId=cid, InstanceId=INSTANCE_ID
        )
        if inv["Status"] in {"Success", "Failed", "TimedOut", "Cancelled"}:
            break
    print(inv.get("StandardOutputContent", "")[:4000])
    if inv.get("StandardErrorContent"):
        print("--- STDERR ---")
        print(inv["StandardErrorContent"][:1000])
    return 0 if inv["Status"] == "Success" else 1


if __name__ == "__main__":
    sys.exit(main())
