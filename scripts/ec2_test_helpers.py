#!/usr/bin/env python3
"""Helpers for running EC2 commands via SSM for the MakeCohortVcf debug test."""

import os
import boto3
import json
import time
import sys

REGION = "ap-southeast-1"
INSTANCE_ID = os.environ.get("GATK_SV_EC2_INSTANCE_ID", "__EC2_INSTANCE_ID__")


def run_command(commands, timeout=600, verbose=True):
    """Run a list of shell commands on the EC2 instance via SSM and return output."""
    ssm = boto3.client("ssm", region_name=REGION)
    if isinstance(commands, str):
        commands = [commands]

    response = ssm.send_command(
        InstanceIds=[INSTANCE_ID],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands, "executionTimeout": [str(timeout)]},
        TimeoutSeconds=timeout,
    )
    cmd_id = response["Command"]["CommandId"]
    if verbose:
        print(f"  CommandId: {cmd_id}")

    # Poll for completion
    for i in range(timeout // 5):
        time.sleep(5)
        try:
            inv = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=INSTANCE_ID)
            status = inv.get("Status")
            if status in ("Success", "Failed", "Cancelled", "TimedOut"):
                return {
                    "status": status,
                    "stdout": inv.get("StandardOutputContent", ""),
                    "stderr": inv.get("StandardErrorContent", ""),
                    "exit_code": inv.get("ResponseCode"),
                }
            if verbose and (i + 1) % 6 == 0:
                print(f"  [{(i+1)*5}s] Status: {status}")
        except ssm.exceptions.InvocationDoesNotExist:
            continue

    return {"status": "Timeout", "stdout": "", "stderr": "", "exit_code": -1}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: ec2_test_helpers.py <command>")
        sys.exit(1)
    result = run_command(sys.argv[1])
    print(f"Status: {result['status']} (exit {result['exit_code']})")
    if result["stdout"]:
        print("STDOUT:")
        print(result["stdout"])
    if result["stderr"]:
        print("STDERR:")
        print(result["stderr"])
