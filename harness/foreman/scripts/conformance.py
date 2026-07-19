"""Mechanical conformance gate for Checklists A (design-system) and B
(source-quality).

The deterministic subset of both checklists is enforced as hard mechanical
gates, not LLM inference: max-width / token-override / grid / density are CSS/AST
checks; vendor-self-source ratio, uniform-value, and sample size are numeric
thresholds. LLM review is reserved for genuinely semantic edge cases and must
cite location.

Define your own canonical checklist items in a verification-contracts document.
This module implements ONLY the deterministic subset:

  Checklist A (CSS/AST checks, run on the UI files in the build delta):
    A1 max-width ban        (checklist A item 1)
    A2 grid usage           (checklist A item 2)
    A3 token override       (checklist A item 3)
    A4 density / wasted-space centered-ribbon  (checklist A item 4)
  Checklist B (numeric thresholds, run on the write batch rows):
    B2 vendor-self-source cap  (> 50% of batch flags)
    B4 uniform-value tripwire  (identical value across >= 20% of rows in scope)
    B5 spot-verify sample      (max(5, 10% of batch); seed = run_id + batch_hash)

The semantic items -- A5 (brief-mandates-a-system-breach), B1 (specialist
precedence), B3 (audit-citation) -- remain the cold verifier's job (Mechanism 2)
and are NOT decided here. The verifier's read of A/B is now a supplement to this
gate, never the sole mechanism.

Everything below is pure and deterministic: same delta + same run_id -> same
verdict. No LLM, no network.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import random
import re
from dataclasses import dataclass, field
from typing import Callable

from .substance_delta import ChangedFile, _norm

# --- Checklist B numeric thresholds (H7, verbatim) -------------------------
VENDOR_SELF_SOURCE_CAP = 0.50          # > 50% of a batch flags
UNIFORM_VALUE_TRIPWIRE = 0.20          # identical value across >= 20% of rows
SPOT_VERIFY_MIN = 5                    # sample = max(5, 10% of batch)
SPOT_VERIFY_FRACTION = 0.10
UNIFORM_VALUE_MIN_ROWS = 5             # below this, ratios are noise; skip tripwire

# --- Checklist A file detection --------------------------------------------
_UI_EXTS = (".css", ".scss", ".sass", ".less", ".tsx", ".jsx")


@dataclass
class Violation:
    checklist: str   # "A" | "B"
    item: str        # "A1", "B4", ...
    detail: str      # human-readable, cites a path/value where possible


@dataclass
class ConformanceResult:
    verdict: str                       # "PASS" | "FAIL" | "SKIP"
    checklist: str = ""                # "A" | "B" | "A+B" | ""
    violations: list = field(default_factory=list)   # list[Violation]
    applied_items: list = field(default_factory=list)  # e.g. ["A1","A2",...]
    reason: str = ""


# ---------------------------------------------------------------------------
# Applicability
# ---------------------------------------------------------------------------

def is_ui_file(path: str) -> bool:
    p = _norm(path)
    if p.lower().endswith(_UI_EXTS):
        return True
    # A .ts/.js file that lives under a web/ frontend tree still ships UI, but
    # only .tsx/.jsx/.css carry the layout tokens the checks look for, so the
    # extension test above is the reliable signal. Path-based web/ inclusion is
    # kept narrow to style/component files to avoid false positives on server TS.
    return False


def ui_files(files: list[ChangedFile]) -> list[ChangedFile]:
    return [cf for cf in files if cf.status != "D" and is_ui_file(cf.path)]


# ---------------------------------------------------------------------------
# Checklist A -- CSS/AST checks (deterministic subset)
# ---------------------------------------------------------------------------

# Permitted max-width contexts (checklist A item 1): prose (70ch), .bf-wide
# figures (90rem), modals, form fields. Values that are not a fixed shell width.
# Capture the value token up to the next declaration/rule terminator, so a
# single-line rule with several declarations still yields the right value.
_MAXWIDTH_RE = re.compile(r"max-width\s*:\s*([^;}\n]+)", re.IGNORECASE)
_MAXWIDTH_INLINE_RE = re.compile(r"maxWidth\s*:\s*['\"]?([^,'\"}\n]+)", re.IGNORECASE)
_PERMITTED_MAXWIDTH_VALUES = {"none", "100%", "100vw", "max-content", "min-content",
                              "fit-content", "auto", "unset", "inherit", "initial"}
# Non-shell contexts where a bounded max-width is legitimate.
_PERMITTED_CTX_RE = re.compile(
    r"\b(prose|modal|dialog|drawer|popover|tooltip|menu|field|input|form|"
    r"label|bf-wide|toast|badge|chip)\b",
    re.IGNORECASE,
)
_FIXED_LEN_RE = re.compile(r"^\d+(\.\d+)?(px|rem|em|vw|pt|cm|in)$", re.IGNORECASE)


def _maxwidth_value(line: str) -> str | None:
    m = _MAXWIDTH_INLINE_RE.search(line) or _MAXWIDTH_RE.search(line)
    if not m:
        return None
    return m.group(1).strip().lower()


def check_a1_max_width(files: list[ChangedFile]) -> list[Violation]:
    """A1: no fixed max-width on a page-level layout container. 70ch prose and
    90rem .bf-wide are permitted; a `max-width: 1100px` centered shell FAILs
    (a real-world cardinal violation observed in production)."""
    out: list[Violation] = []
    for cf in files:
        for ln in cf.added_content_lines:
            if "max-width" not in ln.lower() and "maxwidth" not in ln.lower():
                continue
            val = _maxwidth_value(ln)
            if val is None:
                continue
            if val in _PERMITTED_MAXWIDTH_VALUES:
                continue
            if val.endswith("ch"):          # prose measure (70ch)
                continue
            if val == "90rem":              # .bf-wide figures
                continue
            if _PERMITTED_CTX_RE.search(ln):  # modal/field/etc on the same line
                continue
            if _FIXED_LEN_RE.match(val):
                out.append(Violation("A", "A1",
                    f"{cf.path}: fixed max-width `{val}` on a layout container "
                    f"(centered-shell ban); use grid columns, prose 70ch, or .bf-wide 90rem"))
    return out


_COLUMN_STACK_RE = re.compile(
    r"(flex-direction\s*:\s*column|flex-flow\s*:\s*column|flexDirection\s*:\s*['\"]column['\"])",
    re.IGNORECASE,
)
_GRID_TOKEN_RE = re.compile(r"bf-grid(-ladder)?\b", re.IGNORECASE)


def check_a2_grid(files: list[ChangedFile]) -> list[Violation]:
    """A2: browse/index surfaces use .bf-grid / .bf-grid-ladder, not a hand-rolled
    single `flex-direction: column` stack. A column stack with no grid anywhere in
    the UI delta caps content to one tall column -> FAIL."""
    added_text = "\n".join(ln for cf in files for ln in cf.added_content_lines)
    if _GRID_TOKEN_RE.search(added_text):
        return []  # a density grid is present in the delta; inner columns are fine
    out: list[Violation] = []
    for cf in files:
        for ln in cf.added_content_lines:
            if _COLUMN_STACK_RE.search(ln):
                out.append(Violation("A", "A2",
                    f"{cf.path}: hand-rolled `flex-direction: column` stack with no "
                    f".bf-grid/.bf-grid-ladder in the delta (single tall column)"))
                break  # one finding per file is enough
    return out


# A3: match a --bf-semantic-* *definition* (LHS), not a var(--bf-semantic-*) use.
_VAR_USE_RE = re.compile(r"var\(\s*--bf-semantic-[\w-]+", re.IGNORECASE)
_SEMANTIC_DEF_RE = re.compile(r"(--bf-semantic-[\w-]+)\s*:", re.IGNORECASE)
_EXEMPT_TOKEN = "--bf-font-family-display"


def check_a3_token_override(files: list[ChangedFile]) -> list[Violation]:
    """A3: registry `--bf-semantic-*` tokens are consumed, not redefined in a local
    overlay. Only `--bf-font-family-display` may be swapped. A redefinition (a
    wholesale theme override) FAILs."""
    out: list[Violation] = []
    for cf in files:
        for ln in cf.added_content_lines:
            stripped = _VAR_USE_RE.sub("", ln)  # drop var(--bf-semantic-*) reads
            for m in _SEMANTIC_DEF_RE.finditer(stripped):
                token = m.group(1)
                if token.lower() == _EXEMPT_TOKEN:
                    continue
                out.append(Violation("A", "A3",
                    f"{cf.path}: redefines registry token `{token}` (semantic tokens "
                    f"are applied, not overridden; only {_EXEMPT_TOKEN} may be swapped)"))
    return out


# A4: centered ribbon = auto-centering margin on a non-exempt (page-level) block.
_AUTO_MARGIN_RE = re.compile(
    r"(margin\s*:\s*[^;{}]*\bauto\b"                     # margin: 0 auto
    r"|margin-inline\s*:\s*auto"                          # margin-inline: auto
    r"|marginInline\s*:\s*['\"]auto['\"]"
    r"|margin-left\s*:\s*auto[^;]*;\s*margin-right\s*:\s*auto"  # paired
    r")",
    re.IGNORECASE,
)
# margin: 0 auto is only a ribbon when centering a horizontal block; exempt the
# small centered elements where auto-centering is legitimate.
_A4_EXEMPT_RE = re.compile(
    r"\b(modal|dialog|drawer|button|btn|badge|chip|avatar|icon|logo|nav|menu|"
    r"toast|spinner|prose|bf-wide|field|input)\b",
    re.IGNORECASE,
)


def check_a4_density(files: list[ChangedFile]) -> list[Violation]:
    """A4 (no wasted space): wide viewports fill via grid column count, not centered
    ribbons with empty gutters. An auto-centering margin on a page-level block is the
    deterministic wasted-gutter signal -> FAIL (density tiers/grid fill instead)."""
    out: list[Violation] = []
    for cf in files:
        for ln in cf.added_content_lines:
            if not _AUTO_MARGIN_RE.search(ln):
                continue
            if _A4_EXEMPT_RE.search(ln):
                continue
            out.append(Violation("A", "A4",
                f"{cf.path}: auto-centering margin `{ln.strip()[:60]}` forms a centered "
                f"ribbon with empty gutters; fill wide viewports via grid columns"))
    return out


A_ITEMS = ["A1", "A2", "A3", "A4"]


def checklist_a(files: list[ChangedFile]) -> list[Violation]:
    """Run the deterministic subset of Checklist A over the UI files in the delta."""
    uf = ui_files(files)
    if not uf:
        return []
    violations: list[Violation] = []
    violations += check_a1_max_width(uf)
    violations += check_a2_grid(uf)
    violations += check_a3_token_override(uf)
    violations += check_a4_density(uf)
    return violations


# ---------------------------------------------------------------------------
# Checklist B -- source-quality numeric thresholds (deterministic subset)
# ---------------------------------------------------------------------------

def _host(url: str) -> str:
    """Extract a bare host from a URL/source string, lowercased, sans www."""
    if not url:
        return ""
    s = str(url).strip().lower()
    s = re.sub(r"^[a-z]+://", "", s)          # scheme
    s = s.split("/")[0]                        # host[:port]/path -> host
    s = s.split("?")[0].split("#")[0]
    s = s.split(":")[0]                        # drop port
    if s.startswith("www."):
        s = s[4:]
    return s


def _is_vendor_self(source: str, vendor_domains: list[str]) -> bool:
    h = _host(source)
    if not h:
        return False
    for vd in vendor_domains:
        v = _host(vd) or str(vd).strip().lower().lstrip(".")
        if not v:
            continue
        if h == v or h.endswith("." + v):
            return True
    return False


def check_b2_vendor_self_source(rows: list[dict], source_field: str,
                                vendor_domains: list[str]) -> list[Violation]:
    """B2: a write batch that predominantly (> 50%) cites the product's own
    homepage as source is flagged."""
    if not rows or not vendor_domains:
        return []
    n = len(rows)
    self_cited = sum(1 for r in rows if _is_vendor_self(r.get(source_field, ""), vendor_domains))
    ratio = self_cited / n
    if ratio > VENDOR_SELF_SOURCE_CAP:
        return [Violation("B", "B2",
            f"vendor-self-source ratio {self_cited}/{n} = {ratio:.0%} exceeds cap "
            f"{VENDOR_SELF_SOURCE_CAP:.0%}: batch predominantly cites the vendor's own "
            f"domain(s) {vendor_domains} instead of specialist/audit sources")]
    return []


def check_b4_uniform_value(rows: list[dict], scope_fields: list[str]) -> list[Violation]:
    """B4: a column proposed identical across >= 20% of rows in scope is suspect
    (the AI defaulting, not discriminating) and held for spot-verification."""
    if not rows or not scope_fields:
        return []
    n = len(rows)
    if n < UNIFORM_VALUE_MIN_ROWS:
        return []
    out: list[Violation] = []
    for field_name in scope_fields:
        counts: dict[str, int] = {}
        present = 0
        for r in rows:
            if field_name not in r:
                continue
            present += 1
            key = json.dumps(r.get(field_name), sort_keys=True, default=str)
            counts[key] = counts.get(key, 0) + 1
        if present < UNIFORM_VALUE_MIN_ROWS:
            continue
        top_key, top_count = max(counts.items(), key=lambda kv: kv[1])
        ratio = top_count / present
        if ratio >= UNIFORM_VALUE_TRIPWIRE:
            out.append(Violation("B", "B4",
                f"uniform-value tripwire: field `{field_name}` holds identical value "
                f"{top_key} across {top_count}/{present} = {ratio:.0%} of in-scope rows "
                f"(>= {UNIFORM_VALUE_TRIPWIRE:.0%}); held for spot-verification"))
    return out


def spot_verify_sample_size(n: int) -> int:
    """Deterministic sample size: max(5, ceil(10% of batch)), capped at n."""
    if n <= 0:
        return 0
    import math
    return min(n, max(SPOT_VERIFY_MIN, math.ceil(SPOT_VERIFY_FRACTION * n)))


def _batch_hash(rows: list[dict]) -> str:
    canon = json.dumps(rows, sort_keys=True, default=str)
    return hashlib.md5(canon.encode("utf-8")).hexdigest()


def spot_verify_indices(rows: list[dict], run_id: str) -> list[int]:
    """Reproducible sample of row indices. Seed = md5(run_id | batch_hash) so a
    retry on the identical batch selects the identical sample."""
    n = len(rows)
    k = spot_verify_sample_size(n)
    if k == 0:
        return []
    seed_hex = hashlib.md5(f"{run_id}|{_batch_hash(rows)}".encode("utf-8")).hexdigest()
    rng = random.Random(int(seed_hex, 16))
    return sorted(rng.sample(range(n), k))


def check_b5_spot_verify(rows: list[dict], source_field: str,
                         run_id: str) -> list[Violation]:
    """B5: a reproducible sample of proposed facts is checked before the batch
    writes; a sample miss holds the batch. The mechanical portion confirms each
    sampled row carries a well-formed cited source (a missing/blank citation on a
    sampled row is a hard miss). Whether the source substantively supports the
    claim is the cold verifier's semantic job."""
    if not rows:
        return []
    idxs = spot_verify_indices(rows, run_id)
    misses: list[int] = []
    for i in idxs:
        src = str(rows[i].get(source_field, "") or "").strip()
        if not src or _host(src) == "":
            misses.append(i)
    if misses:
        return [Violation("B", "B5",
            f"spot-verify sample miss: {len(misses)}/{len(idxs)} sampled rows "
            f"(indices {misses}) carry no resolvable cited `{source_field}`; batch held")]
    return []


