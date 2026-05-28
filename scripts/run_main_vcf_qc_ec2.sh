#!/bin/bash
# Run the IdentifyDuplicates + MergeDuplicates portion of MainVcfQc on EC2.
#
# Background: MainVcfQc.wdl contains a 10-VCF IdentifyDuplicates scatter
# followed by a MergeDuplicates aggregator. Both consume per-VCF outputs;
# this trips the HealthOmics 47-second multi-task kill (same pattern as
# Scramble, EvidenceQC.RawVcfQC, MakeCohortVcf.CombineBatches).
#
# Workaround: run the same Docker image and the same Python scripts via
# direct `docker run` on EC2, dispatched via SSM. Same algorithm, same
# outputs, no HealthOmics scheduling.
#
# Required env vars:
#   AWS_ACCOUNT_ID
#   COHORT_PREFIX        — e.g. "main-vcf-qc-smoke-2026-05-27"
#   VCFS                 — newline-separated S3 URIs of input VCFs (need .tbi siblings)
#
# Optional env vars:
#   GATK_SV_COHORT_ID    — for cost tagging (default: $COHORT_PREFIX)
#   OUT_BUCKET           — defaults to healthomics-outputs-${ACCOUNT_ID}-apse1
#   OUT_PREFIX           — defaults to runs/gatk-sv-e2e/${COHORT_PREFIX}/main-vcf-qc-ec2
#
# Usage (locally on EC2):
#   AWS_ACCOUNT_ID=687677765589 \
#   COHORT_PREFIX=main-vcf-qc-smoke-2026-05-27 \
#   VCFS=$(printf 's3://.../HG00096.vcf.gz\ns3://.../HG00097.vcf.gz\n') \
#   bash scripts/run_main_vcf_qc_ec2.sh

set -euo pipefail

ACCOUNT_ID="${AWS_ACCOUNT_ID:?Set AWS_ACCOUNT_ID}"
COHORT_PREFIX="${COHORT_PREFIX:?Set COHORT_PREFIX}"
VCFS="${VCFS:?Set VCFS to a newline-separated list of S3 URIs}"

GATK_SV_COHORT_ID="${GATK_SV_COHORT_ID:-$COHORT_PREFIX}"
OUT_BUCKET="${OUT_BUCKET:-healthomics-outputs-${ACCOUNT_ID}-apse1}"
OUT_PREFIX="${OUT_PREFIX:-runs/gatk-sv-e2e/${COHORT_PREFIX}/main-vcf-qc-ec2}"

SVPIPE_DOCKER="${ACCOUNT_ID}.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604"

WORK=/tmp/main-vcf-qc-ec2
mkdir -p $WORK/{inputs,outputs,logs}
cd $WORK

# Tag the EC2 instance for cost tracking.
INSTANCE_ID="$(curl -fs http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo '')"
if [ -n "$INSTANCE_ID" ]; then
    aws ec2 create-tags \
        --resources "$INSTANCE_ID" \
        --tags \
            "Key=gatk-sv:cohort-id,Value=${GATK_SV_COHORT_ID}" \
            "Key=gatk-sv:workflow-version,Value=main-vcf-qc-ec2-bash" \
            "Key=gatk-sv:module,Value=MainVcfQC" \
            "Key=gatk-sv:environment,Value=validation" \
        --region ap-southeast-1 || true
fi

# Authenticate to ECR.
aws ecr get-login-password --region ap-southeast-1 | \
    docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.ap-southeast-1.amazonaws.com"

echo "=== Stage 1: download inputs ==="
cd $WORK/inputs
echo "$VCFS" | while IFS= read -r uri; do
    [ -z "$uri" ] && continue
    name=$(basename "$uri")
    [ -f "$name" ] || aws s3 cp "$uri" . --quiet
    [ -f "$name.tbi" ] || aws s3 cp "${uri}.tbi" . --quiet
done
ls -la

echo "=== Stage 2: IdentifyDuplicates per VCF ==="
docker pull "$SVPIPE_DOCKER" 2>&1 | tail -3
for vcf_file in $WORK/inputs/*.vcf.gz; do
    name=$(basename "$vcf_file" .vcf.gz)
    full_prefix="${COHORT_PREFIX}.${name}"
    if [ -f "$WORK/outputs/${full_prefix}_duplicate_records.tsv" ]; then
        echo "  skip $name (already done)"
        continue
    fi
    echo "  IdentifyDuplicates: $name"
    docker run --rm \
        -v $WORK/inputs:/inputs:ro \
        -v $WORK/outputs:/outputs \
        -w /outputs \
        "$SVPIPE_DOCKER" \
        bash -c "
            set -euo pipefail
            python /opt/sv-pipeline/scripts/identify_duplicates.py \\
                --vcf /inputs/${name}.vcf.gz \\
                --fout ${full_prefix}
        "
done

echo "=== Stage 3: MergeDuplicates ==="
RECORDS=( $WORK/outputs/${COHORT_PREFIX}.*_duplicate_records.tsv )
COUNTS=( $WORK/outputs/${COHORT_PREFIX}.*_duplicate_counts.tsv )
echo "  ${#RECORDS[@]} records files, ${#COUNTS[@]} counts files"
docker run --rm \
    -v $WORK/outputs:/outputs \
    -w /outputs \
    "$SVPIPE_DOCKER" \
    bash -c "
        set -euo pipefail
        python /opt/sv-pipeline/scripts/merge_duplicates.py \\
            --records $(printf '/outputs/%s ' "${RECORDS[@]##*/}") \\
            --counts  $(printf '/outputs/%s ' "${COUNTS[@]##*/}")  \\
            --fout '${COHORT_PREFIX}.agg'
    "

echo "=== Stage 4: upload outputs to S3 ==="
aws s3 cp $WORK/outputs/ "s3://${OUT_BUCKET}/${OUT_PREFIX}/" \
    --recursive \
    --include '*.tsv' \
    --no-progress

# Tag uploaded objects.
S3_TAGS="TagSet=[
  {Key=gatk-sv:cohort-id,Value=${GATK_SV_COHORT_ID}},
  {Key=gatk-sv:workflow-version,Value=main-vcf-qc-ec2-bash},
  {Key=gatk-sv:module,Value=MainVcfQC},
  {Key=gatk-sv:environment,Value=validation}
]"
for f in $WORK/outputs/*.tsv; do
    aws s3api put-object-tagging \
        --bucket "$OUT_BUCKET" \
        --key "${OUT_PREFIX}/$(basename $f)" \
        --tagging "$S3_TAGS" || echo "WARN tag failed for $f"
done

echo "=== DONE ==="
echo "Outputs at s3://${OUT_BUCKET}/${OUT_PREFIX}/"
