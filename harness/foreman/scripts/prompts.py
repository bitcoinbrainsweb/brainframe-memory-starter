"""Prompt render seam for build and verify agents.

build prompts rendered via this function (not assembled ad hoc).
verify prompts include CI/inference separation.
R-MEM4.AC2: memory-discipline preamble injected at dispatch boundary by task classification.
trace boilerplate injected when TOOL_TRACE_ENABLED=true (uses restricted key, never service role key).
"""
from __future__ import annotations

import os

from scripts.foreman.models import BUILDER_MODEL, VERIFIER_MODEL
from scripts.foreman.worker_pool import MEMORY_DISCIPLINE_PREAMBLE


# trace emission boilerplate injected into build prompts.
# Uses TOOL_TRACE_INSERT_KEY (Phase A, insert-only restricted role) or
# TOOL_TRACE_RELAY_URL + TOOL_TRACE_RELAY_KEY (Phase B, relay Edge Function).
# The database service role key (SUPABASE_SERVICE_KEY) is NEVER referenced here.
_TOOL_TRACE_BOILERPLATE = """\
## Tool Trace Emission

Source the shared snippet once at the top of your session, then call emit_trace_timed
after each significant tool invocation. Emission is fire-and-forget: a failed POST never
halts the build. Required env vars are pre-exported by the orchestrator.

```bash
# Source once (snippet is in the repo):
source scripts/emit_trace.sh

# Before each tool call:
T0=$(date +%s%3N)
# ... run tool ...
emit_trace_timed "bash_tool" "$T0" "$?"

# For named operations:
emit_trace_timed "web_search" "$T0" "$?" '{"query":"..."}' '{"results":1}'
```

Required env vars (set by orchestrator when TOOL_TRACE_ENABLED=true):
  TOOL_TRACE_ENABLED, SESSION_ID, PROJECT
  Phase A: SUPABASE_URL, TOOL_TRACE_INSERT_KEY
  Phase B: TOOL_TRACE_RELAY_URL, TOOL_TRACE_RELAY_KEY
"""

# Mise toolchain bootstrap -- run once after branch checkout.
# Installs pinned tool versions from mise.toml if present; no-ops if mise not installed.
_MISE_BOOTSTRAP = """\
## Toolchain bootstrap (mise)

Run once immediately after branch checkout, before any build steps:

```bash
if command -v mise &>/dev/null && [ -f mise.toml ]; then
  mise install --yes 2>&1 | tail -5
fi
```

This pins Node/Python versions per `mise.toml`. If mise is not installed or no
`mise.toml` is present this block is a no-op -- do not halt on failure.
"""


def _is_fan_out_or_bulk_data(spec: dict) -> bool:
    """Return True if spec declares fan_out or bulk_data capability (H9 boundary rule).

    Classification is the primary gate for preamble injection; content lint is a backstop only.
    """
    caps = spec.get("capabilities") or []
    if isinstance(caps, str):
        caps = [c.strip() for c in caps.split(",") if c.strip()]
    return any(c in {"fan_out", "bulk_data"} for c in caps)


_RATIONALE_PROMPT_FRAGMENT = """\
## Authoring the RATIONALE block (receipt rationale)

After all code is committed and pushed, author a RATIONALE block from THIS build session.
First append the sentinel line to the PR description body, then append the block:

```
<!-- FOREMAN RECEIPT END -->
```foreman-rationale
{
  "decision_rationale": "<why you built it this way over alternatives>",
  "issues_encountered": "<what fought back, including any approach tried and abandoned verbatim>",
  "lessons": "<anything a future build of this kind should know>"
}
```
```

Rules:
- The block MUST be a JSON object with EXACTLY these three string keys.
- If you have nothing for a field, write the literal NONE_PROVIDED (not an em-dash, not blank).
- Never fabricate. Failed approaches are signal -- keep them verbatim.
- No em-dashes (U+2014, U+2013, double-hyphen) anywhere in the block text.
- Write the same three decoded string values to the build_events row for this run.
"""

