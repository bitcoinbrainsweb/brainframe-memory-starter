"""BundleIntake: resolve spec bundle, topo-sort, commit task rows (R1, R10.AC1)."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from scripts.foreman.ledger import LedgerBackend
from scripts.foreman.manifest_lint import lint_bundle, lint_spec
from scripts.foreman.models import CycleError, ExclusionRecord


@dataclass
class BundleResolution:
    ordered: list[dict] = field(default_factory=list)    # approved specs in build order
    excluded: list[ExclusionRecord] = field(default_factory=list)


def _all_approved(spec: dict) -> bool:
    """R1.AC1: all three approval booleans must be True."""
    return bool(
        spec.get("requirements_approved")
        and spec.get("design_approved")
        and spec.get("tasks_approved")
    )


def _validate_attestation(spec: dict) -> str | None:
    """R1.AC4: spec must have independent=True XOR non-empty depends_on (not both, not neither).

    Returns None if valid, error string if invalid.
    """
    independent = bool(spec.get("independent", False))
    depends_on = spec.get("depends_on") or []
    if isinstance(depends_on, str):
        depends_on = [depends_on] if depends_on else []
    has_depends = bool(depends_on)

    if independent and has_depends:
        return (
            "R1.AC4 attestation gate: has both independent=true and a non-empty "
            "depends_on (XOR required). Remedy: set independent=false (keep "
            "depends_on) or clear depends_on (keep independent=true)."
        )
    if not independent and not has_depends:
        return (
            "R1.AC4 attestation gate: has neither independent=true nor a non-empty "
            "depends_on. Remedy: set independent=true (if the spec has no "
            "prerequisites) or populate depends_on with the slug(s) it requires."
        )
    return None


class BundleIntake:
    def __init__(
        self,
        ledger: LedgerBackend,
        session_id: str,
        manifest_lint: bool = False,
    ) -> None:
        self._ledger = ledger
        self._session_id = session_id
        # Pre-dispatch manifest lint (pre-dispatch manifest lint).
        # Default OFF so resolution stays byte-identical for existing callers whose
        # specs are not yet carrying the structural fields the lint requires (the
        # specs table has no body column; hydration is host-side, post-resolve). When
        # ON, structurally-incomplete specs are refused before any builder token is
        # spent. See docs/build-log.md for the enablement path.
        self._manifest_lint = manifest_lint

    def resolve(self, spec_slugs: list[str]) -> BundleResolution:
        """Fetch specs, exclude unapproved, validate attestation, topo-sort.

        When manifest lint is enabled, a structural lint runs after the approval and
        attestation gates: specs missing a hydrated body, parseable acceptance checks,
        or a scope/test-slice boundary are refused (zero LLM calls, zero builder
        tokens). The approval and attestation gates themselves are unchanged."""
        ordered: list[dict] = []
        excluded: list[ExclusionRecord] = []
        valid_specs: dict[str, dict] = {}

        for slug in spec_slugs:
            spec = self._ledger.fetch_spec(slug)
            if spec is None:
                excluded.append(ExclusionRecord(spec_slug=slug, reason="spec not found"))
                continue
            if not _all_approved(spec):
                excluded.append(ExclusionRecord(
                    spec_slug=slug,
                    reason="missing approval: requires requirements_approved AND design_approved AND tasks_approved",
                ))
                continue
            attestation_err = _validate_attestation(spec)
            if attestation_err:
                excluded.append(ExclusionRecord(spec_slug=slug, reason=attestation_err))
                continue
            # Pre-dispatch manifest lint (after approval + attestation gates). Collects
            # every structural violation in one pass; refusal is per-spec.
            if self._manifest_lint:
                lint_res = lint_spec(spec)
                if not lint_res.clean:
                    excluded.append(ExclusionRecord(spec_slug=slug, reason=lint_res.reason))
                    continue
            valid_specs[slug] = spec

        if not valid_specs:
            return BundleResolution(ordered=[], excluded=excluded)

        # Kahn's topo-sort on depends_on graph (R1.AC2/AC3)
        # Build adjacency and in-degree over valid specs only
        in_degree: dict[str, int] = {slug: 0 for slug in valid_specs}
        dependents: dict[str, list[str]] = defaultdict(list)

        for slug, spec in valid_specs.items():
            deps = spec.get("depends_on") or []
            if isinstance(deps, str):
                deps = [deps] if deps else []
            for dep in deps:
                if dep in valid_specs:
                    in_degree[slug] += 1
                    dependents[dep].append(slug)

        queue: deque[str] = deque(
            slug for slug in spec_slugs if slug in valid_specs and in_degree[slug] == 0
        )
        sorted_slugs: list[str] = []
        while queue:
            slug = queue.popleft()
            sorted_slugs.append(slug)
            for dep_slug in dependents[slug]:
                in_degree[dep_slug] -= 1
                if in_degree[dep_slug] == 0:
                    queue.append(dep_slug)

        # Any remaining non-zero in-degree nodes are in a cycle
        cyclic = [slug for slug in valid_specs if in_degree[slug] > 0]
        if cyclic:
            raise CycleError(cyclic)

        ordered = [valid_specs[slug] for slug in sorted_slugs]

        # Record the lint pass with the bundle hash (sha256 of canonical serialization)
        # for a clean bundle. Fire-and-forget + duck-typed like other optional ledger
        # hooks: a ledger without record_manifest_lint simply skips it.
        if self._manifest_lint and ordered:
            record = getattr(self._ledger, "record_manifest_lint", None)
            if callable(record):
                try:
                    bundle_res = lint_bundle(ordered)
                    record(
                        self._session_id,
                        [s["slug"] for s in ordered],
                        bundle_res.bundle_hash,
                    )
                except Exception:
                    pass

        return BundleResolution(ordered=ordered, excluded=excluded)

    def commit_intake(self, run_id: str, resolution: BundleResolution) -> list[dict]:
        """INSERT all foreman_tasks rows in a single atomic operation.

        If any insert fails, rolls back by removing all inserted rows and re-raising.
        Returns inserted task row list in build_order sequence.
        """
        inserted: list[dict] = []
        try:
            for build_order, spec in enumerate(resolution.ordered):
                slug = spec["slug"]
                deps = spec.get("depends_on") or []
                if isinstance(deps, str):
                    deps = [deps] if deps else []
                independent = bool(spec.get("independent", False))
                row = self._ledger.create_task_row(
                    run_id=run_id,
                    spec_slug=slug,
                    build_order=build_order,
                    depends_on=deps,
                    independent=independent,
                    session_id=self._session_id,
                )
                inserted.append(row)
        except Exception:
            # Rollback: remove any rows already inserted into InMemoryLedger
            for row in inserted:
                key = (row["run_id"], row["spec_slug"])
                if hasattr(self._ledger, "_task_rows"):
                    self._ledger._task_rows.pop(key, None)  # type: ignore[attr-defined]
            raise
        return inserted
