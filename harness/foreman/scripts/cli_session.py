"""Per-attempt CLI session lifecycle for the PTY harness.

Manages launch env scrub, FOREMAN_CLI_HOME setup, disposable working trees,
git identity configuration, and orphaned process cleanup.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

_GIT_NET_TIMEOUT = int(os.environ.get("FOREMAN_GIT_TIMEOUT_SECONDS", "120"))

# Only these keys may appear in the CLI subprocess environment.
# HOME is replaced with FOREMAN_CLI_HOME.
# Windows requires additional system vars for Node/npm/the builder CLI to initialise;
# they carry no secrets, so including them is safe.
_ALLOWLIST: frozenset[str] = frozenset({"PATH", "HOME", "LANG", "TERM"})
_WINDOWS_ALLOWLIST: frozenset[str] = frozenset({
    "APPDATA", "LOCALAPPDATA", "USERPROFILE", "USERNAME",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
    "COMPUTERNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "TEMP", "TMP", "OS", "COMSPEC",
    "NODE_PATH", "npm_config_prefix",
})

# Credential file patterns that must not appear in FOREMAN_CLI_HOME.
_FORBIDDEN_PATTERNS: list[re.Pattern] = [
    re.compile(r"\.aws", re.IGNORECASE),
    re.compile(r"doppler", re.IGNORECASE),
    re.compile(r"\.env", re.IGNORECASE),
    re.compile(r"service.{0,10}key", re.IGNORECASE),
    re.compile(r"credentials", re.IGNORECASE),
]


def build_scrubbed_env(foreman_cli_home: str) -> dict[str, str]:
    """Build an allowlist-only env for the CLI subprocess.

    Constructed from scratch (never inherit-then-delete) so secrets
    in the orchestrator env cannot leak into the CLI process.
    HOME is replaced with foreman_cli_home.
    On Windows, additional system vars are included (they carry no secrets).
    """
    import sys as _sys
    parent = os.environ
    active_allowlist = _ALLOWLIST | (_WINDOWS_ALLOWLIST if _sys.platform == "win32" else frozenset())
    env: dict[str, str] = {}
    for key in active_allowlist:
        if key == "HOME":
            env["HOME"] = foreman_cli_home
        elif key in parent:
            env[key] = parent[key]
    return env


def setup_cli_home(
    cli_home: Path,
    builder_model: str,
    verifier_model: str,
) -> None:
    """Write model-pin settings.json to FOREMAN_CLI_HOME/.claude/.

    Sets availableModels + enforceAvailableModels=true so a
    stray /model command cannot select an off-allowlist model.
    No .mcp.json or connector config is written.
    """
    claude_dir = cli_home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            settings = {}
    settings["availableModels"] = [builder_model, verifier_model]
    settings["enforceAvailableModels"] = True
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def assert_no_mcp_connectors(cli_home: Path) -> None:
    """Raise AssertionError if any MCP connector config exists under cli_home."""
    for path in cli_home.rglob("*"):
        name = path.name.lower()
        if ".mcp" in name or "connector" in name:
            raise AssertionError(f"MCP connector config found: {path}")


def assert_no_credential_surface(cli_home: Path) -> None:
    """Raise AssertionError if any forbidden credential files exist under cli_home."""
    for path in cli_home.rglob("*"):
        rel = str(path.relative_to(cli_home))
        for pat in _FORBIDDEN_PATTERNS:
            if pat.search(rel):
                raise AssertionError(f"Credential surface found in cli_home: {path}")


def create_sandbox_worktree(
    workspace: Path,
    sandbox_root: Path,
    run_id: str,
    *,
    git_env: dict[str, str] | None = None,
) -> Path:
    """Clone workspace into a disposable directory under sandbox_root.

    Returns path to the sandbox working tree.
    """
    sandbox = sandbox_root / run_id
    sandbox.mkdir(parents=True, exist_ok=True)
    env = git_env or os.environ.copy()
    try:
        subprocess.run(
            ["git", "clone", str(workspace), str(sandbox)],
            check=True,
            capture_output=True,
            env=env,
            timeout=_GIT_NET_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise subprocess.TimeoutExpired(exc.cmd, exc.timeout) from exc
    return sandbox


def cleanup_sandbox(sandbox: Path) -> None:
    """Remove a disposable sandbox working tree."""
    if sandbox.exists():
        shutil.rmtree(sandbox, ignore_errors=True)


def configure_git_identity(
    working_dir: Path,
    name: str = "Foreman Build Agent",
    email: str = "foreman@example.com",
    *,
    git_env: dict[str, str] | None = None,
) -> None:
    """Pre-configure git identity so the CLI agent's commits have author info."""
    env = git_env or os.environ.copy()
    subprocess.run(
        ["git", "config", "user.name", name],
        cwd=working_dir, check=True, capture_output=True, env=env,
    )
    subprocess.run(
        ["git", "config", "user.email", email],
        cwd=working_dir, check=True, capture_output=True, env=env,
    )


def reap_orphaned_processes() -> list[int]:
    """Kill stale builder-CLI processes marked with FOREMAN_ORCHESTRATED=1.

    A process is stale if its parent no longer exists (crashed orchestrator).
    Uses /proc on Linux. Returns list of reaped PIDs.
    """
    reaped: list[int] = []
    proc_root = Path("/proc")
    if not proc_root.exists():
        return reaped
    current_pid = os.getpid()
    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        pid = int(pid_dir.name)
        if pid == current_pid:
            continue
        try:
            environ_bytes = (pid_dir / "environ").read_bytes()
            environ_str = environ_bytes.replace(b"\x00", b"\n").decode("utf-8", errors="replace")
            if "FOREMAN_ORCHESTRATED=1" not in environ_str:
                continue
            status_text = (pid_dir / "status").read_text(errors="replace")
            ppid = None
            for line in status_text.splitlines():
                if line.startswith("PPid:"):
                    ppid = int(line.split()[1])
                    break
            if ppid is None:
                continue
            # Check if parent is still alive
            parent_dir = proc_root / str(ppid)
            if not parent_dir.exists():
                # Parent gone -- reap the orphan
                try:
                    os.kill(pid, signal.SIGTERM)
                    reaped.append(pid)
                except (ProcessLookupError, PermissionError):
                    pass
        except (PermissionError, FileNotFoundError, ValueError):
            continue
    return reaped
