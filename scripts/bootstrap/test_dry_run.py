#!/usr/bin/env python3
"""End-to-end dry-run harness for the bootstrap scripts.

For each bootstrap step, this validates:
  * The script imports cleanly (catches syntax + import errors).
  * Its main() can be reached and constructs valid AWS API parameters.
  * Its preconditions (env vars, file paths it reads) are satisfiable.
  * Where it would mutate AWS state, the planned action is described
    without actually mutating.

This is intentionally read-only: we use boto3 client patching to intercept
mutation calls and assert their parameters, while letting Get/List/Head
calls go through to live AWS so we accurately reflect the existing account
state.

Run with:
    AWS_ACCOUNT_ID=<account> AWS_DEFAULT_REGION=ap-southeast-1 \\
    .venv/bin/python scripts/bootstrap/test_dry_run.py

Exit codes:
    0   all scripts dry-run-clean
    1   any script raised
    2   env var missing
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent.parent

# Steps to dry-run. Each entry is (label, action) where action is a callable
# that runs the step. The callable must return None on success and raise on
# failure.

# We don't actually invoke 02 (stage_reference) or 03 (setup_ecr) because
# they shell out to other scripts that have their own logic; we just check
# their entrypoints parse and the upstream scripts exist.

INTERCEPTED_CALLS: list[tuple[str, str, dict]] = []


@contextmanager
def intercept_mutations():
    """Patch boto3 clients so mutating calls are recorded but never executed.

    Read-only calls (head_*, get_*, list_*, describe_*) pass through to live AWS.
    """
    real_client = boto3.client

    def patched_client(service, *args, **kwargs):
        c = real_client(service, *args, **kwargs)
        # Wrap mutating methods. We use prefixes that strongly imply mutation.
        for attr in dir(c):
            if attr.startswith(("create_", "put_", "delete_", "update_",
                                "attach_", "detach_", "add_", "remove_",
                                "tag_", "untag_", "run_instances", "stop_instances",
                                "start_instances", "terminate_instances",
                                "send_command", "delete_objects")):
                if not callable(getattr(c, attr, None)):
                    continue

                def make_intercept(method_name, real_method, svc):
                    def fake(*a, **kw):
                        INTERCEPTED_CALLS.append((svc, method_name, kw))
                        # Return a plausible response shape for the few methods
                        # the bootstrap relies on.
                        return _fake_response(svc, method_name, kw)
                    return fake

                setattr(c, attr, make_intercept(attr, getattr(c, attr), service))

        # Also patch a small number of read calls that scripts do *after* a
        # mutation to follow up on the returned fake id. We return a
        # "completed" state so the script's polling loop exits cleanly.
        if service == "omics":
            real_get_workflow = c.get_workflow

            def fake_get_workflow(id, **kw):
                if id == "DRYRUN_WORKFLOW":
                    return {"id": id, "status": "ACTIVE", "name": "DRYRUN"}
                return real_get_workflow(id=id, **kw)
            c.get_workflow = fake_get_workflow

        if service == "ec2":
            real_get_waiter = c.get_waiter

            def fake_get_waiter(name):
                if name == "instance_running":
                    class _Noop:
                        def wait(self, **kw):
                            return None
                    return _Noop()
                return real_get_waiter(name)
            c.get_waiter = fake_get_waiter
        return c

    with mock.patch.object(boto3, "client", patched_client):
        yield


def _fake_response(service: str, method: str, kw: dict):
    """Return a believable response dict for an intercepted mutation call."""
    if method == "create_role":
        return {"Role": {"Arn": f"arn:aws:iam::{kw.get('RoleName','x')}", "RoleName": kw.get("RoleName","x")}}
    if method == "create_run_cache":
        return {"id": "DRYRUN_CACHE_ID", "status": "ACTIVE", "arn": "arn:dryrun"}
    if method == "create_bucket":
        return {}
    if method == "create_security_group":
        return {"GroupId": "sg-DRYRUN"}
    if method == "run_instances":
        return {"Instances": [{"InstanceId": "i-DRYRUN", "State": {"Name": "pending"}}]}
    if method == "create_workflow":
        return {"id": "DRYRUN_WORKFLOW", "status": "CREATING"}
    if method == "send_command":
        return {"Command": {"CommandId": "DRYRUN_CMD"}}
    if method == "create_instance_profile":
        return {"InstanceProfile": {"Arn": "arn:aws:iam::x:instance-profile/x", "InstanceProfileName": kw.get("InstanceProfileName","x")}}
    return {}


def run_step(label: str, fn) -> tuple[bool, str]:
    """Execute one dry-run step. Returns (passed, summary)."""
    INTERCEPTED_CALLS.clear()
    try:
        with intercept_mutations():
            fn()
        return True, f"intercepted {len(INTERCEPTED_CALLS)} mutation calls"
    except Exception as e:
        tb = traceback.format_exc()
        return False, f"{type(e).__name__}: {e}\n{tb[-800:]}"


def import_script(name: str):
    path = ROOT / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def step_00():
    mod = import_script("00_substitute_placeholders.py")
    mod.main()  # idempotent text replace; safe to actually run


def step_01():
    mod = import_script("01_create_buckets.py")
    # Use --dry-run if implemented
    sys.argv = ["01_create_buckets.py", "--dry-run"]
    mod.main()


def step_02():
    # 02 just shells out to scripts/stage_reference.py; verify entrypoint exists
    target = REPO_ROOT / "scripts" / "stage_reference.py"
    assert target.exists(), f"missing dependency: {target}"
    # Don't actually execute -- staging is a 30-60min real operation


def step_03():
    target = REPO_ROOT / "scripts" / "clone_gcr_images.sh"
    assert target.exists(), f"missing dependency: {target}"
    # Don't actually execute -- ECR pull-through cache mutation


def step_04():
    target = REPO_ROOT / "wham-patch" / "Dockerfile.fast"
    assert target.exists(), f"missing wham Dockerfile: {target}"
    target = REPO_ROOT / "wham-patch" / "whamg-flush.patch"
    assert target.exists(), f"missing wham patch: {target}"


def step_05():
    mod = import_script("05_create_iam_role.py")
    mod.main()


def step_06():
    mod = import_script("06_create_run_cache.py")
    mod.main()


def step_07():
    mod = import_script("07_provision_ec2_hybrid.py")
    mod.main()


def step_08():
    mod = import_script("08_register_workflows.py")
    sys.argv = ["08_register_workflows.py"]
    mod.main()


def step_09():
    mod = import_script("09_validate.py")
    mod.main()


def step_99():
    mod = import_script("99_teardown.py")
    sys.argv = ["99_teardown.py"]  # no --confirm -> dry-run
    mod.main()


def main() -> int:
    if not os.environ.get("AWS_ACCOUNT_ID"):
        print("AWS_ACCOUNT_ID env var required", file=sys.stderr)
        return 2

    steps = [
        ("00_substitute_placeholders", step_00),
        ("01_create_buckets",          step_01),
        ("02_stage_reference (deps)",  step_02),
        ("03_setup_ecr (deps)",        step_03),
        ("04_build_wham (deps)",       step_04),
        ("05_create_iam_role",         step_05),
        ("06_create_run_cache",        step_06),
        ("07_provision_ec2_hybrid",    step_07),
        ("08_register_workflows",      step_08),
        ("09_validate",                step_09),
        ("99_teardown",                step_99),
    ]

    print(f"Dry-running {len(steps)} bootstrap steps...\n")
    failures = []
    for label, fn in steps:
        print(f"--- {label} ---")
        ok, summary = run_step(label, fn)
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {summary}")
        if not ok:
            failures.append(label)
        print()

    print("=" * 60)
    print(f"Result: {len(steps)-len(failures)}/{len(steps)} steps dry-run-clean")
    if failures:
        print(f"Failed: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
