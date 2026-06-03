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

## Migrated modules (nineteen, end-to-end)

Listed in workflow submission order. Each module is registered as its own HealthOmics
workflow; the orchestrator chains them via the previous module's outputs.

The migrated set was extended from 10 to 19 modules in the v1.0
completeness amendment (Req 19, 2026-05-26). The amendment added
EvidenceQC, the GQ_Recalibrator chain (JoinRawCalls → SVConcordance →
ScoreGenotypes → FilterGenotypes), RefineComplexVariants, MainVcfQC, and
VisualizeCnvs — and activated RegenotypeCNVs for cohorts ≥ 100 samples.

### Module_Phase boundaries (four)

Per the requirements glossary (Req 19.10), the 19 modules are grouped into four
`Module_Phase` boundaries that match the orchestrator's `--skip-*` CLI flag set:

| Phase | Trigger | Modules in this phase |
|---|---|---|
| **Phase A — per-sample evidence** | once per cohort sample | `GatherSampleEvidence` (A.1–A.5: cc + cse + manta + wham on HealthOmics, scramble on EC2 hybrid), `EvidenceQC` (A.6) |
| **Phase B — cohort modules** | once per cohort | `TrainGCNV`, `GatherBatchEvidence`, `ClusterBatch`, `GenerateBatchMetrics`, `FilterBatch`, `MergeBatchSites`, `GenotypeBatch`, `RegenotypeCNVs` (cohorts ≥ 100 samples) |
| **Phase C — post-processing** | once per cohort, after Phase B | `MakeCohortVcf` (C.0, EC2 hybrid; covers CombineBatches + ResolveComplexVariants + GenotypeComplexVariants + CleanVcf), `RefineComplexVariants` (C.1), then the **GQ_Recalibrator chain**: `JoinRawCalls` (C.2) → `SVConcordance` (C.3) → `ScoreGenotypes` (C.4) → `FilterGenotypes` (C.5) |
| **Phase D — delivery** | once per cohort, after Phase C | `AnnotateVcf` (D.1), `MainVcfQC` (D.2), `VisualizeCnvs` (D.3, optional via `--include-visualize-cnvs`) |

### GQ_Recalibrator chain (C.2–C.5)

The four-workflow sequence `JoinRawCalls → SVConcordance → ScoreGenotypes →
FilterGenotypes` produces a quality-score-recalibrated cohort VCF as the input to
`AnnotateVcf` (Req 19.3). It runs after `RefineComplexVariants` (C.1) and before
`AnnotateVcf` (D.1). Each step is registered as its own HealthOmics workflow so
the run cache and retry logic operate on a per-step basis. The validation harness
compares the FilterGenotypes output (the chain's terminal artifact) against
`expected_post_gq_vcf` from the cohort manifest — see
[validation-runbook.md](validation-runbook.md).

### Phase A — per-sample evidence (per cohort sample)

1. `GatherSampleEvidence` — per-sample SV evidence extraction (Manta, Wham,
   Scramble, GATK-gCNV case-mode, PE/SR/RD/BAF).
   - Production splits this into per-tool runs: `cc`, `cse`, `manta`, `wham`
     on HealthOmics; `scramble` on EC2 hybrid (Phase A.5).
2. `EvidenceQC` *(Phase A.6)* — per-sample QC after Phase A; produces metrics
   that gate entry to the more expensive Phase B modules.

### Phase B — cohort modules (per cohort)

3. `TrainGCNV` — train the gCNV cohort-mode model.
4. `GatherBatchEvidence` — batch-level evidence assembly, gCNV cohort-mode
   genotyping.
5. `ClusterBatch` — SV clustering within each batch.
6. `GenerateBatchMetrics` — per-batch quality metrics.
7. `FilterBatch` — allele frequency filtering.
8. `MergeBatchSites` — merge sites across batches.
9. `GenotypeBatch` — per-site per-sample likelihoods.
10. `RegenotypeCNVs` *(activated for cohorts ≥ 100 samples)* — CNV re-genotyping.

### Phase C — post-processing (per cohort)

11. `MakeCohortVcf` *(Phase C.0, EC2 hybrid)* — cohort-level VCF assembly.
    Runs CombineBatches + ResolveComplexVariants + GenotypeComplexVariants +
    CleanVcf as direct `docker run` on EC2 because the HealthOmics 47-second
    multi-task kill makes the upstream sub-workflow chain unrunnable as
    a HealthOmics workflow.
12. `RefineComplexVariants` *(Phase C.1)* — refines complex SV calls.

The next four modules form the **GQ_Recalibrator chain** (Req 19.3) — they run
sequentially as the final post-processing pass before delivery and produce the
GQ-recalibrated cohort VCF that feeds `AnnotateVcf`:

13. `JoinRawCalls` *(Phase C.2, GQ_Recalibrator step 1/4)* — joins per-sample
    raw calls; first step of the chain.
14. `SVConcordance` *(Phase C.3, GQ_Recalibrator step 2/4)* — annotates
    concordance against the joined raw calls.
15. `ScoreGenotypes` *(Phase C.4, GQ_Recalibrator step 3/4)* — applies the
    trained GQ recalibrator model to score every genotype.
16. `FilterGenotypes` *(Phase C.5, GQ_Recalibrator step 4/4)* — drops
    low-confidence calls below the GQ threshold; output is the GQ-recalibrated
    cohort VCF.

### Phase D — delivery (per cohort)

17. `AnnotateVcf` — VEP-style functional consequences + gnomAD-SV allele
    frequencies + GENCODE.
18. `MainVcfQC` *(Phase D.2)* — cohort-level QC plots (always runs; non-fatal
    if it errors).
19. `VisualizeCnvs` *(Phase D.3, optional)* — per-CNV PNG plots; gated by
    `--include-visualize-cnvs`.

The complete list is also available at runtime via:

```python
from kiro_life_sciences.gatk_sv_healthomics.models import MIGRATED_MODULES
```

The four `Module_Phase` boundaries (A, B, C, D) are documented in the
requirements glossary (Req 19.10), enumerated in the table at the top of this
section, and enforced by the orchestrator's `--skip-*` CLI flags. Per-module
runtime/cost expectations live in
[runtime-and-cost-expectations.md](runtime-and-cost-expectations.md); per-module
workflow IDs and bundle paths live in the main [`README.md`](../README.md).

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
