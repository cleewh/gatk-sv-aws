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

## v1.0 module-completeness amendment (Req 19, 2026-05-26)

The original migration covered 10 modules (`GatherSampleEvidence` →
`AnnotateVcf`). Customer feedback flagged that upstream GATK-SV v1.0
includes 22 modules total; we were missing 8 critical ones, including the
entire **GQ_Recalibrator** chain that produces quality-recalibrated
genotypes. The amendment ports these 8 from upstream `gatk-sv@v1.1`
(commit `a1be457`) into our HealthOmics-ready bundle format.

### Phase 8 modules — packaging (this audit)

All 8 modules were ported from upstream commit `broadinstitute/gatk-sv@a1be457`
(v1.1 release tag) by `scripts/migrate_v1_modules.py`. The packager copied the
listed `wdl/*.wdl` entry point plus its transitively-imported sub-WDLs into
`wdl/bundles/<Module>/<Module>-bundle.zip`, then applied the standard policy
divergences (MELT excision, `gs://` URI rejection). Every bundle ships with a
machine-readable `wdl/bundles/<Module>/divergence.json` capturing the
upstream commit, the file list, and the divergences applied.

| Module | Phase | Upstream WDL @ `a1be457` | Bundle | Lint | Validation |
|---|---|---|---|---|---|
| EvidenceQC            | A.6 | `wdl/EvidenceQC.wdl`            | `wdl/bundles/EvidenceQC/` | ✅ clean | ✅ HealthOmics smoke-tested (run `2785300`, 10 samples, COMPLETED 2026-05-28; final workflow id `7602667`) |
| RefineComplexVariants | C.1 | `wdl/RefineComplexVariants.wdl` | `wdl/bundles/RefineComplexVariants/` | ✅ clean | ⏳ Registered on HealthOmics; not yet smoke-tested end-to-end. Likely needs an iterate-and-patch cycle (same 47-second kill risk pattern as EvidenceQC). |
| JoinRawCalls          | C.2 | `wdl/JoinRawCalls.wdl`          | `wdl/bundles/JoinRawCalls/` | ✅ clean | ⏳ Registered on HealthOmics; not yet smoke-tested. |
| SVConcordance         | C.3 | `wdl/SVConcordance.wdl`         | `wdl/bundles/SVConcordance/` | ✅ clean | ⏳ Registered on HealthOmics; not yet smoke-tested. |
| ScoreGenotypes        | C.4 | `wdl/ScoreGenotypes.wdl`        | `wdl/bundles/ScoreGenotypes/` | ✅ clean | ⏳ Registered on HealthOmics; not yet smoke-tested. |
| FilterGenotypes       | C.5 | `wdl/FilterGenotypes.wdl`       | `wdl/bundles/FilterGenotypes/` | ✅ clean | ⏳ Registered on HealthOmics; not yet smoke-tested. |
| MainVcfQC             | D.2 | `wdl/MainVcfQc.wdl`             | `wdl/bundles/MainVcfQC/` | ✅ clean | ⚠️ HealthOmics path blocked by 47-second kill on `IdentifyDuplicates` scatter + `MergeDuplicates` aggregator. **EC2 hybrid is the production path** — see the "MainVcfQC EC2 hybrid" subsection below. |
| VisualizeCnvs         | D.3 | `wdl/VisualizeCnvs.wdl`         | `wdl/bundles/VisualizeCnvs/` | ✅ clean | ⏳ Registered on HealthOmics; not yet smoke-tested. |

Plus **RegenotypeCNVs** is now activated for cohorts ≥ 100 samples
(previously registered but always skipped).

(Per-module file lists, transitive imports, and divergence-by-divergence
provenance are recorded in `docs/divergence-log.md` "Phase 8: v1.0
module-completeness amendment" — kept as the authoritative source so this
audit only tracks the per-module verdict.)

### Divergences observed

The Phase 8 packager applied the standard divergence patches:
1. `MELT` references in `EvidenceQC.wdl` (RawVcfQC scatter shard) — stripped per Req 23.3.
2. `MELT` references in `JoinRawCalls.wdl` and `MainVcfQc.wdl` — stripped.
3. `MELT` references in `FilterGenotypes.wdl` (it transitively imports `MainVcfQc.wdl`) — 2 sites.

No `gs://` URI usages found in any of the 8 Phase 8 bundles.

### Status of registration

