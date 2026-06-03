# Phase 8 Amendment Smoke Test — Final Run Report

**Cohort:** `gatk-sv-156-smoke-test` (10-sample subset of the 156-sample staged cohort)
**Samples:** HG00096, HG00129, HG00140, HG00150, HG00187, HG00239, HG00277, HG00288, HG00337, HG00349
**Account/region:** __ACCOUNT_ID__ / ap-southeast-1
**Date:** 2026-05-26 → 2026-06-03
**Result:** ✅ **PASS — full A→B→C→D pipeline validated end-to-end on a real, never-before-run cohort.**

This smoke test is the gate for the full 156-sample cohort run (task 8.11). **The full cohort has NOT been run** — it awaits explicit user confirmation.

## Phase summary

| Phase | Scope | Status | Detail report |
|---|---|---|---|
| A | Per-sample evidence (GSE ×40 HealthOmics, Scramble ×10 EC2, EvidenceQC) | ✅ COMPLETE | `phase-a-b-progress.md` |
| B | Cohort modules (GBE → ClusterBatch → GenerateBatchMetrics → FilterBatch → MergeBatchSites → GenotypeBatch; RegenotypeCNVs skip-gated) | ✅ COMPLETE | `phase-a-b-progress.md` |
| C | Post-processing (MakeCohortVcf → RefineComplexVariants → GQ_Recalibrator chain) | ✅ COMPLETE | `phase-c-d-progress.md` |
| D | Delivery (AnnotateVcf → MainVcfQC) | ✅ COMPLETE | `phase-c-d-progress.md` |

## Record flow through post-processing
```
MakeCohortVcf cleaned        13,514
  → RefineComplexVariants    (cpx_refined)
  → JoinRawCalls             46,848   (raw-call join, pre-merge)
  → SVConcordance            (eval=cpx_refined vs truth=join_raw_calls)
  → ScoreGenotypes           13,480   (GQ-recalibrated)
  → FilterGenotypes          12,041   (SL/NCR genotype filtering + header sanitize)
  → AnnotateVcf              12,041   (functional + noncoding + AF annotation)
  → MainVcfQC                86 QC plots
```

## All 19 Migrated_Modules exercised
- **8 original HealthOmics modules** (Phase A/B): GSE (cc/cse/manta/wham), GatherBatchEvidence, ClusterBatch, GenerateBatchMetrics, FilterBatch, MergeBatchSites, GenotypeBatch.
- **6 new Phase 8 modules**: EvidenceQC, RefineComplexVariants, ScoreGenotypes (GqRecalibrator), FilterGenotypes, AnnotateVcf, MainVcfQC.
- **4 EC2/miniwdl-hybrid modules**: Scramble, MakeCohortVcf, JoinRawCalls, SVConcordance (native HealthOmics not viable — scatter-heavy steps trip the HealthOmics multi-task kill).
- **RegenotypeCNVs**: correctly skip-gated (sample_count=10 < 100, Req 19.6).

## Defects found & fixed (this run, all committed)
See per-phase reports for the full list. Highlights:
- Scramble parallel-dispatch disk saturation → strictly serial dispatch + per-sample cleanup.
- Recurring Cromwell-vs-miniwdl `.tbi` sibling-index antipattern → WDL tabix-index patches (ClusterBatch/JoinRawCalls SVCluster, SVConcordance eval/truth, RecalibrateGq genome tracks) + S3 index co-location for HealthOmics outputs.
- Missing smoke-cohort PED → created `cohort-gatk-sv-156-smoke-test.ped` (5M/5F).
- **GQ recalibrator**: missing `genome_tracks` (model properties 35–45) + small-cohort AF (`--min-samples-to-estimate-allele-frequency 1`). Staged the 5 ucsc-genome-tracks BEDs.
- **MainVcfQC**: `rm` of read-only localized inputs in `PlotQcPerFamily` → made non-fatal; `MainVcfQc` workflow-name casing in inputs.

## Key artifacts
- Run state: `gse-cohort-runs-…json`, `scramble-ec2-runs-…v2.json`, `phase-b-runs-…json`, `phase-c-runs-…json`, `gq-chain-runs-…json` (all in repo root, gitignored cohort state).
- Outputs: `s3://healthomics-outputs-__ACCOUNT_ID__-apse1/runs/gatk-sv-e2e/gatk-sv-156-smoke-test/batch/`
- GQ reference: `s3://omics-ref-ap-southeast-1-__ACCOUNT_ID__/gatk-sv/reference/GRCh38/` (`gatk-sv-recalibrator.aou_phase_1.v1.model`, `aou_sl_cutoff_table.tsv`, `ucsc-genome-tracks/`)

## Conclusion
The Phase 8 v1.0 completeness amendment is validated end-to-end. All six new modules and the
GQ_Recalibrator chain run on real cohort data; MainVcfQC produces the expected QC plot set. The
pipeline is ready for the full 156-sample cohort **pending the 8.11 user checkpoint**.
