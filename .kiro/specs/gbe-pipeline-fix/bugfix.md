# Bugfix Requirements Document

## Introduction

The GatherBatchEvidence (GBE) module of the GATK-SV pipeline on AWS HealthOmics fails (run 7020055) due to two root causes in `gatk-sv-healthomics/scripts/run_pipeline.py`:

1. **CondenseReadCounts EvalError** — miniWDL cannot interpolate null optional parameters (`min_interval_size`, `max_interval_size`) in command blocks, causing an EvalError at CollectCoverage.wdl line 112.
2. **OutOfBounds in PESRPreprocessing** — The `discover_gse_outputs()` function globs ALL matching files across test runs and duplicates, producing arrays of mismatched lengths (e.g., 13 counts but only 4 scramble_vcfs) instead of exactly 10 elements (one per sample in SAMPLES order).

These failures prevent the GBE workflow (ID 5772769) from completing successfully.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN `run_pipeline.py --stage gbe` is executed THEN the system launches GBE without `min_interval_size` and `max_interval_size` parameters, causing CondenseReadCounts to fail with an EvalError due to null optional interpolation in miniWDL

1.2 WHEN `discover_gse_outputs()` searches for GSE output files THEN the system collects ALL matching files under each sample's prefix (including duplicates from prior test runs), producing arrays with inconsistent lengths (e.g., 13 counts, 13 manta_vcfs, 4 scramble_vcfs)

1.3 WHEN GBE receives input arrays of mismatched lengths THEN the system fails with an OutOfBounds error at PESRPreprocessing.wdl line 24 because array indices exceed the shortest array's length

1.4 WHEN `discover_gse_outputs()` collects files THEN the system does not guarantee that array elements correspond to the correct sample in SAMPLES order, potentially misaligning per-sample evidence files

### Expected Behavior (Correct)

2.1 WHEN `run_pipeline.py --stage gbe` is executed THEN the system SHALL include `min_interval_size: 101` and `max_interval_size: 2000` in the GBE parameters to prevent null optional interpolation errors

2.2 WHEN `discover_gse_outputs()` searches for GSE output files THEN the system SHALL return exactly one file per sample per output type, producing arrays of exactly 10 elements each

2.3 WHEN GBE receives input arrays THEN the system SHALL provide arrays that are all the same length (10) matching the number of samples, preventing OutOfBounds errors

2.4 WHEN `discover_gse_outputs()` collects files THEN the system SHALL return arrays where element index `i` corresponds to `SAMPLES[i]`, maintaining correct sample-to-file alignment

### Unchanged Behavior (Regression Prevention)

3.1 WHEN `run_pipeline.py --stage gbe` is executed with valid GSE outputs THEN the system SHALL CONTINUE TO discover TrainGCNV outputs (contig_ploidy_model_tar and gcnv_model_tars) correctly

3.2 WHEN `run_pipeline.py --stage gbe` is executed THEN the system SHALL CONTINUE TO include all existing GBE parameters (batch, samples, docker images, reference files, etc.) unchanged

3.3 WHEN `discover_gse_outputs()` resolves paths for NA12878 THEN the system SHALL CONTINUE TO use the `optimized/` prefix path (`runs/gatk-sv-e2e/NA12878/optimized/`)

3.4 WHEN `discover_gse_outputs()` resolves paths for non-NA12878 samples THEN the system SHALL CONTINUE TO use the `gse/` prefix path (`runs/gatk-sv-e2e/{sample_id}/gse/`)

3.5 WHEN `run_pipeline.py --status` is executed THEN the system SHALL CONTINUE TO report pipeline status without errors

3.6 WHEN `run_pipeline.py --dry-run --stage gbe` is executed THEN the system SHALL CONTINUE TO display the dry-run summary without launching a workflow run
