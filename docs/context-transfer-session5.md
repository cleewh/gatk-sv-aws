# CONTEXT TRANSFER: GATK-SV Pipeline — Session 5 (FINAL)

## Account & Region
- Account: __ACCOUNT_ID__
- Region: ap-southeast-1
- Role: arn:aws:iam::__ACCOUNT_ID__:role/gatk-sv-healthomics-run-role

## Pipeline Status (End of Session 5)

| # | Module | Workflow ID | Run ID | Status |
|---|--------|------------|--------|--------|
| 1 | GatherSampleEvidence | various | various | ✅ COMPLETE |
| 2 | TrainGCNV | 2282352 | 8170946 | ✅ COMPLETE |
| 3 | GatherBatchEvidence | 1575165 (v5) | 6129002 | ✅ COMPLETE |
| 4 | ClusterBatch | 2641017 (v3) | 2870194 | ✅ COMPLETE |
| 5 | GenerateBatchMetrics | 5339393 | 2916467 | ✅ COMPLETE |
| 6 | FilterBatch | 3328339 (v3) | 5070716 | ✅ COMPLETE |
| 7 | MergeBatchSites | 3326995 (v2) | 7287325 | ✅ COMPLETE |
| 8 | GenotypeBatch | 9542089 | 3154916 | ✅ COMPLETE |
| 9 | RegenotypeCNVs | 8299455 | — | ⏭️ SKIPPED |
| 10 | MakeCohortVcf | — | — | ⛔ BLOCKED — see analysis below |
| 11 | AnnotateVcf | 6832584 | — | ⏳ Waiting on #10 |

## Bug Fixes Discovered This Session

### Fix 1: Wrong Reference Data ✅ APPLIED (FOUND BY EC2 REPRODUCTION)

The local stratification config files had a wrong schema and were missing the Simple Repeats track. Discovered by reproducing GroupedSVCluster on dedicated EC2 instance (m5.2xlarge), which exposed the actual GATK error:

```
A USER ERROR has occurred: Bad input: more than one column have the same name: 0.5
```

**Fix applied to S3 (`s3://omics-ref-ap-southeast-1-__ACCOUNT_ID__/gatk-sv/reference/GRCh38/`):**

| File | Status |
|------|--------|
| `gs_stratification_config.part_one.tsv` | ✅ Replaced with upstream Broad version (proper schema: `NAME, SVTYPE, MIN_SIZE, MAX_SIZE, TRACKS`) |
| `gs_stratification_config.part_two.tsv` | ✅ Replaced with upstream Broad version |
| `hg38.SimpRep.sorted.pad_100.merged.bed.gz` | ✅ NEW (4.5 MB) |
| `hg38.SimpRep.sorted.pad_100.merged.bed.gz.tbi` | ✅ NEW (965 KB) |

**Run parameters now use:**
```python
"track_bed_files": [
    f"{REF_BASE}/hg38.SimpRep.sorted.pad_100.merged.bed.gz",
    f"{REF_BASE}/segdups.bed.gz",
    f"{REF_BASE}/rmsk.bed.gz",
],
"track_names": ["SR", "SD", "RM"],
```

**Verified working on EC2** (m5.2xlarge, GATK image `mw-gatk-sv-672d85`):
```
04:18:33.418 INFO  ProgressMeter - Traversal complete. Processed 37 total variants in 0.0 minutes.
[May 23, 2026] org.broadinstitute.hellbender.tools.walkers.sv.GroupedSVCluster done. Elapsed time: 0.20 minutes.
✓ ALL STEPS COMPLETED SUCCESSFULLY
```

### Issue 2: HealthOmics-Specific GroupedSVCluster Failure ⛔ UNRESOLVED

Despite the reference data fix, **GroupedSVClusterPart1 still fails on HealthOmics** with the EXACT same pattern:
- Task duration: consistently 46-53 seconds
- No CloudWatch logs ever produced
- Engine logs show only: `error: "Terminated"`
- Affects all 24 chromosomes simultaneously
- Persists across multiple WDL configurations and resource sizes

