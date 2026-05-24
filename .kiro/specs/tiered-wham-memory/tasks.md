# Implementation Plan: Tiered Wham Memory Provisioning

## Overview

Add tiered memory provisioning to `run_gse_cohort.py` so the orchestrator automatically selects the correct wham workflow (16 GiB or 30 GiB) based on each sample's CRAM file size queried via S3 HEAD request. Implementation proceeds incrementally: constants and pure functions first, then S3 integration, then wiring into `launch_run`, and finally tests.

## Tasks

- [ ] 1. Add module-level constants and tier configuration
  - [x] 1.1 Add `WHAM_SIZE_THRESHOLD_BYTES` constant and `WHAM_TIERS` dict to `gatk-sv-healthomics/scripts/run_gse_cohort.py`
    - Define `WHAM_SIZE_THRESHOLD_BYTES: int = 21_474_836_480` (20 GiB)
    - Define `WHAM_TIERS` dict with `"standard"` and `"high_memory"` entries containing `id`, `memory_gib`, and `label` keys
    - Add `"tiered": True` flag to the existing `WORKFLOWS["wham"]` entry
    - _Requirements: 2.4_

- [ ] 2. Implement pure tier selection function
  - [x] 2.1 Implement `select_wham_tier(size_bytes, threshold)` in `gatk-sv-healthomics/scripts/run_gse_cohort.py`
    - Returns `WHAM_TIERS["standard"]` when `size_bytes <= threshold`
    - Returns `WHAM_TIERS["high_memory"]` when `size_bytes > threshold`
    - Default `threshold` parameter to `WHAM_SIZE_THRESHOLD_BYTES`
    - _Requirements: 2.1, 2.2_

  - [ ]* 2.2 Write property test for tier selection partition
    - **Property 2: Tier Selection Partition**
    - Test that for any non-negative `size_bytes` and positive `threshold`, the function returns Standard_Tier when `size_bytes <= threshold` and High_Memory_Tier otherwise
    - Use `st.integers(min_value=0, max_value=100 * 1024**3)` for size and `st.integers(min_value=1, max_value=50 * 1024**3)` for threshold
    - **Validates: Requirements 2.1, 2.2**

  - [ ]* 2.3 Write property test for GiB conversion correctness
    - **Property 1: GiB Conversion Correctness**
    - Test that converting `size_bytes / (1024**3)` and back produces a value within floating-point tolerance of the original
    - Use `st.integers(min_value=0, max_value=100 * 1024**3)`
    - **Validates: Requirements 1.2**

- [ ] 3. Implement S3 CRAM size query function
  - [x] 3.1 Implement `get_cram_size_bytes(s3_client, bucket, key)` in `gatk-sv-healthomics/scripts/run_gse_cohort.py`
    - Issue `s3_client.head_object(Bucket=bucket, Key=key)` and return `response["ContentLength"]`
    - Raise `botocore.exceptions.ClientError` on failure (caller handles)
    - _Requirements: 1.1, 5.1_

  - [ ]* 3.2 Write unit tests for S3 interaction
    - Test that `get_cram_size_bytes` calls `head_object` (not `get_object`) with correct bucket/key
    - Test that `ClientError` propagates to caller
    - Mock `s3_client` using `unittest.mock`
    - _Requirements: 1.1, 5.1_

- [ ] 4. Modify `launch_run` to use tiered selection for wham
  - [x] 4.1 Update `launch_run` in `gatk-sv-healthomics/scripts/run_gse_cohort.py` to detect `wf.get("tiered")` and invoke tier selection
    - When `module == "wham"` and `wf.get("tiered")` is True:
      - Create an S3 client (`boto3.client("s3", region_name=REGION)`)
      - Derive bucket and key from `COHORT_BASE` and `sample_id`
      - Call `get_cram_size_bytes` to get file size
      - Call `select_wham_tier` to get the tier config
      - Log tier selection: `f"[{sample_id}] CRAM {size_gib:.1f} GiB → {tier['label']} (workflow {tier['id']})"`
      - Override `wf["id"]` with `tier["id"]` for the run
      - Add `workflow_id` and `tier` fields to the returned result dict
    - On `ClientError`: log error with sample_id and reason, return `None`
    - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 3.1, 3.2_

  - [x] 4.2 Update the main loop in `main()` to handle `None` returns from `launch_run`
    - Only append to `launched` list when result is not `None`
    - _Requirements: 1.3_

  - [ ]* 4.3 Write property test for log message completeness
    - **Property 4: Log Message Completeness**
    - Test that for any `sample_id` and `size_bytes`, the log message contains the sample_id, size in GiB rounded to 1 decimal, and the correct tier label
    - Use `st.tuples(st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=('L', 'N'))), st.integers(min_value=0, max_value=100 * 1024**3))`
    - **Validates: Requirements 3.1**

  - [ ]* 4.4 Write property test for manifest workflow ID correctness
    - **Property 5: Manifest Workflow ID Correctness**
    - Test that for any wham run result, the `workflow_id` field equals the `id` of the tier returned by `select_wham_tier` for that sample's CRAM size
    - Use `st.integers(min_value=0, max_value=100 * 1024**3)`
    - **Validates: Requirements 3.2**

- [x] 5. Checkpoint - Verify core implementation
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 6. Preserve non-wham module behavior and add remaining tests
  - [x] 6.1 Write unit tests verifying non-wham modules are unchanged
    - Test that non-wham modules (manta, cc, scramble, cse) do not trigger S3 calls
    - Test that non-wham module parameters and output URIs are unchanged
    - Mock S3 client and verify it is never called for non-wham modules
    - _Requirements: 4.1, 4.2_

  - [ ]* 6.2 Write property test for parameter interface invariant
    - **Property 3: Parameter Interface Invariant**
    - Test that for any valid `sample_id`, `build_params("wham", sample_id)` returns the same set of keys regardless of tier selection
    - Use `st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=('L', 'N')))`
    - **Validates: Requirements 2.3**

  - [ ]* 6.3 Write unit test for threshold constant value
    - Verify `WHAM_SIZE_THRESHOLD_BYTES == 21_474_836_480`
    - _Requirements: 2.4_

  - [ ]* 6.4 Write unit test for single HEAD per sample
    - Verify that during a wham launch, `head_object` is called exactly once per sample
    - _Requirements: 5.2_

- [x] 7. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- All tests go in `tests/gatk_sv_healthomics/unit/test_tiered_wham.py`
- Test framework: pytest + hypothesis (already configured in the project)
