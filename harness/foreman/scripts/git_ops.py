"""Local git operations for the Foreman runner.

R9.AC1/AC4, H2/H3: ground-truth gate against origin.
All operations confirm working_dir and remote_url before executing.
Never assume working dir, branch, or remote URL.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from enum import Enum
from pathlib import Path

from scripts.foreman.substance_delta import HARNESS_SCAFFOLD_PATHS

_logger = logging.getLogger("foreman.git_ops")

# Hard ceiling on any single git subprocess. A hung network op (fetch/push/
# ls-remote) must fail fast and become a classified park, never block the runner.
_GIT_TIMEOUT_SECONDS = int(os.environ.get("FOREMAN_GIT_TIMEOUT_SECONDS", "120"))

_GIT_NO_PROMPT_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GCM_PROVIDER": "",
}


class GitError(Exception):
    """Raised when a git operation fails or precondition check fails."""


# ---------------------------------------------------------------------------
# Harness scaffolding guard (Bug 4)
#
# The harness provisions scaffolding (emit_trace.sh, ruleforge_check.py) into a
# build working tree. Two defenses keep it out of builder commits, sharing the
# single path list HARNESS_SCAFFOLD_PATHS with the substance-delta exclusion so
# the two never drift:
#   1. inject_scaffold_exclude -- .git/info/exclude entry so it is never staged
#      (local to the tree, never a committed .gitignore change).
#   2. assert_no_scaffolding -- post-build guard that rejects a commit which
#      staged scaffolding anyway.
# ---------------------------------------------------------------------------

def _scaffolding_leaks(
    paths, scaffold_paths: tuple[str, ...] = HARNESS_SCAFFOLD_PATHS,
) -> list[str]:
    """Return the members of ``paths`` that are harness scaffolding.

    Matches on the normalized full path or the basename, so scaffolding is caught
    wherever the harness provisioned it, not only at its canonical location.
    """
    def _norm(p: str) -> str:
        return p.replace("\\", "/").strip().lstrip("./")

    scaffold_full = {_norm(s) for s in scaffold_paths}
    scaffold_base = {_norm(s).rsplit("/", 1)[-1] for s in scaffold_paths}
    leaked: list[str] = []
    for p in paths:
        n = _norm(p)
        if n in scaffold_full or n.rsplit("/", 1)[-1] in scaffold_base:
            leaked.append(p)
    return leaked


def assert_no_scaffolding(
    paths, scaffold_paths: tuple[str, ...] = HARNESS_SCAFFOLD_PATHS,
) -> None:
    """Reject a builder commit that staged harness scaffolding (Bug 4).

    ``paths`` is the set of paths a commit added or modified. Raises GitError
    naming the leaked scaffolding; returns silently on a clean commit.
    """
    leaked = _scaffolding_leaks(paths, scaffold_paths)
    if leaked:
        raise GitError(
            "harness scaffolding must never be committed by a builder; "
            f"leaked paths: {sorted(set(leaked))}"
        )


def inject_scaffold_exclude(
    working_dir: Path, scaffold_paths: tuple[str, ...] = HARNESS_SCAFFOLD_PATHS,
) -> None:
    """Add harness scaffolding to the tree's .git/info/exclude (Bug 4).

    Local to the working tree and never staged, so scaffolding cannot be caught
    by ``git add -A``. Idempotent. A committed .gitignore change is deliberately
    avoided: the exclusion must not itself appear in the builder's diff.
    """
    working_dir = Path(working_dir)
    res = _run(["git", "rev-parse", "--git-path", "info/exclude"], working_dir)
    if res.returncode == 0 and res.stdout.strip():
        exclude_path = working_dir / res.stdout.strip()
    else:
        exclude_path = working_dir / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)

    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    lines = existing.splitlines()
    marker = "# foreman: harness scaffolding (never commit)"
    additions: list[str] = []
    if marker not in lines:
        additions.append(marker)
    for p in scaffold_paths:
        entry = "/" + p.replace("\\", "/").lstrip("/")
        if entry not in lines:
            additions.append(entry)
    if not additions:
        return
    new_text = existing
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    new_text += "\n".join(additions) + "\n"
    exclude_path.write_text(new_text, encoding="utf-8")


# Env var precedence for the push token, most-specific first. Kept identical to
# the historical blind-precedence order so that, among tokens that CAN push a
# given repo, the same one still wins.
_ENV_TOKEN_NAMES = ("FOREMAN_SANDBOX_PUSH_TOKEN", "GH_TOKEN", "GITHUB_PRIMARY_PAT")
_SECRETS_MANAGER_TOKEN_NAMES = ("FOREMAN_SANDBOX_PUSH_TOKEN", "GITHUB_PRIMARY_PAT")

_GITHUB_API = "https://api.github.com"
_TOKEN_VALIDATION_TIMEOUT = int(
    os.environ.get("FOREMAN_TOKEN_VALIDATION_TIMEOUT_SECONDS", "10")
)

# Per-process cache of definitive validation results, keyed by
# (sha256(token)[:16], "owner/repo"). Foreman loops push many times per run;
# this collapses that to one API call per (token, repo) pair. Only definitive
# 2xx/40x verdicts are cached -- transient network failures are never cached so
# a blip does not poison the process for its lifetime.
_TOKEN_REPO_VALIDATION: dict[tuple[str, str], bool] = {}

_GITHUB_REPO_RE = re.compile(
    r"github\.com[/:]([^/]+)/(.+?)(?:\.git)?/?$"
)


def _parse_github_repo(remote_url: str | None) -> tuple[str, str] | None:
    """Extract (owner, repo) from a github.com remote URL, or None.

    Handles https (with or without embedded credentials) and git@ ssh forms and
    strips any trailing .git. Non-github or unparseable URLs return None, which
    makes the resolver fall back to blind precedence.
    """
    if not remote_url:
        return None
    match = _GITHUB_REPO_RE.search(_strip_credentials(remote_url))
    if not match:
        return None
    return match.group(1), match.group(2)


def _token_repo_key(token: str, repo_full: str) -> tuple[str, str]:
    """Cache key that never stores the token value itself."""
    return (hashlib.sha256(token.encode()).hexdigest()[:16], repo_full)


def _github_repo_status(token: str, owner: str, repo: str) -> int | None:
    """GET /repos/{owner}/{repo} with token; return HTTP status, or None if the
    API is unreachable (network error, timeout).

    The token travels only in the Authorization header -- never logged, never in
    argv. Isolated as its own function so tests can mock the API with no live call.
    """
    request = urllib.request.Request(
        f"{_GITHUB_API}/repos/{owner}/{repo}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "foreman-token-resolver",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TOKEN_VALIDATION_TIMEOUT) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def _token_can_push(token: str, owner: str, repo: str) -> bool | None:
    """Whether `token` can access owner/repo: True (usable), False (403/404/401),
    or None if validation could not be performed (API unreachable).

    Definitive verdicts are cached per (token, repo) for the process lifetime.
    """
    repo_full = f"{owner}/{repo}"
    key = _token_repo_key(token, repo_full)
    if key in _TOKEN_REPO_VALIDATION:
        return _TOKEN_REPO_VALIDATION[key]

    status = _github_repo_status(token, owner, repo)
    if status is None:
        return None
    if 200 <= status < 300:
        _TOKEN_REPO_VALIDATION[key] = True
        return True
    if status in (401, 403, 404):
        _TOKEN_REPO_VALIDATION[key] = False
        return False
    # Unexpected status (5xx, 429, ...): treat as transient/unreachable, do not
    # cache, and let the caller degrade to blind precedence.
    _logger.warning(
        "unexpected HTTP %s validating push token for %s; degrading to precedence",
        status, repo_full,
    )
    return None


def _secrets_manager_token(name: str) -> str | None:
    """Read a single secret from your secrets manager into memory. Never logged.

    Set SECRETS_MANAGER_GET to the command that prints one secret by name, for
    example `export SECRETS_MANAGER_GET="<your-secrets-cli> get"` or your own
    provider's CLI. If it is unset, this fallback yields nothing and only env vars
    are used.
    """
    cmd = os.environ.get("SECRETS_MANAGER_GET")
    if not cmd:
        return None
    try:
        result = subprocess.run(
            [*shlex.split(cmd), name],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _candidate_tokens():
    """Yield push-token candidates in precedence order: env first, then the secrets manager.

    The secrets-manager fallback is a subprocess call, so it is evaluated lazily:
    when the consumer stops at the first usable env token (blind mode, or a
    validated env token), the secrets manager is never invoked, preserving the fast
    path. Duplicate values are yielded once.
    """
    seen: set[str] = set()
    for name in _ENV_TOKEN_NAMES:
        value = os.environ.get(name)
        if value and value not in seen:
            seen.add(value)
            yield value
    for name in _SECRETS_MANAGER_TOKEN_NAMES:
        value = _secrets_manager_token(name)
        if value and value not in seen:
            seen.add(value)
            yield value


def _resolve_push_token(remote_url: str | None = None) -> str | None:
    """Resolve the push token from env, falling back to the secrets manager. Memory only.

    Single source of truth shared with transport._fetch_push_token. Never logged,
    never placed in argv.

    When `remote_url` names a github.com repo, each candidate is validated against
    that repo (GET /repos/{owner}/{repo}: 200 = usable, 403/404 = skip) before it
    is returned, so a token scoped to other repos (e.g. FOREMAN_SANDBOX_PUSH_TOKEN)
    is skipped for repos it cannot push. Precedence order is preserved among valid
    candidates. If the GitHub API is unreachable during validation, the resolver
    degrades to the historical blind-precedence behavior (returns the first
    candidate) with a logged warning rather than blocking the push. When
    `remote_url` is None or not a github repo, blind precedence is used unchanged.
    """
    repo = _parse_github_repo(remote_url)
    candidates = _candidate_tokens()

    if repo is None:
        # No target repo to validate against: legacy blind precedence.
        return next(candidates, None)

    owner, name = repo
    first: str | None = None
    for token in candidates:
        if first is None:
            first = token
        verdict = _token_can_push(token, owner, name)
        if verdict is True:
            return token
        if verdict is None:
            # API unreachable: degrade safely to blind precedence (return the
            # highest-precedence candidate) rather than block the push.
            _logger.warning(
                "push-token validation unavailable for %s/%s; "
                "falling back to precedence order",
                owner, name,
            )
            return first
        # verdict is False: this token cannot push the repo; try the next one.
    # Every candidate was definitively rejected (403/404): no usable token.
    return None


def _authenticated_url(remote_url: str) -> str:
    """Embed the push token in a github.com URL on Windows.

    On Windows, git.exe cannot run a POSIX GIT_ASKPASS script and the
    GIT_TERMINAL_PROMPT=0 path then hard-fails with 'unable to get password
    from user' for any remote operation. Embedding the token in the URL is the
    only reliable headless auth on win32. The token is resolved via
    _resolve_push_token (env then secrets-manager fallback). On POSIX the URL is
    returned unchanged (the no-prompt env plus any configured askpass handles
    auth). Callers must never log the returned URL; pass the bare remote_url to
    error messages instead.
    """
    if sys.platform != "win32":
        return remote_url
    token = _resolve_push_token(remote_url)
    if not token:
        return remote_url
    if remote_url.startswith("https://github.com/"):
        return remote_url.replace(
            "https://github.com/",
            f"https://x-access-token:{token}@github.com/",
            1,
        )
    return remote_url


def _strip_credentials(url: str) -> str:
    """Remove any embedded userinfo from an https URL for safe comparison/logging.

    Turns https://x-access-token:TOKEN@github.com/x/y.git into
    https://github.com/x/y.git so URL-integrity checks and error messages never
    depend on or leak credentials.
    """
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            rest = rest.split("@", 1)[1]
        return f"{scheme}://{rest}"
    return url


def _git(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a git command with credential helpers disabled and prompts off.

    Applies a hard timeout; a hung git call raises GitError rather than blocking.
    """
    full = ["git", "-c", "credential.helper="] + args
    kwargs.setdefault("env", _GIT_NO_PROMPT_ENV)
    kwargs.setdefault("timeout", _GIT_TIMEOUT_SECONDS)
    try:
        return subprocess.run(full, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise GitError(
            f"git command timed out after {kwargs.get('timeout')}s: git {' '.join(args[:2])}"
        ) from exc


class GPGKeyUnavailableError(Exception):
    """Raised when the GPG signing key is absent, expired, or fails to load (U-02.AC3)."""


def _run(args: list[str], cwd: Path, capture: bool = True) -> subprocess.CompletedProcess:
    return _git(args[1:], cwd=cwd, capture_output=capture, text=True)


def confirm_git_state(
    working_dir: Path,
    expected_remote_url: str,
    expected_branch: str | None = None,
) -> None:
    """Verify the working dir is a valid git repo with the expected remote.

    Called before every significant git operation (never assume state).
    Raises GitError on mismatch.
    """
    if not working_dir.exists():
        raise GitError(f"Working dir does not exist: {working_dir}")

    result = _run(["git", "rev-parse", "--git-dir"], working_dir)
    if result.returncode != 0:
        raise GitError(f"Not a git repository: {working_dir}")

    result = _run(["git", "remote", "get-url", "origin"], working_dir)
    if result.returncode != 0:
        raise GitError(f"No 'origin' remote configured in: {working_dir}")

    actual_url = result.stdout.strip()
    if _strip_credentials(actual_url) != _strip_credentials(expected_remote_url):
        raise GitError(
            f"Remote URL mismatch in {working_dir}: "
            f"expected {_strip_credentials(expected_remote_url)!r}, "
            f"got {_strip_credentials(actual_url)!r}"
        )

    if expected_branch is not None:
        result = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], working_dir)
        if result.returncode != 0:
            raise GitError(f"Cannot determine current branch in: {working_dir}")
        actual_branch = result.stdout.strip()
        if actual_branch != expected_branch:
            raise GitError(
                f"Branch mismatch in {working_dir}: "
                f"expected {expected_branch!r}, got {actual_branch!r}"
            )


