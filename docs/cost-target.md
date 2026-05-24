# Per-Sample Cost Target — Measurement & Verification

**Per_Sample_Cost_Target = USD $7.00 per sample**, measured end-to-end across all
migrated GATK-SV modules for a production cohort run in `ap-southeast-1`.

Satisfies Requirement 17.3 and 8.7. Implemented in `cost/__init__.py` (partial — see
[phase-status.md](phase-status.md)).

## Tag taxonomy

Every AWS resource created as part of a cohort run **must** carry the full tag set
below. The `apply_cost_tags` function in `orchestrator/__init__.py` is the single
point of truth; Property 10 verifies every resource-creating call carries both the
cohort-id and workflow-version tags.

| Tag key | Example value | Purpose |
|---|---|---|
| `gatk-sv:cohort-id` | `cohort-sg-2025q1` | Groups all runs of a cohort |
| `gatk-sv:workflow-version` | `1.2.3` | Attributes cost to a specific workflow version (Req 16.4) |
| `gatk-sv:module` | `GenotypeBatch` | Attributes cost per module |
| `gatk-sv:sample-count` | `100` | Enables per-sample cost computation |
| `gatk-sv:environment` | `prod` / `validation` | Separates validation cohort runs from production |

The first three are **always present**; the last two are **conditionally present**
when applicable (they're in the Property 10 tag-set extensions).

Applied to:

- Every HealthOmics run via `StartAHORun(tags=...)` (see `submit_cohort`, Task 3.7.4)
- Every S3 output via `PutObject(Tagging=...)` at write time
- The HealthOmics Run Cache (once per account; re-tagged per cohort is not supported
  by the HealthOmics API, so only `gatk-sv:environment` is pinned at cache creation)
- The CloudWatch Logs log group (once; re-tagged if the `/aws/omics/*` group is
  created by the Migration System rather than inherited from an existing account)

## Verification procedure

### 1. Confirm the tags are present

After a cohort run completes, confirm the tags are attached to every resource
created by the run:

```bash
# Replace <COHORT-ID> and <REGION> with your cohort identifier and region.
COHORT=cohort-sg-2025q1
REGION=ap-southeast-1

# HealthOmics runs
aws omics list-runs --region "$REGION" \
    --query "items[?tags.\"gatk-sv:cohort-id\"=='$COHORT']" \
    --output json

# S3 output objects
aws s3api list-objects-v2 \
    --bucket "$OUTPUT_BUCKET" \
    --prefix "$OUTPUT_PREFIX/$COHORT/" \
    --region "$REGION" \
    --query "Contents[].Key" --output json
# Then for a representative sample:
aws s3api get-object-tagging --bucket "$OUTPUT_BUCKET" --key "<key>" --region "$REGION"
```

### 2. Pull the measured cost from Cost Explorer

```bash
START=2025-01-01
END=2025-02-01

aws ce get-cost-and-usage \
    --time-period "Start=$START,End=$END" \
    --granularity MONTHLY \
    --metrics UnblendedCost \
    --filter '{
        "Tags": {
            "Key": "gatk-sv:cohort-id",
            "Values": ["'$COHORT'"]
        }
    }' \
    --output json
```

Cost Explorer costs are updated approximately 24 hours after resource usage; the
verification is deterministic once the settlement period has passed.

### 3. Compute per-sample cost

```python
total_cost_usd = <from Cost Explorer response>
sample_count = <from the cohort manifest>
per_sample_cost_usd = total_cost_usd / sample_count

TARGET = 7.00
if per_sample_cost_usd > TARGET:
    print(f"OVER target: ${per_sample_cost_usd:.2f}/sample (target ${TARGET})")
else:
    print(f"within target: ${per_sample_cost_usd:.2f}/sample (target ${TARGET})")
```

The Python equivalent is packaged in `cost/__init__.py` as `analyze_cohort(runs, cohort_id)`
(deferred — Task 3.8.1). Once wired, it returns a `CostReport` with `per_sample_cost_usd`,
`over_target`, and per-(module, dimension) `attribution` rows.

### 4. Surface the overage if > $7/sample

When `per_sample_cost_usd > 7.00`, the Cost Optimizer's `surface_overage(cost_report)`
function (Task 3.8.4, deferred) produces an `OverageReport` keyed by
`(module, dimension)` so operators see which stage and which dimension caused the miss.

Dimensions:

- `compute` — EC2-backed task runtime
- `storage` — HealthOmics STATIC storage allocation plus S3 object storage
- `data-transfer` — any data transfer not covered by free-tier intra-region paths
- `container-pulls` — ECR pulls that bypassed the pull-through cache

## Target allocation across modules

The per-sample target decomposes across modules per Design §Cost Model:

| Module | $/sample | Rationale |
|---|---|---|
| `GatherSampleEvidence` | 3.50 | Per-sample scatter-heavy: runs all four callers per sample + evidence extraction |
| `GatherBatchEvidence` | 1.00 | Batch-scoped; amortized per sample |
| `GenotypeBatch` | 0.90 | Per-site per-sample likelihoods |
| `ClusterBatch` | 0.30 | Lightweight per-batch |
| `RegenotypeCNVs` | 0.30 | CNV-only after filtering |
| `MakeCohortVcf` | 0.30 | Single cohort-level pass |
| `AnnotateVcf` | 0.20 | VEP + gnomAD-SV + GENCODE |
| `GenerateBatchMetrics` | 0.20 | Metrics only |
| `FilterBatch` | 0.20 | Frequency filtering |
| `MergeBatchSites` | 0.10 | I/O-bound site merge |
| **Total** | **7.00** | |

These are starting budgets. The Cost Optimizer's `record_peak_working_set` updates the
per-module numbers from measured `AnalyzeAHORunPerformance` output after each run.

## Cost levers the Cost Optimizer pulls

1. **Storage mode** — DYNAMIC by default (no pre-allocated storage for ≤ 1 TiB cohorts).
2. **Instance right-sizing** — After ≥3 observations, `recommend()` returns CPU/memory
   with 20% headroom (Req 9.2). Recommendations reducing ≥25% are surfaced; operator
   approval gates application (Req 9.4).
3. **Run Cache reuse** — `CACHE_ALWAYS` eliminates re-cost of completed tasks on
   re-submission.
4. **Container pull economics** — Pull-through caches keep pulls intra-region; pinned
   tags / digests are cache-hit after first reference.
5. **Data locality** — Cross-region preflight (Property 6) refuses any submission with
   non-regional artifacts, eliminating cross-region S3 and NAT gateway charges.
6. **Networking mode** — RESTRICTED default avoids NAT gateway charges entirely.
7. **Output storage class** — Outputs land in Intelligent-Tiering when the output
   bucket has it set as default.

## Non-cost-target deductions

The $7 target covers **AWS spend** (compute + storage + transfer + container pulls).
It does **not** include:

- Staging cost of the Reference Bundle (one-time; about $5 to copy ~400 GB from
  the Broad into regional S3).
- Cost of keeping the Reference Bundle in S3 after the run (~$9/month at $0.023/GB
  standard tier; negligible if Intelligent-Tiering takes it to cold storage).
- AWS support-plan percentage fees.
- Human operator time.

If your accounting model needs to include these, add them as separate line items in
the `cost-report.json` with `dimension: "reference-staging"` or similar; the Cost
Optimizer's attribution schema is extensible.
