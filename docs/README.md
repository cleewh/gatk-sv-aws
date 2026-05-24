# GATK-SV on AWS HealthOmics — Operator README

This directory is the operator-facing home for the GATK-SV → AWS HealthOmics migration,
deployed in **ap-southeast-1 (Singapore)** with a per-sample cost target of **USD $7.00**.

See the full spec at `.kiro/specs/gatk-sv-healthomics-migration/` (requirements, design, tasks).

## Quick Reference

| Question | Answer |
|---|---|
| Target region | `ap-southeast-1` |
| Reference build | GRCh38 (default); GRCh37 optional |
| Input formats | CRAM + CRAI, or BAM + BAI |
| Migrated modules | 10, end-to-end from `GatherSampleEvidence` to `AnnotateVcf` |
| SV callers in scope | Manta, Wham, Scramble, GATK-gCNV |
| Caller excluded | **MELT** (reduced MEI sensitivity accepted) |
| Per-sample cost target | USD $7.00 |
| Default storage | DYNAMIC (STATIC for cohorts > 1 TiB) |
| Default networking | RESTRICTED (VPC is opt-in) |
| Default caching | CACHE_ALWAYS |

## MELT exclusion — important caveat

MELT (Mobile Element Locator Tool) is **excluded** from this migration. MELT requires a
per-user academic/commercial license from the Scott Devine lab and is not redistributable
in a public container image. Excluding it means:

- The migrated pipeline does not detect **mobile element insertions** (Alu, LINE1, SVA
  retrotransposons) at the same sensitivity as the upstream pipeline.
- Deletion, duplication, insertion (non-MEI), and inversion calling is **unaffected**.

This tradeoff is **accepted** per the spec's Requirement 2a.5. Operators who need MEI
calling must either (a) license MELT themselves and re-add the task, or (b) run a
complementary MEI caller (e.g. MELT alternatives like Mobster) as a separate step.

## Operator workflow (reference; see `phase-status.md` for current implementation state)

### 1. Provision the Reference Bundle

Stage GRCh38 reference files into an S3 bucket in `ap-southeast-1`. See
[reference-bundle.md](reference-bundle.md) for the required files and a staging script.

### 2. Configure ECR pull-through caches

Set up ECR pull-through cache rules for Docker Hub, GCR, and Quay.io in your AWS account.
See [ecr-configuration.md](ecr-configuration.md).

### 3. Build the Container Registry Map

Walk every `runtime.docker` reference in the packaged WDLs and emit the
`container-registry-map.json`. Run:

```bash
.venv/bin/python -m kiro_life_sciences.gatk_sv_healthomics.registry.build \
    --output gatk-sv-healthomics/container-registry-map/container-registry-map.json
```

(The CLI entry point is `gatk-sv-healthomics` per `pyproject.toml`, but the module is not
yet wired as a Click group — see [phase-status.md](phase-status.md).)

### 4. Package each module

```bash
.venv/bin/python - <<'PY'
from kiro_life_sciences.gatk_sv_healthomics.packager import package_module
from kiro_life_sciences.gatk_sv_healthomics.models import MIGRATED_MODULES

COMMIT = "<pinned-upstream-sha>"  # e.g. HEAD of https://github.com/broadinstitute/gatk-sv
for module in MIGRATED_MODULES:
    bundle = package_module(commit=COMMIT, module=module)
    print(f"{module}: {bundle.zip_path} ({len(bundle.divergence)} divergences)")
PY
```

### 5. Lint each bundle

Every packaged ZIP is linted with `miniwdl` locally before registration. The
HealthOmics `LintAHOWorkflowBundle` MCP call is the second gate — run in Phase 5.

### 6. Register each workflow

Call `CreateAHOWorkflow` (or `CreateAHOWorkflowVersion` on subsequent changes) for each
of the 10 modules. See [workflow-registration.md](workflow-registration.md).

### 7. Create the Run Cache + IAM role

One `CACHE_ALWAYS` Run Cache per account. One run role whose permissions are
synthesized from a `RoleScope` via `synthesize_run_role`. See
[iam-and-cache.md](iam-and-cache.md).

### 8. Run the validation cohort

A ≤10-sample validation cohort with expected Cohort_VCF assertions gates promotion to
production. The concordance gates are ≥99% for DEL/DUP and ≥95% for INS/INV.
See [validation-cohort.md](validation-cohort.md).

### 9. Promote to production

After validation passes, tag the registered workflow versions `prod` and submit the
production cohort.

## Documentation map

| Doc | Purpose |
|---|---|
| `README.md` (this file) | Operator landing page |
| `phase-status.md` | **Current implementation status — read this first** |
| `divergence-log.md` | Every edit applied to upstream GATK-SV sources, with rationale |
| `cost-target.md` | How the $7/sample target is measured and verified |
| `runtime-and-cost-expectations.md` | Expected wall-clock and per-sample cost per module |
| `scope-inventory.md` | Supported inputs, reference builds, modules, callers, outputs |
| `cache-and-rollback.md` | Run Cache invalidation + workflow version rollback procedures |

## Repository layout

```
gatk-sv-healthomics/
├── wdl/bundles/<module>/              One dir per migrated module
├── parameter-templates/               HealthOmics parameter template JSONs
├── container-registry-map/            container-registry-map.json
├── iam/policies/                      Synthesized run role policy + trust policy
├── reference-bundle/manifests/        GRCh38 / GRCh37 reference file manifests
├── validation-cohort/
│   ├── inputs/                        ≤10-sample validation manifest
│   └── expected/                      Expected Cohort_VCF for concordance comparison
└── docs/                              This directory
```

The Python package lives at `kiro-life-sciences/src/kiro_life_sciences/gatk_sv_healthomics/`.
Run tests with `cd kiro-life-sciences && .venv/bin/python -m pytest tests/gatk_sv_healthomics -q`.

## Where the spec lives

- **Requirements**: `.kiro/specs/gatk-sv-healthomics-migration/requirements.md` — 18 numbered requirements
- **Design**: `.kiro/specs/gatk-sv-healthomics-migration/design.md` — architecture, components, 10 correctness properties
- **Tasks**: `.kiro/specs/gatk-sv-healthomics-migration/tasks.md` — phased implementation plan

If the code and the spec diverge, the spec is authoritative; open a divergence entry and
update the spec before changing behavior.
