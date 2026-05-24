# GBE Pipeline Fix — Bugfix Design

## Overview

The GatherBatchEvidence (GBE) pipeline launch fails (run 7020055) due to two defects in `gatk-sv-healthomics/scripts/run_pipeline.py`:

1. Missing `min_interval_size` and `max_interval_size` parameters cause a miniWDL EvalError in CondenseReadCounts.
2. `discover_gse_outputs()` collects ALL matching files via glob instead of exactly one per sample in SAMPLES order, producing mismatched-length arrays that cause an OutOfBounds error in PESRPreprocessing.

The fix adds the two missing parameters and rewrites `discover_gse_outputs()` to iterate per-sample, find exactly one file per output type, and raise an error if the expected file is missing.

## Glossary

- **Bug_Condition (C)**: The condition that triggers the bug — either (a) GBE params dict lacks `min_interval_size`/`max_interval_size`, or (b) `discover_gse_outputs()` returns arrays with length ≠ 10 or misaligned sample ordering
- **Property (P)**: The desired behavior — GBE params include interval size parameters, and output arrays have exactly 10 elements aligned to SAMPLES order
- **Preservation**: Existing behavior that must remain unchanged — TrainGCNV discovery, all other GBE parameters, NA12878 optimized/ path routing, --status and --dry-run commands
- **`discover_gse_outputs()`**: Function in `run_pipeline.py` that locates per-sample GSE output files in S3
- **SAMPLES**: Ordered list of 10 sample IDs defining the cohort
- **GBE params dict**: The `params` dictionary passed to `omics.start_run()` for the GatherBatchEvidence workflow

## Bug Details

### Bug Condition

The bug manifests in two independent ways:

1. **Missing interval parameters**: The GBE params dict omits `min_interval_size` and `max_interval_size`, causing miniWDL to fail when interpolating these optional parameters in the CondenseReadCounts command block.

2. **Glob-all discovery**: `discover_gse_outputs()` appends every matching file found under a sample's prefix (including duplicates from prior test runs), producing arrays of inconsistent lengths (e.g., 13 counts, 4 scramble_vcfs) instead of exactly 10 elements.

**Formal Specification:**
```
FUNCTION isBugCondition(params, gse_outputs)
  INPUT: params of type Dict, gse_outputs of type Dict[str, List[str]]
  OUTPUT: boolean

  missing_params := "min_interval_size" NOT IN params
                    OR "max_interval_size" NOT IN params

  wrong_lengths := ANY array IN gse_outputs.values()
                   WHERE len(array) != len(SAMPLES)

  misaligned := ANY i IN range(len(SAMPLES))
                WHERE SAMPLES[i] NOT IN gse_outputs[any_key][i]

  RETURN missing_params OR wrong_lengths OR misaligned
END FUNCTION
```

### Examples

- **Example 1 (missing params)**: `params` dict has 30+ keys but no `min_interval_size` → CondenseReadCounts EvalError at CollectCoverage.wdl line 112
- **Example 2 (wrong lengths)**: `discover_gse_outputs()` returns `counts` with 13 elements, `scramble_vcfs` with 4 elements → OutOfBounds at PESRPreprocessing.wdl line 24
- **Example 3 (misalignment)**: If glob finds NA12878's file before HG00096's file for counts but after for pe_files, array indices no longer correspond to the same sample
- **Example 4 (correct)**: After fix, all 7 arrays have exactly 10 elements, element `i` contains `SAMPLES[i]` in its path, and params include both interval size keys

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- `discover_train_gcnv_outputs()` must continue to find `contig_ploidy_model_tar` and `gcnv_model_tars` correctly
- All existing GBE parameters (batch, samples, docker images, reference files, boolean flags, etc.) must remain in the params dict with their current values
- NA12878 must continue to use the `optimized/` prefix path (`runs/gatk-sv-e2e/NA12878/optimized/`)
- Non-NA12878 samples must continue to use the `gse/` prefix path (`runs/gatk-sv-e2e/{sample_id}/gse/`)
- `--status` command must continue to report pipeline status without errors
- `--dry-run --stage gbe` must continue to display the dry-run summary without launching a workflow

**Scope:**
All inputs that do NOT involve the GBE parameter construction or GSE output discovery should be completely unaffected by this fix. This includes:
- TrainGCNV output discovery logic
- Other pipeline stages (cluster, metrics, filter, etc.)
- Status reporting and dry-run display logic
- Docker image references and reference file paths

