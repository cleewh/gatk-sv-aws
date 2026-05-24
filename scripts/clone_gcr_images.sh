#!/usr/bin/env bash
# Clone GATK-SV GCR images to private ECR in ap-southeast-1.
#
# Prerequisites:
#   - Docker installed and running
#   - AWS CLI configured for ap-southeast-1
#   - gcloud auth configured (for pulling from us.gcr.io)
#
# Usage:
#   bash gatk-sv-healthomics/scripts/clone_gcr_images.sh
#
# This script:
#   1. Logs into ECR
#   2. For each GATK-SV image on us.gcr.io:
#      a. docker pull from GCR
#      b. docker tag to ECR
#      c. docker push to ECR
#   3. Grants HealthOmics access to each new repo
#
# Cost: ~$2 in data transfer (images total ~5 GB compressed).
# Time: ~10 minutes on a fast connection.

set -euo pipefail

ACCOUNT_ID="${AWS_ACCOUNT_ID:?Set AWS_ACCOUNT_ID env var to your 12-digit AWS account ID}"
REGION="ap-southeast-1"
ECR_HOST="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

# Log into ECR
echo "Logging into ECR..."
aws ecr get-login-password --region "${REGION}" | \
  docker login --username AWS --password-stdin "${ECR_HOST}"

# Images to clone (excluding MELT which we don't use)
declare -A IMAGES=(
  ["sv-base-mini"]="us.gcr.io/broad-dsde-methods/gatk-sv/sv-base-mini:2024-10-25-v0.29-beta-5ea22a52"
  ["sv-base"]="us.gcr.io/broad-dsde-methods/gatk-sv/sv-base:2024-10-25-v0.29-beta-5ea22a52"
  ["sv-pipeline"]="us.gcr.io/broad-dsde-methods/gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604"
  ["sv-utils"]="us.gcr.io/broad-dsde-methods/gatk-sv/sv-utils:2025-01-06-v1.0.1-e902bf4e"
  ["manta"]="us.gcr.io/broad-dsde-methods/gatk-sv/manta:2023-09-14-v0.28.3-beta-3f22f94d"
  ["wham"]="us.gcr.io/broad-dsde-methods/gatk-sv/wham:2024-10-25-v0.29-beta-5ea22a52"
  ["scramble"]="us.gcr.io/broad-dsde-methods/gatk-sv/scramble:2024-10-25-v0.29-beta-5ea22a52"
  ["samtools-cloud"]="us.gcr.io/broad-dsde-methods/gatk-sv/samtools-cloud:2024-10-25-v0.29-beta-5ea22a52"
  ["gatk"]="us.gcr.io/broad-dsde-methods/gatk-sv/gatk:mw-gatk-sv-672d85"
  ["cnmops"]="us.gcr.io/broad-dsde-methods/gatk-sv/cnmops:2025-09-02-v1.0.5-f091af0b"
  ["stripy"]="us.gcr.io/broad-dsde-methods/gatk-sv/stripy:2025-11-14-v1.1-7b56c3ac"
  ["genomes-in-the-cloud"]="us.gcr.io/broad-gotc-prod/genomes-in-the-cloud:2.3.2-1510681135"
)

for repo_name in "${!IMAGES[@]}"; do
  source_image="${IMAGES[$repo_name]}"
  # Extract tag from source
  tag="${source_image##*:}"
  ecr_repo="gatk-sv/${repo_name}"
  ecr_image="${ECR_HOST}/${ecr_repo}:${tag}"

  echo ""
  echo "=== ${repo_name} ==="
  echo "  source: ${source_image}"
  echo "  target: ${ecr_image}"

  # Create ECR repo if it doesn't exist
  aws ecr create-repository \
    --repository-name "${ecr_repo}" \
    --region "${REGION}" 2>/dev/null || true

  # Grant HealthOmics access
  aws ecr set-repository-policy \
    --repository-name "${ecr_repo}" \
    --region "${REGION}" \
    --policy-text '{
      "Version": "2012-10-17",
      "Statement": [{
        "Sid": "HealthOmicsAccess",
        "Effect": "Allow",
        "Principal": {"Service": "omics.amazonaws.com"},
        "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
      }]
    }' 2>/dev/null || true

  # Pull, tag, push
  docker pull "${source_image}"
  docker tag "${source_image}" "${ecr_image}"
  docker push "${ecr_image}"

  echo "  ✓ done"
done

echo ""
echo "All images cloned. Update container-registry-map.json with imageMappings."
