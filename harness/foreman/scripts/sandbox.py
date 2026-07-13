"""Docker sandbox for Foreman tool execution.

The container runs with --network none. No orchestrator secrets, no secrets-manager
env, and no extra host bind mounts enter the container. Tools are dispatched
via docker exec (exec form, no shell). The push token never enters the sandbox.

Label schema: foreman-sandbox=<run-id>
Startup self-check verifies required tooling is present on first use of an image.
Orphan reaper: reap_orphans() removes leftover foreman-sandbox containers.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


SANDBOX_LABEL_KEY = "foreman-sandbox"
REQUIRED_TOOLS = ["bash", "git", "cat", "ls", "env"]

# Allowlist of env vars permitted inside the container (additions from image entrypoint
# are accepted, but NOTHING from the orchestrator env passes through).
ENV_ALLOWLIST = frozenset({"PATH", "HOME", "LANG", "TERM", "LC_ALL", "LC_CTYPE"})


class SandboxError(Exception):
    """Raised when a sandbox operation fails."""


class ForemanSandbox:
    """Context manager that manages the lifecycle of one foreman sandbox container.

    Mounts `workspace` at /workspace (rw for build, ro for verify).
    Starts with --network none so no outbound connections are possible.
    On exit (normal or exception), the container is always stopped and removed.

    Usage::

        with ForemanSandbox(run_id="...", workspace=Path("..."), image="...") as sb:
            result = sb.dispatch("read_file", {"path": "README.md"})
            out, err, rc = sb.dispatch_raw(["ls", "/workspace"])
    """

    def __init__(
        self,
        run_id: str,
        workspace: Path,
        image: str,
        readonly: bool = False,
        shard_root: Path | None = None,
        git_name: str = "Foreman",
        git_email: str = "foreman@example.com",
    ) -> None:
        self.run_id = run_id
        self.workspace = workspace.resolve()
        self.image = image
        self.readonly = readonly
        # Shard root: bind-mounted at /shards; rw for worker containers, ro for reduce/validator.
        self.shard_root: Path | None = shard_root.resolve() if shard_root else None
        self.git_name = git_name
        self.git_email = git_email
        self.container_id: str = ""

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "ForemanSandbox":
        mount_mode = "ro" if self.readonly else "rw"
        cmd: list[str] = [
            "docker", "run",
            "--detach",
            "--network", "none",
            "--label", f"{SANDBOX_LABEL_KEY}={self.run_id}",
            "--volume", f"{self.workspace}:/workspace:{mount_mode}",
        ]
        # Shard root: second explicit mount (rw for worker role, ro for reduce/validator role).
        # Worker containers write shards; reduce/validator containers read them.
        if self.shard_root is not None:
            shard_mode = "ro" if self.readonly else "rw"
            cmd.extend(["--volume", f"{self.shard_root}:/shards:{shard_mode}"])
        # No --env flags -- orchestrator env never enters
        cmd.extend([self.image, "sleep", "infinity"])
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SandboxError(
                f"Failed to start sandbox container: {result.stderr.strip()}"
            )
        self.container_id = result.stdout.strip()

        self._self_check()

        if not self.readonly:
            self._configure_git()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.container_id:
            subprocess.run(
                ["docker", "rm", "-f", self.container_id],
                capture_output=True,
            )
            self.container_id = ""
        return False  # do not suppress exceptions

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    def dispatch(self, name: str, inputs: dict) -> str:
        """Dispatch a tool call via docker exec. Returns result string."""
        if name == "read_file":
            return self._read_file(inputs.get("path", ""))
        if name == "write_file":
            if self.readonly:
                return "ERROR: sandbox is read-only (verify mode)"
            return self._write_file(inputs.get("path", ""), inputs.get("content", ""))
        if name == "bash":
            return self._bash(inputs.get("command", ""))
        if name == "git":
            return self._git(inputs.get("args", []))
        return f"ERROR: unknown tool {name!r}"

    def dispatch_raw(self, cmd: list[str]) -> tuple[str, str, int]:
        """Run an arbitrary command in the container. Returns (stdout, stderr, returncode)."""
        out, err, rc = self._docker_exec(cmd)
        return out, err, rc

    # ------------------------------------------------------------------
    # Tool implementations via docker exec
    # ------------------------------------------------------------------

    def _read_file(self, path: str) -> str:
        out, err, rc = self._docker_exec(["cat", f"/workspace/{path}"])
        if rc != 0:
            return f"ERROR: {err.strip() or 'file not found'}"
        return out

    def _write_file(self, path: str, content: str) -> str:
        # Use bash -c via exec form to create dirs and write
        escaped_content = content.replace("'", "'\"'\"'")
        cmd = ["bash", "-c", f"mkdir -p /workspace/$(dirname '{path}') && cat > /workspace/{path}"]
        out, err, rc = self._docker_exec_stdin(cmd, content)
        if rc != 0:
            return f"ERROR: write_file failed: {err.strip()}"
        return f"OK: wrote to {path}"

    def _bash(self, command: str) -> str:
        if not command:
            return "ERROR: empty command"
        out, err, rc = self._docker_exec(["bash", "-c", command])
        result = out
        if err:
            result += err
        if rc != 0:
            result = f"exit {rc}: {result}"
        return result

    def _git(self, args: list) -> str:
        from .agent_tools import GIT_ALLOWED_SUBCOMMANDS, GIT_FORBIDDEN_SUBCOMMANDS
        if not args:
            return "ERROR: git requires at least one argument"
        str_args = [str(a) for a in args]
        subcommand = str_args[0]
        if subcommand in GIT_FORBIDDEN_SUBCOMMANDS:
            return f"ERROR: git subcommand {subcommand!r} is not permitted"
        if subcommand not in GIT_ALLOWED_SUBCOMMANDS:
            return f"ERROR: git subcommand {subcommand!r} is not in the allowed list"
        out, err, rc = self._docker_exec(["git", "-C", "/workspace"] + str_args)
        result = out
        if err:
            result += err
        if rc != 0:
            result = f"exit {rc}: {result}"
        return result

    # ------------------------------------------------------------------
    # Docker exec helpers
    # ------------------------------------------------------------------

    def _docker_exec(self, cmd: list[str]) -> tuple[str, str, int]:
        result = subprocess.run(
            ["docker", "exec", self.container_id] + cmd,
            capture_output=True,
            text=True,
        )
        return result.stdout, result.stderr, result.returncode

    def _docker_exec_stdin(self, cmd: list[str], stdin_data: str) -> tuple[str, str, int]:
        result = subprocess.run(
            ["docker", "exec", "-i", self.container_id] + cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
        )
        return result.stdout, result.stderr, result.returncode

    # ------------------------------------------------------------------
    # Startup helpers
    # ------------------------------------------------------------------

    def _self_check(self) -> None:
        """Verify required tooling is present in the container image."""
        for tool in REQUIRED_TOOLS:
            _, _, rc = self._docker_exec(["which", tool])
            if rc != 0:
                raise SandboxError(
                    f"Required tool {tool!r} not found in image {self.image!r}. "
                    "The sandbox image must include bash, git, coreutils, and ca-certificates."
                )

    def _configure_git(self) -> None:
        """Set git author identity inside the container for build commits."""
        self._docker_exec(["git", "config", "--global", "user.name", self.git_name])
        self._docker_exec(["git", "config", "--global", "user.email", self.git_email])
        self._docker_exec(["git", "config", "--global", "--add", "safe.directory", "/workspace"])


# ---------------------------------------------------------------------------
# Orphan reaper
# ---------------------------------------------------------------------------

def reap_orphans() -> int:
    """Remove all containers labelled foreman-sandbox that were left by a prior crash.

    Called at orchestrator startup. Returns the number of containers removed.
    """
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"label={SANDBOX_LABEL_KEY}", "--format", "{{.ID}}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return 0

    container_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not container_ids:
        return 0

    removed = 0
    for cid in container_ids:
        rm_result = subprocess.run(
            ["docker", "rm", "-f", cid],
            capture_output=True,
        )
        if rm_result.returncode == 0:
            removed += 1

    return removed
