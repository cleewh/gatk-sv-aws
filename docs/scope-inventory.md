# Scope Inventory

Satisfies Requirement 17.5. Complete enumeration of what the Migration System does
and does not support.

## Supported input formats

- **CRAM + CRAI** (preferred; smaller on-disk footprint, cheaper S3 storage)
- **BAM + BAI** (accepted)

Both must be aligned to the GRCh38 primary assembly when using the default reference
build. Any other alignment reference is out of scope.

## Supported reference build

- **GRCh38** — default. Primary assembly plus decoys and alt contigs; matches the
  Broad's standard `Homo_sapiens_assembly38.fasta` layout.
- **GRCh37** — optional. Documented separately; not validated against the $7/sample
  target and not covered by the validation cohort. Operators using GRCh37 must provide
  their own matching reference bundle and expected outputs.

## Migrated modules (ten, end-to-end)

Listed in workflow submission order. Each module is registered as its own HealthOmics
workflow; the orchestrator chains them via the previous module's outputs.

1. `GatherSampleEvidence` — per-sample SV evidence extraction (Manta, Wham, Scramble,
   GATK-gCNV case-mode, PE/SR/RD/BAF)
2. `GatherBatchEvidence` — batch-level evidence assembly, gCNV cohort-mode
3. `ClusterBatch` — SV clustering within each batch
4. `GenerateBatchMetrics` — per-batch quality metrics
5. `FilterBatch` — allele frequency filtering
6. `MergeBatchSites` — merge sites across batches
7. `GenotypeBatch` — per-site per-sample likelihoods
8. `RegenotypeCNVs` — CNV re-genotyping after filtering
9. `MakeCohortVcf` — cohort-level VCF assembly
10. `AnnotateVcf` — VEP, gnomAD-SV, GENCODE annotation

The list is also available at runtime via:

```python
from kiro_life_sciences.gatk_sv_healthomics.models import MIGRATED_MODULES
```

## SV callers in scope

- **Manta** — discordant-read + split-read + local assembly. Detects DEL, DUP, INS, INV, BND.
- **Wham** (Whamg) — discordant-read + split-read. Specialized for large deletions and
  complex SVs.
- **Scramble** — local assembly. Detects insertions including some mobile-element
  insertions.
- **GATK-gCNV** — read-depth-based CNV calling. Used in cohort-mode for batch CNVs.

## Callers explicitly excluded

- **MELT** (Mobile Element Locator Tool) — see [divergence-log.md](divergence-log.md)
  § Policy divergence 1. Requires a per-user license; excluded from every migrated
  module.

## Expected outputs

### Cohort-level outputs (per cohort run)

- `cohort.vcf.gz` — final joint-genotyped SV VCF
- `cohort.vcf.gz.tbi` — tabix index
- `annotations.tsv` — per-site VEP + gnomAD-SV + GENCODE annotations (from `AnnotateVcf`)

### Per-batch outputs

- Clustered SV VCF per batch
- Filtered SV VCF per batch
- Batch metrics TSV
- Regenotyped CNV calls per batch

### Per-sample outputs

- **PE** — paired-end evidence (discordant read pair counts per site)
- **SR** — split-read evidence
- **RD** — read-depth evidence (coverage intervals)
- **BAF** — B-allele frequency evidence (for CNVs)
- Per-caller per-sample VCFs (Manta, Wham, Scramble)
- gCNV per-sample case-mode output

### Run-level outputs

- Parameter template JSON snapshot (per workflow version)
- `workflow-version.json` — workflow ID, version name, semver, upstream commit,
  divergence list
- Run manifest + run logs (captured by the orchestrator)

## Cohort scale

- **Batch size**: 100–500 samples per batch, matching upstream GATK-SV expectations.
  Below 100 samples, gCNV cohort-mode may produce poor calls. Above 500, runtime grows
  super-linearly in several modules.
- **No hard cohort-size limit** per the spec. Larger cohorts mean multiple batches run
  through modules 2–9 before merging at `MakeCohortVcf`.

## Networking

- **RESTRICTED** (default) — HealthOmics blocks task egress to non-AWS endpoints. All
  reference and container traffic stays intra-region.
- **VPC** (opt-in) — Run tasks in a customer VPC subnet. Requires a HealthOmics
  configuration with subnets + security groups pinned to `ap-southeast-1`.

## Storage modes

- **DYNAMIC** (default) — HealthOmics auto-provisions task storage. No pre-allocated
  capacity charge.
- **STATIC** (recommended for cohorts > 1 TiB total input) — Capacity snapped to 1200
  GiB chunks; computed as `max(1200, ceil(peak_working_set_gib × 1.20 / 1200) × 1200)`.
  The Cost Optimizer updates `peak_working_set_gib` from measured runs after each cohort.

## Out of scope

The Migration System explicitly does not cover:

- MELT (see above).
- GRCh37 as the default reference build.
- Somatic SV calling (GATK-SV is germline-only; no tumor-normal workflows).
- Single-sample mode outside a cohort. Operators needing single-sample genotyping
  against a frozen reference panel should submit a cohort-of-one plus the panel.
- Non-germline workflows (cancer, microbial, transcriptomic).
- Long-read SV calling.
- Automatic application of Cost Optimizer recommendations (Operator approval required
  per Req 9.4).

## Compliance

The spec does not pin the Migration System to any specific compliance framework
(HIPAA, PDPA, MTCS). Operators with compliance requirements must assess independently
whether the default IAM role, encryption, and logging posture satisfies their
requirements. The synthesized IAM role is least-privilege per Req 12; the IAM policy
JSONs are committed at `iam/policies/` for audit.