_RULEFORGE_PROMPT_FRAGMENT = """\
## RuleForge codegen guardrail (codegen guardrail)

For each new source module of meaningful scope (more than ~30 LoC or with real module
boundaries), prepend a @RULE: header:

```typescript
/**
 * @RULE:PURPOSE: <one-line responsibility statement>
 * @RULE:IMPORTS_ALLOWED: <glob patterns this module may import, comma-separated>
 * @RULE:IMPORTS_FORBIDDEN: <glob patterns this module must NOT import, comma-separated>
 */
```

(For Python: use a triple-quoted docstring or # comment lines at the top.)

Before opening the PR, run the guardrail check against changed files:

```bash
python scripts/ruleforge_check.py <changed-file-1> <changed-file-2> ...
```

If it exits non-zero, fix the forbidden imports -- do not weaken the rule to pass.
Record guardrail pass/fail in the receipt with one line: `guardrail: pass` or
`guardrail: FAIL -- <violation count> violation(s)`.
"""


def render_build_prompt(
    spec: dict,
    run_id: str,
    branch_name: str,
    attempt: int,
    repo: str,
    prior_findings: str | None = None,
    fix_forward: bool = False,
    lint_findings: str | None = None,
) -> str:
    """Render build prompt. The seam where prompt-writing integration lands later.

    When fix_forward=True, the job instruction directs the builder to patch
    the existing implementation on the branch rather than rebuild from scratch.

    lint_findings (anti-slop static lint) carries the deterministic
    static-lint findings from a refused prior attempt; they render under a dedicated
    heading ABOVE any verifier findings, and never replace them."""
    slug = spec.get("slug", "unknown")
    title = spec.get("title") or slug

    lines = [
        f"printf '\\033]0;build:{slug}\\007'",
        f"# Run this prompt with model: {BUILDER_MODEL}",
        "",
        "You are a build agent in the sequential build-verify-commit loop.",
        f"Run: {run_id}  |  Spec: {slug}  |  Attempt: {attempt}  |  Branch: {branch_name}",
        "",
    ]

    # R-MEM4.AC2: inject memory-discipline preamble at dispatch boundary by classification.
    # Primary gate is task classification (fan_out / bulk_data capability), not content lint.
    if _is_fan_out_or_bulk_data(spec):
        lines.append(MEMORY_DISCIPLINE_PREAMBLE)
        lines.append("")

    if fix_forward:
        lines += [
            "## Your job (fix-forward pass)",
            "",
            f"The branch `{branch_name}` already has a complete build commit for this spec.",
            "The verifier found the findings listed below. Your job is to PATCH the existing",
            "implementation (do NOT rebuild from scratch). Check out the branch, read the",
            "existing code, apply targeted fixes for every finding, then commit and push.",
            "",
            _MISE_BOOTSTRAP,
            "",
            "## Spec content",
            "",
        ]
    else:
        lines += [
            "## Your job",
            "",
            f"Implement the spec described below in the `{repo}` repository on branch `{branch_name}`.",
            "The branch has already been created from current main HEAD.",
            "Build, commit, and push to the branch. Do NOT open a PR. Do NOT merge.",
            "",
            _MISE_BOOTSTRAP,
            "",
            "## Spec content",
            "",
        ]

    spec_body = spec.get("body")
    if spec_body:
        lines += [
            "The spec content is inlined below. Read it now, then write the FIRST file immediately.",
            "",
            "```markdown",
            spec_body,
            "```",
            "",
        ]
    else:
        lines += [
            "## Spec content unavailable",
            "",
            "The spec body was not provided to this prompt. Do NOT attempt to fetch it",
            "yourself; the orchestrator hydrates the spec host-side before dispatch, so a",
            "missing body means a hydration fault. Stop and report the missing spec body.",
            "",
            f"Title: {title}",
            "",
        ]

    # Scope channel (pre-dispatch manifest lint): when the spec
    # carries a scope / test-slice boundary, carry it into the builder context
    # unchanged, right where the spec body is injected. Omitted entirely when absent
    # so a scope-less spec renders byte-identically to before.
    scope = spec.get("scope") or spec.get("test_slice")
    if scope:
        lines += [
            "## Scope (build boundary)",
            "",
            "Stay within this scope; do NOT modify files outside it:",
            "",
            str(scope),
            "",
        ]

    lines += [
        "## Hard rules",
        "",
        "- No em-dashes (U+2014, U+2013, double-hyphen).",
        "- No secrets in committed files.",
        f"- Commit message: build({slug}): <one-line description> [run:{run_id} attempt:{attempt}]",
        f"- Push to: {branch_name}",
        "- Cover ALL spec requirements. Do not truncate.",
        "- WRITE-FIRST: Do NOT narrate, plan, or explore the repo before writing files. Start with the FIRST file write tool call immediately. Planning text that ends a turn without a file write is a failed turn.",
        "- Commit after EACH task (not at the end). If the session ends early, progress is preserved per committed task.",
        "",
    ]

    # inject trace boilerplate when TOOL_TRACE_ENABLED=true; omit otherwise.
    if os.environ.get("TOOL_TRACE_ENABLED", "true").lower() in ("true", "1"):
        lines += [
            _TOOL_TRACE_BOILERPLATE,
            "",
        ]

    # Anti-slop static lint findings (anti-slop static lint) render
    # above the verifier findings. These are mechanical, not opinions: every item
    # must be fixed before committing.
    if lint_findings:
        lines += [
            "## STATIC LINT FINDINGS",
            "",
            "A deterministic pre-verify lint refused the previous attempt. These are",
            "mechanical failures (not reviewer opinions); fix every item before committing:",
            "",
            lint_findings,
            "",
        ]

    if prior_findings:
        if fix_forward:
            lines += [
                "## Verifier findings to fix (patch the existing code for each one)",
                "",
                prior_findings,
                "",
            ]
        else:
            lines += [
                "## Prior verifier findings (address before committing)",
                "",
                prior_findings,
                "",
            ]

    lines += [
        _RULEFORGE_PROMPT_FRAGMENT,
        "",
        _RATIONALE_PROMPT_FRAGMENT,
        "",
        "## Done signal",
        "",
        f"Report the commit SHA pushed to `{branch_name}`.",
    ]
    return "\n".join(lines)


