#!/usr/bin/env python3
"""Provision the EC2 m5.2xlarge instance used for the MakeCohortVcf hybrid path.

Why we need an EC2 instance: HealthOmics terminates ``gatk GroupedSVCluster`` and
``svtk resolve`` at exactly 47s when invoked from inside MakeCohortVcf's deeply
nested sub-workflow chain.  Same task, same image, same inputs run cleanly on a
regular VM.  See ``issue-artifact/`` for the full reproduction and report.

This script:
  1. Picks the default VPC's first public subnet.
  2. Creates a security group ``gatk-sv-ec2-hybrid-sg`` (egress-only, no inbound).
  3. Creates an instance profile ``gatk-sv-ec2-hybrid`` granting:
       - SSM (so the orchestrator can dispatch commands)
       - Read on the reference + cohort + WDL buckets
       - Read/write on the outputs bucket
       - ECR pull access (for the GATK-SV images)
  4. Launches an ``m5.2xlarge`` running Amazon Linux 2023 with Docker pre-installed
     (UserData installs Docker on first boot).
  5. **Stops** the instance immediately so we only pay for storage until first use.

Idempotent: if an instance tagged ``gatk-sv:role=ec2-hybrid`` already exists,
prints its id and returns 0.
"""
from __future__ import annotations

import json
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

ROLE_NAME = "gatk-sv-ec2-hybrid"
SG_NAME = "gatk-sv-ec2-hybrid-sg"
TAG_KEY = "gatk-sv:role"
TAG_VALUE = "ec2-hybrid"

USER_DATA = """#!/bin/bash
set -euxo pipefail
yum update -y
yum install -y docker amazon-ssm-agent unzip
systemctl enable --now docker
systemctl enable --now amazon-ssm-agent
usermod -aG docker ssm-user || true
"""


def get_default_vpc(ec2) -> str:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise RuntimeError("No default VPC found in this region.")
    return vpcs[0]["VpcId"]


def get_subnet(ec2, vpc_id: str) -> str:
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    if not subnets:
        raise RuntimeError(f"No subnets in VPC {vpc_id}")
    # Prefer a public subnet (mapPublicIpOnLaunch==True).
    public = [s for s in subnets if s.get("MapPublicIpOnLaunch")]
    return (public or subnets)[0]["SubnetId"]


def ensure_security_group(ec2, vpc_id: str) -> str:
    existing = ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": [SG_NAME]},
        ],
    )["SecurityGroups"]
    if existing:
        return existing[0]["GroupId"]
    resp = ec2.create_security_group(
        GroupName=SG_NAME,
        Description="GATK-SV EC2 hybrid: egress-only, no inbound",
        VpcId=vpc_id,
        TagSpecifications=[
            {"ResourceType": "security-group",
             "Tags": [{"Key": TAG_KEY, "Value": TAG_VALUE}]},
        ],
    )
    return resp["GroupId"]


def ensure_instance_profile(account: str) -> str:
    iam = boto3.client("iam")
    try:
        iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }],
            }),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
    iam.attach_role_policy(RoleName=ROLE_NAME,
                           PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="ec2-hybrid-data",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow",
                 "Action": ["s3:GetObject", "s3:ListBucket", "s3:PutObject", "s3:AbortMultipartUpload",
                            "s3:GetObjectTagging", "s3:PutObjectTagging"],
                 "Resource": [f"arn:aws:s3:::omics-ref-*-{account}",
                              f"arn:aws:s3:::omics-ref-*-{account}/*",
                              f"arn:aws:s3:::omics-cohorts-*-{account}",
                              f"arn:aws:s3:::omics-cohorts-*-{account}/*",
                              f"arn:aws:s3:::omics-wdl-*-{account}",
                              f"arn:aws:s3:::omics-wdl-*-{account}/*",
                              f"arn:aws:s3:::healthomics-outputs-{account}-*",
                              f"arn:aws:s3:::healthomics-outputs-{account}-*/*"]},
                {"Effect": "Allow",
                 "Action": ["ecr:GetAuthorizationToken",
                            "ecr:BatchCheckLayerAvailability",
                            "ecr:GetDownloadUrlForLayer",
                            "ecr:BatchGetImage"],
                 "Resource": "*"},
                {"Effect": "Allow",
                 "Action": "ec2:CreateTags",
                 "Resource": f"arn:aws:ec2:*:{account}:instance/*"},
            ],
        }),
    )
    try:
        iam.create_instance_profile(InstanceProfileName=ROLE_NAME)
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
    try:
        iam.add_role_to_instance_profile(InstanceProfileName=ROLE_NAME, RoleName=ROLE_NAME)
    except ClientError as e:
        if e.response["Error"]["Code"] != "LimitExceeded":
            raise
    # Wait for instance profile to be visible to EC2 RunInstances.
    time.sleep(8)
    return ROLE_NAME


