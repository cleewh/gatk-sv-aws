# Runtime and Cost Expectations

Satisfies Requirement 17.4. Starting-point expectations for a 100-sample GRCh38 cohort
in `ap-southeast-1`. These numbers are **design-time estimates**; the Cost Optimizer
updates them from measured runs (Req 8.3, 9.2).

## Current status

**No cost or runtime data has been measured yet.** The values below come from the
per-module budget allocation in Design §Cost Model and upstream GATK-SV operator
reports on Terra / Cromwell (roughly equivalent hardware).

When the first real cohort runs, replace the "expected" numbers in this doc with
measured ones, and commit the `cost-report.json` alongside.

## Expected per-sample costs (100-sample cohort)

| Module | $/sample (budget) | Compute shape | Rationale |
|---|---|---|---|
| `GatherSampleEvidence` | **3.50** | per-sample scatter, 4 callers | Dominates. Runs all four callers plus PE/SR/RD/BAF extraction per sample. |
| `GatherBatchEvidence` | 1.00 | batch-level | gCNV cohort-mode; amortized per sample |
| `GenotypeBatch` | 0.90 | per-site × per-sample | Second-heaviest. Scales with sites × samples. |
| `ClusterBatch` | 0.30 | per-batch | SV clustering |
| `RegenotypeCNVs` | 0.30 | per-batch | Small site count after filtering |
| `MakeCohortVcf` | 0.30 | cohort-level | Single joint-VCF pass |
| `AnnotateVcf` | 0.20 | cohort-level | VEP + gnomAD-SV + GENCODE |
| `GenerateBatchMetrics` | 0.20 | per-batch | Metrics only |
| `FilterBatch` | 0.20 | per-batch | Frequency filtering |
| `MergeBatchSites` | 0.10 | cohort-level | Site merge |
| **Total** | **7.00** | | |

## Expected wall-clock runtime (100-sample cohort)

Reported as median wall-clock time from `StartAHORun` to `COMPLETED` per module.

| Module | Expected wall-clock | Dominant task |
|---|---|---|
| `GatherSampleEvidence` | 6–10 hours | Manta on the largest sample |
| `GatherBatchEvidence` | 2–4 hours | gCNV cohort-mode training |
| `ClusterBatch` | 30–60 minutes | SV clustering |
| `GenerateBatchMetrics` | 20–40 minutes | metric computation |
| `FilterBatch` | 20–40 minutes | frequency filtering |
| `MergeBatchSites` | 10–20 minutes | I/O-bound |
| `GenotypeBatch` | 2–4 hours | per-site per-sample likelihoods |
| `RegenotypeCNVs` | 30–60 minutes | CNV re-genotyping |
| `MakeCohortVcf` | 1–2 hours | cohort VCF assembly |
| `AnnotateVcf` | 30–60 minutes | VEP annotation |
| **End-to-end** | **14–22 hours** | |

The 100-sample end-to-end wall-clock is dominated by `GatherSampleEvidence` because
that's the only module running four callers per sample in parallel scatters; the other
nine modules together account for roughly 8 hours.

Actual wall-clock depends on HealthOmics instance availability in `ap-southeast-1` at
submission time. The run cache (Req 10) amortizes this on re-runs after partial failure.

## Reference bundle staging cost (one-time)

Approximately **$5–$10 one-time S3 staging cost** for the GRCh38 reference bundle:

- GRCh38 primary assembly FASTA + index + dict: ~3 GB
- gCNV training model: ~100 MB
- gnomAD-SV site records: ~50 MB
- GENCODE annotations: ~200 MB
- BED files (PAR, allosome/autosome, exclusion): <10 MB
- Contig ploidy priors, allele frequency resources: <100 MB

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
