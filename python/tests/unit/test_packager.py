# Feature: gatk-sv-healthomics-migration, Task 3.1.6: Packager example-based tests
"""Example-based unit tests for the WDL Packager & Linter (Component a).

Covers:

* ``strip_melt`` — positive, negative, empty, case-insensitive, metadata
  preservation (Req 2a.3, 2a.4; Design §Components.a, §Correctness
  Properties → Property 9).
* ``reject_gcs_uris`` — input_paths and source scans, empty inputs
  (Req 2.6; Design §Components.a).
* ``check_wdl_version`` — accept/reject matrix (Req 2.1; Design §Components.a).
* ``check_task_declarations`` — placeholder behavior (Req 9.1, 9.5 will be
  properly enforced once miniwdl wiring lands in Task 3.1.7).
"""

from __future__ import annotations

import pytest

from gatk_sv_aws.models import ChangeKind, DivergenceEntry
from gatk_sv_aws.packager import (
    GcsUriViolation,
    TaskDeclarationError,
    UnsupportedWdlVersionError,
    WdlTree,
    check_task_declarations,
    check_wdl_version,
    reject_gcs_uris,
    strip_melt,
)


# ---------------------------------------------------------------------------
# strip_melt
# ---------------------------------------------------------------------------


def test_strip_melt_removes_melt_task_and_emits_divergence() -> None:
    tree = WdlTree(
        source="(synthetic)",
        tasks=["RunMELT", "RunManta"],
    )
    stripped, divergences = strip_melt(tree)

    assert stripped.tasks == ["RunManta"]
    assert len(divergences) == 1
    (entry,) = divergences
    assert isinstance(entry, DivergenceEntry)
    assert entry.change_kind == ChangeKind.REMOVE_CALLER
    assert "melt" in entry.reason.lower()
    assert entry.upstream_path == tree.upstream_path
    assert entry.upstream_commit == tree.upstream_commit


def test_strip_melt_leaves_non_melt_items_untouched() -> None:
    tree = WdlTree(
        source="(synthetic)",
        tasks=["RunManta", "RunWham", "RunScramble"],
        calls=["call_manta", "call_wham"],
        docker_refs=["quay.io/biocontainers/samtools:1.17"],
        input_paths=["ref.fa", "sample.cram"],
    )
    stripped, divergences = strip_melt(tree)

    assert stripped.tasks == tree.tasks
    assert stripped.calls == tree.calls
    assert stripped.docker_refs == tree.docker_refs
    assert stripped.input_paths == tree.input_paths
    assert divergences == []


def test_strip_melt_on_empty_tree_returns_empty_lists_and_no_divergences() -> None:
    tree = WdlTree(source="")
    stripped, divergences = strip_melt(tree)

    assert stripped.tasks == []
    assert stripped.calls == []
    assert stripped.docker_refs == []
    assert stripped.input_paths == []
    assert divergences == []


def test_strip_melt_is_case_insensitive() -> None:
    tree = WdlTree(
        source="",
        tasks=["MELT", "melt", "Melt", "MeltCaller", "RunManta"],
    )
    stripped, divergences = strip_melt(tree)

    assert stripped.tasks == ["RunManta"]
    assert len(divergences) == 4
    for entry in divergences:
        assert entry.change_kind == ChangeKind.REMOVE_CALLER
        assert "melt" in entry.reason.lower()


def test_strip_melt_preserves_source_and_metadata() -> None:
    tree = WdlTree(
        source="version 1.0\nworkflow w { }\n",
        tasks=["RunMELT", "KeepMe"],
        upstream_path="wdl/GatherSampleEvidence.wdl",
        module="GatherSampleEvidence",
        upstream_commit="abcdef1",
    )
    stripped, divergences = strip_melt(tree)

    assert stripped.source == tree.source
    assert stripped.upstream_path == tree.upstream_path
    assert stripped.module == tree.module
    assert stripped.upstream_commit == tree.upstream_commit
    assert stripped.tasks == ["KeepMe"]
    assert divergences[0].upstream_commit == "abcdef1"


