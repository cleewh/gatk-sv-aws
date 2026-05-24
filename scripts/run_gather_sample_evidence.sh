#!/usr/bin/env bash
# Run the full GatherSampleEvidence pipeline for one sample.
#
# Usage:
#   export AWS_DEFAULT_REGION=ap-southeast-1
#   bash gatk-sv-healthomics/scripts/run_gather_sample_evidence.sh NA12878
#
# Prerequisites:
#   - CRAM + CRAI staged in the cohort bucket
#   - Reference bundle staged
#   - GATK jar uploaded to reference bucket
#   - All workflow IDs registered (see below)
#
# This script:
#   1. Runs the reindex preprocessing step
#   2. Waits for completion
#   3. Launches all 5 GatherSampleEvidence tasks in parallel
#   4. Waits for all to complete
#   5. Reports cost via AnalyzeAHORunPerformance

set -eo pipefail

SAMPLE_ID="${1:?Usage: $0 <sample_id>}"
REGION="${AWS_DEFAULT_REGION:-ap-southeast-1}"
ACCOUNT_ID="${AWS_ACCOUNT_ID:?Set AWS_ACCOUNT_ID env var to your 12-digit AWS account ID}"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/gatk-sv-healthomics-run-role"
CACHE_ID="9564200"
OUTPUT_BASE="s3://healthomics-outputs-${ACCOUNT_ID}-apse1/runs/gatk-sv-e2e/${SAMPLE_ID}"

# Buckets
COHORT_BUCKET="omics-cohorts-${REGION}-${ACCOUNT_ID}"
REF_BUCKET="omics-ref-${REGION}-${ACCOUNT_ID}"

# Input paths
CRAM="s3://${COHORT_BUCKET}/cohorts/gatk-sv-validation-2026q2/${SAMPLE_ID}.final.cram"
CRAI="s3://${COHORT_BUCKET}/cohorts/gatk-sv-validation-2026q2/${SAMPLE_ID}.final.cram.crai"
REF="s3://${REF_BUCKET}/gatk-sv/reference/GRCh38/Homo_sapiens_assembly38.fasta"
REF_FAI="s3://${REF_BUCKET}/gatk-sv/reference/GRCh38/Homo_sapiens_assembly38.fasta.fai"
REF_DICT="s3://${REF_BUCKET}/gatk-sv/reference/GRCh38/Homo_sapiens_assembly38.dict"
GATK_JAR="s3://${REF_BUCKET}/gatk-sv/reference/GRCh38/gatk-4.6.2.0-local.jar"
INTERVALS="s3://${REF_BUCKET}/gatk-sv/reference/GRCh38/gs_preprocessed_intervals.interval_list"
CONTIGS="s3://${REF_BUCKET}/gatk-sv/reference/GRCh38/gs_primary_contigs.list"
DBSNP="s3://${REF_BUCKET}/gatk-sv/reference/GRCh38/Homo_sapiens_assembly38.dbsnp138.vcf"
MANTA_BED="s3://${REF_BUCKET}/gatk-sv/reference/GRCh38/manta_region_bed"
MANTA_TBI="s3://${REF_BUCKET}/gatk-sv/reference/GRCh38/manta_region_bed.tbi"
MEI_BED="s3://${REF_BUCKET}/gatk-sv/reference/GRCh38/mei_bed"
WHAM_BED="s3://${REF_BUCKET}/gatk-sv/reference/GRCh38/wham_include_list.bed"

# Images
SV_BASE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/gatk-sv/sv-base:2024-10-25-v0.29-beta-5ea22a52"
MANTA_IMG="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/gatk-sv/manta:2023-09-14-v0.28.3-beta-3f22f94d"
WHAM_IMG="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/gatk-sv/wham:2024-10-25-v0.29-beta-5ea22a52"
SCRAMBLE_IMG="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/gatk-sv/scramble:2024-10-25-v0.29-beta-5ea22a52"

# Workflow IDs (registered in HealthOmics)
WF_REINDEX="8437840"
WF_COLLECT_COUNTS="3901751"
WF_COLLECT_SV="7038412"
WF_MANTA="6943475"
WF_WHAM="4183891"
WF_SCRAMBLE="1324647"

echo "=== GatherSampleEvidence: ${SAMPLE_ID} ==="
echo "Region: ${REGION}"
echo ""

# Step 1: Reindex CRAM
echo "[1/6] Reindexing CRAM..."
REINDEX_ID=$(aws omics start-run \
  --workflow-id ${WF_REINDEX} \
  --name "reindex-${SAMPLE_ID}" \
  --role-arn "${ROLE_ARN}" \
  --output-uri "${OUTPUT_BASE}/reindex/" \
  --storage-type DYNAMIC \
  --parameters "{\"cram_file\":\"${CRAM}\",\"sample_id\":\"${SAMPLE_ID}\",\"docker\":\"${SV_BASE}\"}" \
  --region ${REGION} \
  --query 'id' --output text)
echo "  Run ID: ${REINDEX_ID}"

# Wait for reindex
while true; do
  STATUS=$(aws omics get-run --id ${REINDEX_ID} --region ${REGION} --query 'status' --output text)
  if [ "$STATUS" = "COMPLETED" ]; then break; fi
  if [ "$STATUS" = "FAILED" ]; then echo "REINDEX FAILED"; exit 1; fi
  sleep 30
done
echo "  Reindex COMPLETED"

