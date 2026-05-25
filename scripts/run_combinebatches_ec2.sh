#!/bin/bash
# Run the complete CombineBatches sub-workflow on EC2 for all 24 contigs.
# This produces the same outputs as Broad's CombineBatches WDL would produce.
#
# Outputs uploaded to:
#   s3://healthomics-outputs-${ACCOUNT_ID}-apse1/runs/gatk-sv-e2e/batch/make-cohort-vcf-ec2/

set -euxo pipefail

ACCOUNT_ID="${AWS_ACCOUNT_ID:?Set AWS_ACCOUNT_ID env var to your 12-digit AWS account ID}"
COHORT="${GATK_SV_COHORT_ID:-gatk-sv-validation-2026q2}"
SAMPLE_COUNT="${GATK_SV_SAMPLE_COUNT:-10}"
ENVIRONMENT="${GATK_SV_ENVIRONMENT:-validation}"

# Property-10 cost-tag set, applied to:
#   - the EC2 instance itself (so EC2 line items in Cost Explorer attribute back)
#   - every S3 object uploaded as part of this workflow (so S3 storage / requests attribute back)
COST_TAGS=(
    "Key=gatk-sv:cohort-id,Value=${COHORT}"
    "Key=gatk-sv:workflow-version,Value=combinebatches-ec2-bash"
    "Key=gatk-sv:module,Value=MakeCohortVcf:CombineBatches"
    "Key=gatk-sv:sample-count,Value=${SAMPLE_COUNT}"
    "Key=gatk-sv:environment,Value=${ENVIRONMENT}"
)

# Tag the running EC2 instance with the cohort id so EC2 cost attributes correctly.
INSTANCE_ID="$(curl -fs http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo '')"
if [ -n "$INSTANCE_ID" ]; then
    aws ec2 create-tags \
        --resources "$INSTANCE_ID" \
        --tags "${COST_TAGS[@]}" \
        --region "${AWS_DEFAULT_REGION:-ap-southeast-1}" || echo "WARN: could not tag $INSTANCE_ID"
fi

GATK_DOCKER="${ACCOUNT_ID}.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/gatk:mw-gatk-sv-672d85"
SV_PIPELINE_DOCKER="${ACCOUNT_ID}.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604"
SV_BASE_MINI_DOCKER="${ACCOUNT_ID}.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/sv-base-mini:2024-10-25-v0.29-beta-5ea22a52"

ROOT=/tmp/combinebatches-ec2
mkdir -p $ROOT/{inputs,refs,work,outputs,logs}

REFS=$ROOT/refs
INPUTS=$ROOT/inputs
WORK=$ROOT/work
OUTPUTS=$ROOT/outputs
LOGS=$ROOT/logs

REF_BUCKET="omics-ref-ap-southeast-1-${ACCOUNT_ID}"
REF_PREFIX="gatk-sv/reference/GRCh38"
OUT_BUCKET="healthomics-outputs-${ACCOUNT_ID}-apse1"
OUT_PREFIX="runs/gatk-sv-e2e/batch/make-cohort-vcf-ec2"

echo "=========================================="
echo "Stage 1: Download inputs and references"
echo "=========================================="

cd $INPUTS
for f in batch_01.genotype_batch.pesr.vcf.gz \
         batch_01.genotype_batch.pesr.vcf.gz.tbi \
         batch_01.genotype_batch.depth.vcf.gz \
         batch_01.genotype_batch.depth.vcf.gz.tbi; do
    if [ ! -f $f ]; then
        case $f in
            *pesr*) aws s3 cp s3://$OUT_BUCKET/runs/gatk-sv-e2e/batch/genotype-batch/3154916/out/genotyped_pesr_vcf/$f . --quiet ;;
            *depth*) aws s3 cp s3://$OUT_BUCKET/runs/gatk-sv-e2e/batch/genotype-batch/3154916/out/genotyped_depth_vcf/$f . --quiet ;;
        esac
    fi
done

