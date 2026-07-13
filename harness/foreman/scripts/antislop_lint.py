"""Anti-slop static lint -- admin-foreman-antislop-static-lint-v1.

A pure, deterministic, zero-network, zero-LLM lint over a unified git diff. It
runs after a build commit exists and before any verify token is spent, refusing
three mechanical slop signals on the ADDED side of the diff:

  slop_phrase        a known filler phrase on an added line
  junk_file          an added file whose basename is a shell-redirection fragment
  comment_only_diff  a diff that is almost entirely comments/blank while the spec
                     demands tests (a hollow, no-real-work commit)

Mechanical signal only: any finding is a FAIL, no finding is a PASS. No scoring,
no LLM, no exemptions beyond the test-path exclusion of the comment ratio. This
mirrors the H6 discriminator contract (JSON on stdout, exit 0 PASS / 1 FAIL /
2 internal error) so it can also run as a standalone subprocess.
"""
from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass

from .models import AntislopConfig, LintFinding, LintResult

# Module-level defaults, sourced from AntislopConfig so there is one source of
# truth (models.py). Overridable per call via the `config` argument.
_DEFAULTS = AntislopConfig()
SLOP_PHRASES: tuple[str, ...] = _DEFAULTS.slop_phrases
JUNK_BASENAME_REGEXES: tuple[str, ...] = _DEFAULTS.junk_basename_regexes
COMMENT_ONLY_THRESHOLD: float = _DEFAULTS.comment_only_threshold

# Prefixes that mark a stripped line as a comment (Python, C/JS, SQL, HTML/XML).
_COMMENT_PREFIXES: tuple[str, ...] = ("#", "//", "/*", "*", "--", "<!--")

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class _AddedLine:
    file: str
    line: int
    content: str
    is_test: bool