def render_loop_005_prompt(
    spec: dict,
    run_id: str,
    branch_name: str,
    default_branch: str,
    attempt: int,
    before_coverage: float | None,
    current_coverage: float | None,
    stalled_files: set | None = None,
) -> str:
    """Render a single-iteration agent prompt for Loop 005 (100% test coverage).

    Each invocation produces one iteration prompt; Foreman re-dispatches until
    the loop terminates. Coverage format routing and commit message
    format comply with the spec.
    """
    target_repo = spec.get("target_repo", "")
    coverage_command = spec.get("coverage_command", "npm run coverage")
    coverage_threshold = float(spec.get("coverage_threshold", 100.0))
    coverage_format = spec.get("coverage_format", "json-summary")
    branch_prefix = spec.get("branch_prefix", "coverage-loop")

    stalled = sorted(stalled_files) if stalled_files else []
    stalled_section = ""
    if stalled:
        stalled_section = (
            "\n## Stalled files (skip these -- no coverage progress possible)\n\n"
            + "\n".join(f"- {f}" for f in stalled)
            + "\n"
        )

    # Coverage format parsing instructions
    format_instructions: dict[str, str] = {
        "json-summary": (
            "Parse JSON: global pct from `total.lines.pct`; "
            "per-file uncovered lines from `<file>.lines.uncovered` (integer)."
        ),
        "tabular": (
            "Parse text table with columns `File | % Lines | Uncovered Lines`. "
            "Global pct is the row named 'All files' or 'Total'."
        ),
        "best-effort": (
            "Attempt JSON-summary parse first, then tabular. "
            "If neither yields per-file uncovered line counts, exit with: "
            'PARTIAL-UNSUPPORTED reason="coverage output lacks per-file data"'
        ),
    }
    fmt_instruction = format_instructions.get(coverage_format, format_instructions["best-effort"])

    # Setup block (first iteration only)
    if attempt == 0:
        setup_block = f"""\
# Setup (first iteration only)
git checkout {default_branch} && git pull
git checkout -b {branch_name}

# Run initial coverage
RUN: {coverage_command}
{fmt_instruction}
IF global_coverage >= {coverage_threshold}: EXIT SUCCESS.
"""
    else:
        setup_block = f"""\
# Branch already checked out: {branch_name}
# Current global coverage: {current_coverage if current_coverage is not None else 'unknown'}%
# Before-coverage (baseline): {before_coverage if before_coverage is not None else 'unknown'}%
"""

    lines = [
        "printf '\\033]0;coverage-loop\\007'",
        f"# Run with model: {BUILDER_MODEL}",
        "",
        f"REPO: {target_repo}",
        f"BRANCH: {branch_name}",
        f"DEFAULT_BRANCH: {default_branch}",
        "",
        setup_block,
        stalled_section,
        "# ITERATE (single iteration -- Foreman re-dispatches for subsequent iterations)",
        "",
        "FIND file with maximum uncovered_lines from coverage output.",
        "Tie-break: lexicographic file path ascending (alphabetically earlier path wins).",
        "IF no file with uncovered lines: EXIT SUCCESS.",
        "",
        "WRITE tests for the target file.",
        "COMMIT with message exactly: [coverage-loop] +{delta:.2f}% coverage: {target_file}",
        "  where {delta} is the numeric percentage gain (two decimal places)",
        "  and {target_file} is the relative file path.",
        "",
        f"RUN: {coverage_command}",
        f"{fmt_instruction}",
        f"UPDATE global_coverage from stdout.",
        f"IF global_coverage >= {coverage_threshold}: EXIT SUCCESS.",
        "",
        "Report your iteration result on a single line exactly as:",
        "EXIT ITERATION_COMPLETE global_coverage={measured_pct} target_file={target_file}",
        "  where {{measured_pct}} is the new global coverage percentage (float)",
        "  and {{target_file}} is the file you targeted this iteration.",
    ]

    return "\n".join(lines)