def latest_amazon_linux_2023(ec2) -> str:
    """Return the AMI id for the latest Amazon Linux 2023 x86_64."""
    images = ec2.describe_images(
        Owners=["amazon"],
        Filters=[
            {"Name": "name", "Values": ["al2023-ami-2023.*-x86_64"]},
            {"Name": "state", "Values": ["available"]},
            {"Name": "architecture", "Values": ["x86_64"]},
        ],
    )["Images"]
    if not images:
        raise RuntimeError("Could not find an Amazon Linux 2023 AMI.")
    return max(images, key=lambda i: i["CreationDate"])["ImageId"]


def existing_instance_id(ec2) -> str | None:
    resp = ec2.describe_instances(
        Filters=[
            {"Name": f"tag:{TAG_KEY}", "Values": [TAG_VALUE]},
            {"Name": "instance-state-name",
             "Values": ["pending", "running", "stopping", "stopped"]},
        ],
    )
    for r in resp["Reservations"]:
        for inst in r["Instances"]:
            return inst["InstanceId"]
    return None


def main() -> int:
    account = os.environ.get("AWS_ACCOUNT_ID")
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
    if not account:
        print("ERROR: AWS_ACCOUNT_ID env var required", file=sys.stderr)
        return 1

    ec2 = boto3.client("ec2", region_name=region)

    existing = existing_instance_id(ec2)
    if existing:
        print(f"EC2 hybrid instance already provisioned: {existing}")
        print(f"\nNext step: export GATK_SV_EC2_INSTANCE_ID={existing}")
        return 0

    print("Provisioning the GATK-SV EC2 hybrid instance...")
    vpc_id = get_default_vpc(ec2)
    subnet_id = get_subnet(ec2, vpc_id)
    sg_id = ensure_security_group(ec2, vpc_id)
    profile = ensure_instance_profile(account)
    ami = latest_amazon_linux_2023(ec2)
    print(f"  VPC={vpc_id} subnet={subnet_id} SG={sg_id} profile={profile} AMI={ami}")

    resp = ec2.run_instances(
        ImageId=ami,
        InstanceType="m5.2xlarge",
        MinCount=1,
        MaxCount=1,
        SecurityGroupIds=[sg_id],
        SubnetId=subnet_id,
        IamInstanceProfile={"Name": profile},
        BlockDeviceMappings=[
            {"DeviceName": "/dev/xvda",
             "Ebs": {"VolumeSize": 200, "VolumeType": "gp3", "DeleteOnTermination": True}},
        ],
        UserData=USER_DATA,
        TagSpecifications=[
            {"ResourceType": "instance",
             "Tags": [
                 {"Key": "Name", "Value": "gatk-sv-ec2-hybrid"},
                 {"Key": TAG_KEY, "Value": TAG_VALUE},
                 {"Key": "gatk-sv:environment", "Value": "production"},
             ]},
        ],
    )
    iid = resp["Instances"][0]["InstanceId"]
    print(f"  Launched {iid}, waiting for it to enter running state...")
    waiter = ec2.get_waiter("instance_running")
    waiter.wait(InstanceIds=[iid])
    print(f"  Stopping it (we only need it on for runs)...")
    ec2.stop_instances(InstanceIds=[iid])
    print(f"\n  EC2 hybrid instance: {iid} (currently stopping)")
    print(f"\nNext step: export GATK_SV_EC2_INSTANCE_ID={iid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
