#!/usr/bin/env python3
"""Launch MakeCohortVcf v12 (track_bed_tarball workaround)."""
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
bundle_bytes = Path('gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/MakeCohortVcf-bundle-v12.zip').read_bytes()

print(f'Creating MakeCohortVcf-v12 ({len(bundle_bytes):,} bytes)...')
print('  Workaround: track_bed_files Array[File] -> File track_bed_tarball')

response = client.create_workflow(
    name='MakeCohortVcf-v12',
    description='v12: track_bed_tarball workaround for HealthOmics Array[File] localization issue',
    engine='WDL',
    definitionZip=bundle_bytes,
    main='wdl/MakeCohortVcf.wdl',
    storageCapacity=20,
    tags={'gatk-sv:module': 'MakeCohortVcf', 'gatk-sv:version': 'v12'},
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
        print(f'  FAILED: {resp.get("statusMessage", "unknown")[:300]}')
        sys.exit(1)

# Get original failed run params, swap track_bed_files for track_bed_tarball
run = client.get_run(id='8724741')
params = run.get('parameters', {}).copy()

# Remove old key, add new key
params.pop('track_bed_files', None)
params['track_bed_tarball'] = f'{REF_BASE}/gatk_sv_clustering_tracks.tar.gz'
params['track_names'] = ['SR', 'SD', 'RM']

print(f"  track_bed_tarball: {params['track_bed_tarball']}")
print(f"  track_names: {params['track_names']}")

response = client.start_run(
    workflowId=workflow_id,
    name='make-cohort-vcf-v12-tarball',
    roleArn=ROLE_ARN,
    outputUri=f'{OUTPUT_BASE}/batch/make-cohort-vcf/',
    parameters=params,
    storageType='DYNAMIC',
    tags={'gatk-sv:module': 'MakeCohortVcf', 'gatk-sv:version': 'v12'},
)
print(f'\n✓ Run started: {response["id"]}')
print(f'  Workflow: {workflow_id}')
