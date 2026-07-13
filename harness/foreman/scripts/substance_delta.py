"""Deliverable-delta substance gate.

Measures whether a run's commit set actually produced substantive product, as
opposed to a hollow set (docs/build-log only, or pre-existing test scaffolding
and import-only edits). Deterministic and mechanical.

This is DISTINCT from the H6 test-substance discriminator
(``foreman_substance_discriminator.py``), which measures TEST quality
(coverage / mutation / negative-control). That gate cannot catch a commit that
touches only a build-log entry, because it never asks whether the spec's
*deliverables* materialized. The incident (fm-20260702-1841-50cc8a) committed
a test ``__init__.py`` + import fixes and a build-log line; both must FAIL here.

The signal is the DELTA of this run's commits against ``base_sha`` -- files that
did not exist at base, or existing product files that were materially changed --
never repo-wide state and never test pass/fail.
"""
from __future__ import annotations

import fnmatch
import os
import posixpath
import re
from dataclasses import dataclass, field
from typing import Callable

# Harness scaffolding provisioned into a build working tree (trace emitter +
# ruleforge linter). The harness injects these so a builder session can source
# them; they are never product and must never be committed by a builder. Single
# source of truth shared with git_ops (commit guard + .git/info/exclude
# injection) so the two exclusions never drift (Bug 4).
HARNESS_SCAFFOLD_PATHS: tuple[str, ...] = (
    "scripts/emit_trace.sh",
    "scripts/ruleforge_check.py",
)

# Paths that never count as a substantive deliverable, no matter what changed.
NON_SUBSTANTIVE_GLOBS: tuple[str, ...] = (
    "docs/*",
    "docs/**",
    "*build-log*",
    "**/build-log*",
    "CHANGELOG*",
    "**/CHANGELOG*",
    "data/*",
    "data/**",
    "*.log",
    "**/*.log",
) + HARNESS_SCAFFOLD_PATHS

# Added lines that carry no product: imports, blanks, comments, bare docstrings.
_INERT_LINE_RE = re.compile(
    r"""^\s*(
        import\s+\w
        | from\s+[\w.]+\s+import
        | \#
        | \"\"\"
        | '''
        | pass\b
        | from\s+__future__
    )""",
    re.VERBOSE,
)


@dataclass
class ChangedFile:
    """One file's change across base_sha..head_sha."""
    path: str
    status: str  # 'A' added, 'M' modified, 'D' deleted, 'R' renamed
    added_content_lines: list[str] = field(default_factory=list)


@dataclass
class DeltaResult:
    # "PASS" | "FAIL" | "INCONCLUSIVE". INCONCLUSIVE means the discriminator
    # evaluated nothing (empty considered set / unresolvable base): absence of
    # evidence, not evidence of a hollow commit. It never vetoes a verifier PASS.
    verdict: str
    substantive: list[str] = field(default_factory=list)
    considered: list[str] = field(default_factory=list)
    reason: str = ""


