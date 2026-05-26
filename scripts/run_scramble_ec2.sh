#!/bin/bash
# Run real Scramble on EC2 for a single sample.
#
# Steps:
#   1. cluster_identifier (12-parallel by chromosome) -- in scramble docker
#   2. SCRAMble.R --eval-meis                          -- in scramble docker
#   3. make_scramble_vcf.py + bcftools sort + tabix    -- in sv_pipeline docker
#
# This bypasses the HealthOmics 47-second kill on multi-task scramble
# workflows. Same Docker images, same arguments, same outputs as upstream.

set -euxo pipefail

ACCOUNT_ID="${AWS_ACCOUNT_ID:?Set AWS_ACCOUNT_ID}"
SAMPLE="${SAMPLE:?Set SAMPLE}"
COHORT_ID="${GATK_SV_COHORT_ID:-validation-fix-2026-05-26}"
COHORTS_BUCKET="${COHORTS_BUCKET:-omics-cohorts-ap-southeast-1-${ACCOUNT_ID}}"
COHORTS_PREFIX="${COHORTS_PREFIX:-cohorts/gatk-sv-validation-2026q2}"
REF_BUCKET="${REF_BUCKET:-omics-ref-ap-southeast-1-${ACCOUNT_ID}}"
REF_PREFIX="${REF_PREFIX:-gatk-sv/reference/GRCh38}"
OUT_BUCKET="${OUT_BUCKET:-healthomics-outputs-${ACCOUNT_ID}-apse1}"
OUT_PREFIX="${OUT_PREFIX:-runs/gatk-sv-e2e/validation-fix-2026-05-26/${SAMPLE}/scramble-real-ec2}"

# Phase A's existing cc + manta outputs for this sample
COUNTS_S3="${COUNTS_S3:-s3://${OUT_BUCKET}/runs/gatk-sv-e2e/gatk-sv-validation-2026q2-rerun-2026-05-25/${SAMPLE}/gse/cc/6242515/out/counts/${SAMPLE}.counts.tsv.gz}"
MANTA_S3="${MANTA_S3:-s3://${OUT_BUCKET}/runs/gatk-sv-e2e/gatk-sv-validation-2026q2-rerun-2026-05-25/${SAMPLE}/gse/manta/2571368/out/manta_vcf/${SAMPLE}.manta.vcf.gz}"

SCRAMBLE_DOCKER="${ACCOUNT_ID}.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/scramble:2024-10-25-v0.29-beta-5ea22a52"
SVPIPE_DOCKER="${ACCOUNT_ID}.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604"

WORK=/tmp/scramble-ec2/${SAMPLE}
mkdir -p $WORK/{refs,inputs,clusters,outputs,logs}
cd $WORK

# Tag this EC2 instance for cost tracking
INSTANCE_ID="$(curl -fs http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo '')"
if [ -n "$INSTANCE_ID" ]; then
    aws ec2 create-tags \
        --resources "$INSTANCE_ID" \
        --tags \
            "Key=gatk-sv:cohort-id,Value=${COHORT_ID}" \
            "Key=gatk-sv:workflow-version,Value=scramble-real-ec2-bash" \
            "Key=gatk-sv:module,Value=GatherSampleEvidence:scramble-real" \
            "Key=gatk-sv:sample-count,Value=1" \
            "Key=gatk-sv:environment,Value=validation" \
        --region ap-southeast-1 || true
fi

# Authenticate to ECR
aws ecr get-login-password --region ap-southeast-1 | \
    docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.ap-southeast-1.amazonaws.com"

echo "=== Stage 1: download inputs + refs ==="
cd $WORK/refs
for f in Homo_sapiens_assembly38.fasta Homo_sapiens_assembly38.fasta.fai \
         gs_primary_contigs.list mei_bed; do
    [ -f "$f" ] || aws s3 cp "s3://${REF_BUCKET}/${REF_PREFIX}/${f}" . --quiet
done