**Bundle versions tested (all failed at GroupedSVClusterPart1):**

| Version | Configuration | Memory | CPUs | Instance | localization_optional | Result |
|---------|--------------|--------|------|----------|----------------------|--------|
| v2 | upstream + IndexFeatureFile in SVCluster | 3.75 | 1 | omics.c.large | true | FAIL (~50s) |
| v3 | + IndexFeatureFile in GroupedSVCluster | 8 | 1 | omics.m.large | true | FAIL |
| v4 | - localization_optional | 8 | 1 | omics.m.large | false | FAIL (~17s) |
| v5 | + 16 GiB / 50 GB disk | 16 | 1 | omics.r.large | true | FAIL (~53s) |
| v6 | upstream-only changes | 3.75 | 1 | omics.c.large | true | FAIL (~48s) |
| v7 | + 8 GiB | 8 | 1 | omics.m.large | true | FAIL (~46s) |
| v8 | + 4 vCPU / 12 GiB | 12 | 4 | omics.m.xlarge | true | FAIL (~46s) |
| v9 | corrected refs (same WDL as v7) | 8 | 1 | omics.m.large | true | FAIL (~46s) |
| v10 | - localization_optional | 8 | 1 | omics.m.large | false | FAIL (~46s) |
| v11 | + 4 vCPU + 100 GB disk | 8 | 4 | omics.c.xlarge | false | FAIL (~46s) |

**Reference: SVCluster-based tasks (JoinVcfs, ClusterSites) work fine on HealthOmics with same image and similar configs:**
- JoinVcfs: 72-75s, produces full logs ✅
- ClusterSites: 52-55s, produces full logs ✅
- GroupedSVClusterPart1: 46-53s, NO logs ❌

The failure pattern is too consistent to be random — same exact 46s duration across 21+ task instances suggests a HealthOmics-side timeout or container setup issue specific to this task type. Local EC2 reproduction shows the GATK tool itself runs in 13s with full logs, ruling out tool-level issues.

## Recommended Next Steps

1. **Open AWS HealthOmics support case** with details:
   - Workflow ID 6720771 (v11), Run ID 9723050 — most recent failure
   - GATK image: `__ACCOUNT_ID__.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/gatk:mw-gatk-sv-672d85`
   - Tool: `gatk GroupedSVCluster` (GATK 4.6.2.0)
   - Symptom: Task terminated at exactly 46s, no container logs in `/aws/omics/WorkflowLog`
   - Reproducible on dedicated EC2 (m5.2xlarge) with same image — confirms tool works correctly

2. **Workaround option**: Run MakeCohortVcf module entirely on EC2/Cromwell, then import the cleaned cohort VCF back into HealthOmics for AnnotateVcf

3. **Workaround option 2**: Skip MakeCohortVcf entirely — the per-batch VCFs from GenotypeBatch are valid SV call sets, just not joint-called across batches

## EC2 Test Setup (for reference / future debugging)

- Instance: `i-02c67bb34211a85ed` (Kiro stack, currently stopped)
- Modified to `m5.2xlarge` (8 vCPU, 32 GiB) — sufficient for GATK GroupedSVCluster
- Test script: `gatk-sv-healthomics/scripts/run_groupedsvcluster_test.sh`
- SSM helper: `gatk-sv-healthomics/scripts/ec2_test_helpers.py`
- Note: `/tmp` is wiped on instance reboot, so re-pull Docker images and re-download refs each session

## Files Created / Modified This Session