cd $REFS
for f in Homo_sapiens_assembly38.fasta \
         Homo_sapiens_assembly38.fasta.fai \
         Homo_sapiens_assembly38.dict \
         gs_primary_contigs.list \
         cohort.ped \
         gs_clustering_config.part_one.tsv \
         gs_clustering_config.part_two.tsv \
         stratify_config.v2.part_one.tsv \
         stratify_config.v2.part_two.tsv \
         hg38.SimpRep.sorted.pad_100.merged.bed.gz \
         hg38.SimpRep.sorted.pad_100.merged.bed.gz.tbi \
         segdups.bed.gz \
         segdups.bed.gz.tbi \
         rmsk.bed.gz \
         rmsk.bed.gz.tbi \
         gs_PESR.encode.blacklist.sorted.bed.gz \
         gs_allosome.fai; do
    if [ ! -f $f ]; then
        aws s3 cp s3://$REF_BUCKET/$REF_PREFIX/$f . --quiet || echo "WARN: $f not found"
    fi
done

# Stratify configs renamed for clarity
cp stratify_config.v2.part_one.tsv gs_stratification_config.part_one.tsv
cp stratify_config.v2.part_two.tsv gs_stratification_config.part_two.tsv

echo
echo "=========================================="
echo "Stage 2: Create ploidy table"
echo "=========================================="

cd $WORK

if [ ! -f cohort.ploidy.tsv ]; then
    docker run --rm -v $REFS:/refs -v $WORK:/work -w /work \
      $SV_PIPELINE_DOCKER \
      python /opt/sv-pipeline/scripts/ploidy_table_from_ped.py \
        --ped /refs/cohort.ped \
        --out /work/cohort.ploidy.FEMALE_chrY_1.tsv \
        --contigs /refs/gs_primary_contigs.list \
        --chr-x chrX --chr-y chrY
    sed -e 's/\t0/\t1/g' $WORK/cohort.ploidy.FEMALE_chrY_1.tsv > $WORK/cohort.ploidy.tsv
    echo "Ploidy table:"
    head -3 $WORK/cohort.ploidy.tsv
fi

echo
echo "=========================================="
echo "Stage 3: Per-contig CombineBatches steps"
echo "=========================================="

CONTIGS=$(cat $REFS/gs_primary_contigs.list | awk '{print $1}')

# Process each contig
for CONTIG in $CONTIGS; do
    echo
    echo "--- Processing $CONTIG ---"
    PREFIX="$COHORT.combine_batches.$CONTIG"
    OUT_VCF="$OUTPUTS/$PREFIX.svtk_formatted.vcf.gz"

    if [ -f "$OUT_VCF" ]; then
        echo "  $CONTIG already done, skipping."
        continue
    fi

    # Step 1: JoinVcfs (SVCluster naive join)
    if [ ! -f "$WORK/$PREFIX.join_vcfs.vcf.gz" ]; then
        echo "  [JoinVcfs] $(date -u +%H:%M:%S)"
        cat > $WORK/$PREFIX.joinvcfs.args <<EOF