def _norm(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/"))


def _match_any(path: str, globs: tuple[str, ...] | list[str]) -> bool:
    p = _norm(path)
    base = posixpath.basename(p)
    for g in globs:
        if fnmatch.fnmatch(p, g) or fnmatch.fnmatch(base, g):
            return True
    return False


def is_non_substantive_path(path: str) -> bool:
    return _match_any(path, NON_SUBSTANTIVE_GLOBS)


def _is_scaffold(cf: ChangedFile) -> bool:
    """A file that carries no real product: an ``__init__.py`` (or similar) whose
    only added lines are imports/blanks/comments."""
    name = posixpath.basename(_norm(cf.path))
    real = [ln for ln in cf.added_content_lines if ln.strip() and not _INERT_LINE_RE.match(ln)]
    if name == "__init__.py" and not real:
        return True
    return False


def _is_import_only(cf: ChangedFile) -> bool:
    """True when every added content line is inert (import/blank/comment)."""
    real = [ln for ln in cf.added_content_lines if ln.strip() and not _INERT_LINE_RE.match(ln)]
    # Only meaningful for modifications; a brand-new file with only inert lines
    # is handled by scaffold detection.
    return len(cf.added_content_lines) > 0 and not real


def is_substantive(cf: ChangedFile) -> bool:
    if cf.status == "D":
        return False
    if is_non_substantive_path(cf.path):
        return False
    if _is_scaffold(cf):
        return False
    if cf.status in ("A", "R"):
        # New/renamed product file with at least one real line.
        real = [ln for ln in cf.added_content_lines
                if ln.strip() and not _INERT_LINE_RE.match(ln)]
        # If we captured no patch lines at all (added file, empty diff capture),
        # treat a non-inert path as substantive by default (fail-open only for
        # genuinely new non-doc files).
        return bool(real) or not cf.added_content_lines
    # Modified existing file: substantive only if it carries real changes.
    return not _is_import_only(cf)


def evaluate_deliverable_delta(
    changed_files: list[ChangedFile],
    required_deliverables: list[str] | None = None,
) -> DeltaResult:
    """PASS iff the commit set contains at least one substantive deliverable.

    ``required_deliverables`` (optional path globs a spec declares as its product)
    further constrains the substantive set: when given, a substantive change must
    also match one of them. When absent, any substantive non-doc change passes.
    """
    considered = [cf.path for cf in changed_files]
    # An empty considered set means the discriminator saw nothing to judge --
    # absence of evidence, not a hollow commit. Emit INCONCLUSIVE so it never
    # vetoes a verifier PASS (Bug 3). The genuine hollow-commit FAIL below still
    # fires whenever considered is non-empty but its substantive subset is empty.
    if not considered:
        return DeltaResult(
            "INCONCLUSIVE", [], [],
            "empty-considered-set (discriminator evaluated no changed files)",
        )
    subs = [cf.path for cf in changed_files if is_substantive(cf)]
    if required_deliverables:
        subs = [p for p in subs if _match_any(p, required_deliverables)]
        if not subs:
            return DeltaResult(
                "FAIL", [], considered,
                "no-change-matches-required-deliverables",
            )
        return DeltaResult("PASS", subs, considered, "ok")
    if not subs:
        return DeltaResult(
            "FAIL", [], considered,
            "empty-deliverable-delta (only docs/build-log or pre-existing scaffolding)",
        )
    return DeltaResult("PASS", subs, considered, "ok")


def resolve_delta_base(
    working_dir,
    builder_ref: str,
    default_branch: str,
    git_fn: Callable | None = None,
) -> str | None:
    """Resolve the correct substance-delta base inside a (possibly ephemeral) tree.

    Returns the merge-base of ``builder_ref`` and ``default_branch`` -- the point
    the builder branched from the target's default branch, which is the correct
    base even when the working tree is a cross-repo clone whose fetched refs make
    a naive ``base_sha..head`` diff empty (Bug 2). Returns ``None`` when the base
    cannot be resolved (either ref absent, no common ancestor); the caller treats
    that as an evaluation error -> INCONCLUSIVE, never a FAIL.
    """
    if git_fn is None:
        from .git_ops import _git as git_fn  # type: ignore
    try:
        res = git_fn(
            ["merge-base", builder_ref, default_branch],
            cwd=working_dir, capture_output=True, text=True,
        )
    except Exception:
        return None
    if getattr(res, "returncode", 1) != 0:
        return None
    base = (getattr(res, "stdout", "") or "").strip()
    return base or None


def delta_from_git(
    working_dir,
    base_sha: str,
    head_sha: str,
    git_fn: Callable | None = None,
) -> list[ChangedFile]:
    """Build the ChangedFile list from local git for base_sha..head_sha.

    ``git_fn(args, cwd, capture_output=True)`` defaults to harness.foreman.scripts.git_ops._git.
    Uses ``--unified=0`` so only genuinely changed content lines are inspected.
    """
    if git_fn is None:
        from .git_ops import _git as git_fn  # type: ignore

    rng = f"{base_sha}..{head_sha}"
    name_status = git_fn(["diff", "--name-status", rng], cwd=working_dir,
                         capture_output=True, text=True)
    status_lines = (getattr(name_status, "stdout", "") or "").splitlines()

    statuses: dict[str, str] = {}
    for ln in status_lines:
        parts = ln.split("\t")
        if len(parts) < 2:
            continue
        code = parts[0][:1]
        path = parts[-1]
        statuses[_norm(path)] = code

    files: list[ChangedFile] = []
    for path, code in statuses.items():
        added: list[str] = []
        if code != "D":
            patch = git_fn(
                ["diff", "--unified=0", rng, "--", path],
                cwd=working_dir, capture_output=True, text=True,
            )
            for pl in (getattr(patch, "stdout", "") or "").splitlines():
                if pl.startswith("+") and not pl.startswith("+++"):
                    added.append(pl[1:])
        files.append(ChangedFile(path=path, status=code, added_content_lines=added))
    return files