# Get the new CRAI path from the output
NEW_CRAI="${OUTPUT_BASE}/reindex/${REINDEX_ID}/out/new_crai/${SAMPLE_ID}.cram.crai"
echo "  New CRAI: ${NEW_CRAI}"

# Step 2-6: Run all tasks in parallel
echo ""
echo "[2-6] Launching all tasks in parallel..."

# CollectCounts
CC_ID=$(aws omics start-run --workflow-id ${WF_COLLECT_COUNTS} --name "cc-${SAMPLE_ID}" --role-arn "${ROLE_ARN}" --output-uri "${OUTPUT_BASE}/collect-counts/" --storage-type DYNAMIC --cache-id ${CACHE_ID} --cache-behavior CACHE_ALWAYS --parameters "{\"cram_or_bam\":\"${CRAM}\",\"cram_or_bam_idx\":\"${NEW_CRAI}\",\"sample_id\":\"${SAMPLE_ID}\",\"ref_fasta\":\"${REF}\",\"ref_fasta_fai\":\"${REF_FAI}\",\"ref_fasta_dict\":\"${REF_DICT}\",\"gatk_jar\":\"${GATK_JAR}\",\"intervals\":\"${INTERVALS}\",\"docker\":\"${SV_BASE}\"}" --region ${REGION} --query 'id' --output text)
echo "  CollectCounts: ${CC_ID}"

# CollectSVEvidence
CSE_ID=$(aws omics start-run --workflow-id ${WF_COLLECT_SV} --name "cse-${SAMPLE_ID}" --role-arn "${ROLE_ARN}" --output-uri "${OUTPUT_BASE}/collect-sv-evidence/" --storage-type DYNAMIC --cache-id ${CACHE_ID} --cache-behavior CACHE_ALWAYS --parameters "{\"cram_or_bam\":\"${CRAM}\",\"cram_or_bam_idx\":\"${NEW_CRAI}\",\"sample_id\":\"${SAMPLE_ID}\",\"ref_fasta\":\"${REF}\",\"ref_fasta_fai\":\"${REF_FAI}\",\"ref_fasta_dict\":\"${REF_DICT}\",\"gatk_jar\":\"${GATK_JAR}\",\"preprocessed_intervals\":\"${INTERVALS}\",\"primary_contigs_list\":\"${CONTIGS}\",\"sd_locs_vcf\":\"${DBSNP}\",\"docker\":\"${SV_BASE}\"}" --region ${REGION} --query 'id' --output text)
echo "  CollectSVEvidence: ${CSE_ID}"

# Manta
MANTA_ID=$(aws omics start-run --workflow-id ${WF_MANTA} --name "manta-${SAMPLE_ID}" --role-arn "${ROLE_ARN}" --output-uri "${OUTPUT_BASE}/manta/" --storage-type DYNAMIC --cache-id ${CACHE_ID} --cache-behavior CACHE_ALWAYS --parameters "{\"cram_or_bam\":\"${CRAM}\",\"cram_or_bam_idx\":\"${NEW_CRAI}\",\"sample_id\":\"${SAMPLE_ID}\",\"ref_fasta\":\"${REF}\",\"ref_fasta_fai\":\"${REF_FAI}\",\"manta_region_bed\":\"${MANTA_BED}\",\"manta_region_bed_index\":\"${MANTA_TBI}\",\"manta_docker\":\"${MANTA_IMG}\"}" --region ${REGION} --query 'id' --output text)
echo "  Manta: ${MANTA_ID}"

# Wham
WHAM_ID=$(aws omics start-run --workflow-id ${WF_WHAM} --name "wham-${SAMPLE_ID}" --role-arn "${ROLE_ARN}" --output-uri "${OUTPUT_BASE}/wham/" --storage-type DYNAMIC --cache-id ${CACHE_ID} --cache-behavior CACHE_ALWAYS --parameters "{\"cram_or_bam\":\"${CRAM}\",\"cram_or_bam_idx\":\"${NEW_CRAI}\",\"sample_id\":\"${SAMPLE_ID}\",\"ref_fasta\":\"${REF}\",\"ref_fasta_fai\":\"${REF_FAI}\",\"wham_include_list_bed\":\"${WHAM_BED}\",\"wham_docker\":\"${WHAM_IMG}\"}" --region ${REGION} --query 'id' --output text)
echo "  Wham: ${WHAM_ID}"

# Scramble
SCR_ID=$(aws omics start-run --workflow-id ${WF_SCRAMBLE} --name "scramble-${SAMPLE_ID}" --role-arn "${ROLE_ARN}" --output-uri "${OUTPUT_BASE}/scramble/" --storage-type DYNAMIC --cache-id ${CACHE_ID} --cache-behavior CACHE_ALWAYS --parameters "{\"cram_or_bam\":\"${CRAM}\",\"cram_or_bam_idx\":\"${NEW_CRAI}\",\"sample_id\":\"${SAMPLE_ID}\",\"ref_fasta\":\"${REF}\",\"ref_fasta_fai\":\"${REF_FAI}\",\"mei_bed\":\"${MEI_BED}\",\"scramble_docker\":\"${SCRAMBLE_IMG}\"}" --region ${REGION} --query 'id' --output text)
echo "  Scramble: ${SCR_ID}"

echo ""
echo "All tasks launched. Monitor with:"
echo "  aws omics list-runs --region ${REGION} --query 'items[?contains(name, \`${SAMPLE_ID}\`)].{name:name,status:status}'"
