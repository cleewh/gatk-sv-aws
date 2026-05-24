#!/usr/bin/env python3
"""Launch MakeCohortVcf v13 (diagnostic build)."""
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
bundle_bytes = Path('gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/MakeCohortVcf-bundle-v13.zip').read_bytes()

print(f'Creating MakeCohortVcf-v13 ({len(bundle_bytes):,} bytes)...')
print('  Diagnostic build: extensive shell diagnostics before GATK invocation')

response = client.create_workflow(
    name='MakeCohortVcf-v13',
    description='v13: diagnostic build - extensive shell output before GATK GroupedSVCluster',
    engine='WDL',
    definitionZip=bundle_bytes,
    main='wdl/MakeCohortVcf.wdl',
    storageCapacity=20,
    tags={'gatk-sv:module': 'MakeCohortVcf', 'gatk-sv:version': 'v13'},
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

run = client.get_run(id='8724741')
params = run.get('parameters', {}).copy()
params.pop('track_bed_files', None)
params['track_bed_tarball'] = f'{REF_BASE}/gatk_sv_clustering_tracks.tar.gz'
params['track_names'] = ['SR', 'SD', 'RM']
# Use fresh paths for stratification configs
params['stratification_config_part1'] = f'{REF_BASE}/stratify_config.v2.part_one.tsv'
params['stratification_config_part2'] = f'{REF_BASE}/stratify_config.v2.part_two.tsv'

response = client.start_run(
    workflowId=workflow_id,
    name='make-cohort-vcf-v13-diagnostic',
    roleArn=ROLE_ARN,
    outputUri=f'{OUTPUT_BASE}/batch/make-cohort-vcf/',
    parameters=params,
    storageType='DYNAMIC',
    tags={'gatk-sv:module': 'MakeCohortVcf', 'gatk-sv:version': 'v13'},
)
print(f'\nRun started: {response["id"]}')
print(f'Workflow: {workflow_id}')