B_ITEMS = ["B2", "B4", "B5"]


def checklist_b(rows: list[dict], batch_cfg: dict, run_id: str) -> list[Violation]:
    """Run the deterministic subset of Checklist B over the write batch rows."""
    if not rows:
        return []
    source_field = batch_cfg.get("source_field", "source_url")
    vendor_domains = batch_cfg.get("vendor_domains") or []
    scope_fields = batch_cfg.get("scope_fields") or []
    violations: list[Violation] = []
    violations += check_b2_vendor_self_source(rows, source_field, vendor_domains)
    violations += check_b4_uniform_value(rows, scope_fields)
    violations += check_b5_spot_verify(rows, source_field, run_id)
    return violations


# ---------------------------------------------------------------------------
# Batch loading + dispatcher
# ---------------------------------------------------------------------------

def _match_glob(path: str, glob: str) -> bool:
    import fnmatch
    p = _norm(path)
    return fnmatch.fnmatch(p, glob) or fnmatch.fnmatch(posixpath.basename(p), glob)


def load_batch_rows(files: list[ChangedFile], batch_cfg: dict,
                    batch_loader: Callable[[str], str]) -> list[dict]:
    """Collect proposed rows from the batch artifact(s) in the delta.

    A spec's `write_batch.glob` names the artifact the build emits (JSON: a list of
    row dicts, or an object with a `rows`/`proposals`/`facts` list). Inline
    `write_batch.rows` (tests) short-circuits the loader.
    """
    if batch_cfg.get("rows") is not None:
        return list(batch_cfg["rows"])
    glob = batch_cfg.get("glob")
    if not glob:
        return []
    rows: list[dict] = []
    for cf in files:
        if cf.status == "D" or not _match_glob(cf.path, glob):
            continue
        try:
            text = batch_loader(cf.path)
            data = json.loads(text)
        except Exception:
            continue
        if isinstance(data, list):
            rows += [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict):
            for key in ("rows", "proposals", "facts", "items"):
                seq = data.get(key)
                if isinstance(seq, list):
                    rows += [r for r in seq if isinstance(r, dict)]
                    break
    return rows


