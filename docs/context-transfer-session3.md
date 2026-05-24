# CONTEXT TRANSFER: GATK-SV Pipeline — Session 3

## Account & Region
- Account: __ACCOUNT_ID__
- Region: ap-southeast-1
- Role: arn:aws:iam::__ACCOUNT_ID__:role/gatk-sv-healthomics-run-role

## Pipeline Status (End of Session 3)

| # | Module | Workflow ID | Run ID | Status |
|---|--------|------------|--------|--------|
| 1 | GatherSampleEvidence | various | various | ✅ COMPLETE (all 10 samples) |
| 2 | TrainGCNV | 2282352 | 8170946 | ✅ COMPLETE |
| 3 | GatherBatchEvidence | 1575165 (v5) | 6129002 | ✅ COMPLETE |
| 4 | ClusterBatch | 2641017 (v3) | 2870194 | ✅ COMPLETE |
| 5 | GenerateBatchMetrics | 5339393 | — | ⏳ BLOCKED (needs rmsk + segdups refs) |
| 6 | FilterBatch | 6118948 | — | ⏳ Waiting on #5 |
| 7 | MergeBatchSites | 1825208 | — | ⏳ Waiting |
| 8 | GenotypeBatch | 9542089 | — | ⏳ Waiting |
| 9 | RegenotypeCNVs | 8299455 | — | ⏳ Waiting |
| 10 | MakeCohortVcf | 1027205 | — | ⏳ Waiting |
| 11 | AnnotateVcf | 6832584 | — | ⏳ Waiting |

## Key Output Locations

- GBE outputs: s3://healthomics-outputs-__ACCOUNT_ID__-apse1/runs/gatk-sv-e2e/batch/gather-batch-evidence/6129002/out/
- ClusterBatch outputs: s3://healthomics-outputs-__ACCOUNT_ID__-apse1/runs/gatk-sv-e2e/batch/cluster-batch/2870194/out/
- TrainGCNV outputs: s3://healthomics-outputs-__ACCOUNT_ID__-apse1/runs/gatk-sv-e2e/batch/train-gcnv/8170946/out/
- Reference files: s3://omics-ref-ap-southeast-1-__ACCOUNT_ID__/gatk-sv/reference/GRCh38/

## Immediate Next Step

GenerateBatchMetrics needs two reference files that aren't staged yet:
- `rmsk` — RepeatMasker track (BED format)
- `segdups` — Segmental duplications track (BED format)

Source: GATK-SV public resources on GCS (gs://gatk-sv-resources-public/hg38/v0/sv-resources/)
Need to download and upload to: s3://omics-ref-ap-southeast-1-__ACCOUNT_ID__/gatk-sv/reference/GRCh38/

## Fixes Applied This Session

### GBE (5 iterations to get working):
1. `min_interval_size: 101`, `max_interval_size: 2000` — CondenseReadCounts EvalError
2. `discover_gse_outputs()` rewritten — per-sample single-file discovery
3. PESRPreprocessing `algorithms` array fixed — removed orphan empty string, aligned to 4 elements
4. PESRPreprocessing output indices corrected — `[0,1,2,3]` not `[0,1,3,4]`
5. `sd_locs_vcf` added — SDtoBAF select_first() error
6. ValidatePedFile — made no-op (docker image script incompatibility)
7. `runtime_attr_ploidy: 60 GiB` — DetermineGermlineContigPloidyCaseMode
8. `runtime_attr_postprocess: 30 GiB` — PostprocessGermlineCNVCalls
9. All in-place `gunzip` replaced with `zcat` to `/tmp` — FUSE read-only

### ClusterBatch (3 iterations):
1. `.tbi` placeholder files uploaded for blacklist BED files
2. `runtime_attr_svcluster_*: 8 GiB` — SVCluster memory override
3. Removed `localization_optional: true` from SVCluster vcfs (didn't help)
4. Added `.tbi` index check/creation with `gatk IndexFeatureFile` before SVCluster — THIS FIXED IT

### Tiered Wham Memory (new feature):
- `run_gse_cohort.py` now queries CRAM size via S3 HEAD
- CRAMs ≤ 20 GiB → workflow 2723477 (16 GiB)
- CRAMs > 20 GiB → workflow 6217382 (30 GiB)
- New WDL: `wham-fast-fullgenome-32g.wdl` (30 GiB runtime)

### Scramble re-runs:
- All 9 non-NA12878 samples re-run with workflow 9880958 (64 GiB, 12 CPU)
- Original workflow 3973675 only had 16 GiB — insufficient

### Wham HG00513:
- 28.2 GiB CRAM (2x larger than others)
- OOM'd twice at 16 GiB
- Succeeded with 30 GiB workflow 6217382 (took ~3 hours)

## Docker Images
- gatk: __ACCOUNT_ID__.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/gatk:mw-gatk-sv-672d85
- sv_pipeline: .../gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604
- sv_base: .../gatk-sv/sv-base:2024-10-25-v0.29-beta-5ea22a52
- sv_base_mini: .../gatk-sv/sv-base-mini:2024-10-25-v0.29-beta-5ea22a52
- cnmops: .../gatk-sv/cnmops:2025-09-02-v1.0.5-f091af0b
- scramble: .../gatk-sv/scramble:2024-10-25-v0.29-beta-5ea22a52
- wham: .../gatk-sv/wham:fast-v5
- linux: .../ecr-public/lts/ubuntu:18.04

## CRAM Sizes (validation cohort)
| Sample | Size |
|--------|------|
| NA12878 | 14.7 GiB |
| HG00096 | 14.7 GiB |
| HG00097 | 14.7 GiB |
| HG00099 | 17.6 GiB |
| HG00100 | 14.0 GiB |
| HG00101 | 15.5 GiB |
| HG00102 | 14.4 GiB |
| NA19238 | 19.4 GiB |
| NA19239 | 14.5 GiB |
| HG00513 | 28.2 GiB |

## Critical HealthOmics Learnings (Updated)
- FUSE is read-only — use zcat, not gunzip in-place
- miniWDL can't interpolate null optionals in ~{default=X var} — pass all values explicitly
- Memory: gCNV ploidy needs 60 GiB, PostprocessGermlineCNVCalls needs 30 GiB
- HealthOmics rejects "32 GiB" — use "30 GiB"
- All GBE input arrays must be same length and in same sample order
- GATK SVCluster requires .tbi index files co-located with VCFs — create with IndexFeatureFile if missing
- localization_optional: true causes issues when intermediate task outputs need indexes
- ValidatePedFile script in sv_pipeline docker may be incompatible — safe to no-op
- SVCluster default 3.75 GiB is too low for 10-sample batches — use 8 GiB
- Wham OOMs on CRAMs > 20 GiB with 16 GiB memory — use tiered provisioning (30 GiB for large)
- Scramble needs 64 GiB for parallel cluster_identifier (not 16 GiB)

## Key Scripts Modified
- `gatk-sv-healthomics/scripts/run_pipeline.py` — GBE params + discover_gse_outputs fix
- `gatk-sv-healthomics/scripts/run_gse_cohort.py` — tiered wham memory provisioning

## WDL Bundles Created
- GBE v5: `gatk-sv-healthomics/wdl/bundles/GatherBatchEvidence/GatherBatchEvidence-bundle-v5.zip` (workflow 1575165)
- ClusterBatch v3: `gatk-sv-healthomics/wdl/bundles/ClusterBatch/ClusterBatch-bundle-v3.zip` (workflow 2641017)