# mei_bed in the reference bucket is BGZF-compressed; make_scramble_vcf.py
# expects plain text. Detect and gunzip in place if needed.
if file $WORK/refs/mei_bed | grep -q -E "(gzip|BGZF)"; then
    cp $WORK/refs/mei_bed $WORK/refs/mei_bed.gz
    gunzip -f $WORK/refs/mei_bed.gz
    echo "Decompressed mei_bed (was BGZF/gzip)"
fi

cd $WORK/inputs
[ -f ${SAMPLE}.cram ]      || aws s3 cp "s3://${COHORTS_BUCKET}/${COHORTS_PREFIX}/${SAMPLE}.final.cram" "${SAMPLE}.cram" --quiet
[ -f ${SAMPLE}.cram.crai ] || aws s3 cp "s3://${COHORTS_BUCKET}/${COHORTS_PREFIX}/${SAMPLE}.final.cram.crai" "${SAMPLE}.cram.crai" --quiet
[ -f ${SAMPLE}.counts.tsv.gz ] || aws s3 cp "$COUNTS_S3" "${SAMPLE}.counts.tsv.gz" --quiet
[ -f ${SAMPLE}.manta.vcf.gz ]  || aws s3 cp "$MANTA_S3"  "${SAMPLE}.manta.vcf.gz" --quiet