def render_loop_006_prompt(
    spec: dict,
    run_id: str,
    branch_name: str,
    default_branch: str,
    attempt: int,
    stalled_axes: set | None = None,
    skipped_axes: set | None = None,
) -> str:
    """Render a single-iteration agent prompt for Loop 006 (GEO/SEO Visibility).

    Each invocation produces one iteration prompt; Foreman re-dispatches until
    the loop terminates. Stalled/skipped axes are excluded from SCORE.
    """
    target_repo = spec.get("target_repo", "")
    target_url = spec.get("target_url", "")
    branch_prefix = spec.get("branch_prefix", "geo-loop")
    topic = spec.get("topic", "")
    expected_queries = spec.get("expected_queries") or []
    visibility_bar = spec.get("visibility_bar", {})

    stalled = sorted(stalled_axes) if stalled_axes else []
    skipped = sorted(skipped_axes) if skipped_axes else []

    stalled_section = ""
    if stalled:
        stalled_section = (
            "\n## Stalled axes (exclude from SCORE -- no progress possible)\n\n"
            + "\n".join(f"- {a}" for a in stalled)
            + "\n"
        )

    skipped_section = ""
    if skipped:
        skipped_section = (
            "\n## Skipped axes (unfixable from prior iteration -- exclude from SCORE)\n\n"
            + "\n".join(f"- {a}" for a in skipped)
            + "\n"
        )

    if attempt == 0:
        setup_block = (
            f"# Setup (first iteration only)\n"
            f"git checkout {default_branch} && git pull\n"
            f"git checkout -b {branch_name}\n"
        )
    else:
        setup_block = f"# Branch already checked out: {branch_name}\n"

    query_block: str
    if expected_queries:
        q_list = "\n".join(f"- {q}" for q in expected_queries)
        query_block = f"EXPECTED_QUERIES (use these for answer-readiness check):\n{q_list}"
    elif topic:
        query_block = (
            f"TOPIC: {topic}\n"
            "Derive answer-readiness queries: fetch homepage, extract 5 most prominent "
            'nouns/entities, form "what is {{entity}}?" queries.'
        )
    else:
        query_block = (
            "EXPECTED_QUERIES: none supplied. TOPIC: none supplied.\n"
            'Skip answer-readiness axis; log: "skipped: no query source".'
        )

    bar_lines = "\n".join(f"  {k}: {v}" for k, v in visibility_bar.items())

    lines = [
        "printf '\\033]0;geo-loop\\007'",
        f"# Run with model: {BUILDER_MODEL}",
        "",
        f"REPO: {target_repo}",
        f"BRANCH: {branch_name}",
        f"DEFAULT_BRANCH: {default_branch}",
        f"TARGET_URL: {target_url}",
        f"STALLED_AXES: {','.join(stalled) if stalled else '(none)'}",
        f"SKIPPED_AXES: {','.join(skipped) if skipped else '(none)'}",
        "",
        setup_block,
        stalled_section,
        skipped_section,
        "# AUDIT",
        f"Fetch {target_url} and its robots.txt, sitemap.xml (or sitemap index).",
        "Check crawlability (AC1 -- robots.txt presence, sitemap presence, canonical tags).",
        "Check indexation (AC2 -- use GSC when GSC_API_KEY set; else site: query heuristic).",
        "Check content structure (AC3 -- H1/H2, schema.org JSON-LD/microdata, meta description).",
        query_block,
        "",
        "For each audited axis, emit one line:",
        "  AUDIT_STATE axis=<axis> state=<summary>",
        "",
        "# SCORE",
        "Rank all identified issues per impact order (highest first):",
        "  1. crawlability: robots.txt missing or blocking all agents",
        "  2. crawlability: sitemap missing",
        "  3. crawlability: canonical tags missing or pointing to wrong URL",
        "  4. content-structure: structured data (schema.org) missing on homepage",
        "  5. content-structure: meta descriptions missing on any page",
        "  6. content-structure: H1/H2 hierarchy invalid or absent",
        "  7. indexation: site not indexed",
        "  8. answer-readiness: fewer than answer_ready_queries_passing queries answered",
        "Filter: exclude stalled and skipped axes from scoring.",
        "Select the single highest-impact FIXABLE issue.",
        "If no fixable issue remains:",
        '  EXIT PARTIAL reason="no remaining fixable issues"',
        "",
        "# APPLY",
        "Fix the selected issue by editing exactly one file in the working branch.",
        "VERIFY: diff touches only files relevant to the target axis.",
        "COMMIT: \"[geo-loop] fix(<axis>): <description>\"",
        "  where <axis> is one of: crawlability, indexation, content-structure, answer-readiness",
        "",
        "# CHECK BAR",
        f"Check all visibility_bar criteria:\n{bar_lines}",
        "IF all pass:",
        f"  git push -u origin {branch_name}",
        '  OPEN PR: "feat(geo): visibility loop pass - <hostname>"',
        "  PR body: table with columns axis | before | after | fix applied",
        "  EXIT SUCCESS.",
        "",
        "Report your iteration result on a single line exactly as:",
        "EXIT ITERATION_COMPLETE axis_fixed=<axis> issue=<description>",
        "  where <axis> is the axis you fixed this iteration",
        "  and <description> is the specific issue resolved.",
    ]

    return "\n".join(lines)