def test_strip_melt_removes_from_all_four_lists() -> None:
    tree = WdlTree(
        source="",
        tasks=["RunMELT", "ok_task"],
        calls=["call_melt", "ok_call"],
        docker_refs=["us.gcr.io/broad-dsde-methods/melt:1.0", "ok_docker"],
        input_paths=["MELT_ref_0.bed", "ok.bed"],
    )
    stripped, divergences = strip_melt(tree)

    assert stripped.tasks == ["ok_task"]
    assert stripped.calls == ["ok_call"]
    assert stripped.docker_refs == ["ok_docker"]
    assert stripped.input_paths == ["ok.bed"]
    # One divergence per removed item across all four lists.
    assert len(divergences) == 4
    # Each names the list it came from for operator triage.
    reasons = [d.reason for d in divergences]
    assert any("tasks:" in r for r in reasons)
    assert any("calls:" in r for r in reasons)
    assert any("docker_refs:" in r for r in reasons)
    assert any("input_paths:" in r for r in reasons)


# ---------------------------------------------------------------------------
# reject_gcs_uris
# ---------------------------------------------------------------------------


def test_reject_gcs_uris_finds_gs_uris_in_input_paths() -> None:
    tree = WdlTree(
        source="",
        input_paths=[
            "s3://ok/path.fa",
            "gs://broad-dsp/references/GRCh38.fasta",
            "https://example.com/ok",
            "gs://another/bucket/file.bed",
        ],
    )
    violations = reject_gcs_uris(tree)

    gs_uris = [v.offending_uri for v in violations]
    assert "gs://broad-dsp/references/GRCh38.fasta" in gs_uris
    assert "gs://another/bucket/file.bed" in gs_uris
    assert len([v for v in violations if v.offending_uri.startswith("gs://")]) == 2
    for v in violations:
        assert isinstance(v, GcsUriViolation)
        assert v.offending_uri.startswith("gs://")
        assert v.line >= 1


def test_reject_gcs_uris_returns_empty_list_when_no_gs_uri_present() -> None:
    tree = WdlTree(
        source="version 1.0\nworkflow w { File ref = 's3://bucket/ref.fa' }",
        input_paths=["s3://ok/path.fa", "https://example.com/ok"],
    )
    assert reject_gcs_uris(tree) == []


def test_reject_gcs_uris_finds_gs_uris_embedded_in_source() -> None:
    source = (
        "version 1.0\n"
        "workflow w {\n"
        "  File ref = 'gs://broad-ref/GRCh38.fasta'\n"
        "  File bed = \"gs://another/track.bed\"\n"
        "}\n"
    )
    tree = WdlTree(source=source)
    violations = reject_gcs_uris(tree)

    uris = sorted(v.offending_uri for v in violations)
    # findall's stop-chars strip the trailing quote.
    assert any(u.startswith("gs://broad-ref/GRCh38.fasta") for u in uris)
    assert any(u.startswith("gs://another/track.bed") for u in uris)
    # Line numbers are 1-indexed and point at the source line of the URI.
    for v in violations:
        assert v.line >= 1


def test_reject_gcs_uris_dedupes_input_path_matches_against_source_scan() -> None:
    # An input_paths entry that also appears verbatim in source MUST NOT be
    # reported twice at the same (path, uri, line) — but input_paths line
    # numbers (1-indexed position) and source line numbers differ, so the
    # dedup key naturally separates them. Verify no unexpected doubles.
    tree = WdlTree(
        source="file = 'gs://bucket/x.bed'\n",
        input_paths=["gs://bucket/x.bed"],
    )
    violations = reject_gcs_uris(tree)
    # Two distinct surfaces (input_paths at line 1, source scan at line 1
    # with the same URI) — dedup collapses them to one.
    assert len(violations) == 1
    assert violations[0].offending_uri == "gs://bucket/x.bed"


# ---------------------------------------------------------------------------
# check_wdl_version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["1.0", "1.1"])
def test_check_wdl_version_accepts_supported_versions(version: str) -> None:
    # Must not raise.
    check_wdl_version(version)


