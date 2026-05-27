# Customer bootstrap

The scripts in this directory provision the AWS resources a brand-new customer
account needs to run the GATK-SV → HealthOmics pipeline. Run them in order;
each script is idempotent (safe to re-run if interrupted).

## Prereqs

- AWS credentials for an account with admin or near-admin permissions
- `python3.11+` with the `gatk_sv_aws` package installed (`pip install -e python/[dev]`)
- Docker (only for step 4 — building the patched Wham image)

## Order of operations

```bash
export AWS_ACCOUNT_ID=<your-12-digit-account-id>
export AWS_DEFAULT_REGION=ap-southeast-1   # or another HealthOmics region

# 1. Substitute __ACCOUNT_ID__ in shipped JSON config files
python scripts/bootstrap/00_substitute_placeholders.py

# 2. Create the four S3 buckets (refs, cohorts, wdl, outputs)
python scripts/bootstrap/01_create_buckets.py

# 3. Stage the GRCh38 reference bundle (~20 GiB, takes 30-60 min)
python scripts/bootstrap/02_stage_reference.py

# 4. Configure ECR pull-through caches and clone the 12 GATK-SV images
python scripts/bootstrap/03_setup_ecr.py

# 5. Verify the upstream Wham image landed in ECR (used to build a fork; reverted 2026-05-26)
python scripts/bootstrap/04_build_wham.py

# 6. Synthesize the IAM run role
python scripts/bootstrap/05_create_iam_role.py

# 7. Create the HealthOmics run cache
python scripts/bootstrap/06_create_run_cache.py
# Save the cache id it prints to GATK_SV_RUN_CACHE_ID

# 8. Provision the EC2 hybrid instance (m5.2xlarge, stopped by default)
python scripts/bootstrap/07_provision_ec2_hybrid.py
# Save the instance id it prints to GATK_SV_EC2_INSTANCE_ID

# 9. Register the 18 HealthOmics module workflows
# (10 original + 8 from the v1.0 amendment, Req 19)
python scripts/bootstrap/08_register_workflows.py
# This writes workflow-ids.json with the registered workflow ids

# 10. Sanity check — confirm everything is in place
python scripts/bootstrap/09_validate.py
```

After step 10 succeeds, you can submit a cohort:

```bash
export GATK_SV_RUN_CACHE_ID=<from-step-7>
export GATK_SV_EC2_INSTANCE_ID=<from-step-8>

python scripts/run_cohort_e2e.py \
    --cohort-id my-cohort-2026q3 \
    --manifest validation-cohort/inputs/manifest.json
```

## What each script does

| Step | Output | Idempotency |
|---|---|---|
| 00 | Substitutes `__ACCOUNT_ID__` placeholder in shipped JSONs | Re-running is a no-op |
| 01 | Creates 4 S3 buckets, sets Intelligent-Tiering on outputs | Skips buckets that already exist |
| 02 | Copies ~28 reference files from upstream Broad GS/S3 | Skips files already at target with matching SHA |
| 03 | Creates Docker Hub / GCR / ECR Public pull-through caches; clones 12 images | Skips already-mirrored images |
| 04 | Verifies upstream `gatk-sv/wham:2024-10-25-...` exists in ECR. The previous custom `fast-v5` build was reverted 2026-05-26 (logic regression — see `docs/wdl-audit.md`). | Read-only check |
| 05 | Creates `gatk-sv-healthomics-run-role` with synthesized least-privilege policy | Idempotent (PutRolePolicy) |
| 06 | Creates a HealthOmics run cache with `CACHE_ALWAYS` | Skips if already exists |
| 07 | Launches m5.2xlarge with Docker pre-installed, then stops it | Skips if instance already tagged `gatk-sv:role=ec2-hybrid` |
| 08 | Packages 18 WDL bundles and registers each as a HealthOmics workflow (10 original + 8 from the v1.0 amendment, Req 19) | Skips already-registered workflows |
| 09 | Reads each resource and confirms it's healthy + accessible | Read-only |

## Estimated cost of bootstrap

| Item | Cost |
|---|---|
| Reference bundle staging | ~$5 one-time (S3 cross-region transfer in) |
| Reference bundle storage | ~$0.50/month (Intelligent-Tiering) |
| ECR storage for 12 images | ~$0.20/month |
| Run cache S3 storage | $0 until first run produces cached objects |
| EC2 hybrid (stopped) | $0 (only billed while running) |
| **Total bootstrap** | **~$5 one-time, ~$0.70/month standby** |

## Tearing it all down

```bash
python scripts/bootstrap/99_teardown.py --confirm
```

(Lists every resource that would be deleted, asks for confirmation, then deletes.)