def get_local_sha(working_dir: Path, ref: str = "HEAD") -> str:
    """Return the SHA of a local ref. Raises GitError if not found."""
    result = _run(["git", "rev-parse", ref], working_dir)
    if result.returncode != 0:
        raise GitError(f"Cannot resolve ref {ref!r} in {working_dir}: {result.stderr.strip()}")
    return result.stdout.strip()


def create_branch(working_dir: Path, branch_name: str, base_sha: str) -> None:
    """Create a new local branch at base_sha. Raises GitError on failure."""
    result = _run(["git", "branch", branch_name, base_sha], working_dir)
    if result.returncode != 0:
        raise GitError(
            f"Cannot create branch {branch_name!r} at {base_sha!r}: {result.stderr.strip()}"
        )


def checkout_branch(working_dir: Path, branch_name: str) -> None:
    """Check out a local branch. Raises GitError on failure."""
    result = _run(["git", "checkout", branch_name], working_dir)
    if result.returncode != 0:
        raise GitError(
            f"Cannot checkout branch {branch_name!r}: {result.stderr.strip()}"
        )


def push_branch(working_dir: Path, remote_url: str, branch_name: str) -> None:
    """Push a branch to origin. Raises GitError on failure."""
    result = _run(
        ["git", "push", _authenticated_url(remote_url), f"{branch_name}:{branch_name}"],
        working_dir,
    )
    if result.returncode != 0:
        raise GitError(
            f"Cannot push branch {branch_name!r} to origin: {result.stderr.strip()}"
        )