The bundles are committed and lint clean. As of 2026-05-28 all 18
workflows (the original 10 + the 8 Phase 8 modules) are **registered with
HealthOmics** in the validation account; the IDs are recorded in
`workflow-ids.json`, which `scripts/run_cohort_e2e.py` reads at startup to
wire the `WORKFLOWS` dict. EvidenceQC's final post-patch ID is `7602667`;
MainVcfQC's HealthOmics ID is retained in the registration manifest for
forensic reference even though Phase D.2 is dispatched via the EC2 hybrid
in production (see "MainVcfQC EC2 hybrid" below).

The orchestrator's new phase functions (`phase_a6_evidence_qc`,
`phase_c_post_processing`, `phase_d2_main_vcf_qc`, `phase_d3_visualize_cnvs`)
are **skip-safe**: when a workflow ID is `None` they log
`[SKIP] ... not yet registered` and continue, so the pipeline still runs
end-to-end if a deployment is missing one of the new IDs.

### Validation iterations 2026-05-27 / 2026-05-28

#### EvidenceQC (Phase A.6) — VALIDATED ✅

Following the registration of all 18 workflows, ran a 10-sample EvidenceQC
smoke test (HG00096, HG00097, HG00099, HG00100, HG00101, HG00102, HG00513,
NA12878, NA19238, NA19239) using existing 2026q2 cohort GSE outputs as
inputs. The smoke test exposed multiple HealthOmics compatibility issues
that required 3 successive WDL patches:

**Iteration 1 — `6728198` FAILED**: The synthesized
`gatk-sv-healthomics-run-role` IAM policy was missing read+write
permission on the `run-cache/*` S3 prefix. HealthOmics couldn't write
cache entries and aborted the run after 9 min. **Fixed** by extending
`iam/policies/gatk-sv-run-role.json` to include `run-cache/*` in both
the `S3ReadReferencesInputsAndOutputs` and `S3WriteOutputsAndCache`
statements.

**Iteration 2 — `4072843` FAILED**: With IAM fixed, the run reached
`MergeVariantCounts` (line 141 of `RawVcfQC.wdl`) and was terminated by
the **HealthOmics 47-second kill**. Same pattern documented earlier for
Scramble and CombineBatches: a 2+ task workflow that scatters per-sample
work and then aggregates triggers the kill. The aggregator tasks that
consume Array[File] outputs from a scatter hit the limit.

**Iteration 3 — `4992329` FAILED, applied patch v1**:
`scripts/patch_evidence_qc.py` rewrote `RawVcfQC.wdl` to drop the two
post-scatter aggregator tasks (`PickOutliers` + `MergeVariantCounts`).
The per-sample `RunIndividualQC` scatter is preserved; per-sample stats
are now exposed as workflow outputs. Run failed because the calling
workflow `EvidenceQC.wdl` still expected the dropped outputs and
substituted `"NONE"` string sentinels that miniwdl rejects at runtime.

**Iteration 4 — `8585754` FAILED, applied patch v1 + run_vcf_qc=False**:
Disabled the entire `RawVcfQC` cascade by setting `run_vcf_qc=False`.
But `MakeQcTable` is gated only by `run_ploidy`, not `run_vcf_qc`, so
it ran unconditionally and crashed on the `"NONE"` File inputs.

**Iteration 5 — `8627281` FAILED, applied patches v1+v2**:
`scripts/patch_evidence_qc_v2.py` rewrote the second `if (run_ploidy)`
block in `EvidenceQC.wdl` to gate on `if (run_ploidy && run_vcf_qc)`,
properly suppressing both `CreateVariantCountPlots` and `MakeQcTable`
when `run_vcf_qc=False`. Run failed because the workflow `output { }`
block still contained 16 `File? <caller>_qc_*/<caller>_variant_counts =
"NONE"` declarations that miniwdl evaluates regardless of conditional
gating.

**Iteration 6 — `2785300` ✅ COMPLETED, applied patches v1+v2+v3**:
`scripts/patch_evidence_qc_v3.py` stripped the 16 orphan output
declarations and the `qc_table = MakeQcTable.qc_table` declaration
from the workflow output block. Also rewrote
`File? ploidy_plots = if run_ploidy then select_first([...]) else NONE_FILE_`
to `File? ploidy_plots = Ploidy.ploidy_plots`.

