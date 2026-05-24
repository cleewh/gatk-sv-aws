#!/usr/bin/env python3
"""Launch MakeCohortVcf v11."""
import os
import boto3
import time
import sys
from pathlib import Path

REGION = 'ap-southeast-1'
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "__ACCOUNT_ID__")
ROLE_ARN = f'arn:aws:iam::{ACCOUNT}:role/gatk-sv-healthomics-run-role'
OUTPUT_BASE = f's3://healthomics-outputs-{ACCOUNT}-apse1/runs/gatk-sv-e2e'
REF_BASE = f's3://omics-ref-{REGION}-{ACCOUNT}/gatk-sv/reference/GRCh38'

client = boto3.client('omics', region_name=REGION)
bundle_bytes = Path('gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/MakeCohortVcf-bundle-v11.zip').read_bytes()

print(f'Creating MakeCohortVcf-v11 ({len(bundle_bytes):,} bytes)...')
print('  GroupedSVClusterTask: 4 vCPU + 8 GiB + no localization_optional')

response = client.create_workflow(
    name='MakeCohortVcf-v11',
    description='v11: 4 vCPU + 8 GiB GroupedSVCluster, no localization_optional, corrected refs',
    engine='WDL',
    definitionZip=bundle_bytes,
    main='wdl/MakeCohortVcf.wdl',
    storageCapacity=20,
    tags={'gatk-sv:module': 'MakeCohortVcf', 'gatk-sv:version': 'v11'},
)
workflow_id = response['id']
print(f'  Workflow created: {workflow_id}')

for i in range(30):
    time.sleep(10)
    resp = client.get_workflow(id=workflow_id)
    status = resp.get('status', 'UNKNOWN')
    if status == 'ACTIVE':
        print(f'  Workflow ACTIVE')
        break
    elif status in ('FAILED', 'DELETED'):
        print(f'  FAILED')
        sys.exit(1)

run = client.get_run(id='8724741')
params = run.get('parameters', {}).copy()
params['track_bed_files'] = [
    f'{REF_BASE}/hg38.SimpRep.sorted.pad_100.merged.bed.gz',
    f'{REF_BASE}/segdups.bed.gz',
    f'{REF_BASE}/rmsk.bed.gz',
]
params['track_names'] = ['SR', 'SD', 'RM']

response = client.start_run(
    workflowId=workflow_id,
    name='make-cohort-vcf-v11-4cpu',
    roleArn=ROLE_ARN,
    outputUri=f'{OUTPUT_BASE}/batch/make-cohort-vcf/',
    parameters=params,
    storageType='DYNAMIC',
    tags={'gatk-sv:module': 'MakeCohortVcf', 'gatk-sv:version': 'v11'},
)
print(f'  Run started: {response["id"]}')
print(f'  Workflow: {workflow_id}')