def push_ref(working_dir: Path, remote_url: str, ref_name: str) -> None:
    """Push HEAD to origin/ref_name. Raises GitError on failure."""
    result = _run(
        ["git", "push", _authenticated_url(remote_url), f"HEAD:{ref_name}"],
        working_dir,
    )
    if result.returncode != 0:
        raise GitError(
            f"Cannot push HEAD to origin/{ref_name}: {result.stderr.strip()}"
        )


def git_ls_remote(remote_url: str, ref: str, working_dir: Path) -> str | None:
    """Run git ls-remote and return the SHA for the given ref, or None if absent.

    Uses working_dir as cwd so git credentials/config apply.
    ref should be the full refspec, e.g. 'refs/heads/main'.
    """
    auth_url = _authenticated_url(remote_url)
    result = _run(["git", "ls-remote", auth_url, ref], working_dir)
    if result.returncode != 0:
        raise GitError(
            f"git ls-remote {remote_url!r} {ref!r} failed: {result.stderr.strip()}"
        )
    line = result.stdout.strip()
    if not line:
        return None
    return line.split()[0]


def fetch_ref(working_dir: Path, remote_url: str, branch_name: str) -> None:
    """Fetch a remote branch to get its objects locally.

    Does not update any local branch; only populates object store.
    """
    auth_url = _authenticated_url(remote_url)
    result = _run(
        ["git", "fetch", auth_url,
         f"refs/heads/{branch_name}:refs/foreman/fetched/{branch_name}"],
        working_dir,
    )
    if result.returncode != 0:
        # Fallback: plain fetch of the branch name
        result = _run(
            ["git", "fetch", auth_url, branch_name],
            working_dir,
        )
        if result.returncode != 0:
            raise GitError(
                f"git fetch {branch_name!r} failed: {result.stderr.strip()}"
            )