## Hypothesized Root Cause

Based on the bug description and code analysis, the root causes are:

1. **Missing Parameters (CondenseReadCounts EvalError)**: The `params` dict in the `gbe` stage block simply never includes `min_interval_size` or `max_interval_size`. The GBE WDL declares these as optional `Int?` inputs with `default=101` / `default=2000` in command blocks, but miniWDL's `~{default=X var}` syntax fails when the variable is truly null (not passed). The TrainGCNV fix (run 8170946) already proved that explicitly passing these values resolves the EvalError.

2. **Glob-All Discovery (OutOfBounds)**: `discover_gse_outputs()` iterates over SAMPLES but uses `paginator.paginate(Prefix=prefix)` which returns ALL objects under that prefix. Prior test runs left duplicate output files (e.g., multiple `.counts.tsv.gz` files from different run attempts). The function appends every match without deduplication or per-sample limiting, so arrays grow beyond 10 elements. Additionally, different output types may have different numbers of duplicates (13 counts vs 4 scramble_vcfs), causing length mismatches.

3. **No Sample-Order Guarantee**: Even if lengths happened to match, the function doesn't enforce that `outputs["counts"][i]` corresponds to `SAMPLES[i]`. Files are appended in S3 listing order (lexicographic by key), not in SAMPLES order.

4. **No Validation**: The function has no assertion or error check to verify array lengths or sample alignment before passing arrays to the workflow.

## Correctness Properties

Property 1: Bug Condition - GBE Parameters Include Interval Sizes

_For any_ invocation of the GBE stage, the params dict SHALL contain `min_interval_size` with value `101` and `max_interval_size` with value `2000`, preventing null optional interpolation errors in CondenseReadCounts.

**Validates: Requirements 2.1**

Property 2: Bug Condition - Output Arrays Have Exactly N Elements

_For any_ set of S3 listings (including listings with duplicates or missing files), the fixed `discover_gse_outputs()` function SHALL return arrays where each array has exactly `len(SAMPLES)` elements, raising an error if any sample is missing a required output file.

**Validates: Requirements 2.2, 2.3**

Property 3: Bug Condition - Output Arrays Maintain Sample Order

_For any_ set of S3 listings, the fixed `discover_gse_outputs()` function SHALL return arrays where element at index `i` contains a file path that includes `SAMPLES[i]` in its URI, maintaining correct sample-to-file alignment.

**Validates: Requirements 2.4**

Property 4: Preservation - Existing GBE Parameters Unchanged

_For any_ invocation of the GBE stage, the fixed params dict SHALL contain all previously-existing parameters (batch, samples, docker images, reference files, boolean flags) with their original values, preserving the full parameter set.

**Validates: Requirements 3.1, 3.2**

Property 5: Preservation - NA12878 Uses Optimized Prefix

_For any_ invocation of `discover_gse_outputs()`, the file paths for NA12878 SHALL use the `optimized/` prefix (`runs/gatk-sv-e2e/NA12878/optimized/`) while all other samples SHALL use the `gse/` prefix (`runs/gatk-sv-e2e/{sample_id}/gse/`).

**Validates: Requirements 3.3, 3.4**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `gatk-sv-healthomics/scripts/run_pipeline.py`

**Function 1**: GBE params dict (in `main()`, `gbe` stage block)

**Specific Changes**:
1. **Add interval size parameters**: Insert `"min_interval_size": 101` and `"max_interval_size": 2000` into the `params` dict, after the existing numeric parameters (e.g., after `min_svsize`).

**Function 2**: `discover_gse_outputs()`

**Specific Changes**:
2. **Per-sample iteration with single-file selection**: Rewrite the function to iterate over SAMPLES, construct the correct prefix for each sample, list objects under that prefix, and select exactly one file per output type per sample.

3. **Suffix-based matching**: For each output type, match files by their suffix pattern:
   - counts: `.counts.tsv.gz`
   - pe_files: `.pe.txt.gz`
   - sr_files: `.sr.txt.gz`
   - sd_files: `.sd.txt.gz`
   - manta_vcfs: file contains `manta` and ends with `.vcf.gz` (exclude `.tbi`)
   - wham_vcfs: file contains `wham` and ends with `.vcf.gz`
   - scramble_vcfs: file contains `scramble` and ends with `.vcf.gz`

