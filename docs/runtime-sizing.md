# Runtime Sizing Guide

## How HealthOmics Resource Allocation Works

HealthOmics allocates compute based on the `cpu` and `memory` values in the WDL `runtime` block.
You **cannot** override these at run time — they're baked into the workflow definition at registration.

To change resources, you must:
1. Update the WDL runtime block
2. Re-package the bundle
3. Register a new workflow version

## Current Registered Workflows (Single-Task, Flattened)

These are the workflows currently deployed for the GatherSampleEvidence module:

| Task | Workflow ID | Current CPU | Current Memory |
|------|------------|-------------|----------------|
| Reindex | `8437840` | 2 | 7.5 GB |
| CollectCounts | `3901751` | 4 | 7.5 GB |
| CollectSVEvidence | `7038412` | 4 | 7.5 GB |
| Manta | `6943475` | 4 | 16 GB |
| Wham | `4183891` | 2 | 8 GB |
| Scramble | `1324647` | 2 | 8 GB |

## Recommended Sizing for Fastest Execution

### Single-Sample Mode (GatherSampleEvidence)

| Task | CPU | Memory | HealthOmics Instance | Rationale |
|------|-----|--------|---------------------|-----------|
| Reindex | 4 | 8 GB | omics.c.xlarge | samtools index is single-threaded but needs I/O bandwidth |
| CollectCounts | 8 | 16 GB | omics.c.2xlarge | GATK CollectReadCounts benefits from parallel GC |
| CollectSVEvidence | 8 | 16 GB | omics.c.2xlarge | GATK CollectSVEvidence is CPU-bound |
| Manta | 8 | 16 GB | omics.c.2xlarge | Manta uses all available threads |
| Wham | 4 | 8 GB | omics.c.xlarge | Wham is moderately CPU-bound |
| Scramble | 4 | 8 GB | omics.c.xlarge | Scramble is I/O-bound, more CPU doesn't help much |

### Cohort Mode (Modules 2–10, 10-sample cohort)

| Module | CPU | Memory | HealthOmics Instance | Rationale |
|--------|-----|--------|---------------------|-----------|
| GatherBatchEvidence | 8 | 32 GB | omics.m.2xlarge | Merges per-sample evidence files |
| ClusterBatch | 4 | 16 GB | omics.m.xlarge | SV clustering is memory-bound |
| GenerateBatchMetrics | 4 | 16 GB | omics.m.xlarge | Metric computation |
| FilterBatch | 8 | 32 GB | omics.m.2xlarge | Random forest + filtering |
| MergeBatchSites | 4 | 8 GB | omics.c.xlarge | Simple merge, not resource-heavy |
| GenotypeBatch | 16 | 64 GB | omics.m.4xlarge | Re-genotypes all samples simultaneously |
| RegenotypeCNVs | 8 | 32 GB | omics.m.2xlarge | CNV-specific re-genotyping |
| MakeCohortVcf | 16 | 64 GB | omics.m.4xlarge | Assembles final VCF (largest task) |
| AnnotateVcf | 8 | 32 GB | omics.m.2xlarge | Annotation is I/O-heavy |

### Cohort Mode (100+ samples)

| Module | CPU | Memory | HealthOmics Instance |
|--------|-----|--------|---------------------|
| GatherBatchEvidence | 16 | 64 GB | omics.m.4xlarge |
| ClusterBatch | 8 | 32 GB | omics.m.2xlarge |
| GenerateBatchMetrics | 8 | 32 GB | omics.m.2xlarge |
| FilterBatch | 16 | 64 GB | omics.m.4xlarge |
| MergeBatchSites | 8 | 16 GB | omics.c.2xlarge |
| GenotypeBatch | 48 | 192 GB | omics.m.12xlarge |
| RegenotypeCNVs | 16 | 64 GB | omics.m.4xlarge |
| MakeCohortVcf | 48 | 192 GB | omics.m.12xlarge |
| AnnotateVcf | 16 | 64 GB | omics.m.4xlarge |

## Cost vs Speed Tradeoff

HealthOmics bills per vCPU-hour and per GB-hour of memory:
- Over-provisioning CPU: Usually cost-neutral (task finishes faster → fewer hours billed)
- Over-provisioning memory: Costs more (memory-hours billed even if unused)

**Recommendation**: Start with the 10-sample column. After the first run, use
`AnalyzeAHORunPerformance` to see actual CPU/memory utilization, then right-size.

## How to Apply

To update a workflow's resources:

```bash
# 1. Modify the WDL runtime block in the packaged bundle
# 2. Re-register with a new version
gatk-sv-healthomics run --module GatherSampleEvidence --version v1.1.0-optimized
```

Or use the Python packager:
```python
from kiro_life_sciences.gatk_sv_healthomics.packager import package_module
# The packager's HealthOmics rewrites already bump mem_gb: 3.75 → 7.5
# To go higher, modify the _HEALTHOMICS_REWRITES in packager/__init__.py
```