@pytest.mark.parametrize("version", ["draft-2", "1.2", "2.0", "", "latest"])
def test_check_wdl_version_rejects_unsupported_versions(version: str) -> None:
    with pytest.raises(UnsupportedWdlVersionError):
        check_wdl_version(version)


# ---------------------------------------------------------------------------
# check_task_declarations
# ---------------------------------------------------------------------------


def test_check_task_declarations_returns_empty_list_placeholder() -> None:
    # Placeholder until miniwdl-backed task representation lands in Task 3.1.7.
    tree = WdlTree(source="", tasks=["RunManta", "RunWham"])
    result = check_task_declarations(tree)
    assert result == []
    assert isinstance(result, list)
    # Return type annotation is ``list[TaskDeclarationError]``; the empty
    # list is compatible with that shape at runtime.
    assert all(isinstance(e, TaskDeclarationError) for e in result)


# ---------------------------------------------------------------------------
# Task 3.1.1 / 3.1.5 / 3.1.7 (local) — fetch_upstream, package_module, lint_bundle
# ---------------------------------------------------------------------------

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

from gatk_sv_aws.models import (
    LintReport,
    PackagedBundle,
)
from gatk_sv_aws.packager import (
    PackagingError,
    fetch_upstream,
    lint_bundle,
    package_module,
)


pytestmark_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git required"
)


def _init_fixture_repo(repo_dir: Path, contents: str = "hello\n") -> str:
    """Initialize a tiny git repo at ``repo_dir`` with one commit and return its SHA."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    # Minimal user config so ``git commit`` works in CI sandboxes.
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "kiro@example.com"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "Kiro Test"],
        check=True,
        capture_output=True,
        text=True,
    )
    (repo_dir / "README.md").write_text(contents, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo_dir), "add", "README.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-q", "-m", "initial"],
        check=True,
        capture_output=True,
        text=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha


@pytestmark_git
def test_fetch_upstream_idempotent_when_sha_already_checked_out(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    sha = _init_fixture_repo(upstream)
    dest = tmp_path / "clone"

    first = fetch_upstream(f"file://{upstream}", sha, dest=dest)
    assert first.resolve() == dest.resolve()
    assert (first / ".git").exists()

    # Drop a marker to prove the second call does NOT re-clone (if it did,
    # the marker would disappear because the destination gets wiped).
    marker = first / "kiro-marker.txt"
    marker.write_text("keep me", encoding="utf-8")

    second = fetch_upstream(f"file://{upstream}", sha, dest=dest)
    assert second.resolve() == dest.resolve()
    assert marker.exists(), "fetch_upstream must be idempotent for a matching SHA"


@pytestmark_git
def test_fetch_upstream_raises_on_mismatched_sha(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    _init_fixture_repo(upstream)
    dest = tmp_path / "clone"

    bogus_sha = "0" * 40
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        fetch_upstream(f"file://{upstream}", bogus_sha, dest=dest)


def _write_fake_source_root(root: Path, module: str, wdl_body: str) -> Path:
    wdl_dir = root / "wdl"
    wdl_dir.mkdir(parents=True, exist_ok=True)
    (wdl_dir / f"{module}.wdl").write_text(wdl_body, encoding="utf-8")
    return root


def test_package_module_missing_module_raises_file_not_found(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    (source_root / "wdl").mkdir(parents=True)

    with pytest.raises(FileNotFoundError):
        package_module(
            commit="test",
            module="GatherSampleEvidence",
            source_root=source_root,
            output_dir=tmp_path / "out",
        )


_MELT_WDL = """version 1.0

task RunMELT {
  input {
    File bam
  }
  command <<<
    run_melt ~{bam}
  >>>
  runtime {
    docker: "us.gcr.io/broad/melt:v1.0"
    cpu: 2
    memory: "4 GiB"
  }
  output {
    File vcf = "out.vcf"
  }
}

task RunManta {
  input {
    File bam
  }
  command <<<
    manta ~{bam}
  >>>
  runtime {
    docker: "quay.io/biocontainers/manta:1.6"
    cpu: 2
    memory: "4 GiB"
  }
  output {
    File vcf = "out.vcf"
  }
}

