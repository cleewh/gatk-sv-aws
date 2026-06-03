# Runtime and Cost Expectations

Satisfies Requirement 17.4 and Requirement 19.10. Starting-point expectations for a
100-sample GRCh38 cohort in `ap-southeast-1`. These numbers are **design-time
estimates**; the Cost Optimizer updates them from measured runs (Req 8.3, 9.2).

## Current status

**No cost or runtime data has been measured at production scale yet.** The values
below come from the per-module budget allocation in Design §Cost Model and upstream
GATK-SV operator reports on Terra / Cromwell (roughly equivalent hardware).

The 10 original modules have been individually exercised at the validation-cohort
scale (10 samples) so their numbers are calibrated. The 8 v1.0-amendment modules
(EvidenceQC + the GQ_Recalibrator chain + RefineComplexVariants + MainVcfQC +
VisualizeCnvs) have linted bundles registered as HealthOmics workflows but have
not yet been smoke-tested end-to-end; their per-module budgets are marked **TBD
(estimate)** until task 8.10 produces the first measured numbers and `cost-report.json`
is committed under `validation-cohort/reports/2026-05-26-amendment-smoke/`.

When the first 100-sample production cohort runs, replace the "expected" numbers in
this doc with measured ones, and commit the `cost-report.json` alongside.

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
