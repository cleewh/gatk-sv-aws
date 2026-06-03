"""Unit tests for Phase A.5 (scramble on EC2) serial dispatch behaviour.

The original Phase A.5 implementation submitted ALL per-sample SSM
``send_command`` calls upfront and then polled them, causing 9+ samples to
race for the 3 GB reference FASTA + per-sample CRAMs and saturate the
single ``m5.2xlarge`` instance's network/disk.

The fix is to dispatch SERIALLY: send one SSM command, poll it to terminal
status, then send the next. This file pins that behaviour:

* Source-level invariant: the function body must contain exactly one
  ``ssm.send_command(`` call within a single per-sample ``for`` loop, and
  the polling loop (``ssm.get_command_invocation``) must live inside the
  same per-sample loop body — i.e. there must NOT be a second
  ``for ... in pending`` loop after a batch ``send_command`` loop.

* Behavioural invariant: when invoked with a fake SSM client, each
  ``send_command`` must observe that the previously-issued command's
  invocation has already reached terminal status before the next
  ``send_command`` is issued.

Both checks together guarantee that re-dispatch on the single EC2 instance
is one-at-a-time (Phase A.5 fix; cohort smoke-test re-run 2026-05-29).
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ORCHESTRATOR_PATH = _REPO_ROOT / "scripts" / "run_cohort_e2e.py"


@pytest.fixture(scope="module")
def orch():
    os.environ.setdefault("AWS_ACCOUNT_ID", "000000000000")
    os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")
    spec = importlib.util.spec_from_file_location(
        "run_cohort_e2e", str(_ORCHESTRATOR_PATH)
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_cohort_e2e"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Source-level invariant
# ---------------------------------------------------------------------------


def test_phase_a5_source_has_no_batch_send_loop(orch) -> None:
    """The function must have exactly one ``send_command`` call, and that
    call must sit inside the same per-sample ``for`` loop that polls the
    invocation. A second ``for cmd_id, meta in pending`` style loop after
    a batch-dispatch loop would re-introduce the parallel-dispatch bug.
    """
    src = inspect.getsource(orch.phase_a5_scramble_ec2)

    # exactly one send_command call
    assert src.count("ssm.send_command(") == 1, (
        "phase_a5_scramble_ec2 must issue exactly one send_command call "
        "(inside the per-sample loop). Multiple send_command call sites "
        "indicate parallel dispatch."
    )

    # polling must be inside the same loop, not in a separate post-loop pass.
    # We check that no `for <name>, <name> in pending` style loop exists,
    # which was the marker of the old "submit all, then poll all" pattern.
    assert not re.search(r"for\s+\w+\s*,\s*\w+\s+in\s+pending", src), (
        "phase_a5_scramble_ec2 must not contain a `for ... in pending` "
        "post-dispatch poll loop. Polling must happen inline, immediately "
        "after each send_command, before the next sample is dispatched."
    )

    # there must be a per-sample for-loop and the get_command_invocation
    # call must be in the same source as the send_command call (sanity)
    assert "for sid in samples" in src
    assert "ssm.get_command_invocation(" in src


# ---------------------------------------------------------------------------
# Behavioural invariant: serial dispatch with a fake SSM client
# ---------------------------------------------------------------------------


class _FakeSSMExceptions:
    class InvocationDoesNotExist(Exception):
        pass


class _FakeSSM:
    """Minimal ssm client that records the *order* of send_command vs
    get_command_invocation calls and forces every command to terminal
    Success status on first poll.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []  # (op, cmd_id)
        self.exceptions = _FakeSSMExceptions()
        self._counter = 0

    def send_command(self, **kwargs: Any) -> dict[str, Any]:
        self._counter += 1
        cmd_id = f"cmd-{self._counter:04d}"
        self.events.append(("send", cmd_id))
        return {"Command": {"CommandId": cmd_id}}

    def get_command_invocation(self, *, CommandId: str, InstanceId: str) -> dict[str, Any]:
        self.events.append(("poll", CommandId))
        return {
            "Status": "Success",
            "StandardOutputContent": "",
            "StandardErrorContent": "",
        }


class _FakeS3:
    def put_object(self, **kwargs: Any) -> None:
        return None


def _build_args(orch, samples: list[str], cohort_id: str = "cohort-x") -> Any:
    parser = orch._build_parser()
    return parser.parse_args([
        "--cohort-id", cohort_id,
        "--samples", ",".join(samples),
    ])