echo "=== Stage 2: cluster_identifier + SCRAMble.R (scramble docker) ==="
docker run --rm \
    -v $WORK/refs:/refs:ro \
    -v $WORK/inputs:/inputs:ro \
    -v $WORK/clusters:/clusters \
    -v $WORK/outputs:/outputs \
    -w /clusters \
    "$SCRAMBLE_DOCKER" \
    bash -c '
        set -euxo pipefail
        cd /clusters
        SAMPLE='${SAMPLE}'

        # Calibrate cutoff
        zcat /inputs/${SAMPLE}.counts.tsv.gz \
          | awk "\$0!~\"@\"" \
          | sed 1d \
          | awk "NR % 100 == 0" \
          | cut -f4 \
          | Rscript -e "cat(round(0.22*median(data.matrix(read.csv(file(\"stdin\"))))))" \
          > cutoff.txt
        MIN_CLIPPED_READS=$(cat cutoff.txt)
        echo "MIN_CLIPPED_READS: $MIN_CLIPPED_READS"

        # 12-parallel cluster_identifier by chromosome
        mkdir -p shards
        PIDS=()
        while read region; do
            /app/scramble-gatk-sv/cluster_identifier/src/build/cluster_identifier \
                -l \
                -s $MIN_CLIPPED_READS \
                -r "$region" \
                -t /refs/Homo_sapiens_assembly38.fasta \
                /inputs/${SAMPLE}.cram > shards/${region}.txt &
            PIDS+=($!)
            if (( ${#PIDS[@]} >= 12 )); then
                wait "${PIDS[@]}"
                PIDS=()
            fi
        done < /refs/gs_primary_contigs.list
        if (( ${#PIDS[@]} > 0 )); then
            wait "${PIDS[@]}"
        fi

        # Concatenate in regions_list order
        while read region; do
            cat shards/${region}.txt
        done < /refs/gs_primary_contigs.list | gzip > ${SAMPLE}.scramble_clusters.tsv.gz
        echo "clusters: $(zcat ${SAMPLE}.scramble_clusters.tsv.gz | wc -l) lines"

        # SCRAMble.R
        cat /refs/Homo_sapiens_assembly38.fasta | makeblastdb -in - -parse_seqids -title ref -dbtype nucl -out ref
        clusterFile=$PWD/clusters
        gunzip -c ${SAMPLE}.scramble_clusters.tsv.gz > $clusterFile
        Rscript --vanilla /app/scramble-gatk-sv/cluster_analysis/bin/SCRAMble.R \
            --out-name $clusterFile \
            --cluster-file $clusterFile \
            --install-dir /app/scramble-gatk-sv/cluster_analysis/bin \
            --mei-refs /app/scramble-gatk-sv/cluster_analysis/resources/MEI_consensus_seqs.fa \
            --ref $PWD/ref \
            --no-vcf \
            --eval-meis \
            --cores 7 \
            --pct-align 70 \
            -n $MIN_CLIPPED_READS \
            --mei-score 90
        mv ${clusterFile}_MEIs.txt /outputs/${SAMPLE}.scramble.tsv
        gzip /outputs/${SAMPLE}.scramble.tsv

        # Save clusters too for traceability
        cp ${SAMPLE}.scramble_clusters.tsv.gz /outputs/
    '

echo "=== Stage 3: make_scramble_vcf.py + sort + tabix (sv_pipeline docker) ==="
docker run --rm \
    -v $WORK/refs:/refs:ro \
    -v $WORK/inputs:/inputs:ro \
    -v $WORK/outputs:/outputs \
    -w /outputs \
    "$SVPIPE_DOCKER" \
    bash -c '
        set -euxo pipefail
        cd /outputs
        SAMPLE='${SAMPLE}'
        python /opt/sv-pipeline/scripts/make_scramble_vcf.py \
            --table ${SAMPLE}.scramble.tsv.gz \
            --input-vcf /inputs/${SAMPLE}.manta.vcf.gz \
            --alignments-file /inputs/${SAMPLE}.cram \
            --sample $SAMPLE \
            --reference /refs/Homo_sapiens_assembly38.fasta \
            --mei-bed /refs/mei_bed \
            --out unsorted.vcf.gz
        bcftools sort unsorted.vcf.gz -Oz -o ${SAMPLE}.scramble.vcf.gz
        tabix ${SAMPLE}.scramble.vcf.gz
        echo "Final VCF records: $(bcftools view -H ${SAMPLE}.scramble.vcf.gz | wc -l)"
    '

echo "=== Stage 4: upload VCF to S3 ==="
# Note: --tagging is not supported by older `aws s3 cp` versions; rely on the
# bucket's default tags + post-upload put-object-tagging if cost-tagging needed.
aws s3 cp "$WORK/outputs/${SAMPLE}.scramble.vcf.gz"     "s3://${OUT_BUCKET}/${OUT_PREFIX}/${SAMPLE}.scramble.vcf.gz"
aws s3 cp "$WORK/outputs/${SAMPLE}.scramble.vcf.gz.tbi" "s3://${OUT_BUCKET}/${OUT_PREFIX}/${SAMPLE}.scramble.vcf.gz.tbi"
aws s3 cp "$WORK/outputs/${SAMPLE}.scramble.tsv.gz"     "s3://${OUT_BUCKET}/${OUT_PREFIX}/${SAMPLE}.scramble.tsv.gz"
aws s3 cp "$WORK/outputs/${SAMPLE}.scramble_clusters.tsv.gz" "s3://${OUT_BUCKET}/${OUT_PREFIX}/${SAMPLE}.scramble_clusters.tsv.gz"

# Apply Property-10 cost tags via put-object-tagging (works on every awscli version).
for OBJ in \
    "${OUT_PREFIX}/${SAMPLE}.scramble.vcf.gz" \
    "${OUT_PREFIX}/${SAMPLE}.scramble.vcf.gz.tbi" \
    "${OUT_PREFIX}/${SAMPLE}.scramble.tsv.gz" \
    "${OUT_PREFIX}/${SAMPLE}.scramble_clusters.tsv.gz"
do
    aws s3api put-object-tagging --bucket "$OUT_BUCKET" --key "$OBJ" \
        --tagging "TagSet=[
          {Key=gatk-sv:cohort-id,Value=${COHORT_ID}},
          {Key=gatk-sv:workflow-version,Value=scramble-real-ec2-bash},
          {Key=gatk-sv:module,Value=GatherSampleEvidence:scramble-real},
          {Key=gatk-sv:sample-count,Value=1},
          {Key=gatk-sv:environment,Value=validation}
        ]" || echo "WARN: failed to tag $OBJ"
done

echo "=== DONE ==="
echo "Output: s3://${OUT_BUCKET}/${OUT_PREFIX}/${SAMPLE}.scramble.vcf.gz"