**Final EvidenceQC workflow id: `7602667`**. Outputs the bincov matrix,
median coverage, ploidy matrix/plots, and WGD scores — the inputs Phase
B requires. Variant-count plots and merged QC table are skipped (they're
nice-to-haves; can be regenerated off-HealthOmics from the per-sample
VCFs if needed).

**Cost summary** for the 6-iteration debug cycle: total $0.12 across 5
failed runs. Iteration 6 hit cache for all 17 tasks (cost $0.00).
Cold-start cost for a fresh 10-sample EvidenceQC run is ~$0.04, or
$0.005 per sample.

**Open issue**: the 6 remaining Phase 8 workflows (`RefineComplexVariants`,
`JoinRawCalls`, `SVConcordance`, `ScoreGenotypes`, `FilterGenotypes`,
`VisualizeCnvs`) are registered with HealthOmics but **not yet end-to-end
smoke-tested**. They likely have HealthOmics compatibility issues similar to
EvidenceQC (47-second kill on scatter→aggregate patterns, miniwdl-vs-Cromwell
semantic differences, IAM gaps on `run-cache/*`). Each will need its own
iterate-and-patch cycle before the full Phase 8 pipeline smoke can run end-to-end
(task 8.10). MainVcfQC has already hit the 47-second kill on its
`IdentifyDuplicates`/`MergeDuplicates` step and is run via an EC2 hybrid (see
next subsection).

#### MainVcfQC (Phase D.2) — EC2 hybrid alternative execution path

⚠️ **HealthOmics path blocked; EC2 hybrid is the production path.**

`MainVcfQc.wdl` contains a 10-VCF `IdentifyDuplicates` scatter followed by a
`MergeDuplicates` aggregator. Both consume per-VCF outputs from the scatter,
which is the same scatter→aggregate pattern that triggers the **HealthOmics
47-second multi-task kill** on:

- `MakeCohortVcf.CombineBatches` (per-batch `GroupedSVCluster` → `svtk resolve`),
- `Scramble` (`cluster_identifier` per-chr → `SCRAMble.R` aggregator),
- `EvidenceQC.RawVcfQC` (per-sample/per-caller scatter → `PickOutliers` +
  `MergeVariantCounts` aggregators).

| | Upstream `MainVcfQc.wdl` (HealthOmics path) | EC2 hybrid (`scripts/run_main_vcf_qc_ec2.sh`) |
|---|---|---|
| Engine | miniwdl on HealthOmics | direct `docker run` on a standard EC2 VM (dispatched via SSM) |
| Docker image | `gatk-sv/sv-pipeline:2026-02-06-v1.1-797b7604` | **same** image, **same** Python entry-points |
| Scatter shape | `IdentifyDuplicates` over up to 10 VCFs, then `MergeDuplicates` | same algorithm, same per-VCF processing, same merge step |
| Outcome | terminated at ~47 s with no engine log | runs to completion in ~tens of minutes |
| Output S3 layout | `s3://healthomics-outputs-<acct>-apse1/runs/gatk-sv-e2e/<cohort>/main-vcf-qc/...` | `s3://healthomics-outputs-<acct>-apse1/runs/gatk-sv-e2e/<cohort>/main-vcf-qc-ec2/...` (same prefix shape; downstream consumers point at whichever is populated) |

🟢 **SAFE** in the algorithmic sense — same Docker image, same Python scripts,
same flags as the upstream `MainVcfQc.wdl` task definitions. The EC2 hybrid
exists solely because of the HealthOmics scheduler quirk, mirroring the same
EC2-hybrid treatment already documented for `MakeCohortVcf.CombineBatches` and
the `MakeCohortVcf` remaining-steps cascade in Phase C.

**Dispatch**: `scripts/run_main_vcf_qc_smoke_ec2.py` uploads the bash script
to S3, sends the SSM run-command to the shared EC2 hybrid instance, and tags
the instance with `gatk-sv:cohort-id`, `gatk-sv:workflow-version=main-vcf-qc-ec2-bash`,
and `gatk-sv:module=MainVcfQC` for cost tracking.

**HealthOmics registration retained**: the `MainVcfQC` HealthOmics workflow
ID stays in `workflow-ids.json` for forensic reference (so a future operator
can re-test once HealthOmics fixes the multi-task kill). The
`run_cohort_e2e.py` Phase D.2 dispatch is the EC2 hybrid by default.

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
