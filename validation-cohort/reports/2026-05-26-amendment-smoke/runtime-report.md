# Measured Runtime — gatk-sv-156-smoke-test (10 samples)

All wall-clock figures harvested from HealthOmics `GetRun` (`startTime`→`stopTime`) and
EC2 miniwdl run logs on 2026-06-03. This is the first end-to-end measured run.

## Per-phase wall-clock

| Phase | What | Wall-clock | Notes |
|---|---|---|---|
| **A** | GatherSampleEvidence (40 HealthOmics runs, all parallel) | **~3.0 h** | The 40 GSE runs (cc/cse/manta/wham × 10) launched together; wall-clock = slowest run (wham-HG00150 ~3.0 h). |
| **A.5** | Scramble (10 samples, serial on EC2) | ~5.0 h | Serial dispatch (one m5.2xlarge), ~30 min/sample. Parallel-capable if disk allows. |
| **A.6** | EvidenceQC (HealthOmics) | 22 min | |
| **B** | 6 cohort modules (HealthOmics, sequential) | **~3.2 h compute** | GBE 75m, ClusterBatch 36m, GenerateBatchMetrics 20m, FilterBatch 29m, MergeBatchSites 13m, GenotypeBatch 21m. RegenotypeCNVs skipped (<100). |
| **C** | MakeCohortVcf + RefineComplexVariants + GQ chain | **~3.9 h** | MakeCohortVcf(EC2) ~2.0 h; RefineComplexVariants(HealthOmics) 27 min; JoinRawCalls+SVConcordance+ScoreGenotypes+FilterGenotypes(EC2) ~1.4 h. |
| **D** | AnnotateVcf + MainVcfQC (EC2) | **~18 min** | AnnotateVcf 24-contig scatter ~12 min; MainVcfQC ~6 min. |

## End-to-end

- **Critical-path wall-clock (if phases run back-to-back):** ~15–16 hours, dominated by Phase A scramble (5 h serial) + GSE (3 h) overlap, then ~7 h of cohort + post-processing.
- **Actual elapsed across the smoke test:** spread over several days due to manual phase-by-phase dispatch and SSO re-auth gaps — not representative of an automated run.
- **Single longest task:** `RunWham` (max 10,392 s ≈ 2.9 h on one sample) and `RunManta` (max 5,851 s ≈ 1.6 h). Wham is the per-sample long pole.

## Slowest tasks (measured means across 10 samples)

| Task | Mean runtime | Max runtime | Caller |
|---|---|---|---|
| RunWham | 7,561 s (2.1 h) | 10,392 s | wham |
| RunCollectSVEvidence | 5,372 s (1.5 h) | 6,263 s | cse (PE/SR/RD/BAF) |
| RunManta | 4,460 s (1.2 h) | 5,851 s | manta |
| collect-counts "T" task | 4,257 s (1.2 h) | 4,974 s | cc |
| GermlineCNVCallerCaseMode (×15 shards) | ~1,500 s | 1,726 s | GBE gCNV |

## Takeaways
- **Phase A is the runtime long pole** — four per-sample callers running in parallel scatters. Everything else combined (~7 h) is less than half the per-sample evidence time when GSE and scramble overlap.
- **The post-processing chain (C/D) is fast** (~4 h) and cheap — the GQ chain + annotation + QC together are under $1 and under 1.5 h of compute.
- **Scramble's 5 h is serial-dispatch overhead**, not intrinsic — it can be parallelized on a larger instance or fanned out, cutting Phase A to ~3 h.
