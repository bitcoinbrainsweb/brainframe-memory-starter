"""Pre-dispatch manifest lint -- admin-foreman-predispatch-manifest-lint-v1.

A pure, zero-LLM, zero-builder-token structural gate. Before a bundle is dispatched
it verifies that each resolved spec carries the structural elements a builder needs:
a hydrated (self-contained) spec body, parseable acceptance checks, and a
scope / test-slice boundary. A spec that fails is refused; the report names EVERY
missing or malformed field in one pass (violations are collected, never fail-on-first).

This module is a leaf (stdlib only). :class:`~harness.foreman.scripts.bundle.BundleIntake`
calls :func:`lint_spec` / :func:`lint_bundle` after the approval + attestation gates.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

# Fields hashed to identify a spec's structural content. Volatile row metadata
# (timestamps, ids) is excluded so the hash is stable across fetches.
_CANON_KEYS: tuple[str, ...] = (
    "slug", "body", "scope", "test_slice", "acceptance",
    "depends_on", "independent",
    "requirements_approved", "design_approved", "tasks_approved",
)

# A body that is really a pointer/reference, not hydrated content. PR 138 made
# hydration host-side; the lint verifies it actually produced content.
_POINTER_RE = re.compile(
    r"""^\s*(
        https?://\S+                # a URL
        | [\w./-]+\.md              # a bare '*.md' path pointer
        | see\s+\S+                 # 'see <ref>'
        | ref(?:erence)?:\s*\S+     # 'ref: <x>'
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# Acceptance markers we can parse out of a body when there is no acceptance field.
_AC_MARKERS = (
    re.compile(r"\bgiven\b.+\bwhen\b.+\bthen\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"^\s*[-*]\s+.+", re.MULTILINE),           # a bulleted list item
    re.compile(r"^\s*#{1,6}\s*acceptance", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bR\d+\.AC\d+\b"), # style tokens
)

# A hydrated body must carry at least this much real content to be self-contained.
_MIN_BODY_CHARS = 40


@dataclass
class LintViolation:
    field: str
    message: str


@dataclass
class LintResult:
    clean: bool
    violations: list[LintViolation] = field(default_factory=list)
    spec_hash: str = ""
    scope: str | None = None
    slug: str = ""

    @property
    def reason(self) -> str:
        """One-line report naming every violation (for an ExclusionRecord)."""
        body = "; ".join(f"{v.field}: {v.message}" for v in self.violations)
        return f"manifest-lint refused: {body}" if body else "manifest-lint: clean"


@dataclass
class BundleLintResult:
    clean: bool
    results: list[LintResult] = field(default_factory=list)
    bundle_hash: str = ""


def _canonical(spec: dict) -> dict:
    return {k: spec.get(k) for k in _CANON_KEYS}


def _spec_hash(spec: dict) -> str:
    payload = json.dumps(_canonical(spec), sort_keys=True, default=str, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_scope(spec: dict) -> str | None:
    """scope OR test_slice satisfies the boundary requirement."""
    for key in ("scope", "test_slice"):
        val = spec.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return None


def _body_is_pointer(body: str) -> bool:
    stripped = body.strip()
    if len(stripped) < _MIN_BODY_CHARS and _POINTER_RE.match(stripped):
        return True
    return bool(_POINTER_RE.match(stripped))


def _has_ac_markers(text: str) -> bool:
    return any(rx.search(text) for rx in _AC_MARKERS)


def _acceptance_ok(spec: dict) -> bool:
    """Acceptance is parseable if the acceptance field is a non-empty list of items,
    or a string carrying recognizable acceptance markers, OR (when the field is
    absent) the body carries such markers. A present-but-shapeless value (an int, an
    empty list, or a prose string with no markers) is unparseable."""
    ac = spec.get("acceptance")
    if ac is not None:
        if isinstance(ac, list) and ac and all(isinstance(x, (str, dict)) for x in ac):
            return True
        if isinstance(ac, str) and ac.strip() and _has_ac_markers(ac):
            return True
        # present but malformed (int, empty list, marker-less prose, etc.)
        return False
    body = spec.get("body")
    if isinstance(body, str) and _has_ac_markers(body):
        return True
    return False


def lint_spec(spec: dict) -> LintResult:
    """Pure structural lint of one resolved spec. Collects all violations in one pass.

    Zero LLM calls, zero builder tokens. Returns a LintResult carrying the spec hash
    and the resolved scope (for the builder scope channel)."""
    violations: list[LintViolation] = []
    slug = str(spec.get("slug") or "")

    # 1. Hydrated, self-contained spec body.
    body = spec.get("body")
    if not isinstance(body, str) or not body.strip():
        violations.append(LintViolation("body", "missing hydrated spec body"))
    elif len(body.strip()) < _MIN_BODY_CHARS or _body_is_pointer(body):
        violations.append(LintViolation(
            "body",
            "spec body is a pointer/reference rather than self-contained hydrated "
            "content (host-side hydration must inline the real spec)",
        ))

    # 2. Parseable acceptance checks.
    if not _acceptance_ok(spec):
        violations.append(LintViolation(
            "acceptance",
            "no parseable acceptance checks (need an acceptance list or "
            "Given/When/Then markers in the body)",
        ))

    # 3. Scope / test-slice boundary.
    scope = _resolve_scope(spec)
    if scope is None:
        violations.append(LintViolation(
            "scope", "missing scope/test-slice field (build boundary)",
        ))

    return LintResult(
        clean=not violations,
        violations=violations,
        spec_hash=_spec_hash(spec),
        scope=scope,
        slug=slug,
    )


def lint_bundle(specs: list[dict]) -> BundleLintResult:
    """Lint every spec in a bundle and compute the bundle hash (sha256 of the
    canonical serialization of all specs in order)."""
    results = [lint_spec(s) for s in specs]
    payload = json.dumps(
        [_canonical(s) for s in specs], sort_keys=True, default=str, ensure_ascii=True
    )
    bundle_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return BundleLintResult(
        clean=all(r.clean for r in results),
        results=results,
        bundle_hash=bundle_hash,
    )
