# Expected Cohort_VCF for the validation harness

This directory holds the **expected** Cohort_VCF used by the GATK-SV
HealthOmics validation harness (see Req 13.1, 13.2 and Design §Components.j).
The validation harness compares the *produced* Cohort_VCF (output of an
end-to-end run of the 10-module pipeline against the validation manifest at
[`../inputs/manifest.json`](../inputs/manifest.json)) against the *expected*
Cohort_VCF stored here, on per-SV-type site concordance:

- `≥ 99%` for DEL and DUP (Req 13.3)
- `≥ 95%` for INS and INV (Req 13.3)

When concordance falls below those thresholds, the run is failed and the
discordant sites are listed (Req 13.3).

## Files

| Path | Tracked by git | Purpose |
| ---- | -------------- | ------- |
| `expected.vcf.gz` | **No** (gitignored, see [`../.gitignore`](../.gitignore)) | The reference Cohort_VCF produced by Broad's upstream WDL at the **post-AnnotateVcf** stage. Bit-identical-or-bust ground truth for `compare_cohort_vcf` and the ±50 bp fuzz baseline for `compare_cohort_vcf_fuzzy`. (Req 13.1, 13.2) |
| `expected.vcf.gz.tbi` | **No** (gitignored) | Tabix index for the above. |
| `expected_post_gq_vcf` *(referenced by manifests, not stored locally)* | **No** (S3-only) | The reference Cohort_VCF at the **post-FilterGenotypes** stage of the GQ_Recalibrator chain (JoinRawCalls → SVConcordance → ScoreGenotypes → FilterGenotypes). Used by the validation harness to gate concordance against the GQ-recalibrated VCF per Req 19.8 with the same per-SV-type thresholds declared in Req 13.3. The S3 URI lives under `expected_post_gq_vcf` in [`../inputs/manifest.json`](../inputs/manifest.json) and [`../inputs/manifest-gatk-sv-156.json`](../inputs/manifest-gatk-sv-156.json); its sibling `.tbi` URI lives under `expected_post_gq_vcf_index`. The full record (status, checksums, regeneration triggers) is in `expected-metadata.json` → `post_gq_artifact`. |
| `expected-metadata.json` | **Yes** | Records exactly which upstream commit, caller container versions, reference bundle, cohort membership, and HealthOmics workflow IDs the expected VCFs were produced against. Holds metadata for both `expected.vcf.gz` (post-AnnotateVcf) and the post-GQ artifact (`post_gq_artifact` block). **This is the file that gets committed and reviewed.** |
| `README.md` | **Yes** | This file. |
| `.gitkeep` | **Yes** | Keeps the directory present for fresh clones. |

The binary VCFs are intentionally not committed — they are large, opaque,
and produced off-prem (on Terra / Cromwell-on-GCP) per
[`../../docs/validation-runbook.md`](../../docs/validation-runbook.md) Step 1.
The metadata file is the auditable record of what `expected.vcf.gz`
*should* be, and what the post-GQ expected artifact *should* be.

### Post-GQ vs post-AnnotateVcf — they cover different stages

`expected.vcf.gz` and `expected_post_gq_vcf` are **distinct artifacts**
covering **different stages** of the same pipeline:

- `expected_post_gq_vcf` is the cohort VCF immediately after
  **FilterGenotypes** (the last step of the GQ_Recalibrator chain) and
  **before** AnnotateVcf. It is the gate enforced by Req 19.8.
- `expected.vcf.gz` is the cohort VCF after **AnnotateVcf** (and the
  rest of the post-processing tail). It is the gate enforced by
  Req 13.1, 13.2.

Both live alongside each other in
`s3://omics-ref-ap-southeast-1-<ACCOUNT_ID>/gatk-sv/validation/broad-reference/`,
keyed off the cohort id (e.g. `gatk-sv-validation-2026q2.post-gq.cleaned.vcf.gz`
vs `gatk-sv-validation-2026q2.cleaned.vcf.gz`). They are produced by the
same upstream Terra run, just snapshotted at two different points in the
WDL graph.

## How `expected.vcf.gz` is produced

Per Req 13.1, the expected Cohort_VCF must be **documented**. We produce
it by running Broad's upstream GATK-SV WDL (the same commit our HealthOmics
port is migrated from) on Terra against the same 10-sample cohort. The
full procedure lives in
[`../../docs/validation-runbook.md`](../../docs/validation-runbook.md)
Step 1, in summary:

1. On a Terra workspace, import `broadinstitute/gatk-sv` at the commit
   recorded in `expected-metadata.json` →
   `upstream_pipeline.broad_reference_vcf_commit`.
2. Run the per-module sequence GSE → GBE → ClusterBatch → ... →
   MakeCohortVcf → AnnotateVcf on the 10 samples listed in
   [`../inputs/manifest.json`](../inputs/manifest.json).
3. Copy the final `<cohort>.cleaned.vcf.gz{,.tbi}` to
   `s3://omics-ref-ap-southeast-1-<ACCOUNT_ID>/gatk-sv/validation/broad-reference/`.
4. Pull both files into this directory:
   ```bash
   aws s3 cp \
     s3://omics-ref-ap-southeast-1-<ACCOUNT_ID>/gatk-sv/validation/broad-reference/gatk-sv-validation-2026q2.cleaned.vcf.gz \
     validation-cohort/expected/expected.vcf.gz
   aws s3 cp \
     s3://omics-ref-ap-southeast-1-<ACCOUNT_ID>/gatk-sv/validation/broad-reference/gatk-sv-validation-2026q2.cleaned.vcf.gz.tbi \
     validation-cohort/expected/expected.vcf.gz.tbi
   ```
5. Recompute SHA-256 and update `expected-metadata.json` per the steps
   under `regeneration_procedure` in that file.

## When `expected.vcf.gz` MUST be regenerated (Req 13.2)

The expected output is **stale** — and MUST be regenerated by the
procedure above — whenever any of the following change:

- A caller container version in `expected-metadata.json` →
  `callers.container_versions` (see also
  [`../../container-registry-map/container-registry-map.json`](../../container-registry-map/container-registry-map.json)).
- A reference file in
  [`../../reference-bundle/manifests/GRCh38.json`](../../reference-bundle/manifests/GRCh38.json).
- The pinned upstream commit in `expected-metadata.json` →
  `upstream_pipeline.broad_reference_vcf_commit`.
- A new entry in
  [`../../docs/divergence-log.md`](../../docs/divergence-log.md) for any
  module on the path that produces the Cohort_VCF.
- The cohort membership in
  [`../inputs/manifest.json`](../inputs/manifest.json) changes.

After regenerating, update `expected-metadata.json` (its
`regeneration_procedure.procedure` block walks through every required
field) and commit the metadata change. Do **not** commit the new
`expected.vcf.gz` itself — it stays in S3.

## How the validation harness consumes this

The acceptance test
`kiro-life-sciences/tests/gatk_sv_healthomics/acceptance/test_validation_concordance.py`
loads `expected.vcf.gz` from this directory and compares it against the
produced Cohort_VCF using `compare_cohort_vcf` (strict) by default and
`compare_cohort_vcf_fuzzy` (±50 bp) when invoked with the fuzzy
comparator. The test is RUN only when `RUN_ACCEPTANCE_TESTS=1` is set in
the environment AND `expected.vcf.gz` is present locally.

## Current status

The current state of `expected.vcf.gz` is recorded in the `status` field
of [`expected-metadata.json`](expected-metadata.json):

- `PENDING_FIRST_VALIDATION_RUN` — no real expected has been produced yet;
  the binary file is absent. Acceptance test is skipped.
- `READY` — a real expected is present locally and its checksums in
  `expected-metadata.json` match the computed digests. Acceptance test
  runs.
- `STALE` — one of the regeneration triggers has fired since the
  current expected was produced; rerun the procedure above before
  trusting the next acceptance run.

The current state of the **post-GQ** expected artifact is recorded in
the `post_gq_artifact.status` field of
[`expected-metadata.json`](expected-metadata.json) and mirrored in each
manifest's `expected_post_gq_vcf_status` field:

- `PENDING_FIRST_VALIDATION_RUN` — no post-GQ expected has been produced
  yet; the validation harness skips the GQ-recalibrator concordance gate
  while `expected_post_gq_vcf_sha256` is null in the consuming manifest.
- `READY` — produced and uploaded; sha256 recorded; the GQ-recalibrator
  concordance gate runs.
- `STALE` — one of the regeneration triggers in
  `post_gq_artifact.regeneration_triggers` has fired since the current
  post-GQ expected was produced; reproduce before trusting the next
  acceptance run.