def is_ancestor_or_equal(working_dir: Path, ancestor: str, descendant: str) -> bool:
    """Return True if ancestor is an ancestor of (or equal to) descendant.

    Uses git merge-base --is-ancestor. Considers a commit ancestor-or-equal of itself.
    """
    result = _run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        working_dir,
    )
    return result.returncode == 0


def ff_merge_local(working_dir: Path, branch_name: str) -> bool:
    """Fast-forward merge branch_name into current HEAD.

    Returns True on success, False if not fast-forward-able (exit 128 or non-zero).
    HEAD must already be checked out to the target base ref.
    """
    result = _run(["git", "merge", "--ff-only", branch_name], working_dir)
    if result.returncode == 0:
        return True
    # exit 1 = merge conflict; exit 128 = other git error (detached HEAD, etc.)
    return False


class OriginCheckResult(str, Enum):
    CONFIRMED = "confirmed"
    PHANTOM_COMPLETION = "phantom-completion"
    ALREADY_CONTAINS = "already-contains"


def check_origin_ground_truth(
    working_dir: Path,
    remote_url: str,
    branch_name: str,
    claimed_sha: str,
    base_sha: str,
) -> tuple[OriginCheckResult, str | None]:
    """R9.AC1/AC4, H2/H3: ground-truth gate against origin.

    Proves the claimed SHA is ancestor-or-equal of the exact target ref on origin
    (not merely present anywhere on origin).

    Returns (outcome, confirmed_sha | None):
    - CONFIRMED: claimed_sha is new and verified on origin; proceed to merge
    - PHANTOM_COMPLETION: origin ref did not advance or SHA is absent; park
    - ALREADY_CONTAINS: claimed_sha was already in origin's history before the build
    """
    confirm_git_state(working_dir, remote_url)

    remote_tip = git_ls_remote(
        remote_url, f"refs/heads/{branch_name}", working_dir
    )

    if remote_tip is None:
        return OriginCheckResult.PHANTOM_COMPLETION, None

    if remote_tip == base_sha:
        # No push occurred: feature branch still at the base we created from
        return OriginCheckResult.PHANTOM_COMPLETION, None

    # Fetch objects so we can use merge-base
    fetch_ref(working_dir, remote_url, branch_name)

    # Check: claimed_sha is ancestor-or-equal of remote tip
    try:
        is_in_remote = is_ancestor_or_equal(working_dir, claimed_sha, remote_tip)
    except Exception:
        return OriginCheckResult.PHANTOM_COMPLETION, None

    if not is_in_remote:
        # Remote advanced but claimed SHA is not in its history
        return OriginCheckResult.PHANTOM_COMPLETION, None

    # Check whether claimed_sha was already there before the build started
    try:
        was_already_there = is_ancestor_or_equal(working_dir, claimed_sha, base_sha)
    except Exception:
        was_already_there = False

    if was_already_there:
        # The SHA existed in the origin's history before this build ran
        return OriginCheckResult.ALREADY_CONTAINS, claimed_sha

    return OriginCheckResult.CONFIRMED, claimed_sha


