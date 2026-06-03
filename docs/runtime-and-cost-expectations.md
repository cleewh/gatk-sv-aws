# Runtime and Cost Expectations

Satisfies Requirement 17.4 and Requirement 19.10. Starting-point expectations for a
100-sample GRCh38 cohort in `ap-southeast-1`. These numbers are **design-time
estimates**; the Cost Optimizer updates them from measured runs (Req 8.3, 9.2).

## Current status

**First measured run complete (2026-06-03).** The `gatk-sv-156-smoke-test` 10-sample
end-to-end run produced measured cost and runtime, harvested via
`AnalyzeAHORunPerformance` (HealthOmics, instance-second based) plus EC2 on-demand
pricing for the hybrid steps. The full report is at
`validation-cohort/reports/2026-05-26-amendment-smoke/` (`cost-report.json`,
`runtime-report.md`).

**Measured headline (10-sample cohort):**

- **Total: $36.16 → $3.62 per sample** (well under the $7 target).
- HealthOmics subtotal $31.99; EC2-hybrid subtotal $4.18 (m5.2xlarge @ $0.48/hr × ~8.7 active hours).
- **GatherSampleEvidence dominates at $2.85/sample** (Manta $1.13, Wham $0.59, CollectCounts $0.57, CollectSVEvidence $0.53 per sample on average).
- Cohort-level work (Phase B/C/D) adds only ~$0.77/sample at n=10 and less at scale.
- Per-sample cost projects to **~$3.40 at 100–156 samples** (cohort modules amortize); the full 156-sample cohort projects to **~$530 total**.

**Measured runtime:** Phase A (per-sample evidence) is the long pole at ~3 h GSE +
~5 h serial scramble; Phases B+C+D together are ~7 h of compute. Single longest task
is `RunWham` (up to 2.9 h on one sample). See `runtime-report.md`.

The estimates below were the pre-run design budgets; the measured numbers came in
**below budget** (the $7/sample target had assumed less efficient GSE). The original
estimate table is retained for reference.

> **Note:** the measured $3.62/sample reflects on-demand pricing with no reserved
> capacity or discounts. The EC2-hybrid steps ran on a shared, already-running
> instance, so their true marginal cost is even lower than the $0.48/hr allocation.

## Migrated_Modules and Module_Phase boundaries

The Migration System ports **19 modules** (Req 19.10) grouped into **four
`Module_Phase` boundaries** (per the requirements glossary). Listed in execution
order:

| Phase | Trigger | Modules |
|---|---|---|
| **Phase A** — per-sample evidence | runs once per cohort sample | `GatherSampleEvidence` (A.1–A.4: cc, cse, manta, wham on HealthOmics; A.5: scramble on EC2 hybrid), `EvidenceQC` (A.6) |
| **Phase B** — cohort modules | runs once per cohort | `TrainGCNV` (B.1), `GatherBatchEvidence` (B.2), `ClusterBatch` (B.3), `GenerateBatchMetrics` (B.4), `FilterBatch` (B.5), `MergeBatchSites` (B.6), `GenotypeBatch` (B.7), `RegenotypeCNVs` (B.8, activated for cohorts ≥ 100 samples) |
| **Phase C** — post-processing | runs once per cohort after Phase B | `MakeCohortVcf` (C.0, EC2 hybrid covering CombineBatches + ResolveComplexVariants + GenotypeComplexVariants + CleanVcf), `RefineComplexVariants` (C.1), then the **GQ_Recalibrator chain**: `JoinRawCalls` (C.2) → `SVConcordance` (C.3) → `ScoreGenotypes` (C.4) → `FilterGenotypes` (C.5) |
| **Phase D** — delivery | runs once per cohort after Phase C | `AnnotateVcf` (D.1), `MainVcfQC` (D.2), `VisualizeCnvs` (D.3, optional; gated by `--include-visualize-cnvs`) |

The **GQ_Recalibrator chain** (per the requirements glossary) is the four-workflow
sequence `JoinRawCalls → SVConcordance → ScoreGenotypes → FilterGenotypes` that
produces a quality-score-recalibrated cohort VCF as the input to `AnnotateVcf`.
Each step is registered as its own HealthOmics workflow so the run cache and
retry logic operate on a per-step basis (Req 19.3).

Cross-references: [scope-inventory.md](scope-inventory.md) lists the same 19
modules in submission-order with caller and output details; the main
[`README.md`](../README.md) carries the per-module workflow IDs and bundle paths.

## Expected per-sample costs (100-sample cohort)