def render_verify_prompt(
    spec: dict,
    run_id: str,
    branch_name: str,
    attempt: int,
    prior_findings: str | None = None,
    diff_text: str | None = None,
) -> str:
    """Render verify prompt. Cold verifier receives only the diff, no build context."""
    slug = spec.get("slug", "unknown")

    spec_body = spec.get("body")
    if spec_body:
        spec_section = [
            "## Spec (inlined below -- do not fetch)",
            "",
            "```markdown",
            spec_body,
            "```",
            "",
        ]
    else:
        spec_section = [
            "## Spec unavailable",
            "",
            "The spec body was not provided to this prompt. Do NOT attempt to fetch it",
            "yourself. Without the spec you cannot verify requirement coverage; report a",
            "verifier-tooling failure rather than guessing at the acceptance criteria.",
            "",
        ]

    if diff_text:
        diff_section = [
            "## Diff under review (inlined below -- this IS the complete build)",
            "",
            "This is the full diff of the build branch against main. Evaluate ONLY",
            "this diff. Do NOT run local git commands such as",
            "`git rev-list origin/main..HEAD` or `git diff origin/main..HEAD` -- your",
            "workspace is checked out on main, not the build branch, so those report",
            "an empty diff and are misleading. The presence of the diff below is",
            "proof the build is non-empty.",
            "",
            "```diff",
            diff_text,
            "```",
            "",
        ]
    else:
        diff_section = [
            "## Fetch the diff",
            "",
            f"  gh api repos/YOUR_ORG/YOUR_REPO/compare/main...{branch_name} "
            "--jq '.files[].patch'",
            "",
            "If the command above returns nothing or errors, do NOT conclude the build",
            "is empty -- report a verifier-tooling failure instead. Never use",
            "`git rev-list origin/main..HEAD` as an empty-build signal; your workspace",
            "is on main, not the build branch.",
            "",
        ]

    lines = [
        f"printf '\\033]0;verify:{slug}\\007'",
        f"# Run this prompt with model: {VERIFIER_MODEL}",
        "",
        "You are an adversarial verifier in the sequential build-verify-commit loop.",
        f"Run: {run_id}  |  Spec: {slug}  |  Attempt: {attempt}  |  Branch: {branch_name}",
        "",
        "You did NOT write this code. Find failures the builder missed.",
        "",
        *spec_section,
        *diff_section,
        "## Checklist",
        "",
        "- All spec requirements implemented (no silent omissions).",
        "- No em-dashes (U+2014, U+2013, double-hyphen) in any file touched.",
        "- No secrets in committed files.",
        "- No placeholder content in production files.",
        f"- Commit message format: build({slug}): <desc> [run:{run_id} attempt:{attempt}]",
        "- Tests present and passing where required by spec.",
        "- House-rule compliance (GLOBAL_RULES.md).",
        "",
        "## CI vs inference",
        "",
        "- Read CI output; do not re-run tests yourself.",
        "- Flag test-coverage claims as unverified if no CI tooling is present for this repo.",
        "",
        "## Conformance Checklists A/B -- mechanical gate runs separately",
        "",
        "A deterministic conformance gate runs mechanically in the verify phase and",
        "will independently FAIL this build on any violation of the following, so you",
        "do NOT need to re-derive them:",
        "  A (UI): max-width ban, .bf-grid density usage, semantic-token override,",
        "          centered-ribbon wasted space.",
        "  B (data-write): vendor-self-source ratio, uniform-value tripwire, spot-verify",
        "          sample citations.",
        "Your job on A/B is the SEMANTIC SUPPLEMENT the mechanical gate cannot decide:",
        "A5 (brief mandates a system breach), B1 (specialist-source precedence), and",
        "B3 (audit-citation). Cite file:line for any such finding.",
        "",
    ]

    if prior_findings:
        lines += [
            "## Prior findings (confirm each is addressed)",
            "",
            "Confirming these are resolved does not substitute for a full re-evaluation.",
            "If the build passes all checklist items above, verdict is PASS regardless of prior findings.",
            "",
            prior_findings,
            "",
        ]

    lines += [
        "## Response format",
        "",
        "Emit the VERDICT line first, then your findings on the lines below it. The",
        "orchestrator reads only the text that follows the VERDICT line; anything",
        "above the VERDICT line is discarded. Respond in exactly this shape:",
        "",
        "  VERDICT: PASS",
        "  <one line confirming all checklist items passed>",
        "",
        "or",
        "",
        "  VERDICT: FAIL",
        "  FINDINGS:",
        "  - <at least one specific issue>",
        "",
        "If VERDICT: FAIL you MUST list at least one specific issue on the lines after",
        "the VERDICT line. A VERDICT: FAIL with no findings after it is malformed; the",
        "orchestrator will retry the verification and then park the task as unresolvable.",
    ]
    return "\n".join(lines)