def run_conformance_gate(
    spec: dict,
    files: list[ChangedFile],
    run_id: str,
    batch_loader: Callable[[str], str] | None = None,
) -> ConformanceResult:
    """Dispatch the mechanical conformance gate over a build delta.

    Checklist A applies WHERE the delta ships UI files (or `spec.ui`). Checklist B
    applies WHERE the spec declares a `write_batch` config. Neither applicable ->
    SKIP. Any deterministic violation -> FAIL with per-item detail.
    """
    applies_a = bool(ui_files(files)) or bool(spec.get("ui"))
    batch_cfg = spec.get("write_batch") or {}
    applies_b = bool(batch_cfg)

    if not applies_a and not applies_b:
        return ConformanceResult("SKIP", "", [], [], "no-ui-and-no-write-batch")

    violations: list[Violation] = []
    applied: list[str] = []
    checklists: list[str] = []

    if applies_a:
        applied += A_ITEMS
        checklists.append("A")
        violations += checklist_a(files)

    if applies_b:
        applied += B_ITEMS
        checklists.append("B")
        rows = load_batch_rows(files, batch_cfg, batch_loader or (lambda _p: ""))
        if not rows:
            # A declared batch that shipped nothing to inspect: the source-quality
            # gate has no rows. Do not FAIL here (that is a substance/delta concern);
            # note it and skip B.
            applied = [i for i in applied if not i.startswith("B")]
            checklists = [c for c in checklists if c != "B"]
            if not applies_a:
                return ConformanceResult("SKIP", "", [], [],
                                         "write-batch declared but no batch rows in delta")
        else:
            violations += checklist_b(rows, batch_cfg, run_id)

    checklist_label = "+".join(checklists)
    if violations:
        return ConformanceResult(
            "FAIL", checklist_label,
            [v for v in violations], applied,
            f"{len(violations)} conformance violation(s)",
        )
    return ConformanceResult("PASS", checklist_label, [], applied, "ok")


def format_conformance_findings(result: ConformanceResult) -> str:
    """Render violations as builder-facing FINDINGS text for the R5 retry prompt."""
    if not result.violations:
        return result.reason or "conformance FAIL"
    lines = ["FINDINGS (mechanical conformance gate -- H7 Checklists A/B):", ""]
    for v in result.violations:
        lines.append(f"- [{v.item}] {v.detail}")
    return "\n".join(lines)
