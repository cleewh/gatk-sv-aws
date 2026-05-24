# GBE Launch Fix Notes

## Run 7020055 failed with two issues:

### 1. CondenseReadCounts EvalError (line 112, CollectCoverage.wdl)
- Same issue as TrainGCNV: `~{default=101 min_interval_size}` evaluates null
- Fix: Add `min_interval_size: 101` and `max_interval_size: 2000` to GBE params
- These are optional inputs in the GBE workflow parameter template

### 2. OutOfBounds in PESRPreprocessing.wdl (line 24)
- Array index out of bounds because input arrays have mismatched lengths
- Root cause: `discover_gse_outputs()` found 13 counts, 13 manta_vcfs, but only 4 scramble_vcfs
- The function globs ALL matching files, including duplicates from test runs
- Fix: Rewrite to find exactly 1 file per sample in SAMPLES order
- Each array must have exactly 10 elements matching the 10 samples

### Fix approach for run_pipeline.py:
```python
def discover_gse_outputs_per_sample(s3_client) -> dict:
    """Discover exactly one output per sample per module."""
    outputs = {k: [] for k in ["counts", "pe_files", "sr_files", "sd_files",
                                "manta_vcfs", "wham_vcfs", "scramble_vcfs"]}
    
    for sample_id in SAMPLES:
        # Use the known GSE output paths from the cohort run
        base = f"runs/gatk-sv-e2e/{sample_id}/gse"
        # For NA12878, use the optimized paths
        # For others, use the gse/ paths
        # Find the LATEST run for each module per sample
        ...
```

### Additional GBE params needed:
```python
"min_interval_size": 101,
"max_interval_size": 2000,
```

## TrainGCNV Final Config (working - run 8170946):
- Workflow: 2282352 (v8)
- num_intervals_per_scatter: 100000
- filter_intervals: false
- min_interval_size: 101
- max_interval_size: 2000
- Memory: 60 GiB for ploidy/gCNV, 30 GiB for PostprocessGermlineCNVCalls
- FUSE fix: zcat instead of gunzip in-place