workflow GatherSampleEvidence {
  input {
    File sample_bam
  }
  call RunMELT { input: bam = sample_bam }
  call RunManta { input: bam = sample_bam }
  output {
    File melt_vcf = RunMELT.vcf
    File manta_vcf = RunManta.vcf
  }
}
"""


def test_package_module_emits_zip_and_divergence_json(tmp_path: Path) -> None:
    source_root = _write_fake_source_root(
        tmp_path / "src", "GatherSampleEvidence", _MELT_WDL
    )
    output_dir = tmp_path / "out"

    bundle = package_module(
        commit="testsha0",
        module="GatherSampleEvidence",
        source_root=source_root,
        output_dir=output_dir,
    )

    assert isinstance(bundle, PackagedBundle)
    assert bundle.zip_path.exists()
    assert bundle.main_wdl_path == "wdl/GatherSampleEvidence.wdl"

    with zipfile.ZipFile(bundle.zip_path, mode="r") as zf:
        assert "wdl/GatherSampleEvidence.wdl" in zf.namelist()
        rewritten = zf.read("wdl/GatherSampleEvidence.wdl").decode("utf-8")

    # Text-level MELT strip: no MELT task/call/docker references remain.
    lowered = rewritten.lower()
    assert "task runmelt" not in lowered
    assert "call runmelt" not in lowered
    assert "melt:v1.0" not in lowered
    # Non-MELT content is preserved.
    assert "task RunManta" in rewritten
    assert "quay.io/biocontainers/manta:1.6" in rewritten

    divergence_json = output_dir / "divergence.json"
    assert divergence_json.exists()
    entries = json.loads(divergence_json.read_text(encoding="utf-8"))
    assert isinstance(entries, list)
    assert len(entries) >= 1
    assert any(entry["change_kind"] == "remove_caller" for entry in entries)


def test_package_module_records_commit_sha_in_divergence_entries(
    tmp_path: Path,
) -> None:
    source_root = _write_fake_source_root(
        tmp_path / "src", "GatherSampleEvidence", _MELT_WDL
    )

    bundle = package_module(
        commit="abcdef1234",
        module="GatherSampleEvidence",
        source_root=source_root,
        output_dir=tmp_path / "out",
    )

    assert bundle.divergence, "Expected at least one MELT divergence"
    for entry in bundle.divergence:
        assert entry.upstream_commit == "abcdef1234"


def _valid_wdl() -> str:
    return """version 1.0

task RunEcho {
  input {
    String name
  }
  command <<<
    echo ~{name}
  >>>
  runtime {
    docker: "ubuntu:22.04"
    cpu: 1
    memory: "1 GiB"
  }
  output {
    String greeting = "hi"
  }
}

workflow Echo {
  input {
    String who
  }
  call RunEcho { input: name = who }
  output {
    String greeting = RunEcho.greeting
  }
}
"""


def _make_bundle_zip(
    tmp_path: Path, main_rel: str, source: str
) -> PackagedBundle:
    zip_path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(main_rel, source)
    return PackagedBundle(
        zip_path=zip_path,
        main_wdl_path=main_rel,
        module="GatherSampleEvidence",
        upstream_commit="testsha",
        divergence=[],
        lint_report=None,
    )


def test_lint_bundle_accepts_well_formed_wdl(tmp_path: Path) -> None:
    bundle = _make_bundle_zip(tmp_path, "wdl/Echo.wdl", _valid_wdl())
    report = lint_bundle(bundle)

    assert isinstance(report, LintReport)
    assert report.status == "success"
    assert report.errors == []
    assert "miniwdl" in report.raw_output.lower()


def test_lint_bundle_reports_syntax_error(tmp_path: Path) -> None:
    broken = "version 1.0\ntask broken { command <<< echo >>> runtime {\n"
    bundle = _make_bundle_zip(tmp_path, "wdl/broken.wdl", broken)

    report = lint_bundle(bundle)

    assert report.status == "error"
    assert report.errors, "Expected at least one miniwdl error message"