def test_phase_a5_serial_dispatch_order(orch, monkeypatch, tmp_path) -> None:
    """With a fake SSM client, the sequence of operations must alternate
    send -> poll(...) -> send -> poll(...). Every send_command must be
    preceded only by completed polls (or be the first call).

    This verifies that dispatch is strictly serial: command N+1 is never
    sent while command N is still being polled.
    """
    # Patch global SSM/S3 client factory used by the function.
    fake_ssm = _FakeSSM()
    fake_s3 = _FakeS3()

    def _fake_client(name: str, region_name: str | None = None):
        if name == "ssm":
            return fake_ssm
        if name == "s3":
            return fake_s3
        raise AssertionError(f"unexpected boto3.client('{name}')")

    monkeypatch.setattr(orch.boto3, "client", _fake_client)
    # Don't actually sleep between polls.
    monkeypatch.setattr(orch.time, "sleep", lambda *_args, **_kw: None)
    # Make sure run_scramble_ec2.sh exists (it does in repo) -- nothing to mock.

    samples = ["S1", "S2", "S3", "S4"]
    args = _build_args(orch, samples)

    # The function reads the local shell script from disk; that already
    # exists in the repo so no patch needed. It also writes per-cohort key
    # to S3, our _FakeS3.put_object is a no-op.
    records = orch.phase_a5_scramble_ec2(
        args,
        output_base=f"s3://bucket/runs/gatk-sv-e2e/{args.cohort_id}",
        gse_run_results=None,
        cohort_base=None,
    )

    # All four samples produced exactly one StageRecord with Success.
    assert len(records) == 4
    assert [r.status for r in records] == ["Success"] * 4
    assert [r.stage for r in records] == [f"scramble_ec2:{s}" for s in samples]

    # Build the (op, cmd_id) sequence and verify strict serialisation:
    # for every send op, all preceding polls reference *previous* command
    # ids, never the just-issued one's neighbours.
    sends = [(i, c) for i, (op, c) in enumerate(fake_ssm.events) if op == "send"]
    assert len(sends) == 4, "must issue one send_command per sample"

    # Between send N and send N+1 there must be at least one poll for cmd N
    # ending in a terminal status (our fake always returns Success on the
    # first poll, so any poll suffices).
    for k in range(len(sends) - 1):
        i_send_k, cmd_k = sends[k]
        i_send_k1, _ = sends[k + 1]
        polls_between = [
            (i, c) for i, (op, c) in enumerate(fake_ssm.events)
            if op == "poll" and i_send_k < i < i_send_k1
        ]
        assert polls_between, (
            f"no polls between send #{k} and send #{k+1}: "
            "dispatch was not serialised"
        )
        # every intermediate poll must be for the just-sent command
        assert all(c == cmd_k for _, c in polls_between), (
            f"polls between send #{k} ({cmd_k}) and send #{k+1} reference "
            "a different cmd id, indicating overlapping dispatch"
        )

    # No poll may reference a command that hasn't been sent yet.
    sent_so_far: set[str] = set()
    for op, cmd_id in fake_ssm.events:
        if op == "send":
            sent_so_far.add(cmd_id)
        else:
            assert cmd_id in sent_so_far, (
                f"poll references {cmd_id} before any send_command issued it"
            )


def test_phase_a5_aborts_on_failure_before_next_dispatch(orch, monkeypatch) -> None:
    """If sample N's command ends in Failed, the orchestrator must raise
    *before* dispatching sample N+1. This guarantees serial-with-fail-fast
    semantics so we don't keep spawning work on top of a broken instance.
    """

    class _FailingSSM(_FakeSSM):
        def get_command_invocation(self, *, CommandId: str, InstanceId: str):
            self.events.append(("poll", CommandId))
            # Fail the SECOND command (S2) immediately.
            if CommandId.endswith("0002"):
                return {"Status": "Failed", "StandardErrorContent": "boom"}
            return {"Status": "Success", "StandardOutputContent": ""}

    fake_ssm = _FailingSSM()
    fake_s3 = _FakeS3()

    def _fake_client(name: str, region_name: str | None = None):
        return {"ssm": fake_ssm, "s3": fake_s3}[name]

    monkeypatch.setattr(orch.boto3, "client", _fake_client)
    monkeypatch.setattr(orch.time, "sleep", lambda *_a, **_k: None)

    args = _build_args(orch, ["S1", "S2", "S3"])

    with pytest.raises(RuntimeError, match=r"scramble-ec2 for sample S2 ended in status Failed"):
        orch.phase_a5_scramble_ec2(
            args,
            output_base="s3://bucket/runs/x",
            gse_run_results=None,
            cohort_base=None,
        )

    # Only TWO sends should have been issued (S1 success, S2 failed -> abort);
    # S3 must NOT be dispatched.
    sends = [c for op, c in fake_ssm.events if op == "send"]
    assert len(sends) == 2, (
        f"expected 2 sends before fail-fast abort, got {len(sends)}: {sends}"
    )