4. **Exactly-one enforcement**: For each sample × output type, assert exactly one matching file is found. If zero matches, raise a `FileNotFoundError`. If multiple matches, select the most recently modified (latest `LastModified`) to handle duplicate test-run artifacts.

5. **Sample-order guarantee**: Because we iterate SAMPLES in order and append one file per sample, the resulting arrays are inherently aligned to SAMPLES order.

6. **Validation at end**: After building all arrays, assert all arrays have length equal to `len(SAMPLES)` as a safety check.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code, then verify the fix works correctly and preserves existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write unit tests that mock S3 responses with realistic duplicate files and verify the current `discover_gse_outputs()` produces wrong-length arrays. Also verify the params dict lacks interval size keys.

**Test Cases**:
1. **Missing Params Test**: Assert `params` dict does not contain `min_interval_size` or `max_interval_size` (will fail on unfixed code — confirms bug)
2. **Duplicate Files Test**: Mock S3 listing with 2 counts files per sample, verify function returns 20 counts instead of 10 (will fail on unfixed code)
3. **Mismatched Lengths Test**: Mock S3 with varying duplicates per output type, verify arrays have different lengths (will fail on unfixed code)
4. **Ordering Test**: Mock S3 responses and verify element `i` does not reliably correspond to `SAMPLES[i]` (will fail on unfixed code)

**Expected Counterexamples**:
- `len(outputs["counts"]) == 13` instead of 10
- `len(outputs["scramble_vcfs"]) == 4` instead of 10
- `"min_interval_size" not in params` is True

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL s3_listings WHERE duplicates_exist OR multiple_runs_present DO
  result := discover_gse_outputs_fixed(s3_listings)
  ASSERT len(result[key]) == len(SAMPLES) FOR ALL key
  ASSERT SAMPLES[i] IN result[key][i] FOR ALL key, i
END FOR

FOR ALL params_dict built by gbe stage DO
  ASSERT "min_interval_size" IN params_dict
  ASSERT params_dict["min_interval_size"] == 101
  ASSERT "max_interval_size" IN params_dict
  ASSERT params_dict["max_interval_size"] == 2000
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL s3_listings WHERE exactly_one_file_per_sample_per_type DO
  ASSERT discover_gse_outputs_original(s3_listings) == discover_gse_outputs_fixed(s3_listings)
END FOR

FOR ALL existing_param_keys IN original_params DO
  ASSERT existing_param_keys IN fixed_params
  ASSERT original_params[key] == fixed_params[key]
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many S3 listing configurations automatically
- It catches edge cases like empty prefixes, single-file samples, or unusual key orderings
- It provides strong guarantees that non-buggy inputs produce identical results

**Test Plan**: Observe behavior on UNFIXED code first for clean S3 listings (exactly one file per sample per type), then write property-based tests capturing that behavior.

**Test Cases**:
1. **Clean Listing Preservation**: Generate S3 listings with exactly 1 file per sample per type, verify fixed function produces same result as original
2. **Parameter Preservation**: Verify all original params (batch, samples, docker, refs) remain unchanged in fixed params dict
3. **Prefix Routing Preservation**: Verify NA12878 → optimized/ and others → gse/ routing is unchanged
4. **TrainGCNV Discovery Preservation**: Verify `discover_train_gcnv_outputs()` is completely unaffected

### Unit Tests

- Test `discover_gse_outputs()` with mocked S3 responses containing exactly one file per sample
- Test `discover_gse_outputs()` with mocked S3 responses containing duplicates (selects latest)
- Test `discover_gse_outputs()` with missing files (raises FileNotFoundError)
- Test GBE params dict contains `min_interval_size: 101` and `max_interval_size: 2000`
- Test GBE params dict retains all existing parameters

### Property-Based Tests

- Generate random S3 listing configurations (varying numbers of files per sample, varying timestamps) and verify fixed function always returns arrays of length 10 with correct sample alignment
- Generate random subsets of existing params and verify they are all preserved in the fixed params dict
- Generate random sample lists and verify prefix routing (NA12878 → optimized/, others → gse/)

### Integration Tests

- Test full `--dry-run --stage gbe` flow with mocked S3 and verify output summary is correct
- Test `--status` command continues to work after code changes
- Test that when S3 has realistic data (one clean file per sample), the end-to-end parameter construction produces a valid HealthOmics `start_run` payload