-V /inputs/batch_01.genotype_batch.pesr.vcf.gz
-V /inputs/batch_01.genotype_batch.depth.vcf.gz
EOF
        docker run --rm -v $REFS:/refs -v $INPUTS:/inputs -v $WORK:/work -w /work \
          $GATK_DOCKER \
          gatk --java-options "-Xmx12g" SVCluster \
            --arguments_file /work/$PREFIX.joinvcfs.args \
            --output /work/$PREFIX.join_vcfs.vcf.gz \
            --reference /refs/Homo_sapiens_assembly38.fasta \
            --ploidy-table /work/cohort.ploidy.tsv \
            -L $CONTIG \
            --pesr-sample-overlap 0 --pesr-interval-overlap 1 --pesr-breakend-window 0 \
            --depth-sample-overlap 0 --depth-interval-overlap 1 --depth-breakend-window 0 \
            --mixed-sample-overlap 0 --mixed-interval-overlap 1 --mixed-breakend-window 0 \
            > $LOGS/$PREFIX.joinvcfs.log 2>&1
    fi

    # Step 2: ClusterSites (SVCluster, first round of clustering)
    if [ ! -f "$WORK/$PREFIX.cluster_sites.vcf.gz" ]; then
        echo "  [ClusterSites] $(date -u +%H:%M:%S)"
        docker run --rm -v $REFS:/refs -v $WORK:/work -w /work \
          $GATK_DOCKER \
          gatk --java-options "-Xmx12g" SVCluster \
            -V /work/$PREFIX.join_vcfs.vcf.gz \
            --output /work/$PREFIX.cluster_sites.vcf.gz \
            --reference /refs/Homo_sapiens_assembly38.fasta \
            --ploidy-table /work/cohort.ploidy.tsv \
            --breakpoint-summary-strategy REPRESENTATIVE \
            --variant-prefix "${COHORT}_${CONTIG}_" \
            --pesr-sample-overlap 0.5 --pesr-interval-overlap 0.1 --pesr-breakend-window 300 \
            --depth-sample-overlap 0.5 --depth-interval-overlap 0.5 --depth-breakend-window 500000 \
            --mixed-sample-overlap 0.5 --mixed-interval-overlap 0.5 --mixed-breakend-window 1000000 \
            > $LOGS/$PREFIX.cluster_sites.log 2>&1
    fi

    # Step 3: GroupedSVClusterPart1
    if [ ! -f "$WORK/$PREFIX.recluster_part_1.vcf.gz" ]; then
        echo "  [GroupedSVClusterPart1] $(date -u +%H:%M:%S)"
        docker run --rm -v $REFS:/refs -v $WORK:/work -w /work \
          $GATK_DOCKER \
          gatk --java-options "-Xmx12g" GroupedSVCluster \
            -V /work/$PREFIX.cluster_sites.vcf.gz \
            -O /work/$PREFIX.recluster_part_1.vcf.gz \
            --reference /refs/Homo_sapiens_assembly38.fasta \
            --ploidy-table /work/cohort.ploidy.tsv \
            --clustering-config /refs/gs_clustering_config.part_one.tsv \
            --stratify-config /refs/gs_stratification_config.part_one.tsv \
            --track-intervals /refs/hg38.SimpRep.sorted.pad_100.merged.bed.gz \
            --track-intervals /refs/segdups.bed.gz \
            --track-intervals /refs/rmsk.bed.gz \
            --track-name SR --track-name SD --track-name RM \
            --stratify-overlap-fraction 0 \
            --stratify-num-breakpoint-overlaps 1 \
            --stratify-num-breakpoint-overlaps-interchromosomal 1 \
            --breakpoint-summary-strategy REPRESENTATIVE \
            > $LOGS/$PREFIX.recluster_part_1.log 2>&1
    fi

    # Step 4: GroupedSVClusterPart2
    if [ ! -f "$WORK/$PREFIX.recluster_part_2.vcf.gz" ]; then
        echo "  [GroupedSVClusterPart2] $(date -u +%H:%M:%S)"
        docker run --rm -v $REFS:/refs -v $WORK:/work -w /work \
          $GATK_DOCKER \
          gatk --java-options "-Xmx12g" GroupedSVCluster \
            -V /work/$PREFIX.recluster_part_1.vcf.gz \
            -O /work/$PREFIX.recluster_part_2.vcf.gz \
            --reference /refs/Homo_sapiens_assembly38.fasta \
            --ploidy-table /work/cohort.ploidy.tsv \
            --clustering-config /refs/gs_clustering_config.part_two.tsv \
            --stratify-config /refs/gs_stratification_config.part_two.tsv \
            --track-intervals /refs/hg38.SimpRep.sorted.pad_100.merged.bed.gz \
            --track-intervals /refs/segdups.bed.gz \
            --track-intervals /refs/rmsk.bed.gz \
            --track-name SR --track-name SD --track-name RM \
            --stratify-overlap-fraction 0 \
            --stratify-num-breakpoint-overlaps 1 \
            --stratify-num-breakpoint-overlaps-interchromosomal 1 \
            --breakpoint-summary-strategy REPRESENTATIVE \
            > $LOGS/$PREFIX.recluster_part_2.log 2>&1
    fi

    # Step 5: GatkToSvtkVcf — convert to svtk format for downstream
    if [ ! -f "$OUT_VCF" ]; then
        echo "  [GatkToSvtkVcf] $(date -u +%H:%M:%S)"
        docker run --rm -v $REFS:/refs -v $WORK:/work -w /work \
          $SV_PIPELINE_DOCKER \
          bash -c "
            python /opt/sv-pipeline/scripts/format_gatk_vcf_for_svtk.py \
              --vcf /work/$PREFIX.recluster_part_2.vcf.gz \
              --out /work/$PREFIX.svtk_formatted.vcf.gz \
              --source depth \
              --contigs /refs/gs_primary_contigs.list \
              --remove-infos AC,AF,AN,HIGH_SR_BACKGROUND,BOTHSIDES_SUPPORT,SR1POS,SR2POS \
              --remove-formats CN,RD_MCR \
              --set-pass
            tabix /work/$PREFIX.svtk_formatted.vcf.gz
          " > $LOGS/$PREFIX.gatktosvtk.log 2>&1

        cp $WORK/$PREFIX.svtk_formatted.vcf.gz $OUT_VCF
        cp $WORK/$PREFIX.svtk_formatted.vcf.gz.tbi $OUT_VCF.tbi

        # Step 6: ExtractSRVariantLists - produces high_sr_background and bothsides_sr_support files
        echo "  [ExtractSRVariantLists] $(date -u +%H:%M:%S)"
        docker run --rm -v $WORK:/work -w /work \
          $SV_BASE_MINI_DOCKER \
          bash -c "
            bcftools query -f '%ID\t%HIGH_SR_BACKGROUND\t%BOTHSIDES_SUPPORT\n' /work/$PREFIX.recluster_part_2.vcf.gz > /work/$PREFIX.flags.txt
            awk -F'\t' '(\$2 != \".\"){print \$1}' /work/$PREFIX.flags.txt > /work/$PREFIX.high_sr_background.txt
            awk -F'\t' '(\$3 != \".\"){print \$1}' /work/$PREFIX.flags.txt > /work/$PREFIX.bothsides_sr_support.txt
          " > $LOGS/$PREFIX.extract_sr.log 2>&1

        cp $WORK/$PREFIX.high_sr_background.txt $OUTPUTS/
        cp $WORK/$PREFIX.bothsides_sr_support.txt $OUTPUTS/
    fi
    echo "  ✓ $CONTIG complete: $(ls -la $OUT_VCF)"
done

echo
echo "=========================================="
echo "Stage 4: Upload outputs to S3 (with cost tags)"
echo "=========================================="

# Build URL-encoded S3 tagging string from the cost tags above.
S3_TAGGING="gatk-sv%3Acohort-id=${COHORT}&gatk-sv%3Aworkflow-version=combinebatches-ec2-bash&gatk-sv%3Amodule=MakeCohortVcf%3ACombineBatches&gatk-sv%3Asample-count=${SAMPLE_COUNT}&gatk-sv%3Aenvironment=${ENVIRONMENT}"

# aws s3 sync doesn't accept --tagging, so upload object-by-object via cp.
for f in "$OUTPUTS"/*; do
    base="$(basename "$f")"
    aws s3 cp "$f" "s3://$OUT_BUCKET/$OUT_PREFIX/combine_batches/$base" \
        --quiet \
        --tagging "$S3_TAGGING"
done

echo
echo "=========================================="
echo "✓ ALL DONE"
echo "=========================================="
echo "Output files:"
ls -la $OUTPUTS/
echo
echo "Uploaded to: s3://$OUT_BUCKET/$OUT_PREFIX/combine_batches/"