| # | Phase | Module | $/sample (budget) | Compute shape | Rationale |
|---|---|---|---:|---|---|
| 1 | A.1–A.5 | `GatherSampleEvidence` | **3.50** | per-sample scatter, 4 callers | Dominates. Runs all four callers plus PE/SR/RD/BAF extraction per sample. Scramble lives on the A.5 EC2 hybrid path. |
| 2 | A.6 | `EvidenceQC` | TBD (~0.05) | per-sample QC | Lightweight QC; small fraction of A. |
| 3 | B.1 | `TrainGCNV` | included in B.2 | batch-level | Ploidy + gCNV training; cost folded into `GatherBatchEvidence` budget historically. |
| 4 | B.2 | `GatherBatchEvidence` | 1.00 | batch-level | gCNV cohort-mode + PE/SR/RD merging; amortized per sample. |
| 5 | B.3 | `ClusterBatch` | 0.30 | per-batch | SV clustering. |
| 6 | B.4 | `GenerateBatchMetrics` | 0.20 | per-batch | Metrics only. |
| 7 | B.5 | `FilterBatch` | 0.20 | per-batch | Frequency filtering. |
| 8 | B.6 | `MergeBatchSites` | 0.10 | cohort-level | Site merge. |
| 9 | B.7 | `GenotypeBatch` | 0.90 | per-site × per-sample | Second-heaviest. Scales with sites × samples. |
| 10 | B.8 | `RegenotypeCNVs` | 0.30 | per-batch | Active for cohorts ≥ 100 samples; skipped for smaller cohorts. |
| 11 | C.0 | `MakeCohortVcf` *(EC2 hybrid)* | 0.30 | cohort-level | Single joint-VCF assembly via direct `docker run` + `miniwdl run` on EC2 (47-second-kill workaround). |
| 12 | C.1 | `RefineComplexVariants` | TBD (~0.05) | cohort-level | Refines complex SV calls. |
| 13 | C.2 | `JoinRawCalls` *(GQ chain 1/4)* | TBD (~0.05) | cohort-level | Joins per-sample raw calls. |
| 14 | C.3 | `SVConcordance` *(GQ chain 2/4)* | TBD (~0.05) | cohort-level | Annotates concordance vs raw calls. |
| 15 | C.4 | `ScoreGenotypes` *(GQ chain 3/4)* | TBD (~0.10) | cohort-level | Recalibrator scoring; runs the trained GQ model. |
| 16 | C.5 | `FilterGenotypes` *(GQ chain 4/4)* | TBD (~0.05) | cohort-level | Drops low-confidence calls below GQ threshold. |
| 17 | D.1 | `AnnotateVcf` | 0.20 | cohort-level | VEP + gnomAD-SV + GENCODE. |
| 18 | D.2 | `MainVcfQC` | TBD (~0.05) | cohort-level | Cohort QC plots. Always runs; non-fatal if it errors. EC2-hybrid alternative path documented in `wdl-audit.md`. |
| 19 | D.3 | `VisualizeCnvs` *(optional)* | TBD (~0.05 if run) | per-CNV scatter | Per-CNV PNG plots; opt-in via `--include-visualize-cnvs`. |
| | | **Total budget (validated 10)** | **7.00** | | Original Req 8.1 target; the v1.0 amendment additions are budgeted as overhead within the same envelope. |
| | | **Total expected (post-smoke)** | **TBD ≤ 7.50** | | Pending cost numbers from task 8.10 smoke test. |

## Expected wall-clock runtime (100-sample cohort)

Reported as median wall-clock time from `StartAHORun` to `COMPLETED` per module.

