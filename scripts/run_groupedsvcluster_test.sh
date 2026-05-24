#!/bin/bash
# Reproduce GroupedSVClusterPart1 for chrY locally to capture the actual error
set -euxo pipefail

cd /tmp/gatk-sv-test/work

ACCOUNT_ID="${AWS_ACCOUNT_ID:?Set AWS_ACCOUNT_ID env var to your 12-digit AWS account ID}"

GATK_DOCKER="${ACCOUNT_ID}.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/gatk:mw-gatk-sv-672d85"
SV_PIPELINE_DOCKER="${ACCOUNT_ID}.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604"

REFS=/tmp/gatk-sv-test/refs
INPUTS=/tmp/gatk-sv-test/inputs
WORK=/tmp/gatk-sv-test/work

CONTIG="chrY"
COHORT="gatk-sv-validation-2026q2"

echo "=== Step 1: Create ploidy table ==="
docker run --rm \
  -v $REFS:/refs \
  -v $WORK:/work \
  -w /work \
  $SV_PIPELINE_DOCKER \
  python /opt/sv-pipeline/scripts/ploidy_table_from_ped.py \
    --ped /refs/cohort.ped \
    --out /work/cohort.ploidy.FEMALE_chrY_1.tsv \
    --contigs /refs/gs_primary_contigs.list \
    --chr-x chrX --chr-y chrY
sed -e 's/\t0/\t1/g' $WORK/cohort.ploidy.FEMALE_chrY_1.tsv > $WORK/cohort.ploidy.tsv
echo "Ploidy table:"
head -5 $WORK/cohort.ploidy.tsv

echo "=== Step 2: JoinVcfs (SVCluster naive join) for chrY ==="
cat > $WORK/joinvcfs.args <<EOF
-V /inputs/batch_01.genotype_batch.pesr.vcf.gz
-V /inputs/batch_01.genotype_batch.depth.vcf.gz
EOF

docker run --rm \
  -v $REFS:/refs \
  -v $INPUTS:/inputs \
  -v $WORK:/work \
  -w /work \
  $GATK_DOCKER \
  gatk --java-options "-Xmx12g" SVCluster \
    --arguments_file /work/joinvcfs.args \
    --output /work/${COHORT}.combine_batches.${CONTIG}.join_vcfs.vcf.gz \
    --reference /refs/Homo_sapiens_assembly38.fasta \
    --ploidy-table /work/cohort.ploidy.tsv \
    -L ${CONTIG} \
    --pesr-sample-overlap 0 --pesr-interval-overlap 1 --pesr-breakend-window 0 \
    --depth-sample-overlap 0 --depth-interval-overlap 1 --depth-breakend-window 0 \
    --mixed-sample-overlap 0 --mixed-interval-overlap 1 --mixed-breakend-window 0

echo "JoinVcfs output:"
ls -la $WORK/${COHORT}.combine_batches.${CONTIG}.join_vcfs.vcf.gz*

echo "=== Step 3: ClusterSites for chrY ==="
docker run --rm \
  -v $REFS:/refs \
  -v $WORK:/work \
  -w /work \
  $GATK_DOCKER \
  gatk --java-options "-Xmx12g" SVCluster \
    -V /work/${COHORT}.combine_batches.${CONTIG}.join_vcfs.vcf.gz \
    --output /work/${COHORT}.combine_batches.${CONTIG}.cluster_sites.vcf.gz \
    --reference /refs/Homo_sapiens_assembly38.fasta \
    --ploidy-table /work/cohort.ploidy.tsv \
    --breakpoint-summary-strategy REPRESENTATIVE \
    --variant-prefix "${COHORT}_${CONTIG}_" \
    --pesr-sample-overlap 0.5 --pesr-interval-overlap 0.1 --pesr-breakend-window 300 \
    --depth-sample-overlap 0.5 --depth-interval-overlap 0.5 --depth-breakend-window 500000 \
    --mixed-sample-overlap 0.5 --mixed-interval-overlap 0.5 --mixed-breakend-window 1000000

echo "ClusterSites output:"
ls -la $WORK/${COHORT}.combine_batches.${CONTIG}.cluster_sites.vcf.gz*

echo "=== Step 4: GroupedSVClusterPart1 for chrY (THE FAILING STEP) ==="
docker run --rm \
  -v $REFS:/refs \
  -v $WORK:/work \
  -w /work \
  $GATK_DOCKER \
  gatk --java-options "-Xmx12g" GroupedSVCluster \
    -V /work/${COHORT}.combine_batches.${CONTIG}.cluster_sites.vcf.gz \
    --output /work/${COHORT}.combine_batches.${CONTIG}.recluster_part_1.vcf.gz \
    --reference /refs/Homo_sapiens_assembly38.fasta \
    --ploidy-table /work/cohort.ploidy.tsv \
    --clustering-config /refs/gs_clustering_config.part_one.tsv \
    --stratify-config /refs/gs_stratification_config.part_one.tsv \
    --track-intervals /refs/hg38.SimpRep.sorted.pad_100.merged.bed.gz \
    --track-intervals /refs/segdups.bed.gz \
    --track-intervals /refs/rmsk.bed.gz \
    --track-name SR \
    --track-name SD \
    --track-name RM \
    --stratify-overlap-fraction 0 \
    --stratify-num-breakpoint-overlaps 1 \
    --stratify-num-breakpoint-overlaps-interchromosomal 1 \
    --breakpoint-summary-strategy REPRESENTATIVE

echo "GroupedSVClusterPart1 output:"
ls -la $WORK/${COHORT}.combine_batches.${CONTIG}.recluster_part_1.vcf.gz*
echo "✓ ALL STEPS COMPLETED SUCCESSFULLY"
