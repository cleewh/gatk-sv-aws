# whamg Memory-Bounded Patch for AWS HealthOmics

## Problem

whamg builds an unbounded in-memory graph (`globalGraph`) that grows as it
processes reads. On AWS HealthOmics, tasks are terminated when memory usage
exceeds the allocated limit (~40 min into processing chr1 at 30x on local disk).

## Root Cause

`globalGraph` (line 61 of whamg.cpp) is never cleared between chromosomes.
When processing from fast local disk, the graph grows to 10-20 GB for chr1
alone, triggering HealthOmics's OOM killer.

## Fix

Add a `--flush-per-chr` flag that processes and outputs each chromosome's
graph immediately after reading, then clears the graph before the next
chromosome. This caps peak memory at ~2-3 GB (one chromosome's graph).

Results are identical when using `-c` (single chromosome mode) since there
are no cross-chromosome edges to preserve.

## Patch

See `whamg-flush.patch` for the diff against commit `master` (HEAD).

## Build

```bash
cd wham
git apply ../whamg-flush.patch
make
```

## Usage

```bash
# Original (OOMs on HealthOmics):
whamg -x 4 -c chr1 -a ref.fasta -f input.bam > out.vcf

# Patched (memory-bounded, identical output):
whamg --flush-per-chr -x 4 -c chr1 -a ref.fasta -f input.bam > out.vcf
```

## Verification

Output is byte-identical to unpatched whamg when using `-c` (single chromosome).
The flag only affects multi-chromosome runs where it flushes between chromosomes.