| # | Phase | Module | Expected wall-clock | Dominant task |
|---|---|---|---|---|
| 1 | A.1–A.5 | `GatherSampleEvidence` | 6–10 hours | Manta on the largest sample |
| 2 | A.6 | `EvidenceQC` | 10–20 minutes | Per-sample metric collection |
| 3 | B.1 | `TrainGCNV` | folded into B.2 | gCNV training kernel |
| 4 | B.2 | `GatherBatchEvidence` | 2–4 hours | gCNV cohort-mode genotyping |
| 5 | B.3 | `ClusterBatch` | 30–60 minutes | SV clustering |
| 6 | B.4 | `GenerateBatchMetrics` | 20–40 minutes | metric computation |
| 7 | B.5 | `FilterBatch` | 20–40 minutes | frequency filtering |
| 8 | B.6 | `MergeBatchSites` | 10–20 minutes | I/O-bound |
| 9 | B.7 | `GenotypeBatch` | 2–4 hours | per-site per-sample likelihoods |
| 10 | B.8 | `RegenotypeCNVs` | 30–60 minutes | CNV re-genotyping (≥100-sample cohorts only) |
| 11 | C.0 | `MakeCohortVcf` *(EC2 hybrid)* | 1–2 hours | cohort VCF assembly |
| 12 | C.1 | `RefineComplexVariants` | TBD (~30 min) | complex SV refinement |
| 13 | C.2 | `JoinRawCalls` *(GQ chain 1/4)* | TBD (~20 min) | join per-sample raw calls |
| 14 | C.3 | `SVConcordance` *(GQ chain 2/4)* | TBD (~30 min) | concordance annotation |
| 15 | C.4 | `ScoreGenotypes` *(GQ chain 3/4)* | TBD (~45 min) | GQ recalibrator scoring |
| 16 | C.5 | `FilterGenotypes` *(GQ chain 4/4)* | TBD (~20 min) | GQ-threshold filtering |
| 17 | D.1 | `AnnotateVcf` | 30–60 minutes | VEP annotation |
| 18 | D.2 | `MainVcfQC` | TBD (~30 min) | cohort QC plot generation |
| 19 | D.3 | `VisualizeCnvs` *(optional)* | TBD (~30 min if run) | per-CNV plotting |
| | | **End-to-end (validated 10)** | **14–22 hours** | dominated by `GatherSampleEvidence` |
| | | **End-to-end (full 19)** | **TBD ≤ 26 hours** | pending task 8.10 smoke test measurement |

The 100-sample end-to-end wall-clock is dominated by `GatherSampleEvidence` because
that's the only module running four callers per sample in parallel scatters. The
other modules together (B.2 + B.7 in particular) account for roughly 8 hours; the
v1.0-amendment modules add an estimated 3–4 hours on top, mostly in the GQ chain
(C.2–C.5).

Actual wall-clock depends on HealthOmics instance availability in `ap-southeast-1`
at submission time. The run cache (Req 10) amortizes this on re-runs after partial
failure.

## Reference bundle staging cost (one-time)

Approximately **$5–$10 one-time S3 staging cost** for the GRCh38 reference bundle:

- GRCh38 primary assembly FASTA + index + dict: ~3 GB
- gCNV training model: ~100 MB
- gnomAD-SV site records: ~50 MB
- GENCODE annotations: ~200 MB
- BED files (PAR, allosome/autosome, exclusion): <10 MB
- Contig ploidy priors, allele frequency resources: <100 MB
- GQ_Recalibrator model artifacts (added 2026-05-26): ~50 MB

**Total on-disk**: ~5 GB (not ~400 GB as earlier estimated; the "400 GB" figure was a
bad number. The Broad's reference bundle is compact).

Standard S3 storage in `ap-southeast-1`: $0.025/GB/month → $0.125/month steady-state.
Intelligent-Tiering moves unused files to Infrequent Access after 30 days:
$0.0135/GB/month.

Data transfer in to S3 is free; data transfer out of S3 in-region to HealthOmics is
free.

## Cost Optimizer targets (post-measurement)

After the first three 100-sample cohort runs at production scale, the Cost Optimizer's
`recommend()` output should:

- Tighten CPU/memory per task to `observed_peak × 1.20`
- Surface any reduction ≥25% for operator approval
- Update the per-module budgets in the table above with measured numbers
- Replace **TBD** rows with measured `$/sample` and wall-clock figures

Once those updates land, this doc should be re-generated from measured data:

```python
from kiro_life_sciences.gatk_sv_healthomics.cost import analyze_cohort
report = analyze_cohort(runs=[...], cohort_id="cohort-sg-2025q1")
# Use report.runs and report.attribution to rewrite the table above.
```

## Breakeven scale for per-sample cost

The $7/sample target assumes reasonable batch scale. Smaller batches carry the same
per-batch overhead across fewer samples, so:

| Samples per cohort | Approx $/sample |
|---|---|
| 10 (validation cohort) | ~$12–15 (batch-level modules cost ~$70 amortized over 10) |
| 50 | ~$8.50 |
| 100 | ~$7 (target) |
| 200 | ~$6.50 |
| 500 | ~$6 |

If your cohorts are consistently under 50 samples, the $7 target is unlikely to hold;
consider batching multiple small projects into a single cohort run, or loosen the
per-sample target for validation-style runs (which are already tagged
`gatk-sv:environment = validation`).

## See also

- [`scope-inventory.md`](scope-inventory.md) — full enumeration of the 19 migrated modules with caller details, expected outputs, and out-of-scope items.
- [`runtime-sizing.md`](runtime-sizing.md) — CPU / memory / instance-type recommendations per task.
- [`cost-target.md`](cost-target.md) — Per_Sample_Cost_Target methodology and Cost Explorer tag taxonomy.
- [`wdl-audit.md`](wdl-audit.md) — WDL divergences for each module, including the v1.0-amendment ports.
