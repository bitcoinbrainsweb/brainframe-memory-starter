"""AgentTransport interface and implementations.

The seam between the orchestrator and agent invocation.
Real adapter dispatches Claude API calls.
FakeTransport is used in tests.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from .git_ops import GitError as _GitError
from .git_ops import _git as _git_no_prompt

import os as _os_transport
_GIT_NET_TIMEOUT = int(_os_transport.environ.get("FOREMAN_GIT_TIMEOUT_SECONDS", "120"))


@dataclass
class BuildResult:
    """Result returned by the build agent."""
    commit_sha: str | None
    raw_output: str
    reason: str | None = None
    error_class: str | None = None
    error_body: str | None = None
    status_code: int | None = None
    # R2: wedge duration + last output excerpt when the watchdog killed the session.
    wedge_detail: dict | None = None


@dataclass
class VerifyResult:
    """Result returned by the verify agent."""
    verdict: str  # "PASS" | "FAIL" | "MALFORMED" (unparseable/truncated output)
    findings: str
    raw_output: str


@runtime_checkable
class AgentTransport(Protocol):
    """Interface for dispatching build and verify agents.

    The real adapter calls the Claude API; the fake adapter is used in tests.
    Agents never share context: verify is dispatched fresh with only the diff.
    """

    def build(self, prompt: str, model: str) -> BuildResult:
        """Dispatch a build agent. Returns claimed commit SHA (or None on failure)."""
        ...

    def verify(self, prompt: str, model: str) -> VerifyResult:
        """Dispatch a cold verify agent. Returns verdict PASS or FAIL."""
        ...


class FakeTransport:
    """In-test transport. Creates a real git commit in working_dir when build() is called.

    The integration test passes working_dir so the fake can act like a real build agent:
    make a commit on the current branch, push to origin, return the SHA.

    For unit tests that only need to control the verdict, use verdict_sequence and
    sha_sequence to inject predetermined responses.
    """

    def __init__(
        self,
        working_dir: Path | None = None,
        verdict: str = "PASS",
        findings: str = "",
        sha_override: str | None = None,
        git_user: str = "Test Bot",
        git_email: str = "test@example.com",
    ) -> None:
        self._wd = working_dir
        self._verdict = verdict
        self._findings = findings
        self._sha_override = sha_override
        self._git_user = git_user
        self._git_email = git_email
        self.build_calls: list[dict] = []
        self.verify_calls: list[dict] = []

    def build(self, prompt: str, model: str) -> BuildResult:
        self.build_calls.append({"prompt": prompt, "model": model})

        if self._sha_override is not None:
            return BuildResult(commit_sha=self._sha_override, raw_output=f"fake build; sha={self._sha_override}")

        if self._wd is None:
            # Return a fake non-None SHA for unit tests that don't need real git
            fake_sha = "a" * 40
            return BuildResult(commit_sha=fake_sha, raw_output=f"fake build; sha={fake_sha}")

        # Create a real commit in the working dir (integration test path)
        marker = uuid.uuid4().hex[:8]
        test_file = self._wd / f"foreman_test_{marker}.txt"
        test_file.write_text(f"foreman build output {marker}\n")

        env_patch = {
            "GIT_AUTHOR_NAME": self._git_user,
            "GIT_AUTHOR_EMAIL": self._git_email,
            "GIT_COMMITTER_NAME": self._git_user,
            "GIT_COMMITTER_EMAIL": self._git_email,
        }
        import os
        env = {**os.environ, **env_patch}

        subprocess.run(
            ["git", "add", test_file.name],
            cwd=self._wd, check=True, capture_output=True, env=env,
        )
        subprocess.run(
            ["git", "commit", "-m", f"fake build commit [{marker}]"],
            cwd=self._wd, check=True, capture_output=True, env=env,
        )
        subprocess.run(
            ["git", "push", "origin", "HEAD"],
            cwd=self._wd, check=True, capture_output=True, env=env,
        )
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=self._wd,
        ).decode().strip()

        return BuildResult(commit_sha=sha, raw_output=f"fake build commit={sha}")

    def verify(self, prompt: str, model: str) -> VerifyResult:
        self.verify_calls.append({"prompt": prompt, "model": model})
        return VerifyResult(
            verdict=self._verdict,
            findings=self._findings,
            raw_output=f"VERDICT: {self._verdict}\n{self._findings}",
        )


class FailingBuildTransport:
    """Transport whose build() returns no SHA, simulating a failed build."""

    def build(self, prompt: str, model: str) -> BuildResult:
        return BuildResult(commit_sha=None, raw_output="build failed")

    def verify(self, prompt: str, model: str) -> VerifyResult:
        return VerifyResult(verdict="FAIL", findings="build did not produce a commit", raw_output="")


# ---------------------------------------------------------------------------
# Phase C real adapter
# ---------------------------------------------------------------------------

_VERDICT_RE = re.compile(r"\bVERDICT:\s*(PASS|FAIL)\b", re.IGNORECASE)

_BUILD_SYSTEM_PROMPT = (
    "You are a build agent in the Foreman orchestration system. "
    "Implement the specification in the repository mounted at /workspace and commit your changes. "
    "Use read_file to read existing files, write_file to create or modify files, "
    "bash to run tests and linters, and git (allowed: add status diff commit log show) to "
    "stage and commit. Do NOT push -- the orchestrator handles pushing. "
    "Make exactly one commit that implements the specification."
)

_VERIFY_SYSTEM_PROMPT_TEMPLATE = (
    "You are a cold verify agent in the Foreman orchestration system. "
    "Review the build commit diff below and verify it meets all acceptance criteria. "
    "Use read_file (read-only) and bash (write to /tmp only) to inspect the workspace. "
    "Do NOT modify workspace files. "
    "End your response with VERDICT: PASS or VERDICT: FAIL on its own line, "
    "followed by your detailed findings.\n\n"
    "## Diff of the build commit\n\n```diff\n{diff}\n```"
)


def _is_local_remote(url: str) -> bool:
    return not url.startswith(("http://", "https://", "git@", "ssh://", "git://"))


def _fetch_push_token(remote_url: str | None = None) -> str | None:
    """Fetch the push token from env or your secrets manager into memory only.

    Forwards to git_ops._resolve_push_token (single source of truth). When
    remote_url names a github repo, the resolver validates candidates against it
    so a token scoped to other repos is skipped. Never logged, never in argv.
    """
    from .git_ops import _resolve_push_token
    return _resolve_push_token(remote_url)


def _push_url(remote_url: str, token: str | None) -> str:
    """On Windows, return a token-embedded push URL (GIT_ASKPASS is unreliable
    against GCM); on POSIX, return the bare URL. Never log the result."""
    import sys as _sys
    if _sys.platform == "win32" and token and remote_url.startswith("https://github.com/"):
        return remote_url.replace(
            "https://github.com/", f"https://x-access-token:{token}@github.com/", 1
        )
    return remote_url



def _write_askpass_script() -> str:
    """Write a platform-correct GIT_ASKPASS helper that echoes the token.

    Windows: .cmd file using %VAR% expansion (Windows git can execute .cmd natively).
    POSIX: .sh file using $VAR expansion with executable bit set.
    Token never appears in argv or logs; it lives in the subprocess env only.
    """
    import sys as _sys
    if _sys.platform == "win32":
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".cmd", delete=False, prefix="foreman_askpass_"
        ) as f:
            f.write("@echo %_FOREMAN_ASKPASS_TOKEN%\r\n")
            path = f.name
        # No chmod needed on Windows; .cmd executes by extension
    else:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sh", delete=False, prefix="foreman_askpass_"
        ) as f:
            f.write('#!/bin/sh\necho "$_FOREMAN_ASKPASS_TOKEN"\n')
            path = f.name
        os.chmod(path, stat.S_IRWXU)
    return path


def _credential_git_flags() -> list[str]:
    """Extra -c flags that suppress interactive credential dialogs on Windows."""
    import sys as _sys
    if _sys.platform == "win32":
        return ["-c", "credential.interactive=false"]
    return []


def _git_sha(workspace: Path, env: dict) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=workspace, env=env
    ).decode().strip()


def _git_current_branch(workspace: Path, env: dict) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace, env=env
    ).decode().strip()


def _git_show_head(workspace: Path, env: dict) -> str:
    result = subprocess.run(
        ["git", "show", "--stat", "--patch", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout if result.returncode == 0 else ""


def _extract_verdict(raw_output: str) -> tuple[str, str]:
    """Parse a verdict from agent output into (verdict, findings).

    Three classes (Bug 1): PASS and FAIL are well-formed judgments; MALFORMED is
    unparseable / schema-invalid / truncated output that carries no parseable
    VERDICT token, OR a bare ``VERDICT: FAIL`` with no findings after it.
    MALFORMED must never be recorded as a genuine FAIL: doing so burns a spec's
    build-attempt budget on output the harness could not read. The caller retries
    the verify step (not the build) on MALFORMED, then parks verify-malformed
    (operational / retryable) after the retry cap.

    Findings are the text AFTER the VERDICT line. A FAIL whose findings are empty
    means the verifier either emitted a bare verdict or placed its findings above
    the verdict line where they are discarded (run fm-20260708-2253 parked 3/3
    specs this way, a verdict-last verifier). That is unresolvable output, so it
    is classified MALFORMED rather than a spurious FAIL.
    """
    match = _VERDICT_RE.search(raw_output)
    if match:
        verdict = match.group(1).upper()
        lines = raw_output.splitlines()
        verdict_line = next(
            (i for i, ln in enumerate(lines) if re.search(r"\bVERDICT:", ln, re.IGNORECASE)),
            len(lines) - 1,
        )
        findings = "\n".join(lines[verdict_line + 1:]).strip()
        if verdict == "FAIL" and not findings:
            return "MALFORMED", (
                "[malformed: VERDICT:FAIL emitted with no findings after the"
                " verdict line; treating as unresolvable verifier output]"
            )
        return verdict, findings
    # No parseable VERDICT token: unparseable or truncated before the verdict.
    # This is MALFORMED, not a judged FAIL.
    return "MALFORMED", raw_output.strip()


# Register _extract_verdict as the foreman_verdict handler in the structured-output
# repair layer (admin-structured-output-repair-v1). This UNIFIES the verdict surface
# into the one registry without changing the parser's behavior: the layer wraps this
# function; the verdict retry/park loop stays in bundle_runner._dispatch_verify.
from . import output_schema as _output_schema  # noqa: E402

_output_schema.register_verdict_handler(_extract_verdict)


def _push_from_orchestrator(
    workspace: Path,
    current_branch: str,
    remote_url: str,
    git_env: dict,
) -> bool:
    """Push HEAD to origin/<current_branch> from the orchestrator.

    Token never in argv: supplied via GIT_ASKPASS env var pointing to a temp
    script that reads _FOREMAN_ASKPASS_TOKEN from the subprocess environment.
    Token never in logs: we do not print it.
    """
    env = {**git_env, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GCM_PROVIDER": ""}
    askpass_path: str | None = None

    push_target = "origin"
    if not _is_local_remote(remote_url):
        token = _fetch_push_token(remote_url)
        if token:
            askpass_path = _write_askpass_script()
            env["GIT_ASKPASS"] = askpass_path
            env["_FOREMAN_ASKPASS_TOKEN"] = token  # in subprocess env only, not argv
            push_target = _push_url(remote_url, token)

    try:
        result = _git_no_prompt(
            ["-c", "core.hooksPath=/dev/null",
             *_credential_git_flags(),
             "push", push_target, f"HEAD:{current_branch}"],
            cwd=workspace,
            capture_output=True,
            text=True,
            env=env,
            timeout=_GIT_NET_TIMEOUT,
        )
        return result.returncode == 0
    except _GitError:
        return False
    finally:
        if askpass_path:
            try:
                Path(askpass_path).unlink()
            except OSError:
                pass


class ApiAgentTransport:
    """Real Claude API transport. Implements AgentTransport Protocol synchronously.

    The agent loop runs in the orchestrator process via httpx.Client (sync).
    Tools are dispatched via an injectable factory (in-process for tests,
    ForemanSandbox for production). The build agent commits inside the dispatcher
    workspace; the orchestrator pushes from the host after the loop exits.
    The push token is fetched from your secrets manager at call time, lives in process memory
    only, and is injected via GIT_ASKPASS (never in argv, never logged).

    Constructor parameters prefixed with _ are test seams; do not pass in production.
    """

    def __init__(
        self,
        workspace: Path,
        remote_url: str,
        api_key: str | None = None,
        *,
        _httpx_client: Any = None,
        _dispatcher_factory: Callable[[Path, bool], Any] | None = None,
        _git_env: dict | None = None,
    ) -> None:
        self._workspace = Path(workspace)
        self._remote_url = remote_url
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._httpx_client = _httpx_client
        self._dispatcher_factory = _dispatcher_factory
        self._git_env = _git_env or os.environ.copy()

    def build(self, prompt: str, model: str) -> BuildResult:
        """Dispatch a build agent. Returns BuildResult with commit_sha (or None on failure)."""
        from .agent_harness import AgentHarness
        from .agent_tools import TOOL_SCHEMAS, InProcessDispatcher

        start_sha = _git_sha(self._workspace, self._git_env)
        current_branch = _git_current_branch(self._workspace, self._git_env)

        dispatcher = self._make_dispatcher(readonly=False)

        harness = AgentHarness(
            api_key=self._api_key,
            model=model,
            system_prompt=_BUILD_SYSTEM_PROMPT,
            tool_schemas=TOOL_SCHEMAS,
            dispatcher=dispatcher,
            httpx_client=self._httpx_client,
            retry_delays=[0, 0, 0] if self._httpx_client is not None else None,
        )

        from .agent_harness import ForemanApiError
        try:
            run_result = harness.run(prompt)
        except ForemanApiError as exc:
            return BuildResult(
                commit_sha=None,
                raw_output=str(exc),
                error_class="build-api-error",
                error_body=exc.error_body,
                status_code=exc.status_code,
            )

        raw_output = run_result.raw_output

        end_sha = _git_sha(self._workspace, self._git_env)
        if end_sha == start_sha:
            return BuildResult(
                commit_sha=None,
                raw_output=raw_output or "Build agent made no commit.",
                error_class="build-no-commit",
            )

        push_ok = _push_from_orchestrator(
            workspace=self._workspace,
            current_branch=current_branch,
            remote_url=self._remote_url,
            git_env=self._git_env,
        )
        if not push_ok:
            return BuildResult(
                commit_sha=None,
                raw_output=raw_output + "\npush failed",
                error_class="build-push-failed",
            )

        return BuildResult(commit_sha=end_sha, raw_output=raw_output)

    def verify(self, prompt: str, model: str) -> VerifyResult:
        """Dispatch a cold verify agent. Returns VerifyResult with PASS or FAIL verdict."""
        from .agent_harness import AgentHarness
        from .agent_tools import VERIFY_TOOL_SCHEMAS

        diff = _git_show_head(self._workspace, self._git_env)
        system_prompt = _VERIFY_SYSTEM_PROMPT_TEMPLATE.format(diff=diff)

        dispatcher = self._make_dispatcher(readonly=True)

        harness = AgentHarness(
            api_key=self._api_key,
            model=model,
            system_prompt=system_prompt,
            tool_schemas=VERIFY_TOOL_SCHEMAS,
            dispatcher=dispatcher,
            httpx_client=self._httpx_client,
            retry_delays=[0, 0, 0] if self._httpx_client is not None else None,
        )

        run_result = harness.run(prompt)
        raw_output = run_result.raw_output

        verdict, findings = _extract_verdict(raw_output)
        return VerifyResult(verdict=verdict, findings=findings, raw_output=raw_output)

    def _make_dispatcher(self, readonly: bool) -> Any:
        """Create a tool dispatcher using the injected factory or sandbox."""
        if self._dispatcher_factory is not None:
            return self._dispatcher_factory(self._workspace, readonly)

        from .sandbox import ForemanSandbox
        raise NotImplementedError(
            "Production sandbox dispatcher requires a running ForemanSandbox context; "
            "use _dispatcher_factory for tests."
        )


# ---------------------------------------------------------------------------
# PTY CLI adapter
# ---------------------------------------------------------------------------

import uuid as _uuid

from .cli_session import (
    assert_no_credential_surface,
    assert_no_mcp_connectors,
    build_scrubbed_env,
    cleanup_sandbox,
    configure_git_identity,
    create_sandbox_worktree,
    setup_cli_home,
)
from .pty_harness import (
    BUILDER_CLI,
    FOREMAN_AGENT_WALLCLOCK_BUDGET,
    FOREMAN_VERIFY_WALLCLOCK_BUDGET,
    HarnessResult,
    LogicalRunContext,
    PtyHarness,
    _HAS_SPAWN,
    run_with_resumes,
)
from .watchdog import (
    SessionWatchdog,
    WEDGE_REASONS,
    attempt_max_s,
    heartbeat_interval_s,
)


_CLI_VERIFY_DIFF_PREFIX = (
    "You are a cold verify agent. Review the diff below and verify it meets all "
    "acceptance criteria. End your response with VERDICT: PASS or VERDICT: FAIL on "
    "its own line, followed by your detailed findings.\n\n"
    "## Diff of the build commit\n\n```diff\n{diff}\n```\n\n"
    "## Your task\n\n{prompt}"
)


def _push_from_orchestrator_nonforce(
    workspace: Path,
    current_branch: str,
    remote_url: str,
    git_env: dict,
) -> tuple[bool, str]:
    """Push branch to origin NON-FORCE. Returns (success, reason)."""
    env = {**git_env, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GCM_PROVIDER": ""}
    askpass_path: str | None = None
    push_target = "origin"

    if not _is_local_remote(remote_url):
        token = _fetch_push_token(remote_url)
        if token:
            askpass_path = _write_askpass_script()
            env["GIT_ASKPASS"] = askpass_path
            env["_FOREMAN_ASKPASS_TOKEN"] = token
            # On Windows GIT_ASKPASS is unreliable against Git Credential Manager;
            # push to the token-embedded URL directly. Never logged.
            push_target = _push_url(remote_url, token)

    try:
        result = _git_no_prompt(
            ["-c", "core.hooksPath=/dev/null",
             *_credential_git_flags(),
             "push", push_target, f"{current_branch}:{current_branch}"],
            cwd=workspace, capture_output=True, text=True, env=env,
            timeout=_GIT_NET_TIMEOUT,
        )
        if result.returncode == 0:
            return True, ""
        stderr = result.stderr.lower()
        if "non-fast-forward" in stderr or "rejected" in stderr:
            return False, "push-rejected-non-fast-forward"
        return False, f"push-failed: {result.stderr.strip()[:200]}"
    except _GitError:
        return False, "push-timeout"
    finally:
        if askpass_path:
            try:
                Path(askpass_path).unlink()
            except OSError:
                pass


class CliAgentTransport:
    """PTY CLI transport. Implements AgentTransport Protocol synchronously.

    Dispatch drives the interactive Claude Code CLI on the host's subscription
    auth via a PTY harness (pexpect). Model is passed via --model flag only --
    never /model in-session (that writes to process-global settings.json and
    would propagate to all sessions).

    build() creates branch foreman/<spec-slug>/<run-id>, CLI agent commits,
    orchestrator pushes NON-FORCE after the session exits. Push token fetched
    from your secrets manager at call time into process memory only -- never in CLI env,
    never in argv, never logged.

    verify() spawns a cold session in a fresh clone at the build SHA, with the
    unified diff as initial context. Never sees the build agent reasoning.

    Parameters prefixed with _ are test seams; do not pass in production.
    """

    def __init__(
        self,
        workspace: Path,
        remote_url: str,
        spec_slug: str | None = None,
        run_id: str | None = None,
        *,
        foreman_cli_home: Path | str | None = None,
        sandbox_root: Path | str | None = None,
        builder_model: str | None = None,
        verifier_model: str | None = None,
        _spawn_fn: Any = None,
        _git_env: dict | None = None,
    ) -> None:
        self._workspace = Path(workspace)
        self._remote_url = remote_url
        self._spec_slug = spec_slug
        self._run_id = run_id or str(_uuid.uuid4())
        self._foreman_cli_home = Path(
            foreman_cli_home or os.environ.get("FOREMAN_CLI_HOME", str(Path.home() / ".foreman-cli"))
        )
        self._sandbox_root = Path(
            sandbox_root or os.environ.get("FOREMAN_SANDBOX_ROOT", str(Path.home() / ".foreman-sandbox"))
        )
        self._builder_model = builder_model or os.environ.get("FOREMAN_BUILDER_MODEL", "your-builder-model")
        self._verifier_model = verifier_model or os.environ.get("FOREMAN_VERIFIER_MODEL", "your-verifier-model")
        self._spawn_fn = _spawn_fn
        self._git_env = _git_env or os.environ.copy()
        # R2: heartbeat sink (bound by bundle_runner per task) + last-session failure
        # count for the run-report note.
        self._heartbeat_sink: Callable[[], None] | None = None
        self._last_hb_failures: int = 0

    # ------------------------------------------------------------------
    # Per-task binding (multi-spec bundles): the runner calls set_task before
    # each build/verify so one transport instance serves every spec in a bundle.
    # ------------------------------------------------------------------

    def set_task(self, spec_slug: str, run_id: str) -> None:
        """Bind the transport to the spec_slug and run_id of the next build/verify."""
        self._spec_slug = spec_slug
        self._run_id = run_id

    def set_heartbeat_sink(self, sink: Callable[[], None] | None) -> None:
        """Bind the last_heartbeat_at writer used by the liveness watchdog.

        Called by bundle_runner before each task with a sink bound to (ledger, run_id,
        spec_slug). Duck-typed: transports without a watchdog ignore it.
        """
        self._heartbeat_sink = sink

    def _make_watchdog(self) -> SessionWatchdog:
        """Build a fresh liveness watchdog for one build/verify session."""
        return SessionWatchdog(heartbeat_sink=self._heartbeat_sink)

    @property
    def last_heartbeat_failures(self) -> int:
        """Consecutive heartbeat-write failures observed in the last session."""
        return self._last_hb_failures

    # ------------------------------------------------------------------
    # AgentTransport Protocol implementation
    # ------------------------------------------------------------------

    def _run_print_mode(
        self,
        prompt: str,
        model: str,
        cwd: Path,
        env: dict,
        timeout: int = 1800,
        disallowed_tools: tuple[str, ...] = (),
        watchdog: SessionWatchdog | None = None,
    ) -> HarnessResult:
        """Windows fallback: run `claude --print <prompt>` via subprocess (no PTY required).

        Used when pexpect.spawn is unavailable (Windows without real PTY support).
        The --print flag runs Claude non-interactively; tools (Bash/Write/Edit) still execute.
        Terminal reason is 'turn_complete' on exit code 0, else 'cli-crashed'.

        R2: the print path cannot stream output for byte-level wedge detection, but the
        hard per-attempt wall-clock cap still applies -- the subprocess timeout
        is bounded by FOREMAN_ATTEMPT_MAX_SECONDS -- and a background thread keeps the
        heartbeat fresh while the process is alive (timer fallback).
        """
        import shutil as _shutil
        import sys as _sys
        import threading as _threading

        # On Windows, BUILDER_CLI is often a .cmd wrapper via npm.
        # subprocess with shell=False can't find .cmd files; resolve to full path.
        # Prompt is passed via stdin to avoid cmd.exe newline-quoting issues.
        import time as _time

        builder_exe = _shutil.which(BUILDER_CLI) or BUILDER_CLI
        # CREATE_NO_WINDOW (0x08000000) prevents a console window from appearing on Windows.
        _creation_flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if _sys.platform == "win32"
            else 0
        )
        # Keep --dangerously-skip-permissions (required for non-interactive --print:
        # without it the CLI blocks on tool-approval prompts that no human can answer),
        # but scope it down with an explicit deny list so the agent cannot perform
        # actions the orchestrator owns (e.g. git push). Build/test still work.
        cmd = [builder_exe, "--model", model, "--dangerously-skip-permissions", "--print"]
        for _tool in disallowed_tools:
            cmd += ["--disallowedTools", _tool]

        # bound the subprocess timeout by the hard per-attempt cap.
        effective_timeout = min(timeout, attempt_max_s())

        # background timer-fallback heartbeat while the subprocess runs.
        _hb_stop = _threading.Event()
        _hb_thread = None
        if watchdog is not None:
            watchdog.start()

            def _hb_loop() -> None:
                interval = max(1, heartbeat_interval_s())
                while not _hb_stop.wait(interval):
                    watchdog.tick(alive=True)

            _hb_thread = _threading.Thread(target=_hb_loop, daemon=True,
                                           name="foreman-print-hb")
            _hb_thread.start()

        _start = _time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                shell=(builder_exe.lower().endswith(".cmd")),
                creationflags=_creation_flags,
            )
            _elapsed = _time.monotonic() - _start
            raw = (result.stdout or "") + (result.stderr or "")
            reason = "turn_complete" if result.returncode == 0 else "cli-crashed"
            return HarnessResult(
                raw_output=raw,
                terminal_reason=reason,
                turns_used=1,
                wallclock_active_secs=_elapsed,
            )
        except subprocess.TimeoutExpired:
            _elapsed = _time.monotonic() - _start
            # Timed out at the hard cap: treat identically to a wedge.
            _reason = "attempt-wallclock-exceeded"
            _wedge_detail = watchdog.wedge_detail(_reason) if watchdog is not None else None
            return HarnessResult(
                raw_output="",
                terminal_reason=_reason,
                turns_used=0,
                wallclock_active_secs=_elapsed,
                wedge_detail=_wedge_detail,
            )
        finally:
            _hb_stop.set()
            if _hb_thread is not None:
                _hb_thread.join(timeout=2)

    def build(self, prompt: str, model: str) -> BuildResult:
        """Dispatch build agent via PTY CLI. Orchestrator pushes after session exits."""
        if not self._spec_slug:
            raise RuntimeError("CliAgentTransport.build called before set_task; spec_slug unbound")

        # Setup CLI home with model-pin settings
        setup_cli_home(self._foreman_cli_home, self._builder_model, self._verifier_model)

        # Unique sandbox ID per call to avoid collision on retry when Windows rmtree
        # silently fails to clean up the previous attempt's sandbox (file locking).
        _sandbox_id = f"{self._run_id}-{self._spec_slug}-{_uuid.uuid4().hex[:6]}"
        self._sandbox_root.mkdir(parents=True, exist_ok=True)

        # Build credential-aware git env for sandbox clone (T4: thread askpass through clone).
        _clone_git_env, _clone_askpass = self._make_clone_env()
        sandbox = create_sandbox_worktree(
            self._workspace, self._sandbox_root, _sandbox_id, git_env=_clone_git_env
        )

        try:
            # Update sandbox origin to point to the real remote_url
            subprocess.run(
                ["git", "remote", "set-url", "origin", self._remote_url],
                cwd=sandbox, check=True, capture_output=True, env=self._git_env,
            )

            # The bundle_runner already created and checked out the build branch in
            # workspace before calling build(). The sandbox is cloned from workspace
            # so it starts on that same branch. Commit to it directly - no foreman/*
            # branch needed (bundle_runner tracks build/* and verifies via that ref).
            branch_name = self._git_current_branch(sandbox)
            base_sha = self._git_sha(sandbox)

            configure_git_identity(sandbox, git_env=self._git_env)

            env = build_scrubbed_env(str(self._foreman_cli_home))

            # R2: one liveness watchdog per session (heartbeat + wedge/wall-clock kill).
            watchdog = self._make_watchdog()
            if _HAS_SPAWN or self._spawn_fn is not None:
                # Unix / test path: PTY harness with pexpect.spawn.
                ctx = LogicalRunContext(run_id=self._run_id)
                harness = PtyHarness(spawn_fn=self._spawn_fn, watchdog=watchdog)
                result = run_with_resumes(harness, prompt, model, sandbox, env, ctx)
            else:
                # Windows fallback: claude --print (no PTY needed).
                # Pass build budget explicitly so verify can use its tighter budget.
                result = self._run_print_mode(
                    prompt, model, sandbox, env,
                    timeout=FOREMAN_AGENT_WALLCLOCK_BUDGET,
                    disallowed_tools=("Bash(git push:*)",),
                    watchdog=watchdog,
                )
            self._last_hb_failures = watchdog.heartbeat_failures

            # the watchdog killed a wedged / over-cap session. Report it as
            # builder-wedged so bundle_runner retries once then parks. Handled before the
            # commit check: a killed session's partial commit is not trustworthy.
            if result.terminal_reason in WEDGE_REASONS:
                return BuildResult(
                    commit_sha=None,
                    raw_output=result.raw_output or "",
                    reason=result.terminal_reason,
                    error_class="builder-wedged",
                    wedge_detail=result.wedge_detail,
                )

            # The orchestrator owns the push. Builder sessions are denied `git push`
            # (deny-list on the print path / sandbox policy), so whether the session
            # ended cleanly or crashed, any commits it left on the branch are pushed
            # from HERE with the orchestrator's credentials before we conclude the
            # branch is empty. A non-terminal session that still produced commits
            # must never be discarded as build-no-commit -- that silently ate two
            # commits of real work in fm-20260706-1622-a6c4fd.
            end_sha = self._git_sha(sandbox)
            has_commits = end_sha != base_sha

            if not has_commits:
                # Genuinely empty branch. Preserve the terminal-reason signal for a
                # crashed/timed-out session; otherwise a clean no-commit turn. Both
                # are build-no-commit (empty branch), distinct from build-unpushed.
                reason = (
                    result.terminal_reason
                    if result.terminal_reason not in ("turn_complete",)
                    else "no-commit"
                )
                return BuildResult(
                    commit_sha=None,
                    raw_output=result.raw_output or "Build agent made no commit.",
                    reason=reason,
                    error_class="build-no-commit",
                )

            # Branch has commits: the orchestrator-side push is mandatory, not gated
            # on how the agent session terminated.
            push_ok, push_reason = _push_from_orchestrator_nonforce(
                workspace=sandbox,
                current_branch=branch_name,
                remote_url=self._remote_url,
                git_env=self._git_env,
            )
            if not push_ok:
                # Commits exist but are not on the remote. Reserve build-no-commit
                # for a genuinely empty branch; classify this as build-unpushed so
                # the orchestrator parks with the push error, not "no commit".
                return BuildResult(
                    commit_sha=None,
                    raw_output=result.raw_output + f"\n{push_reason}",
                    reason=push_reason,
                    error_class="build-unpushed",
                )

            # Sync workspace's local build branch from origin so ff_merge_local works.
            # The sandbox committed and pushed; workspace's local branch ref needs updating.
            self._sync_workspace_branch(branch_name)

            commit_sha = self._git_sha(sandbox)
            return BuildResult(commit_sha=commit_sha, raw_output=result.raw_output)

        finally:
            if _clone_askpass:
                try:
                    Path(_clone_askpass).unlink()
                except OSError:
                    pass
            cleanup_sandbox(sandbox)

    def verify(self, prompt: str, model: str) -> VerifyResult:
        """Dispatch cold verify agent in a fresh clone. Never sees build reasoning."""
        setup_cli_home(self._foreman_cli_home, self._builder_model, self._verifier_model)

        verify_id = str(_uuid.uuid4())
        self._sandbox_root.mkdir(parents=True, exist_ok=True)

        # Build credential-aware git env for sandbox clone (T4: thread askpass through clone).
        _clone_git_env, _clone_askpass = self._make_clone_env()
        sandbox = create_sandbox_worktree(
            self._workspace, self._sandbox_root, verify_id, git_env=_clone_git_env
        )

        try:
            # Update sandbox origin to point to the real remote_url
            subprocess.run(
                ["git", "remote", "set-url", "origin", self._remote_url],
                cwd=sandbox, check=True, capture_output=True, env=self._git_env,
            )

            diff = self._get_head_diff(sandbox)
            full_prompt = _CLI_VERIFY_DIFF_PREFIX.format(diff=diff, prompt=prompt)

            configure_git_identity(sandbox, git_env=self._git_env)
            env = build_scrubbed_env(str(self._foreman_cli_home))
            # a verifier session is heartbeated and wedge-watched like a builder.
            watchdog = self._make_watchdog()
            if _HAS_SPAWN or self._spawn_fn is not None:
                # Unix / test path: PTY harness with tighter verify budget (T3).
                ctx = LogicalRunContext(run_id=verify_id,
                                       wallclock_budget=FOREMAN_VERIFY_WALLCLOCK_BUDGET)
                harness = PtyHarness(spawn_fn=self._spawn_fn, watchdog=watchdog)
                result = run_with_resumes(harness, full_prompt, model, sandbox, env, ctx)
            else:
                # Windows fallback: claude --print with tighter verify budget (T3).
                result = self._run_print_mode(
                    full_prompt, model, sandbox, env,
                    timeout=FOREMAN_VERIFY_WALLCLOCK_BUDGET,
                    disallowed_tools=("Bash(git push:*)", "Write", "Edit"),
                    watchdog=watchdog,
                )
            self._last_hb_failures = watchdog.heartbeat_failures

            # a wedged verifier is killed; surface FAIL so the standard
            # retry/park path runs rather than hanging the run indefinitely.
            if result.terminal_reason in WEDGE_REASONS:
                return VerifyResult(
                    verdict="FAIL",
                    findings=f"verifier session killed: {result.terminal_reason}",
                    raw_output=result.raw_output or "",
                )

            verdict, findings = _extract_verdict(result.raw_output)
            return VerifyResult(verdict=verdict, findings=findings, raw_output=result.raw_output)

        finally:
            if _clone_askpass:
                try:
                    Path(_clone_askpass).unlink()
                except OSError:
                    pass
            cleanup_sandbox(sandbox)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _git_run(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=self._git_env)

    def _git_sha(self, working_dir: Path) -> str:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=working_dir, env=self._git_env
        ).decode().strip()

    def _git_current_branch(self, working_dir: Path) -> str:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=working_dir, env=self._git_env,
        ).decode().strip()

    def _git_sha_from_branch(self, working_dir: Path, branch_name: str) -> str:
        return subprocess.check_output(
            ["git", "rev-parse", f"refs/heads/{branch_name}"],
            cwd=working_dir,
            env=self._git_env,
        ).decode().strip()

    def _get_head_diff(self, working_dir: Path) -> str:
        result = subprocess.run(
            ["git", "show", "--stat", "--patch", "HEAD"],
            cwd=working_dir, capture_output=True, text=True, env=self._git_env,
        )
        return result.stdout if result.returncode == 0 else ""

    def _make_clone_env(self) -> tuple[dict, str | None]:
        """Build git env for sandbox clone with credential suppression (T4).

        Returns (enriched_env, askpass_path_or_None). Caller must unlink askpass_path
        in a finally block.
        """
        env: dict = {**self._git_env, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GCM_PROVIDER": ""}
        if _is_local_remote(self._remote_url):
            return env, None
        token = _fetch_push_token(self._remote_url)
        if not token:
            return env, None
        askpass_path = _write_askpass_script()
        env["GIT_ASKPASS"] = askpass_path
        env["_FOREMAN_ASKPASS_TOKEN"] = token
        return env, askpass_path

    def _sync_workspace_branch(self, branch_name: str) -> None:
        """Fetch the build branch from origin and ff-merge into workspace's local branch.

        After the sandbox pushes new commits to origin, workspace's local branch is still
        at base_sha. This brings it current so bundle_runner's ff_merge_local works.
        git fetch with src:dst cannot update a checked-out branch; use fetch+merge instead.
        """
        env = {**self._git_env, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GCM_PROVIDER": ""}
        askpass_path: str | None = None
        if not _is_local_remote(self._remote_url):
            token = _fetch_push_token(self._remote_url)
            if token:
                askpass_path = _write_askpass_script()
                env["GIT_ASKPASS"] = askpass_path
                env["_FOREMAN_ASKPASS_TOKEN"] = token
        try:
            try:
                fetch_ok = _git_no_prompt(
                    [*_credential_git_flags(),
                     "fetch", self._remote_url, branch_name],
                    cwd=self._workspace, capture_output=True, env=env,
                    timeout=_GIT_NET_TIMEOUT,
                ).returncode == 0
            except _GitError:
                fetch_ok = False
            if fetch_ok:
                subprocess.run(
                    ["git", "merge", "--ff-only", "FETCH_HEAD"],
                    cwd=self._workspace, capture_output=True, env=self._git_env,
                )
        finally:
            if askpass_path:
                try:
                    Path(askpass_path).unlink()
                except OSError:
                    pass