def post_push_check(
    working_dir: Path,
    remote_url: str,
    base_ref: str,
    merged_sha: str,
) -> bool:
    """R9.AC4: confirm remote ref advanced to or past merged_sha after push.

    Returns True if the remote ref now contains merged_sha.
    Returns False (local-only push) if remote ref did not advance.
    """
    confirm_git_state(working_dir, remote_url)

    remote_tip = git_ls_remote(
        remote_url, f"refs/heads/{base_ref}", working_dir
    )

    if remote_tip is None:
        return False

    if remote_tip == merged_sha:
        return True

    # Accept if merged_sha is ancestor of remote_tip (someone else pushed on top)
    try:
        return is_ancestor_or_equal(working_dir, merged_sha, remote_tip)
    except Exception:
        return False


def _verify_gpg_key(key_id: str) -> None:
    """Verify the GPG key is present and not expired. Raises GPGKeyUnavailableError if not.

    Key material is never written to disk or logs (U-02.AC4).
    key_id is the alias from your secrets manager (FOREMAN_GPG_KEY_ID), never key material.
    """
    result = subprocess.run(
        ["gpg", "--batch", "--status-fd", "1", "--list-secret-keys", key_id],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Redact key_id from error output to avoid leaking the alias in logs
        raise GPGKeyUnavailableError(
            "GPG key unavailable or expired: gpg --list-secret-keys failed. "
            "Set FOREMAN_GPG_KEY_ID in your secrets manager and ensure the key is imported. "
            "(U-02.AC3)"
        )


def gpg_signed_ff_merge(
    working_dir: Path,
    branch_name: str,
    gpg_key_id: str,
) -> bool:
    """Fast-forward merge branch_name with a GPG-signed commit and DCO sign-off (U-02).

    Merges branch_name into current HEAD using --no-ff to produce a merge commit,
    then signs it with gpg_key_id. The merge commit message includes the DCO sign-off line.

    Returns True on success.
    Raises GPGKeyUnavailableError if the key cannot be loaded (U-02.AC3).
    Raises GitError on merge failure.

    Key material is never written to disk or logged (U-02.AC4).
    """
    _verify_gpg_key(gpg_key_id)

    dco = "Signed-off-by: Foreman Runner <foreman@example.com>"
    msg = f"chore(foreman): ff-merge {branch_name}\n\n{dco}"

    result = _git(
        [
            "-c", f"user.signingkey={gpg_key_id}",
            "-c", "commit.gpgsign=true",
            "merge", "--no-ff",
            "-S",
            "-m", msg,
            branch_name,
        ],
        cwd=working_dir,
        capture_output=True,
        text=True,
        env=_GIT_NO_PROMPT_ENV,
    )
    if result.returncode != 0:
        stderr_safe = result.stderr.replace(gpg_key_id, "[REDACTED]") if gpg_key_id else result.stderr
        raise GitError(f"GPG-signed merge failed: {stderr_safe}")
    return True
