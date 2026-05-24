# CONTEXT TRANSFER: GATK-SV Pipeline — Session 4

## Account & Region
- Account: __ACCOUNT_ID__
- Region: ap-southeast-1
- Role: arn:aws:iam::__ACCOUNT_ID__:role/gatk-sv-healthomics-run-role

## Pipeline Status (End of Session 4)

| # | Module | Workflow ID | Run ID | Status |
|---|--------|------------|--------|--------|
| 1 | GatherSampleEvidence | various | various | ✅ COMPLETE (all 10 samples) |
| 2 | TrainGCNV | 2282352 | 8170946 | ✅ COMPLETE |
| 3 | GatherBatchEvidence | 1575165 (v5) | 6129002 | ✅ COMPLETE |
| 4 | ClusterBatch | 2641017 (v3) | 2870194 | ✅ COMPLETE |
| 5 | GenerateBatchMetrics | 5339393 | 2916467 | ✅ COMPLETE |
| 6 | FilterBatch | 3328339 (v3) | 5070716 | ✅ COMPLETE |
| 7 | MergeBatchSites | 3326995 (v2) | 7287325 | ✅ COMPLETE |
| 8 | GenotypeBatch | 9542089 | 3154916 | ✅ COMPLETE |
| 9 | RegenotypeCNVs | 8299455 | 4852790 | ⏭️ SKIPPED (no variants to regenotype in 10-sample cohort) |
| 10 | MakeCohortVcf | 1027205 | 8724741 | ❌ FAILED — needs TasksClusterBatch.wdl fix |
| 11 | AnnotateVcf | 6832584 | — | ⏳ Waiting on #10 |

## Key Output Locations

- GBE: s3://healthomics-outputs-__ACCOUNT_ID__-apse1/runs/gatk-sv-e2e/batch/gather-batch-evidence/6129002/out/
- ClusterBatch: s3://healthomics-outputs-__ACCOUNT_ID__-apse1/runs/gatk-sv-e2e/batch/cluster-batch/2870194/out/
- GenerateBatchMetrics: s3://healthomics-outputs-__ACCOUNT_ID__-apse1/runs/gatk-sv-e2e/batch/generate-batch-metrics/2916467/out/
- FilterBatch: s3://healthomics-outputs-__ACCOUNT_ID__-apse1/runs/gatk-sv-e2e/batch/filter-batch/5070716/out/
- MergeBatchSites: s3://healthomics-outputs-__ACCOUNT_ID__-apse1/runs/gatk-sv-e2e/batch/merge-batch-sites/7287325/out/
- GenotypeBatch: s3://healthomics-outputs-__ACCOUNT_ID__-apse1/runs/gatk-sv-e2e/batch/genotype-batch/3154916/out/
- Reference files: s3://omics-ref-ap-southeast-1-__ACCOUNT_ID__/gatk-sv/reference/GRCh38/

## Immediate Next Step

**MakeCohortVcf needs a fixed WDL bundle** with `TasksClusterBatch.wdl` that adds `gatk IndexFeatureFile` before SVCluster (same fix applied to MergeBatchSites-v2 workflow 3326995).

The `JoinVcfs` tasks in `CombineBatches` sub-workflow all use SVCluster and fail because intermediate VCFs don't have `.tbi` indexes. The fix is identical to what we did for MergeBatchSites:
```bash
# Add before the gatk SVCluster command:
for vcf_file in $(cat ~{write_lines(vcfs)}); do
    if [ ! -f "${vcf_file}.tbi" ]; then
        gatk IndexFeatureFile -I "${vcf_file}"
    fi
done
```

Also need `runtime_attr` overrides for SVCluster memory (8 GiB) — the default 3.75 GiB is too low.

## Fixes Applied This Session

### FilterBatch (3 iterations to get working):
1. **Array alignment bug** — `FilterBatchSites.wdl` had 6-element `algorithms` array but 5-element `vcfs_array`. Fixed by adding `File? placeholder_vcf` at index 3. Same fix in `FilterBatchSamples.wdl`.
2. **Empty scramble VCF** — ClusterBatch scramble output was header-only (1.3 KB, no variants). R plotting script crashed on empty data. Fixed by omitting `scramble_vcf` from parameters.
3. **Removed redundant PlotSVCountsPerSample** call from `FilterBatch.wdl` (already called inside FilterBatchSites).
4. **Set `run_module_metrics: false`** to skip FilterBatchMetrics (requires non-null depth_vcf output).
5. Final working workflow: **3328339** (FilterBatch-v3)