- `gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/MakeCohortVcf-bundle-v2.zip` through `v11.zip`
- `gatk-sv-healthomics/wdl/bundles/MakeCohortVcf/divergence.json` — documents WDL changes
- `gatk-sv-healthomics/scripts/launch_make_cohort_vcf_v2.py` — launch helper
- `gatk-sv-healthomics/scripts/run_groupedsvcluster_test.sh` — local reproduction script
- `gatk-sv-healthomics/scripts/ec2_test_helpers.py` — SSM helper
- `gatk-sv-healthomics/scripts/launch_v11.py` — v11 launcher
- `s3://omics-ref-ap-southeast-1-__ACCOUNT_ID__/gatk-sv/reference/GRCh38/`:
  - `gs_stratification_config.part_one.tsv` (replaced)
  - `gs_stratification_config.part_two.tsv` (replaced)
  - `hg38.SimpRep.sorted.pad_100.merged.bed.gz` (new)
  - `hg38.SimpRep.sorted.pad_100.merged.bed.gz.tbi` (new)

## All HealthOmics Workflow Versions Created (Cumulative)

| Workflow | ID | Description |
|----------|-----|-------------|
| FilterBatch-v2 | 1765546 | Array alignment fix in FilterBatchSites only |
| FilterBatch-v3 | 3328339 | Full fix: all 3 WDLs + removed redundant PlotSVCountsPerSample |
| MergeBatchSites-v2 | 3326995 | IndexFeatureFile before SVCluster |
| MakeCohortVcf-v2 | 5146527 | IndexFeatureFile + 8 GiB SVCluster |
| MakeCohortVcf-v3 | 3584634 | + IndexFeatureFile in GroupedSVCluster (later removed) |
| MakeCohortVcf-v4 | 3275497 | + removed localization_optional |
| MakeCohortVcf-v5 | 7609471 | + 16 GiB / 50 GB disk |
| MakeCohortVcf-v6 | 4112340 | Reverted CombineBatches to upstream |
| MakeCohortVcf-v7 | 5902498 | + 8 GiB GroupedSVCluster memory |
| MakeCohortVcf-v8 | 5190636 | + 4 vCPU / 12 GiB |
| MakeCohortVcf-v9 | 5902498 (re-used v7) | + corrected reference data ← first successful with refs |
| MakeCohortVcf-v10 | 7294659 | + no localization_optional + corrected refs |
| MakeCohortVcf-v11 | 6720771 | + 4 vCPU + 100 GB disk + no localization_optional + corrected refs |
| MakeCohortVcf-v12 | 3229664 | + track_bed_tarball workaround (Array[File] → File) — STILL FAILED 46s no logs |
| MakeCohortVcf-v13 | 5502288 | + diagnostic shell output before GATK — no logs ever produced |
| MakeCohortVcf-v14 | 5052070 | + 90s sleep loop — proved tasks NOT killed at fixed timer |
| MakeCohortVcf-v15 | 7601894 | + Combined ClusterSites+GroupedSVPart1+Part2 in single task — STILL FAILED |

## Critical Learnings (Updated)

- **"Terminated" with no logs ≠ HealthOmics issue** — Sometimes it IS, sometimes it's a tool error suppressed by the container failure mode. Always reproduce on EC2 to know which.
- **Reference data schema matters** — Verify against upstream Broad. The `stratify_config` schema is `NAME, SVTYPE, MIN_SIZE, MAX_SIZE, TRACKS`.
- **Track names must match clustering config codes** — Broad uses `["SR", "SD", "RM"]`, not arbitrary names.
- **Some GATK tools have HealthOmics compatibility issues** — `GroupedSVCluster` (GATK 4.6) is one; same image and inputs work on EC2 but consistently fail on HealthOmics.
- All other learnings from sessions 1-4 remain valid.

## Docker Images (unchanged)
- gatk: __ACCOUNT_ID__.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/gatk:mw-gatk-sv-672d85
- sv_pipeline: .../gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604
- sv_base_mini: .../gatk-sv/sv-base-mini:2024-10-25-v0.29-beta-5ea22a52
- linux: .../ecr-public/lts/ubuntu:18.04

## Samples
NA12878, HG00096, HG00097, HG00099, HG00100, HG00101, HG00102, NA19238, NA19239, HG00513 (batch_01)
