# GATK-SV HealthOmics Validation Runbook

This runbook covers the three validation steps needed to back the
"bit-identical to Broad" claim in Requirement 2.4.

## Step 1: produce the Broad reference cohort VCF (off-prem)

The HealthOmics-migrated pipeline must be compared against a known-good
output of Broad's upstream WDL on the same 10-sample cohort. Run this
once on Terra/Cromwell-on-GCP using the upstream master branch and
upload the resulting VCF to our S3 bucket.

### Required inputs
- 10 CRAM/BAI pairs for samples NA12878, HG00096, HG00097, HG00099,
  HG00100, HG00101, HG00102, HG00513, NA19238, NA19239 (already in S3
  in `omics-ref-ap-southeast-1-__ACCOUNT_ID__` plus the 1000 Genomes
  buckets).
- Cohort PED: `gatk-sv-healthomics/validation-cohort/inputs/cohort.ped`.
- Upstream Broad GATK-SV at the same commit as our migration:
  `672d855` (the GATK image tag we use is `mw-gatk-sv-672d85`, matching).

### Procedure
1. On a Terra workspace with billing enabled, import the `gatk-sv` repo at
   commit `672d855` (`https://github.com/broadinstitute/gatk-sv.git`).
2. Use `inputs/values/resources_hg38.json` for references.
3. Run the full `GATKSVPipelineSingleSample` chain or, equivalently, the
   per-module sequence GSE → GBE → ClusterBatch → ... → MakeCohortVcf →
   AnnotateVcf, on the 10-sample cohort.
4. Copy the final `<cohort>.cleaned.vcf.gz` (plus `.tbi`) and the
   `<cohort>.annotated.vcf.gz` to:
   - `s3://omics-ref-ap-southeast-1-__ACCOUNT_ID__/gatk-sv/validation/broad-reference/<cohort>.cleaned.vcf.gz`
   - same for `.tbi` and the annotated variant

### Then
Drop the produced VCF into the local `expected/` directory:
```
aws s3 cp \
  s3://omics-ref-ap-southeast-1-__ACCOUNT_ID__/gatk-sv/validation/broad-reference/gatk-sv-validation-2026q2.cleaned.vcf.gz \
  gatk-sv-healthomics/validation-cohort/expected/expected.vcf.gz
```
The acceptance test
`tests/gatk_sv_healthomics/acceptance/test_validation_concordance.py`
becomes runnable once that file exists.

## Step 2: run end-to-end concordance check

```bash
RUN_ACCEPTANCE_TESTS=1 /Users/cleewh/Desktop/KiroLS/.venv/bin/python -m pytest \
  kiro-life-sciences/tests/gatk_sv_healthomics/acceptance/test_validation_concordance.py -v
```

Pass criterion (Req 13.3):
- DEL ≥ 99% concordance
- DUP ≥ 99% concordance
- INS ≥ 95% concordance
- INV ≥ 95% concordance

The default comparator is the **strict** comparator
(`compare_cohort_vcf`). If you want the Broad-grade ±50bp fuzz tolerance:

```python
from kiro_life_sciences.gatk_sv_healthomics.validation.fuzzy import (
    compare_cohort_vcf_fuzzy,
)
report = compare_cohort_vcf_fuzzy(produced, expected, pos_fuzz_bp=50)
```

When concordance fails, the report's `discordant_sites` enumerates every
site that's in one VCF but not the other.

## Step 3: cross-engine divergence (HealthOmics vs EC2/miniwdl)

Validates that the per-sample evidence emitted by GatherSampleEvidence
on HealthOmics matches the same module's output on EC2/miniwdl. This is
the step that actually shows the engines themselves agree on a single
sample's intermediate outputs.

### Procedure
1. Pick a sample (e.g. NA12878). Find the GSE production run output
   prefix from `gatk-sv-healthomics/gse-cohort-runs.json`.
2. Re-run that sample on EC2 via miniwdl using the registered
   HealthOmics WDL bundle:
   ```bash
   /Users/cleewh/Desktop/KiroLS/.venv/bin/python \
     gatk-sv-healthomics/scripts/run_gse_one_sample_ec2.py \
       --sample NA12878 \
       --reads-uri  s3://.../NA12878.cram \
       --reads-index-uri s3://.../NA12878.cram.crai
   ```
3. Once EC2 finishes (~1-2 hours for one sample), pull both result sets
   to local disk:
   ```bash
   /Users/cleewh/Desktop/KiroLS/.venv/bin/python \
     gatk-sv-healthomics/scripts/divergence_pull.py \
       --sample NA12878 \
       --ec2-prefix s3://healthomics-outputs-__ACCOUNT_ID__-apse1/runs/gatk-sv-e2e/divergence/NA12878/ec2
   ```
4. Run the test:
   ```bash
   RUN_ACCEPTANCE_TESTS=1 \
   /Users/cleewh/Desktop/KiroLS/.venv/bin/python -m pytest \
     kiro-life-sciences/tests/gatk_sv_healthomics/acceptance/test_engine_divergence.py \
     -k NA12878 -v
   ```

### What it checks
For each per-sample artifact (`pe.txt.gz`, `sr.txt.gz`, `rd.txt.gz`,
`manta.vcf.gz`, `wham.vcf.gz`, `scramble.vcf.gz`):
- VCF: skip ##header lines, sort records, hash → same between engines.
- TSV.gz: full body hash → same between engines.
- Plain text: full hash → same between engines.

A failed test names every artifact whose hash differs and prints the
pair of hashes to make tracing the divergence easy.

## What this validates and what it doesn't

| Validation | What it shows | What it doesn't |
|---|---|---|
| Cohort concordance vs Broad | The final cohort VCF agrees with what Broad's pipeline would produce on Terra | Doesn't reveal which intermediate module diverged |
| Cross-engine divergence (Step 3) | A single sample's GSE outputs are identical between HealthOmics and miniwdl/EC2 | Only covers GSE; downstream modules need separate divergence runs |
| End-to-end pipeline run (already done) | The pipeline runs to completion and produces a plausibly-correct cohort VCF | Doesn't verify scientific correctness |

For full Req 2.4 coverage, all three are needed.
