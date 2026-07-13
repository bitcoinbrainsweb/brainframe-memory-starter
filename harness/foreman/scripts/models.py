"""Model constants, enums, and dataclasses for the build and verify phases.

Core rule: the builder and the verifier must be different model families, so a
model never grades its own family's output. Every agent invocation declares its
exact model string.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# Replace these with your own two model strings. The ONLY hard rule is that the
# builder and the verifier resolve to DIFFERENT families (see get_family and
# assert_different_family below), so the verifier is never the same family that
# produced the code. Encode the family as a token inside the string so get_family
# can detect it (here "family_a" / "family_b").
BUILDER_MODEL = "YOUR_BUILD_MODEL-family_a"
VERIFIER_MODEL = "YOUR_VERIFY_MODEL-family_b"

SESSION_ID = "foreman-build"


class ModelFamily(str, Enum):
    FAMILY_A = "family_a"
    FAMILY_B = "family_b"
    FAMILY_C = "family_c"
    OTHER = "other"


class SameFamilyError(Exception):
    """Raised when builder and verifier share the same model family."""


# Map a model string to its family by substring. Replace these rules with the
# family tokens your own providers use; the point is that two models from the
# same family collapse to one, so a same-family builder/verifier pair is rejected.
def get_family(model: str) -> ModelFamily:
    m = model.lower()
    if "family_a" in m:
        return ModelFamily.FAMILY_A
    if "family_b" in m:
        return ModelFamily.FAMILY_B
    if "family_c" in m:
        return ModelFamily.FAMILY_C
    return ModelFamily.OTHER


def assert_different_family(builder_model: str, verifier_model: str) -> None:
    """Enforce that the verifier is a different family from the builder.

    Raises SameFamilyError before any agent call if families match.
    """
    bf = get_family(builder_model)
    vf = get_family(verifier_model)
    if bf == vf:
        raise SameFamilyError(
            f"Builder model '{builder_model}' (family={bf.value}) and verifier model "
            f"'{verifier_model}' (family={vf.value}) share the same family. "
            "The verifier must be a different family from the builder."
        )


# ---------------------------------------------------------------------------
# Phase 2 additions
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    QUEUED = "queued"
    BUILDING = "building"
    VERIFYING = "verifying"
    CI_GATING = "ci-gating"
    MERGING = "merging"
    COMMITTED = "committed"
    PARKED = "parked"
    DEPENDENT_HALTED = "dependent-halted"


# Status transition matrix (write-ahead: DB write precedes the action it names)
# verifying -> ci-gating -> merging (pass) | parked (fail/timeout/api-error)
VALID_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.QUEUED: {TaskStatus.BUILDING, TaskStatus.DEPENDENT_HALTED, TaskStatus.PARKED},
    TaskStatus.BUILDING: {TaskStatus.VERIFYING, TaskStatus.PARKED},
    TaskStatus.VERIFYING: {TaskStatus.CI_GATING, TaskStatus.MERGING, TaskStatus.BUILDING, TaskStatus.PARKED},
    TaskStatus.CI_GATING: {TaskStatus.MERGING, TaskStatus.PARKED},
    TaskStatus.MERGING: {TaskStatus.COMMITTED, TaskStatus.PARKED},
}

TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.COMMITTED,
    TaskStatus.PARKED,
    TaskStatus.DEPENDENT_HALTED,
})


class ForemanRunLocked(Exception):
    """Raised when a single-flight guard blocks a new run due to an in-flight run."""


class InvalidTransition(Exception):
    """Raised when TaskQueue.advance() receives an illegal status move."""


class ForemanIntegrityError(Exception):
    """Raised when a task would reach 'committed' without a build_run_specs
    ledger row carrying verify_result='PASS' (F1 invariant). Mirrors the
    DB-level trg_foreman_committed_requires_pass trigger so the guard holds in
    tests and in-process, not only against live Postgres."""


class CycleError(Exception):
    """Raised by BundleIntake.resolve() when depends_on graph contains a cycle."""

    def __init__(self, cyclic_slugs: list[str]) -> None:
        self.cyclic_slugs = cyclic_slugs
        super().__init__(f"Dependency cycle detected among: {cyclic_slugs}")


# ---------------------------------------------------------------------------
# Anti-slop static lint knobs
#
# Defaults live here so AntislopConfig is the single source of truth. The
# antislop_lint module binds its module-level defaults from an AntislopConfig()
# instance; that module imports these names, so models must never import it back.
# ---------------------------------------------------------------------------

# Case-insensitive substring markers matched on ADDED lines only.
_DEFAULT_SLOP_PHRASES: tuple[str, ...] = (
    "in a real",
    "in production you would",
    "for now, we just",
    "placeholder implementation",
    "TODO: implement",
    "left as an exercise",
)

# Regexes matched against the basename of ADDED files. These catch shell
# redirection fragments that get committed as accidental junk filenames.
_DEFAULT_JUNK_BASENAME_REGEXES: tuple[str, ...] = (
    r"^&\d*$",
    r"^\]\*+$",
    r"^-$",
    r"^\d>&\d$",
    r"^2>&1$",
)


@dataclass
class AntislopConfig:
    slop_phrases: tuple[str, ...] = _DEFAULT_SLOP_PHRASES
    junk_basename_regexes: tuple[str, ...] = _DEFAULT_JUNK_BASENAME_REGEXES
    comment_only_threshold: float = 0.90


@dataclass
class BundleConfig:
    spec_slugs: list[str]
    repo: str
    base_ref: str
    remote_url: str
    working_dir: Path
    builder_model: str = BUILDER_MODEL
    verifier_model: str = VERIFIER_MODEL
    session_id: str = SESSION_ID
    # True when the run targets a repo other than the host checkout and operates
    # in an ephemeral clone. The substance-delta base is then resolved via
    # merge-base inside that clone rather than trusted as-passed (Bug 2).
    is_xrepo: bool = False
    # Anti-slop static lint configuration.
    antislop: AntislopConfig = field(default_factory=AntislopConfig)


@dataclass
class InvariantResult:
    ok: bool
    violation_count: int
    reason: str


@dataclass
class PreconditionResult:
    ok: bool
    actual_model: str
    reason: str


@dataclass
class ExclusionRecord:
    spec_slug: str
    reason: str


@dataclass
class ParkedRecord:
    spec_slug: str
    park_reason: str
    failure_trail: dict = field(default_factory=dict)


@dataclass
class HaltRecord:
    spec_slug: str
    halted_because: str  # which parked slug caused this halt


@dataclass
class HaltChain:
    parked_slug: str
    halted_slugs: list[str] = field(default_factory=list)


@dataclass
class SubstanceResult:
    aggregate_verdict: str
    blocking_criteria: list
    spec_slug: str = ""
    run_id: str = ""
    per_criterion: list = field(default_factory=list)
    error: str = ""


@dataclass
class LintFinding:
    # One of "slop_phrase", "junk_file", "comment_only_diff".
    check: str
    file: str
    line: int | None
    detail: str


@dataclass
class LintResult:
    verdict: str  # "PASS" | "FAIL"
    findings: list = field(default_factory=list)
    # Set only when the fail-closed wrapper caught an internal exception.
    error: str = ""