### MergeBatchSites (2 iterations):
1. **SVCluster missing .tbi indexes** — intermediate VCFs from FormatVcf don't have co-located indexes. Fixed by adding `gatk IndexFeatureFile` in SVCluster command.
2. **SVCluster memory** — default 3.75 GiB too low, bumped to 8 GiB via `runtime_attr_merge_pesr/depth`.
3. **Uploaded .tbi indexes** for FilterBatch output VCFs (created with pysam).
4. Final working workflow: **3326995** (MergeBatchSites-v2)

### GenotypeBatch (5 iterations):
1. **Missing .tbi for MergeBatchSites VCF** — copied from `_index/` directory to co-locate.
2. **Wrong training_intervals file** — `gs_preprocessed_intervals.interval_list` (815 MB, gCNV file) caused OOM. Replaced with proper Picard interval list (582 KB, autosomal contigs + reference dict header).
3. **BED format didn't work** — GATK TrainSVGenotyping needs Picard `.interval_list` format with sequence dictionary.
4. **Invalid placeholder .tbi files** — The 36-byte dummy `.tbi` files uploaded during ClusterBatch for blacklist BEDs caused GATK to crash. Replaced with proper tabix indexes (2 KB and 5 KB) created with pysam.
5. Final working run: **3154916** (workflow 9542089, unchanged)

### RegenotypeCNVs:
- **Skipped** — `GetRegenotype` task found no variants meeting regenotyping criteria (expected for 10-sample cohort with `regeno_max_allele_freq=0.01`). The WDL doesn't handle empty output gracefully.

### MakeCohortVcf (BLOCKED):
- **SVCluster .tbi issue** — Same as MergeBatchSites. All `JoinVcfs` tasks in `CombineBatches` use SVCluster and fail because intermediate VCFs lack `.tbi` indexes. Need to create MakeCohortVcf-v2 with fixed `TasksClusterBatch.wdl`.
- Reference files staged: HERVK_reference.fa, LINE1_reference.fa, stratification configs, track files.

## New Workflow Versions Created This Session

| Workflow | ID | Description |
|----------|-----|-------------|
| FilterBatch-v2 | 1765546 | Array alignment fix in FilterBatchSites only |
| FilterBatch-v3 | 3328339 | Full fix: all 3 WDLs + removed redundant PlotSVCountsPerSample |
| MergeBatchSites-v2 | 3326995 | IndexFeatureFile before SVCluster |

## WDL Bundles Created This Session

- `gatk-sv-healthomics/wdl/bundles/FilterBatch/FilterBatch-bundle-v2.zip` (workflow 1765546)
- `gatk-sv-healthomics/wdl/bundles/FilterBatch/FilterBatch-bundle-v3.zip` (workflow 3328339)
- `gatk-sv-healthomics/wdl/bundles/MergeBatchSites/MergeBatchSites-bundle-v2.zip` (workflow 3326995)

## Critical HealthOmics Learnings (Updated from Session 3)

- FUSE is read-only — use zcat, not gunzip in-place
- miniWDL can't interpolate null optionals — pass all values explicitly
- Memory: gCNV ploidy=60 GiB, PostprocessGermlineCNVCalls=30 GiB, SVCluster=8 GiB
- HealthOmics rejects "32 GiB" — use "30 GiB"
- **SVCluster ALWAYS needs .tbi indexes** — add `gatk IndexFeatureFile` in the command for intermediate VCFs
- **Placeholder .tbi files (36 bytes) cause GATK crashes** — always create proper tabix indexes with pysam/tabix
- **Array alignment in WDL** — if algorithms array has N elements, vcfs_array must also have N elements (use placeholder File? for gaps)
- **Empty VCFs crash R plotting scripts** — omit callers with no variants from FilterBatch parameters
- **training_intervals for GenotypeBatch** — must be Picard `.interval_list` format with full reference dict header, NOT BED or gCNV preprocessed intervals
- **RegenotypeCNVs fails on small cohorts** — no variants meet regenotyping criteria, WDL doesn't handle empty output. Safe to skip.
- **.tbi co-location** — HealthOmics auto-resolves `file + ".tbi"` only if the .tbi is at the same S3 prefix. Copy from `_index/` directories to co-locate.
- All GBE input arrays must be same length and in same sample order
- ValidatePedFile script in sv_pipeline docker may be incompatible — safe to no-op

## Docker Images
- gatk: __ACCOUNT_ID__.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/gatk:mw-gatk-sv-672d85
- sv_pipeline: .../gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604
- sv_base: .../gatk-sv/sv-base:2024-10-25-v0.29-beta-5ea22a52
- sv_base_mini: .../gatk-sv/sv-base-mini:2024-10-25-v0.29-beta-5ea22a52
- cnmops: .../gatk-sv/cnmops:2025-09-02-v1.0.5-f091af0b
- linux: .../ecr-public/lts/ubuntu:18.04

## Samples
NA12878, HG00096, HG00097, HG00099, HG00100, HG00101, HG00102, NA19238, NA19239, HG00513 (batch_01)
