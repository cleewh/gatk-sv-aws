# 10-sample Phase 8 Amendment Smoke Test — Phase A + B progress

**Cohort:** `gatk-sv-156-smoke-test`
**Samples (10):** HG00096, HG00129, HG00140, HG00150, HG00187, HG00239, HG00277, HG00288, HG00337, HG00349
**Account/region:** __ACCOUNT_ID__ / ap-southeast-1
**Run cache:** 9564200 (CACHE_ALWAYS)

## Phase A — per-sample evidence ✅ COMPLETE

| Sub-phase | What | Result |
|---|---|---|
| A.1–A.4 | GSE fan-out: cc, cse, manta, wham × 10 samples on HealthOmics (40 runs) | ✅ 40/40 COMPLETED; wham avg ~134min, manta ~79min, cc ~77min, cse ~95min |
| A.5 | Scramble on EC2 hybrid × 10 samples (SSM → run_scramble_ec2.sh) | ✅ 10/10 Success, ~30 min/sample (serial) |
| A.6 | EvidenceQC on HealthOmics (workflow 7602667, run 7349077) | ✅ COMPLETED, ~26 min, run_vcf_qc=False (validated config) |

### Phase A defects found & fixed
1. **Scramble parallel-dispatch saturation** — original `phase_a5_scramble_ec2` sent all 10 SSM commands at once; the single m5.2xlarge ran out of disk (3 GB ref + 15 GB CRAM × 10 concurrent). Patched to strictly sequential dispatch (send → poll-to-terminal → next) + per-sample `rm -rf $WORK` cleanup. Unit tests: `python/tests/unit/test_phase_a5_serial_dispatch.py`.
2. **`--cohort-base` wiring** — `run_gse_cohort_tagged.py` had a duplicate `sample_files()` masking the cohort_base override + no CLI flag; `run_cohort_e2e.py` `_swap_uris` only handled one cohort string. Patched + `python/tests/unit/test_launcher_blockers.py`.

## Phase B — cohort modules ✅ COMPLETE (3.2 h HealthOmics wall-clock)

| Module | Run ID | Wall-clock | Notes |
|---|---|---|---|
| GatherBatchEvidence (B.1) | 5349299 | 74 min | reused 200-sample gCNV/ploidy model tars |
| ClusterBatch (B.2) | 3362090 | 36 min | |
| GenerateBatchMetrics (B.3) | 1444376 | 19 min | |
| FilterBatch (B.4) | 9136972 | 29 min | depth/manta/wham only (no scramble, matches v3 divergence) |
| MergeBatchSites (B.5) | 7425654 | 12 min | single batch → single combined sites VCF |
| GenotypeBatch (B.6) | 1552478 | 20 min | produces genotyped pesr + depth VCFs |
| RegenotypeCNVs | — | SKIPPED | sample_count=10 < 100 (Req 19.6) |

### Phase B defects found & fixed
1. **Missing smoke-cohort PED** — GBE cn.MOPS `CNSampleNormal` failed because the staged `cohort.ped` only contained the original validation cohort. Created `validation-cohort/inputs/cohort-gatk-sv-156-smoke-test.ped` (5 male + 5 female, sex from 1000G panel) and staged to S3.
2. **Cromwell-vs-miniwdl `.tbi` antipattern** (recurring) — downstream modules construct `<file>+".tbi"` expecting the index beside the data file, but HealthOmics writes each File output to its own `_index/` subfolder. Co-located the indexes via S3-to-S3 copy for: ClusterBatch caller VCFs (4), GBE evidence files PE/SR/RD/BAF (4), MergeBatchSites cohort VCF (1).

### Phase B orchestration
- `.dispatch-phase-b-gbe.py` — GBE dispatcher (template-copy + per-sample array replacement, no `_swap_uris` so model-tar filenames stay intact).
- `.dispatch-phase-b-chain.py` — B.2–B.6 chain dispatcher with per-module input builders that copy template globals and wire prior-module outputs from each run's `outputs.json`.
- State: `phase-b-runs-gatk-sv-156-smoke-test.json`.

## Remaining — ✅ COMPLETE (see `phase-c-d-progress.md`)

| Phase | Module | Status |
|---|---|---|
| C.0 | MakeCohortVcf (EC2 hybrid) | ✅ 13,514-record cleaned cohort VCF (cohort-parameterized `run_combinebatches_ec2.sh` + `run_remaining_steps_ec2.py`). |
| C.1 | RefineComplexVariants | ✅ HealthOmics run 7401347, 32 min. |
| C.2–C.5 | GQ_Recalibrator chain (JoinRawCalls → SVConcordance → ScoreGenotypes → FilterGenotypes) | ✅ EC2/miniwdl hybrid. 46,848 → … → 12,041 PASS-eligible records. GQ model `genome_tracks` + small-cohort AF fixes applied. |
| D.1 | AnnotateVcf | ✅ EC2/miniwdl, 24-contig scatter, 12,041-record annotated VCF. |
| D.2 | MainVcfQC | ✅ EC2/miniwdl, 86 QC plots. `rm`-of-localized-input antipattern patched. |

## What this smoke run has validated
- The full Phase 8 amendment wiring through **all four phases A→B→C→D** works end-to-end on a real, never-before-run cohort.
- EvidenceQC, RefineComplexVariants, the entire GQ_Recalibrator chain, AnnotateVcf, and MainVcfQC (the six new Phase 8 modules) all run on real cohort inputs.
- All the launcher fixes (cohort-base, serial scramble, ped, .tbi co-location, GQ genome_tracks, small-cohort AF, MainVcfQC rm) hold.
- RegenotypeCNVs skip-gate fires correctly for <100-sample cohorts.

## Status: PHASE_A_B_COMPLETE → PHASE_C_D_COMPLETE — full pipeline validated. Gated on 8.11 user checkpoint before the 156-sample run.
