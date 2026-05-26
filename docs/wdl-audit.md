# WDL Audit — output equivalence vs. upstream Broad GATK-SV

**Date:** 2026-05-26 (v2 — validation results recorded)
**Triggered by:** customer reported "this sample takes 2+ hours" — our pipeline finished it in ~1.5 hours, raising the question of whether we'd quietly changed pipeline logic in the name of perf optimization.

This document audits every workflow we run in production against the upstream
GATK-SV WDL the spec mandates. Each finding is classified:

| Class | Meaning |
|---|---|
| 🟢 SAFE | Resource changes only (CPU / mem / instance type / parallelism counts that don't alter the output algorithm). Output bit-identical or scientifically equivalent to upstream. |
| 🟡 NEEDS VALIDATION | Algorithmically equivalent in theory, but the change touches the calling layer (e.g. multi-threading). Output equivalence claimed but not formally tested. |
| 🔴 LOGIC REGRESSION | Output **differs** from upstream in a way that reduces sensitivity, drops a caller, or skips a step. |

## Phase A: per-sample GSE workflows

### GSE-cc / CollectCounts (workflow 8771956)

🟢 **SAFE.**

| | Upstream `CollectCoverage.wdl` | Our `CollectCountsOptimized` |
|---|---|---|
| Tool | `gatk CollectReadCounts` | same |
| Algorithm flags | `-L intervals --format TSV --interval-merging-rule OVERLAPPING_ONLY` | identical |
| Memory flag | `-Xmx<runtime_mem - 0.5GB>m` | `-Xmx6g` (slightly smaller heap) |
| Pre-localization | localized by HealthOmics fuse mount | explicit `cp` to `/tmp` for sequential I/O |

The pre-localize-to-`/tmp` step avoids HealthOmics FUSE random-read latency for
the CRAM. `gatk CollectReadCounts` flags are identical. Output: counts.tsv.gz
should be byte-identical at the same intervals input. **Verdict: safe.**

### GSE-cse / CollectSVEvidence (workflow 7038412)

🟢 **SAFE.**

| | Upstream `CollectSVEvidence.wdl` | Our `CollectSVEvidenceFlat` |
|---|---|---|
| Tool | `gatk CollectSVEvidence` | same |
| Algorithm flags | `-I --sample-name -F sd_locs_vcf -SR -PE -SD --site-depth-min-mapq 6 --site-depth-min-baseq 10 -R -L primary_contigs_list --read-filter NonZeroReferenceLengthAlignmentReadFilter` | identical |
| Memory | `-Xmx<runtime_mem - 0.5GB>m` | `-Xmx24576m` (24 GiB; runs on 32 GiB instance) |
| Pre-localization | (none) | (none) |
| Index regen | (CRAM index from caller) | extra `samtools index ~{cram}` step (idempotent if already indexed) |

Same GATK tool, same flags, same `-L` interval list, same min-mapq/baseq.
Output: pe.txt.gz / sr.txt.gz / sd.txt.gz should be byte-identical. **Verdict: safe.**

### GSE-manta (workflow 4091926)

🟢 **SAFE.**

| | Upstream `Manta.wdl` | Our `MantaOptimized` |
|---|---|---|
| Tool | Illumina Manta `configManta.py` + `runWorkflow.py` | same |
| Algorithm flags | `--bam --referenceFasta --callRegions manta_region_bed --runDir manta_run` | identical |
| Concurrency | `-j 8` (upstream default) | `-j $(nproc)` (= 16 on omics.c.4xlarge) |
| Pre-localization | (none; manta loads from FUSE) | explicit `cp` of CRAM + CRAI to `/tmp` |

`-j` only changes how many concurrent Manta sub-workflows run, not the
algorithm. Pre-localization to `/tmp` is a perf workaround; Manta's output
VCF is deterministic for the same input regardless of `-j`. **Cross-engine
verified**: today's Manta-NA12878 body-MD5 matched the EC2/miniwdl run
exactly (13,988 records, MD5 `3c3d300b2dfc514babdf7f6ab0e757d3`). **Verdict: safe.**

### GSE-wham (workflows 2723477 / 6217382)

🔴 **LOGIC REGRESSION** — confirmed 2026-05-26 by side-by-side validation.

| | Upstream `whamg` (workflow 8098138) | `whamg-fast -x 16` (workflow 2723477/6217382) |
|---|---|---|
| Binary | `whamg` from `gatk-sv/wham:2024-10-25-...` | **`whamg-fast`** from custom `gatk-sv/wham:fast-v5` |
| Concurrency | single-threaded full-genome (no `-c`) | OpenMP `-x 16` |
| Memory | 16 GiB on `omics.m.xlarge` | 16 GiB / 30 GiB tiered |
| Wall time (HG00096, 30× CRAM) | **3 h 02 m** | ~17 min |
| Records | **7,324** | 7,307 |
| Body MD5 (records sorted) | `7ab0486b9b074986a6cb9e74532959d4` | `fdc89bb46d603f6adc05ffeb29a03b01` |
| Records only in upstream | — | **1,217** |
| Records only in fast | **1,200** | — |
| Common records | 6,107 | 6,107 |

Only **83 % of records overlap.** Sample diffs reveal differing SVLEN values,
shifted breakpoints, and divergent DI (distance-index) statistics — i.e. a
real algorithmic divergence, not numerical noise.

The custom `whamg-fast` binary added two extensions to upstream `whamg`:
(a) `--flush-per-chr` (claimed byte-identical with `-c` per-chr scatter),
(b) `-x N` OpenMP region-parallel mode. Production was running `-x 16` on the
full genome with no `-c`, and the `wham-patch` README explicitly warned the
output is byte-identical "only with `-c`."

**Production action 2026-05-26:**
- All launchers (`run_gse_cohort.py`, `run_gse_cohort_tagged.py`) now point
  wham at the upstream Whamg workflow (8098138) and use the upstream
  `gatk-sv/wham:2024-10-25-v0.29-beta-5ea22a52` Docker image.
- The `WHAM_TIERS` map is preserved for cost-tag compatibility, but both
  tiers now reference the same upstream workflow — the upstream binary has
  uniform memory needs.
- Adds ~3 h to per-sample wall-clock; the "fast" claim cost ~17 % of
  variant calls.

### GSE-scramble (workflow 9880958)

🔴 **LOGIC REGRESSION** — confirmed; **fix validated** 2026-05-26.

| | Upstream `Scramble.wdl` (3 tasks) | Our `ScrambleTest12Par` (1 task) |
|---|---|---|
| Step 1 | `ScramblePart1`: cluster_identifier (single-threaded over `regions_list`) + cutoff calibration from counts | `cluster_identifier -r chr1` ... `-r chr12` in 12-parallel; **chr13–chrY skipped**; no cutoff calibration (no counts file input) |
| Step 2 | `ScramblePart2`: `Rscript SCRAMble.R --eval-meis` against MEI consensus refs | **never runs** |
| Step 3 | `MakeScrambleVcf`: `make_scramble_vcf.py --table --input-vcf manta.vcf` | **never runs** |
| Output | scramble VCF with per-sample MEI calls | header-only VCF with **zero records** |

Production scramble has been emitting **empty VCFs for every sample since session 3**. This was glossed over in session 4 as "ClusterBatch detected empty Scramble VCF, omit it from FilterBatch params" — we routed around the regression instead of fixing it.

**MEI sensitivity in our cohort VCF is therefore reduced even more than the documented MELT exclusion would suggest.** MELT excluded → no Alu/LINE1/SVA mobile-element insertions detected by MELT. Scramble broken → no Alu/LINE1/SVA detected by scramble either. Net: **the cohort VCF effectively has no MEI calls.**

**Validation result 2026-05-26**: ran the upstream 3-task pipeline as a 2-task and 3-task HealthOmics workflow first (runs 8587037 and 2865779) — both terminated at 48 s, confirming the HealthOmics 47-second kill triggers on **any 2+ task workflow with inter-task data flow**, not only the deeply-nested MakeCohortVcf case.

Switched to an **EC2 hybrid** (same Docker images, same flags, same multi-task algorithm — just `docker run` directly on EC2 instead of HealthOmics):
- `scripts/run_scramble_ec2.sh` runs `cluster_identifier` 12-parallel by chromosome (all 24 contigs), `SCRAMble.R --eval-meis` against MEI consensus refs, and `make_scramble_vcf.py + bcftools sort + tabix`.
- Validated on HG00096 2026-05-26: **1,379 MEI VCF records** (vs 0 in production). All `INS:ME:ALU/L1/SVA` types present.
- Output at `s3://healthomics-outputs-<account>-apse1/runs/gatk-sv-e2e/<cohort>/<sample>/scramble-real-ec2/<sample>.scramble.vcf.gz`.
- Wall time per sample ~30 min on the shared EC2 hybrid instance (cluster_identifier dominates, ~10 min; SCRAMble.R ~17 min; make_scramble_vcf.py ~1 min).

**Production action 2026-05-26**:
- `run_cohort_e2e.py` Phase A is now 4 modules (cc, cse, manta, wham); scramble is removed from Phase A.
- New Phase A.5 dispatches `run_scramble_ec2.sh` once per sample via SSM and polls to completion before Phase B begins.
- `run_gse_cohort.py` and `run_gse_cohort_tagged.py` reject `--modules scramble` with an explicit error pointing the operator at the EC2 launcher.
- The broken HealthOmics scramble workflows (9880958, 3973675) are kept registered for forensic reference but no launcher will start them.

## Phase B: cohort modules

| Module | Bundle | Verdict |
|---|---|---|
| TrainGCNV | 6 WDL files, 17.7 KB | 🟢 SAFE — upstream multi-task structure preserved |
| GatherBatchEvidence v5 | 18 WDL files, 38.7 KB | 🟢 SAFE — upstream structure + only resource/FUSE/array fixes (see context-transfer-session3.md) |
| ClusterBatch v3 | 10 WDL files, 24.9 KB | 🟢 SAFE — upstream structure + IndexFeatureFile fix + 8 GiB SVCluster mem |
| GenerateBatchMetrics | 6 WDL files, 16.7 KB | 🟢 SAFE — upstream multi-task structure |
| FilterBatch v3 | 11 WDL files, 23.9 KB | 🟢 SAFE — upstream + array-alignment + run_module_metrics=false (cosmetic) |
| MergeBatchSites v2 | 5 WDL files, 13.3 KB | 🟢 SAFE — upstream + IndexFeatureFile + 8 GiB mem |
| GenotypeBatch | 4 WDL files, 12.4 KB | 🟢 SAFE — upstream multi-task structure |
| AnnotateVcf | 7 WDL files, 17.8 KB | 🟢 SAFE — upstream multi-task structure |

All cohort modules retain the upstream multi-task structure (10-39 KB bundles
with the original WDL filenames preserved). Their only changes are documented
in `docs/divergence-log.md` and the per-module `divergence.json` files in
each `wdl/bundles/<Module>/` directory; every change is either a
HealthOmics-compatibility patch (FUSE handling, IndexFeatureFile for missing
.tbi) or a memory bump.

## Phase C: MakeCohortVcf hybrid (EC2 path)

### CombineBatches via run_combinebatches_ec2.sh

🟢 **SAFE** in the algorithmic sense — runs the same `gatk GroupedSVCluster` and
`svtk resolve` Docker images with the same arguments as upstream's
CombineBatches sub-workflow. The hybrid path is on EC2 only because
HealthOmics terminates these tasks at exactly 47s when invoked from a deeply
nested sub-workflow (see issue-artifact/REPORT.md). Output should match
upstream MakeCohortVcf.CombineBatches exactly.

### RemainingSteps via miniwdl on EC2

🟢 **SAFE** in the algorithmic sense, with one caveat — we apply five WDL
patches for Cromwell-vs-miniwdl semantic differences (`vcf+".tbi"` →
explicit `vcf_indexes`; `rm <localized-input>` → `rm -f ... 2>/dev/null ||
true`; `mv <localized-input>` → `cp` then operate). These are **engine
compatibility patches**, not logic changes — the same WDL runs unmodified
on Cromwell-on-Terra producing the same output. See
`scripts/build_remaining_steps_v2.py` for the patch list.

## Summary

| | Class | Action |
|---|---|---|
| GSE-cc, GSE-cse, GSE-manta | 🟢 SAFE | None |
| **GSE-wham** | 🔴 **LOGIC REGRESSION** | Reverted to upstream Whamg (workflow 8098138) 2026-05-26. ✅ |
| **GSE-scramble** | 🔴 LOGIC REGRESSION | Replaced by EC2 hybrid (`scripts/run_scramble_ec2.sh`) 2026-05-26. ✅ |
| TrainGCNV → AnnotateVcf | 🟢 SAFE | None |
| MakeCohortVcf hybrid | 🟢 SAFE | None |

## Why this slipped through (process gaps)

1. **No cross-engine validation gate** — when we created the `whamg-fast` and
   `scramble-12par` workflows in session 3, we didn't run a side-by-side
   body-MD5 comparison against upstream. The Manta cross-engine validation
   in session 6 was a one-off, not a CI gate.

2. **"Empty Scramble VCF" was treated as a downstream bug, not an upstream
   regression** — when ClusterBatch failed on the empty scramble VCF, the
   fix was to drop scramble from the input parameters rather than
   investigate why scramble was empty.

3. **Custom WDLs replaced upstream wholesale** — the 700-940 byte GSE
   per-tool bundles are tiny because we wrote single-task replacements for
   the upstream multi-task workflows. Any divergence is invisible from a
   diff perspective because the file structure is different.

## Action items

1. ✅ Audit complete (this document)
2. ✅ Validate wham — run `7020853` completed 2026-05-26 (3 h 02 m on `omics.m.xlarge`); 17 % record divergence confirmed; reverted to upstream Whamg.
3. ✅ Replace scramble — EC2 hybrid validated on HG00096 (1,379 MEI records); production launchers updated to dispatch via SSM.
4. ⏭ Add property-based test: for every registered workflow, compare its
   output for NA12878 against the upstream Broad reference output (when
   that becomes available — see validation-runbook.md Step 1)
5. ⏭ Add the cross-engine divergence test to CI: any workflow change
   triggers an automatic comparison against the prior version's output
   for a known sample