def _strip_git_prefix(path: str) -> str:
    """Drop a leading a/ or b/ that git puts on diff header paths."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _is_test_path(path: str) -> bool:
    """A line belongs to test code when its path contains /tests/ or the file is
    named test_*.py or *_test.py."""
    if "/tests/" in path or path.startswith("tests/"):
        return True
    base = path.rsplit("/", 1)[-1]
    return (base.startswith("test_") and base.endswith(".py")) or base.endswith("_test.py")


def _is_comment_or_blank(content: str) -> bool:
    s = content.strip()
    if not s:
        return True
    return s.startswith(_COMMENT_PREFIXES)


def _parse_diff(diff_text: str) -> tuple[list[_AddedLine], set[str]]:
    """Single pass over a unified git diff.

    Returns (added_lines, added_files). `added_files` is the set of paths that the
    diff creates from scratch (old side empty), used for the junk-basename check.
    Handles both `git diff` output and the header-lite GitHub compare patch format
    (`--- <path>` directly, chunks separated by a blank line).
    """
    added_lines: list[_AddedLine] = []
    added_files: set[str] = set()

    current_file = ""
    new_lineno = 0
    in_hunk = False
    current_is_new = False

    for raw in diff_text.splitlines():
        # A truly empty raw line ends the current file section (the GitHub compare
        # format joins per-file patches with a blank line). Context/added/removed
        # blank lines carry a leading space/+/-, so an empty string is never hunk
        # body.
        if raw == "":
            in_hunk = False
            continue

        if raw.startswith("diff --git"):
            in_hunk = False
            current_is_new = False
            m = re.search(r" b/(\S+)$", raw)
            if m:
                current_file = _strip_git_prefix(m.group(1))
            continue

        if not in_hunk:
            if raw.startswith("new file mode"):
                current_is_new = True
                if current_file:
                    added_files.add(current_file)
                continue
            if raw.startswith("--- "):
                old = raw[4:].strip()
                if old == "/dev/null":
                    current_is_new = True
                else:
                    # GitHub compare style carries only this header, so adopt it as
                    # the current file. A following +++ header (real git diff) wins.
                    current_file = _strip_git_prefix(old)
                continue
            if raw.startswith("+++ "):
                new = raw[4:].strip()
                if new != "/dev/null":
                    current_file = _strip_git_prefix(new)
                if current_is_new and current_file:
                    added_files.add(current_file)
                continue

        m = _HUNK_RE.match(raw)
        if m:
            in_hunk = True
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_lineno = int(m.group(3))
            # A hunk with an empty old side (-0,0) creates the file. This is the
            # only new-file signal the header-lite compare format carries.
            if old_start == 0 and old_count == 0 and current_file:
                added_files.add(current_file)
            continue

        if not in_hunk:
            # Other header noise (index, similarity, mode bits): skip.
            continue

        if raw.startswith("+"):
            content = raw[1:]
            added_lines.append(_AddedLine(
                file=current_file,
                line=new_lineno,
                content=content,
                is_test=_is_test_path(current_file),
            ))
            new_lineno += 1
        elif raw.startswith("-"):
            # Removed line: does not advance the new-side counter.
            continue
        elif raw.startswith("\\"):
            # "\ No newline at end of file" marker.
            continue
        else:
            # Context line (leading space) advances the new-side counter.
            new_lineno += 1

    return added_lines, added_files


def run_antislop_lint(
    diff_text: str,
    spec_demands_tests: bool,
    config: AntislopConfig | None = None,
) -> LintResult:
    """Deterministic lint over a unified diff. FAIL on any finding, else PASS."""
    cfg = config or _DEFAULTS
    added_lines, added_files = _parse_diff(diff_text)
    findings: list[LintFinding] = []

    # 1. Slop phrases on added lines (case-insensitive substring).
    phrase_pairs = [(p, p.lower()) for p in cfg.slop_phrases]
    for al in added_lines:
        low = al.content.lower()
        for original, lowered in phrase_pairs:
            if lowered in low:
                findings.append(LintFinding(
                    check="slop_phrase", file=al.file, line=al.line, detail=original,
                ))

    # 2. Junk basenames on added files.
    junk_res = [re.compile(rx) for rx in cfg.junk_basename_regexes]
    for path in sorted(added_files):
        base = path.rsplit("/", 1)[-1]
        for rx in junk_res:
            if rx.match(base):
                findings.append(LintFinding(
                    check="junk_file", file=path, line=None, detail=path,
                ))
                break

    # 3. Comment-only diff, gated on spec_demands_tests. Test-path added lines are
    # excluded from both numerator and denominator.
    if spec_demands_tests:
        non_test = [al for al in added_lines if not al.is_test]
        if non_test:
            comment_or_blank = sum(1 for al in non_test if _is_comment_or_blank(al.content))
            ratio = comment_or_blank / len(non_test)
            if ratio >= cfg.comment_only_threshold:
                findings.append(LintFinding(
                    check="comment_only_diff", file="", line=None, detail=f"{ratio:.2f}",
                ))

    verdict = "FAIL" if findings else "PASS"
    return LintResult(verdict=verdict, findings=findings)


def format_lint_findings(result: LintResult) -> str:
    """Render a LintResult's findings as a plain-text block for prompt injection."""
    lines: list[str] = []
    for f in result.findings:
        loc = f.file if f.line is None else f"{f.file}:{f.line}"
        loc = loc or "<diff>"
        lines.append(f"- [{f.check}] {loc}: {f.detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Anti-slop static lint over a unified git diff")
    parser.add_argument("--diff-file", required=True, help="Path to a unified diff file")
    parser.add_argument(
        "--spec-demands-tests",
        action="store_true",
        help="Enable the comment-only-diff check (spec requires tests)",
    )
    args = parser.parse_args(argv)

    try:
        diff_text = Path(args.diff_file).read_text(encoding="utf-8")
        result = run_antislop_lint(diff_text, args.spec_demands_tests)
    except Exception as exc:  # noqa: BLE001 -- CLI must never leak a traceback
        print(json.dumps({"verdict": "FAIL", "findings": [], "error": str(exc)}))
        return 2

    print(json.dumps(dataclasses.asdict(result)))
    return 0 if result.verdict == "PASS" else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
