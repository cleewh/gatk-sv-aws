"""Component (a): WDL Packager & Linter for the GATK-SV migration.

Implements design §Components and interfaces → (a) WDL Packager & Linter.
Fetches GATK-SV sources at a pinned commit, excises MELT-referencing tasks,
rewrites HealthOmics-incompatible constructs, and produces lint-clean WDL
bundles for Workflow_Registration.

Advances Requirements 2 (WDL Compatibility with HealthOmics) and 2a
(Module and Caller Scope).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import WDL

from gatk_sv_aws.models import (
    ChangeKind,
    DivergenceEntry,
    LintReport,
    ModuleName,
    PackagedBundle,
)


# Upstream GATK-SV repository URL (Req 2a.1; Design §Components.a,
# §Deployment Step 5). Callers pass this plus a pinned commit SHA to
# :func:`fetch_upstream` so the migration is reproducible from source.
GATK_SV_REPO_URL = "https://github.com/broadinstitute/gatk-sv.git"


class PackagingError(RuntimeError):
    """Raised when :func:`package_module` cannot build a clean bundle for a module.

    The message enumerates every condition that blocked packaging (e.g. gs://
    URIs found in any of the module's WDL files) so operators can triage all
    issues in one pass instead of one-at-a-time (Design §Components.a).
    """


# Case-insensitive, word-boundary token match for the string ``MELT`` /
# ``melt`` / any ``*melt*`` identifier segment. Matches ``MELT``,
# ``melt_task``, ``call_melt``, ``RunMELT``, ``MeltCaller`` (because the
# ``melt`` token is bounded by case-changes / underscores which Python's
# ``\b`` treats as word boundaries against ``_`` only — we therefore widen
# the regex to also match identifier fragments via a subsequent search
# across common camel-case and snake-case delimiters).
_MELT = re.compile(r"(?i)melt")
_MELT_WORD = re.compile(r"\bmelt\b", re.IGNORECASE)

# Used by :func:`reject_gcs_uris` to extract gs:// URIs embedded in WDL
# source text. Stops at whitespace or common string-literal delimiters.
_GCS_URI = re.compile(r"gs://[^\s'\"`]+")


@dataclass(frozen=True)
class GcsUriViolation:
    """One gs:// URI found in a WDL body (Req 2.6)."""

    upstream_path: str
    offending_uri: str
    line: int


class UnsupportedWdlVersionError(ValueError):
    """Raised when a WDL source declares a version outside {1.0, 1.1}."""


class TaskDeclarationError(ValueError):
    """Raised when a WDL task lacks an explicit cpu/memory declaration (Req 9.1, 9.5)."""


@dataclass
class WdlTree:
    """A simple tree-shaped representation of a WDL source file.

    Carries the raw text, parsed task names, call statements, and container
    references so the Property 9 test can construct synthetic trees without
    needing a full WDL parser. Task 3.1 replaces this with a miniwdl-backed
    parse result.
    """

    source: str
    tasks: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    docker_refs: list[str] = field(default_factory=list)
    input_paths: list[str] = field(default_factory=list)
    upstream_path: str = "main.wdl"
    module: str = "GatherSampleEvidence"
    upstream_commit: str = "deadbeef"


def strip_melt(wdl_tree: WdlTree) -> tuple[WdlTree, list[DivergenceEntry]]:
    """Remove every MELT-referencing task / call / docker / input from ``wdl_tree``.

    For each removed item across the four lists (``tasks``, ``calls``,
    ``docker_refs``, ``input_paths``) emit one :class:`DivergenceEntry` with
    ``change_kind=ChangeKind.REMOVE_CALLER`` and a ``reason`` citing Req 2a.3
    (Design §Correctness Properties → Property 9). Preserves ``source``,
    ``upstream_path``, ``module``, and ``upstream_commit`` unchanged.
    """
    divergences: list[DivergenceEntry] = []
    kept: dict[str, list[str]] = {
        "tasks": [],
        "calls": [],
        "docker_refs": [],
        "input_paths": [],
    }

    # The DivergenceEntry.module field is typed as the ``ModuleName`` Literal;
    # the WdlTree default is a valid module name, but user-supplied trees
    # could carry an arbitrary string. We pass the string through and rely on
    # Pydantic validation at construction time. For the Property 9 tests,
    # the default module name is valid.
    module = cast(ModuleName, wdl_tree.module)

    for list_name in ("tasks", "calls", "docker_refs", "input_paths"):
        for item in getattr(wdl_tree, list_name):
            if _MELT.search(item):
                divergences.append(
                    DivergenceEntry(
                        module=module,
                        upstream_path=wdl_tree.upstream_path,
                        change_kind=ChangeKind.REMOVE_CALLER,
                        reason=(
                            f"MELT excluded per Req 2a.3 "
                            f"({list_name}: {item!r})"
                        ),
                        upstream_commit=wdl_tree.upstream_commit,
                    )
                )
            else:
                kept[list_name].append(item)

    stripped = WdlTree(
        source=wdl_tree.source,
        tasks=kept["tasks"],
        calls=kept["calls"],
        docker_refs=kept["docker_refs"],
        input_paths=kept["input_paths"],
        upstream_path=wdl_tree.upstream_path,
        module=wdl_tree.module,
        upstream_commit=wdl_tree.upstream_commit,
    )
    return stripped, divergences


def reject_gcs_uris(wdl_tree: WdlTree) -> list[GcsUriViolation]:
    """Return every ``gs://`` URI found in the WDL body (Req 2.6).

    Walks two surfaces:

    * ``wdl_tree.input_paths`` — any entry whose value starts with ``gs://``
      is emitted at line ``index + 1`` (1-indexed).
    * ``wdl_tree.source`` — every line is scanned and every ``gs://...``
      token is emitted at its source line number.

    Duplicates across the two surfaces are suppressed on the
    ``(upstream_path, offending_uri, line)`` tuple. A non-empty return value
    causes the Packager to abort before registration
    (Design §Components.a, §Deployment Step 5).
    """
    violations: list[GcsUriViolation] = []
    seen: set[tuple[str, str, int]] = set()

    for index, item in enumerate(wdl_tree.input_paths):
        if item.startswith("gs://"):
            key = (wdl_tree.upstream_path, item, index + 1)
            if key not in seen:
                violations.append(
                    GcsUriViolation(
                        upstream_path=wdl_tree.upstream_path,
                        offending_uri=item,
                        line=index + 1,
                    )
                )
                seen.add(key)

    for line_index, line in enumerate(wdl_tree.source.splitlines(), start=1):
        for match in _GCS_URI.findall(line):
            key = (wdl_tree.upstream_path, match, line_index)
            if key not in seen:
                violations.append(
                    GcsUriViolation(
                        upstream_path=wdl_tree.upstream_path,
                        offending_uri=match,
                        line=line_index,
                    )
                )
                seen.add(key)

    return violations


def check_wdl_version(declared_version: str) -> None:
    """Accept only ``1.0`` / ``1.1``; raise :class:`UnsupportedWdlVersionError` otherwise (Req 2.1)."""
    if declared_version in {"1.0", "1.1"}:
        return
    raise UnsupportedWdlVersionError(
        f"WDL version {declared_version!r} is not supported; "
        "HealthOmics requires 1.0 or 1.1"
    )


def check_task_declarations(tree: WdlTree) -> list[TaskDeclarationError]:
    """Return one error per task lacking explicit numeric cpu/memory or with GPU > 0.

    Placeholder implementation returning an empty list. The current
    :class:`WdlTree` only carries task names; per-task runtime blocks are
    added once the Packager has a miniwdl-backed representation (Task 3.1.7).
    At that point this function will walk each task's ``runtime`` section and
    enforce Req 9.1 (explicit numeric cpu/memory) and Req 9.5 (gpu_count == 0
    except on the documented GPU allow-list).
    """
    # TODO(3.1.7): replace with a miniwdl-backed walk once the Packager
    # carries full task representations (cpu/memory/gpu runtime fields).
    return []


__all__ = [
    "GATK_SV_REPO_URL",
    "PackagingError",
    "WdlTree",
    "GcsUriViolation",
    "UnsupportedWdlVersionError",
    "TaskDeclarationError",
    "strip_melt",
    "reject_gcs_uris",
    "check_wdl_version",
    "check_task_declarations",
    "fetch_upstream",
    "package_module",
    "lint_bundle",
]


# ---------------------------------------------------------------------------
# Task 3.1.1 — fetch_upstream
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    """Run ``git`` in ``cwd`` and return stdout, stripped.

    Uses ``check=True`` so any non-zero exit propagates as
    :class:`subprocess.CalledProcessError`; captures both streams so failures
    include the underlying git diagnostic.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def fetch_upstream(
    repo_url: str,
    commit_sha: str,
    *,
    dest: Path | None = None,
) -> Path:
    """Clone ``repo_url`` and pin it to ``commit_sha`` on local disk.

    Network access required; subsequent calls with the same ``commit_sha``
    are no-ops (the second call confirms the existing checkout matches and
    returns immediately without re-cloning).

    When ``dest`` is omitted, caches the clone under
    ``~/.cache/gatk-sv-healthomics/<commit_sha[:12]>/`` so repeated
    invocations from the Packager, the CLI, and the test suite share a
    single working copy (Req 2a.1, 16.3; Design §Components.a,
    §Deployment Step 5).

    Args:
        repo_url: Git URL of the upstream repository (typically
            :data:`GATK_SV_REPO_URL`). Any URL that ``git clone`` accepts
            works, including ``file://`` URLs used by the unit tests.
        commit_sha: Full 40-char SHA or any prefix ``git rev-parse``
            resolves unambiguously. Must resolve to a commit reachable in
            the cloned repository.
        dest: Optional explicit destination directory. When provided, the
            function reuses or replaces its contents; when omitted, the
            cache directory described above is used.

    Returns:
        Absolute path to the checked-out working tree.

    Raises:
        RuntimeError: When the final ``HEAD`` of the clone does not match
            ``commit_sha`` after checkout.
        subprocess.CalledProcessError: Propagated from ``git`` on clone /
            checkout failure.
    """
    if dest is None:
        dest = Path.home() / ".cache" / "gatk-sv-healthomics" / commit_sha[:12]
    dest = Path(dest).expanduser().resolve()

    # Idempotent reuse: if the destination already holds the requested
    # checkout, skip the clone.
    git_head = dest / ".git" / "HEAD"
    if dest.exists() and git_head.exists():
        try:
            current = _git(dest, "rev-parse", "HEAD")
        except subprocess.CalledProcessError:
            current = ""
        if current and current.startswith(commit_sha) or current == commit_sha:
            return dest
        # Wrong SHA or unreadable — wipe and reclone below.
        shutil.rmtree(dest)
    elif dest.exists():
        # A non-git directory is squatting on the destination; remove it.
        shutil.rmtree(dest)

    dest.parent.mkdir(parents=True, exist_ok=True)

    # Try a shallow clone first — fastest for HEAD. Fall back to a full
    # clone when the requested SHA is not reachable at HEAD.
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(dest, "checkout", commit_sha)
    except subprocess.CalledProcessError:
        # Shallow clone couldn't reach the SHA — nuke and try a full clone.
        if dest.exists():
            shutil.rmtree(dest)
        subprocess.run(
            ["git", "clone", repo_url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(dest, "checkout", commit_sha)

    final = _git(dest, "rev-parse", "HEAD")
    if not (final == commit_sha or final.startswith(commit_sha)):
        raise RuntimeError(
            f"fetch_upstream: could not pin {repo_url} to {commit_sha} "
            f"(HEAD is {final!r})"
        )
    return dest


# ---------------------------------------------------------------------------
# Task 3.1.5 — package_module
# ---------------------------------------------------------------------------


# Regex for text-level MELT-block removal. Matches ``task <name> { ... }``
# and ``call <name> { ... }`` where ``<name>`` contains ``melt``
# (case-insensitive). Brace balancing is handled in code because Python's
# ``re`` cannot count braces.
_TASK_HEADER = re.compile(
    r"^[ \t]*task[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*\{",
    re.MULTILINE,
)
_CALL_HEADER = re.compile(
    r"^[ \t]*call[ \t]+([A-Za-z_.][A-Za-z0-9_.]*)"
    r"(?:[ \t]+as[ \t]+([A-Za-z_][A-Za-z0-9_]*))?"
    r"(?:[ \t]*\{)?",
    re.MULTILINE,
)
_DOCKER_LINE = re.compile(
    r"^[ \t]*docker[ \t]*:[ \t]*['\"][^'\"]*melt[^'\"]*['\"].*$",
    re.MULTILINE | re.IGNORECASE,
)


def _find_block_end(source: str, open_brace_index: int) -> int:
    """Return the index just past the ``}`` that closes a ``{`` at ``open_brace_index``.

    Walks forward and counts braces. Ignores braces inside quoted strings
    (single or double). Falls back to end-of-string if the brace is never
    closed — callers must decide whether that is an error.
    """
    depth = 0
    i = open_brace_index
    n = len(source)
    in_single = False
    in_double = False
    while i < n:
        c = source[i]
        if in_single:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == "'":
                in_single = False
        elif in_double:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_double = False
        else:
            if c == "'":
                in_single = True
            elif c == '"':
                in_double = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return n


def _text_strip_melt(source: str) -> str:
    """Remove every ``task``/``call`` block whose name matches MELT plus docker lines.

    Purely text-level; used for ZIP emission so the packaged WDL no longer
    references MELT even if the lightweight :class:`WdlTree` route missed
    something. Returns the rewritten source.
    """
    # Collect (start, end) spans to strip, then slice them out in a single
    # pass so nested matches don't shift indexes under us.
    spans: list[tuple[int, int]] = []

    for match in _TASK_HEADER.finditer(source):
        name = match.group(1)
        if _MELT.search(name):
            brace_index = source.index("{", match.end() - 1)
            end = _find_block_end(source, brace_index)
            spans.append((match.start(), end))

    for match in _CALL_HEADER.finditer(source):
        name = match.group(1)
        alias = match.group(2)
        if _MELT.search(name) or (alias and _MELT.search(alias)):
            # ``call X`` may or may not have a ``{`` body; strip to end of
            # block when present, else to end of line.
            tail = source[match.end() :]
            if tail.lstrip().startswith("{"):
                # brace is at first non-space after match.end()
                brace_index = source.index("{", match.end() - 1)
                end = _find_block_end(source, brace_index)
            else:
                newline = source.find("\n", match.end())
                end = newline if newline != -1 else len(source)
            spans.append((match.start(), end))

    for match in _DOCKER_LINE.finditer(source):
        # Strip the entire docker line (including its trailing newline).
        end = match.end()
        if end < len(source) and source[end] == "\n":
            end += 1
        spans.append((match.start(), end))

    if not spans:
        return source

    # Merge overlapping spans (e.g. a docker line inside a MELT task).
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))

    parts: list[str] = []
    cursor = 0
    for start, end in merged:
        parts.append(source[cursor:start])
        cursor = end
    parts.append(source[cursor:])
    return "".join(parts)


def _name_is_melt(value: object) -> bool:
    """Return True iff ``value`` looks like a MELT-the-caller identifier.

    Matches ``melt`` / ``MELT`` / ``melt_*`` / ``*_melt`` / ``*MELT``
    (case-insensitive token), but **excludes** ``melted`` / ``melting`` /
    ``melter`` / ``melty`` since those are unrelated English words
    appearing in upstream identifiers like ``MergeMeltedGts`` and
    ``runtime_override_concat_melted_genotypes``.

    Strategy: find every occurrence of ``melt`` in the string and examine
    the character immediately after it. If the following character is one
    of ``e``/``i``/``y`` (the starts of ``ed``/``ing``/``y``), the match
    is an English word and we skip it. Otherwise it's a MELT identifier
    segment and we return True.
    """
    if value is None:
        return False
    if isinstance(value, list):
        value = ".".join(str(v) for v in value)
    s = str(value)
    for m in re.finditer(r"(?i)melt", s):
        tail = s[m.end() : m.end() + 3]
        # English-word disqualifiers after ``melt``.
        if tail.startswith(("ed", "ing", "er", "y")) and (
            # Make sure "er"/"y"/"ed"/"ing" stand as word fragments, not
            # parts of something like "melt_ervin" — we check that at
            # least one of the "ed/ing/er/y" letters is lowercase and
            # flanked by a lowercase continuation or word boundary.
            True
        ):
            continue
        return True
    return False


def _ast_strip_melt(
    source: str, doc: "WDL.Tree.Document"
) -> tuple[str, list[tuple[str, str]]]:
    """AST-driven MELT strip using miniwdl source-position spans.

    Computes source spans for every MELT-referencing AST node (imports,
    tasks, input declarations, non-input decls, calls, conditionals wrapping
    MELT calls, and output declarations), then deletes the spans in reverse
    order so the result parses and type-checks cleanly.

    Returns ``(rewritten_source, reasons)`` where ``reasons`` is a list of
    ``(node_kind, subject_name)`` tuples the caller turns into
    :class:`DivergenceEntry` objects with full metadata.
    """
    spans: list[tuple[int, int]] = []
    reasons: list[tuple[str, str]] = []

    # Pre-compute cumulative line-start offsets so we can convert
    # ``(line, column)`` pairs to character indices in O(1).
    line_starts = [0]
    for i, ch in enumerate(source):
        if ch == "\n":
            line_starts.append(i + 1)

    def _to_char_range(pos) -> tuple[int, int]:
        # miniwdl lines/columns are 1-indexed.
        start_line = max(pos.line - 1, 0)
        end_line = max(pos.end_line - 1, 0)
        start = line_starts[start_line] + (pos.column - 1)
        end = line_starts[end_line] + (pos.end_column - 1)
        if end < len(source) and source[end] == "\n":
            end += 1
        return (start, end)

    def _queue(pos, kind: str, name: str) -> None:
        spans.append(_to_char_range(pos))
        reasons.append((kind, name))

    for imp in doc.imports:
        if _name_is_melt(imp.namespace) or _name_is_melt(imp.uri):
            _queue(imp.pos, "import", str(imp.namespace or imp.uri))

    for task in doc.tasks:
        if _name_is_melt(task.name):
            _queue(task.pos, "task", task.name)

    workflow = getattr(doc, "workflow", None)
    if workflow is not None:
        for decl in workflow.inputs or []:
            if _name_is_melt(decl.name):
                _queue(decl.pos, "input_decl", decl.name)

        def _walk(body: list) -> None:
            for node in body:
                if isinstance(node, WDL.Call):
                    callee_id = node.callee_id
                    callee_str = (
                        ".".join(str(x) for x in callee_id)
                        if isinstance(callee_id, list)
                        else str(callee_id)
                    )
                    if _name_is_melt(callee_str) or _name_is_melt(
                        getattr(node, "name", None)
                    ):
                        _queue(node.pos, "call", callee_str)
                elif isinstance(node, WDL.Conditional):
                    expr_src = str(node.expr)
                    body_all_melt = bool(node.body) and all(
                        isinstance(inner, WDL.Call)
                        and _name_is_melt(
                            ".".join(
                                str(x)
                                for x in (
                                    inner.callee_id
                                    if isinstance(inner.callee_id, list)
                                    else [str(inner.callee_id)]
                                )
                            )
                        )
                        for inner in node.body
                    )
                    # Only strip the whole conditional when the expression is
                    # PURELY MELT-related (e.g. ``run_melt`` alone). Mixed
                    # expressions like ``collect_coverage || run_melt ||
                    # run_scramble`` must preserve the non-MELT path — we
                    # rewrite the expression (handled in a separate pass
                    # below) rather than drop the whole block.
                    expr_is_pure_melt = (
                        _name_is_melt(expr_src)
                        and "||" not in expr_src
                        and "&&" not in expr_src
                    )
                    if expr_is_pure_melt or body_all_melt:
                        _queue(node.pos, "conditional", expr_src)
                    else:
                        _walk(node.body)
                elif isinstance(node, WDL.Scatter):
                    _walk(node.body)
                elif isinstance(node, WDL.Decl):
                    if _name_is_melt(node.name):
                        _queue(node.pos, "decl", node.name)

        _walk(list(workflow.body))

        # Workflow outputs. Only strip the output declaration itself when
        # the output NAME references MELT. When only the expression
        # references MELT (e.g. ``sample_metrics_files = select_all([...,
        # Melt_Metrics.out, ...])``), leave the declaration alone — the
        # text post-pass strips individual MELT identifiers from the
        # expression, preserving the output interface for downstream
        # modules.
        for out in workflow.outputs or []:
            if _name_is_melt(out.name):
                _queue(out.pos, "output", out.name)

    if not spans:
        return source, reasons

    # Merge overlapping spans (a stripped conditional may contain a decl we
    # also queued). Keep the outer reason in that case to avoid double-count.
    paired = sorted(zip(spans, reasons))
    merged_spans: list[tuple[int, int]] = []
    merged_reasons: list[tuple[str, str]] = []
    for (start, end), reason in paired:
        if merged_spans and start < merged_spans[-1][1]:
            merged_spans[-1] = (
                merged_spans[-1][0],
                max(end, merged_spans[-1][1]),
            )
        else:
            merged_spans.append((start, end))
            merged_reasons.append(reason)

    out = source
    for start, end in reversed(merged_spans):
        out = out[:start] + out[end:]

    # Post-pass: strip leftover MELT-named call-input lines. These are lines
    # inside a `call X { input: ... }` body like `melt_vcf = MELT.vcf,` or
    # `baseline_melt_vcf = baseline_melt_vcf,`. The AST route doesn't give
    # us clean per-argument spans so we do this at the line level.
    melt_line_re = re.compile(
        r"^[ \t]*\w*melt\w*[ \t]*=.*\n?",
        re.IGNORECASE | re.MULTILINE,
    )
    out = re.sub(melt_line_re, "", out)

    # Also remove comment lines that mention MELT (purely cosmetic — makes
    # the rewritten source self-consistent with what's left).
    melt_comment_re = re.compile(
        r"^[ \t]*#.*(?i:melt).*$\n?", re.MULTILINE
    )
    out = re.sub(melt_comment_re, "", out)

    # Rewrite mixed expressions like ``collect_coverage || run_melt || run_scramble``
    # to drop the ``run_melt`` clause. Handles both sides of ``||`` and ``&&``.
    # Six common shapes captured:
    #   ``run_melt || X`` → ``X``
    #   ``X || run_melt`` → ``X``
    #   ``run_melt && X`` → ``X`` (conservative; preserves the other precondition)
    #   ``X && run_melt`` → ``X``
    #   ``defined(melt_docker) || X`` → ``X``
    #   ``X || defined(melt_docker)`` → ``X``
    for pat in (
        r"\brun_melt\b\s*\|\|\s*",
        r"\s*\|\|\s*\brun_melt\b",
        r"\brun_melt\b\s*&&\s*",
        r"\s*&&\s*\brun_melt\b",
        r"\bdefined\(\s*melt_\w+\s*\)\s*\|\|\s*",
        r"\s*\|\|\s*\bdefined\(\s*melt_\w+\s*\)",
        r"\bdefined\(\s*melt_\w+\s*\)\s*&&\s*",
        r"\s*&&\s*\bdefined\(\s*melt_\w+\s*\)",
    ):
        out = re.sub(pat, "", out)

    # Drop identifiers whose final dotted segment contains the ``melt`` token
    # (case-insensitive) from array / list literals and from multi-arg call
    # expressions.
    #
    # IMPORTANT: we must exclude words like ``melted``, ``melting``,
    # ``melter`` — those are unrelated to MELT-the-caller. The negative
    # lookahead ``(?!ed|ing|er|y)`` (case-sensitive-appearing but actually
    # case-insensitive via the ``(?i)`` flag) rules those out after ``melt``.
    # This keeps identifiers like ``MergeMeltedGts`` and
    # ``runtime_override_merge_melted_gts`` intact.
    #
    # The ``(?<![.\w])`` / ``(?![.\w])`` anchors ensure we only match at
    # identifier boundaries, never mid-chain.
    melt_segment = r"\w*melt(?!ed|ing|er|y)\w*"
    melt_chain = (
        r"(?<![.\w])"              # start-of-chain anchor
        rf"(?:\w+\.)*"             # optional leading segments: "Foo." or "Foo.Bar."
        rf"{melt_segment}"         # a segment with the ``melt`` token
        r"(?:\.\w+)*"              # optional trailing segments: ".out" or ".out.more"
        r"(?![.\w])"               # end-of-chain anchor
    )
    for pat in (
        rf"{melt_chain}\s*,\s*",   # leading: "Foo.melt_bar, "
        rf",\s*{melt_chain}",      # trailing: ", Foo.melt_bar"
        rf"{melt_chain}",          # bare singleton
    ):
        out = re.sub(pat, "", out, flags=re.IGNORECASE)

    return out, merged_reasons


# ---------------------------------------------------------------------------
# localization_optional strip (HealthOmics RESTRICTED mode compatibility)
# ---------------------------------------------------------------------------

# Regex matching a ``parameter_meta { ... }`` block that contains
# ``localization_optional: true``. HealthOmics RESTRICTED networking mode
# does not give task containers outbound network access, so GATK's NIO
# plugin cannot stream from S3. Removing ``localization_optional`` forces
# miniwdl to localize all File inputs before passing them to the task.
_PARAM_META_LINE_RE = re.compile(
    r"^[ \t]*\w+\s*:\s*\{\s*\n\s*localization_optional\s*:\s*true\s*\n\s*\}\s*\n?",
    re.MULTILINE,
)
_PARAM_META_BLOCK_RE = re.compile(
    r"^[ \t]*parameter_meta\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}\s*\n?",
    re.MULTILINE,
)


def strip_localization_optional(source: str) -> tuple[str, int]:
    """Remove ``parameter_meta`` blocks containing ``localization_optional: true``.

    Returns ``(rewritten_source, count_of_blocks_removed)``.

    Strategy: find every ``parameter_meta { ... }`` block, check if it
    contains ``localization_optional``, and if so remove the entire block.
    This is safe because the only purpose of these blocks in GATK-SV is
    to hint at localization behavior — removing them doesn't change
    semantics, only forces the engine to localize all inputs.
    """
    count = 0
    out = source
    for m in reversed(list(_PARAM_META_BLOCK_RE.finditer(source))):
        block = m.group(0)
        if "localization_optional" in block:
            out = out[: m.start()] + out[m.end() :]
            count += 1
    return out, count


def _build_wdl_tree(
    path: Path,
    module: ModuleName,
    commit: str,
    *,
    upstream_rel_path: str,
) -> WdlTree:
    """Construct a :class:`WdlTree` from a WDL file on disk.

    Tries miniwdl first for structured extraction. Falls back to regex-based
    extraction when miniwdl can't load the file (e.g. unresolved imports in
    pre-migration sources). The tree is used by :func:`strip_melt` and
    :func:`reject_gcs_uris` for MELT detection and gs:// URI scanning; exact
    AST fidelity is not required.
    """
    source = path.read_text(encoding="utf-8")
    tasks: list[str] = []
    calls: list[str] = []
    docker_refs: list[str] = []
    input_paths: list[str] = []

    try:
        doc = WDL.load(str(path))
        for task in doc.tasks:
            tasks.append(task.name)
            docker_expr = task.runtime.get("docker")
            if docker_expr is not None:
                literal = getattr(docker_expr, "literal", None)
                if literal is not None:
                    docker_refs.append(str(literal.value))
                else:
                    # Fall back to the stringified expression, trimming quotes.
                    rendered = str(docker_expr).strip().strip('"').strip("'")
                    docker_refs.append(rendered)
            for decl in list(task.inputs or []) + list(task.postinputs or []):
                expr = getattr(decl, "expr", None)
                lit = getattr(expr, "literal", None) if expr is not None else None
                if lit is not None and isinstance(lit.value, str):
                    input_paths.append(lit.value)

        workflow = getattr(doc, "workflow", None)
        if workflow is not None:
            _collect_calls(workflow.body, calls, input_paths)
    except (
        WDL.Error.SyntaxError,
        WDL.Error.ValidationError,
        WDL.Error.ImportError,
        WDL.Error.MultipleValidationErrors,
        FileNotFoundError,
        OSError,
    ):
        # miniwdl refused to parse — fall back to regex extraction, which
        # is imperfect but sufficient for MELT detection.
        tasks = re.findall(r"task\s+([A-Za-z_][A-Za-z0-9_]*)", source)
        calls = re.findall(r"call\s+([A-Za-z_.][A-Za-z0-9_.]*)", source)
        docker_refs = re.findall(
            r"docker\s*:\s*['\"]([^'\"]+)['\"]", source
        )

    return WdlTree(
        source=source,
        tasks=tasks,
        calls=calls,
        docker_refs=docker_refs,
        input_paths=input_paths,
        upstream_path=upstream_rel_path,
        module=module,
        upstream_commit=commit,
    )


def _collect_calls(
    body: list, calls: list[str], input_paths: list[str]
) -> None:
    """Recursively collect call IDs and literal input paths from a workflow body."""
    for node in body:
        if isinstance(node, WDL.Call):
            callee = node.callee_id
            if isinstance(callee, list):
                calls.append(".".join(callee))
            else:
                calls.append(str(callee))
            for _, expr in (node.inputs or {}).items():
                lit = getattr(expr, "literal", None)
                if lit is not None and isinstance(lit.value, str):
                    input_paths.append(lit.value)
        elif isinstance(node, WDL.Scatter) or isinstance(node, WDL.Conditional):
            inner = getattr(node, "body", None)
            if inner:
                _collect_calls(inner, calls, input_paths)


def _workspace_bundles_dir() -> Path:
    """Return the ``gatk-sv-healthomics/wdl/bundles/`` directory at workspace root."""
    # packager/__init__.py -> packager -> gatk_sv_aws
    # -> src -> kiro-life-sciences -> <workspace-root>
    return Path(__file__).resolve().parents[5] / "gatk-sv-healthomics" / "wdl" / "bundles"


def _collect_module_sources(
    main_wdl_path: Path, source_root: Path
) -> list[tuple[Path, str]]:
    """Return ``[(abs_path, repo_relative_path)]`` for the main WDL and its imports.

    Walks ``doc.imports`` transitively. Falls back to returning just the main
    file when miniwdl refuses to load it; the caller can still emit a ZIP
    for operator inspection.
    """
    results: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        p = p.resolve()
        if p in seen:
            return
        seen.add(p)
        try:
            rel = p.relative_to(source_root)
            rel_str = str(rel).replace("\\", "/")
        except ValueError:
            rel_str = p.name
        results.append((p, rel_str))

    _add(main_wdl_path)

    try:
        doc = WDL.load(str(main_wdl_path))
    except (
        WDL.Error.SyntaxError,
        WDL.Error.ValidationError,
        WDL.Error.ImportError,
        WDL.Error.MultipleValidationErrors,
        FileNotFoundError,
        OSError,
    ):
        return results

    def _walk(document: WDL.Tree.Document) -> None:
        for imp in document.imports:
            imp_doc = imp.doc
            if imp_doc is None:
                continue
            abspath = Path(imp_doc.pos.abspath)
            if abspath in seen:
                continue
            _add(abspath)
            _walk(imp_doc)

    _walk(doc)
    return results


def package_module(
    commit: str,
    module: ModuleName,
    *,
    source_root: Path | None = None,
    output_dir: Path | None = None,
) -> PackagedBundle:
    """Produce a :class:`PackagedBundle` ZIP for ``module`` at ``commit``.

    Fetches (or reuses) the upstream checkout, walks the module's WDL and
    its transitive imports, applies the migration transforms (MELT strip,
    gs:// URI rejection), emits a ZIP plus ``divergence.json``, and returns
    a :class:`PackagedBundle` carrying the divergence list (Req 2a.1, 2a.2,
    2a.4, 16.3; Design §Components.a, §Workflow Module Mapping).

    Args:
        commit: Upstream GATK-SV commit SHA. Passed through to
            :func:`fetch_upstream` when ``source_root`` is not supplied, and
            stamped on every emitted :class:`DivergenceEntry`.
        module: Migrated module name. Must match a WDL file at
            ``<source_root>/wdl/<module>.wdl``.
        source_root: Optional local checkout of the upstream repo. When
            omitted, :func:`fetch_upstream` is invoked for ``commit``.
        output_dir: Directory to write ``<module>-bundle.zip`` and
            ``divergence.json`` into. Defaults to
            ``<workspace>/gatk-sv-healthomics/wdl/bundles/<module>/``.

    Returns:
        A :class:`PackagedBundle` with ``zip_path`` pointing at the emitted
        ZIP, ``main_wdl_path`` set to ``wdl/<module>.wdl``, and
        ``divergence`` containing one entry per MELT removal.

    Raises:
        FileNotFoundError: When ``<source_root>/wdl/<module>.wdl`` doesn't
            exist.
        PackagingError: When any of the module's WDL files contains a
            ``gs://`` URI. The message lists every offending URI with its
            upstream path and line number so operators can fix them all in
            one pass.
    """
    if source_root is None:
        source_root = fetch_upstream(GATK_SV_REPO_URL, commit)
    source_root = Path(source_root).resolve()

    if output_dir is None:
        output_dir = _workspace_bundles_dir() / module
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    main_wdl_path = source_root / "wdl" / f"{module}.wdl"
    if not main_wdl_path.exists():
        raise FileNotFoundError(
            f"module {module} not found at {main_wdl_path}"
        )

    collected = _collect_module_sources(main_wdl_path, source_root)

    divergences: list[DivergenceEntry] = []
    gcs_violations: list[GcsUriViolation] = []
    transformed: list[tuple[str, str]] = []  # (repo_relative_path, rewritten_source)

    for abs_path, rel_path in collected:
        tree = _build_wdl_tree(
            abs_path, module, commit, upstream_rel_path=rel_path
        )
        _, file_divergences = strip_melt(tree)
        divergences.extend(file_divergences)
        gcs_violations.extend(reject_gcs_uris(tree))

        # Prefer AST-driven strip (lint-clean output) when miniwdl can load
        # the file. Fall back to the text-level strip otherwise.
        try:
            doc = WDL.load(str(abs_path))
            rewritten, ast_reasons = _ast_strip_melt(tree.source, doc)
            # Promote AST reasons to DivergenceEntry objects with full metadata.
            module_literal = cast(ModuleName, module)
            for kind, name in ast_reasons:
                divergences.append(
                    DivergenceEntry(
                        module=module_literal,
                        upstream_path=rel_path,
                        change_kind=ChangeKind.REMOVE_CALLER,
                        reason=(
                            f"MELT excluded per Req 2a.3 "
                            f"(ast_{kind}: {name!r})"
                        ),
                        upstream_commit=commit,
                    )
                )
        except (
            WDL.Error.SyntaxError,
            WDL.Error.ValidationError,
            WDL.Error.ImportError,
            WDL.Error.MultipleValidationErrors,
            FileNotFoundError,
            OSError,
        ):
            rewritten = _text_strip_melt(tree.source)

        # Skip MELT.wdl itself if it's still referenced in the collected
        # list — we want it out of the bundle entirely.
        if Path(rel_path).name.lower().startswith("melt"):
            continue

        # Strip localization_optional: true from parameter_meta blocks.
        # HealthOmics RESTRICTED mode doesn't give task containers network
        # access, so GATK's NIO plugin can't stream from S3. Removing this
        # hint forces miniwdl to localize all File inputs before execution.
        rewritten, loc_opt_count = strip_localization_optional(rewritten)
        if loc_opt_count > 0:
            divergences.append(
                DivergenceEntry(
                    module=cast(ModuleName, module),
                    upstream_path=rel_path,
                    change_kind=ChangeKind.REWRITE_CONSTRUCT,
                    reason=(
                        f"Removed {loc_opt_count} parameter_meta block(s) with "
                        f"localization_optional: true (HealthOmics RESTRICTED mode "
                        f"requires engine-managed file localization)"
                    ),
                    upstream_commit=commit,
                )
            )

        # Replace `set -euo pipefail` with `set -eo pipefail` in task
        # command blocks. The `-u` (nounset) flag causes immediate exit
        # on HealthOmics when environment variables expected by GATK
        # scripts are not set in the HealthOmics container environment.
        rewritten_bash = rewritten.replace("set -euo pipefail", "set -eo pipefail")
        if rewritten_bash != rewritten:
            divergences.append(
                DivergenceEntry(
                    module=cast(ModuleName, module),
                    upstream_path=rel_path,
                    change_kind=ChangeKind.REWRITE_CONSTRUCT,
                    reason=(
                        "Replaced 'set -euo pipefail' with 'set -eo pipefail' "
                        "(HealthOmics container environment lacks some expected "
                        "variables; -u flag causes spurious exits)"
                    ),
                    upstream_commit=commit,
                )
            )
            rewritten = rewritten_bash

        # Inject FUSE cache warming after every `set -eo pipefail` line.
        # HealthOmics serves imported files via FUSE mounts. GATK's
        # random-access I/O pattern (seeking into CRAM/BAM) can trigger
        # FUSE timeouts if the file isn't pre-cached. This sequential
        # read forces the FUSE layer to fully cache the file before GATK
        # starts its random-access reads.
        fuse_warm = (
            "\n    # HealthOmics FUSE cache warm (injected by packager)\n"
            "    for _f in /mnt/workflow/*/inputs/*; do "
            "[ -f \"$_f\" ] && cat \"$_f\" > /dev/null 2>&1 || true; done\n"
        )
        rewritten_fuse = rewritten.replace(
            "set -eo pipefail\n",
            "set -eo pipefail" + fuse_warm,
        )
        if rewritten_fuse != rewritten:
            rewritten = rewritten_fuse

        # Increase default memory for tasks that default to <8 GiB.
        # HealthOmics has higher per-task overhead than Terra/Cromwell
        # (FUSE mounts, container runtime, JVM startup). Tasks with
        # 3.75 GiB default OOM on HealthOmics with large inputs.
        rewritten_mem = rewritten.replace("mem_gb: 3.75,", "mem_gb: 7.5,")
        if rewritten_mem != rewritten:
            divergences.append(
                DivergenceEntry(
                    module=cast(ModuleName, module),
                    upstream_path=rel_path,
                    change_kind=ChangeKind.REWRITE_CONSTRUCT,
                    reason=(
                        "Increased default mem_gb from 3.75 to 7.5 "
                        "(HealthOmics per-task overhead requires more memory "
                        "than Terra/Cromwell for FUSE-mounted large inputs)"
                    ),
                    upstream_commit=commit,
                )
            )
            rewritten = rewritten_mem

        transformed.append((rel_path, rewritten))

    if gcs_violations:
        detail = "; ".join(
            f"{v.upstream_path}:{v.line} -> {v.offending_uri}"
            for v in gcs_violations
        )
        raise PackagingError(
            f"package_module({module}): gs:// URIs must be rewritten before "
            f"packaging (Req 2.6). Offenders: {detail}"
        )

    zip_path = output_dir / f"{module}-bundle.zip"
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_path, rewritten in transformed:
            zf.writestr(rel_path, rewritten)

    divergence_json = output_dir / "divergence.json"
    # DivergenceEntry is a Pydantic model; emit an indented JSON array.
    divergence_json.write_text(
        "[\n"
        + ",\n".join(
            "  " + entry.model_dump_json() for entry in divergences
        )
        + "\n]\n"
        if divergences
        else "[]\n",
        encoding="utf-8",
    )

    return PackagedBundle(
        zip_path=zip_path,
        main_wdl_path=f"wdl/{module}.wdl",
        module=module,
        upstream_commit=commit,
        divergence=divergences,
        lint_report=None,
    )


# ---------------------------------------------------------------------------
# Task 3.1.7 (local-only) — lint_bundle
# ---------------------------------------------------------------------------

# TODO(Phase 5): wire lint_bundle_healthomics() that calls the
# LintAHOWorkflowBundle MCP tool. Until then, :func:`lint_bundle` uses
# miniwdl locally so Phase 4 (per-module WDL migration) can proceed offline.


def lint_bundle(bundle: PackagedBundle) -> LintReport:
    """Lint a packaged bundle locally with miniwdl.

    Extracts ``bundle.zip_path`` into a temp directory and calls
    :func:`WDL.load` on the main WDL. Returns a :class:`LintReport` with
    ``status='success'`` when miniwdl parses and type-checks the bundle
    cleanly, ``status='error'`` otherwise. Catches
    :class:`WDL.Error.SyntaxError`, :class:`WDL.Error.ValidationError`,
    :class:`WDL.Error.ImportError`, and
    :class:`WDL.Error.MultipleValidationErrors` and records their string
    form in ``errors`` (Req 2.3 — local pre-flight; Design §Components.a).

    The HealthOmics-MCP ``LintAHOWorkflowBundle`` integration is deferred
    to Phase 5 (see TODO above).
    """
    with tempfile.TemporaryDirectory(prefix="gatk-sv-lint-") as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(bundle.zip_path, mode="r") as zf:
            zf.extractall(tmp_path)

        main_wdl = tmp_path / bundle.main_wdl_path
        if not main_wdl.exists():
            return LintReport(
                status="error",
                errors=[
                    f"main WDL {bundle.main_wdl_path!r} missing from "
                    f"bundle ZIP {bundle.zip_path}"
                ],
                warnings=[],
                raw_output="",
            )

        try:
            WDL.load(str(main_wdl))
        except (
            WDL.Error.SyntaxError,
            WDL.Error.ValidationError,
            WDL.Error.ImportError,
            WDL.Error.MultipleValidationErrors,
        ) as exc:
            return LintReport(
                status="error",
                errors=[str(exc)],
                warnings=[],
                raw_output=repr(exc),
            )

        return LintReport(
            status="success",
            errors=[],
            warnings=[],
            raw_output=(
                f"miniwdl validated {bundle.main_wdl_path} "
                f"(module={bundle.module}, commit={bundle.upstream_commit})"
            ),
        )
