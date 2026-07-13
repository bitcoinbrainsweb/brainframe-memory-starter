"""Tool schemas and in-process dispatcher for the Foreman agent harness (Phase A).

Phase A tools run in-process; bash and git are gated by FOREMAN_UNSAFE_INPROCESS=1.
In Phase C these same schemas are sent to the model, but dispatching goes via docker exec
in sandbox.py rather than through this module.

Shard tools:
- write_shard(run_id, item_key, data): atomic write to {FOREMAN_SHARD_ROOT}/{run_id}/{item_key}.json
- ShardAssembler: iterates shards one-at-a-time (never all-in-memory); computes pending_items for resume.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

WORKSPACE_ROOT = Path("/workspace")

# Allowed git subcommands; forbids remote-config and credential-helper.
GIT_ALLOWED_SUBCOMMANDS = frozenset({
    "add", "status", "diff", "commit", "log", "show", "rev-parse",
    "ls-files", "diff-tree",
})
GIT_FORBIDDEN_SUBCOMMANDS = frozenset({
    "remote", "credential", "push", "pull", "fetch", "clone",
    "submodule", "filter-branch",
})

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "read_file",
        "description": "Read a file from the workspace. Path is relative to /workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to /workspace (e.g. 'src/main.py')",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file in the workspace. Creates intermediate directories.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to /workspace",
                },
                "content": {
                    "type": "string",
                    "description": "UTF-8 content to write",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "bash",
        "description": "Execute a shell command in /workspace via bash -c. Use for build, test, lint.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "git",
        "description": (
            "Execute a git command in /workspace. "
            "Allowed subcommands: add, status, diff, commit, log, show, rev-parse. "
            "Remote and credential operations are forbidden."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Git arguments (first element is the subcommand, e.g. ['add', 'file.py'])",
                },
            },
            "required": ["args"],
        },
    },
]

# Verify agent only gets read + bash (no write_file, no git).
VERIFY_TOOL_SCHEMAS: list[dict] = [
    TOOL_SCHEMAS[0],  # read_file
    TOOL_SCHEMAS[2],  # bash
]


def resolve_workspace_path(path: str, workspace: Path) -> Path | None:
    """Resolve `path` relative to `workspace`, return None if it escapes.

    Uses realpath-like resolution without requiring the path to exist.
    """
    ws = workspace.resolve()
    # Combine and resolve step by step
    candidate = (ws / path).resolve()
    try:
        candidate.relative_to(ws)
        return candidate
    except ValueError:
        return None


class InProcessDispatcher:
    """Dispatches tool calls in-process (Phase A / conformance tests).

    bash and git require env FOREMAN_UNSAFE_INPROCESS=1.
    write_file is blocked when readonly=True (verify agent).
    All paths are confined to workspace via resolve_workspace_path().
    bash and git run via exec form (list), never shell=True.
    """

    def __init__(
        self,
        workspace: Path,
        readonly: bool = False,
        git_env: dict | None = None,
    ) -> None:
        self._workspace = workspace
        self._readonly = readonly
        self._env = git_env or os.environ.copy()

    def __call__(self, name: str, inputs: dict) -> str:
        if name == "read_file":
            return self._read_file(inputs.get("path", ""))
        if name == "write_file":
            return self._write_file(inputs.get("path", ""), inputs.get("content", ""))
        if name == "bash":
            return self._bash(inputs.get("command", ""))
        if name == "git":
            return self._git(inputs.get("args", []))
        return f"ERROR: unknown tool {name!r}"

    # ------------------------------------------------------------------
    # tool implementations
    # ------------------------------------------------------------------

    def _read_file(self, path: str) -> str:
        resolved = resolve_workspace_path(path, self._workspace)
        if resolved is None:
            return f"ERROR: path {path!r} escapes workspace boundary"
        try:
            return resolved.read_text(encoding="utf-8")
        except FileNotFoundError:
            return f"ERROR: file not found: {path}"
        except IsADirectoryError:
            return f"ERROR: {path!r} is a directory"
        except Exception as exc:
            return f"ERROR: {exc}"

    def _write_file(self, path: str, content: str) -> str:
        if self._readonly:
            return "ERROR: workspace is read-only for verify agent"
        resolved = resolve_workspace_path(path, self._workspace)
        if resolved is None:
            return f"ERROR: path {path!r} escapes workspace boundary"
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
            return f"OK: wrote {len(content)} bytes to {path}"
        except Exception as exc:
            return f"ERROR: {exc}"

    def _bash(self, command: str) -> str:
        if os.environ.get("FOREMAN_UNSAFE_INPROCESS") != "1":
            return (
                "ERROR: bash execution is disabled in Phase A "
                "(set FOREMAN_UNSAFE_INPROCESS=1 to enable in tests)"
            )
        if not command:
            return "ERROR: empty command"
        # Exec form: ["bash", "-c", <command>] -- no shell=True, no injection via args
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=self._workspace,
            capture_output=True,
            text=True,
            env=self._env,
        )
        output = result.stdout
        if result.stderr:
            output += result.stderr
        if result.returncode != 0:
            output = f"exit {result.returncode}: {output}"
        return output

    def _git(self, args: list) -> str:
        if os.environ.get("FOREMAN_UNSAFE_INPROCESS") != "1":
            return (
                "ERROR: git execution is disabled in Phase A "
                "(set FOREMAN_UNSAFE_INPROCESS=1 to enable in tests)"
            )
        if not args:
            return "ERROR: git requires at least one argument"

        str_args = [str(a) for a in args]
        subcommand = str_args[0]

        if subcommand in GIT_FORBIDDEN_SUBCOMMANDS:
            return f"ERROR: git subcommand {subcommand!r} is not permitted"
        if subcommand not in GIT_ALLOWED_SUBCOMMANDS:
            return f"ERROR: git subcommand {subcommand!r} is not in the allowed list"

        # Exec form: ["git"] + args -- each element is a separate argument, no shell
        result = subprocess.run(
            ["git"] + str_args,
            cwd=self._workspace,
            capture_output=True,
            text=True,
            env=self._env,
        )
        output = result.stdout
        if result.stderr:
            output += result.stderr
        if result.returncode != 0:
            output = f"exit {result.returncode}: {output}"
        return output


# ---------------------------------------------------------------------------
# Shard-write tool
# ---------------------------------------------------------------------------

def _shard_dir(shard_root: Path, run_id: str) -> Path:
    return shard_root / run_id


def _shard_path(shard_root: Path, run_id: str, item_key: str) -> Path:
    return _shard_dir(shard_root, run_id) / f"{item_key}.json"


def write_shard(run_id: str, item_key: str, data: dict) -> None:
    """Write result to canonical run-scoped shard path.

    Path: {FOREMAN_SHARD_ROOT}/{run_id}/{item_key}.json

    Atomic write via temp file + os.rename (POSIX-atomic on same filesystem).
    A crash mid-write leaves a .tmp file but never a partial .json.
    Call this before returning from the worker function.
    """
    shard_root_str = os.environ.get("FOREMAN_SHARD_ROOT")
    if not shard_root_str:
        raise ValueError("FOREMAN_SHARD_ROOT is not set")

    shard_root = Path(shard_root_str)
    target = _shard_path(shard_root, run_id, item_key)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file in the same directory, then rename atomically.
    # os.rename is atomic on POSIX when src and dst are on the same filesystem.
    # The temp file shares the same parent dir to guarantee same-filesystem rename.
    tmp_fd, tmp_name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        Path(tmp_name).rename(target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class ShardAssembler:
    """Reads landed shards one-at-a-time and computes pending items for resume.

    The assembler/validator role holds this object. The orchestrator passes it shard_root
    and item keys; content reads happen here, not in the orchestrator.

    The orchestrator holds paths and ledger state; it never reads
    shard contents for synthesis. Route content reads through this class.
    """

    def __init__(self, shard_root: Path) -> None:
        self._shard_root = Path(shard_root)

    def iter_shards(self, run_id: str, keys: list[str]):
        """Yield one shard dict at a time (generator -- never all-in-memory simultaneously).

        Yields only shards that exist and parse cleanly. Callers must check
        pending_items() separately to identify absent/corrupt shards.
        """
        for key in keys:
            path = _shard_path(self._shard_root, run_id, key)
            if not path.exists():
                continue
            try:
                yield json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

    def pending_items(self, run_id: str, all_keys: list[str]) -> list[str]:
        """Return keys whose shard is absent or fails JSON parse (must be re-extracted).

        A .tmp file with no corresponding .json is treated as absent (crash-safe atomicity).
        A corrupt .json (parse failure) is treated as absent and logged for re-extraction.
        """
        pending: list[str] = []
        for key in all_keys:
            path = _shard_path(self._shard_root, run_id, key)
            if not path.exists():
                pending.append(key)
                continue
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pending.append(key)
        return pending


# ---------------------------------------------------------------------------
# Gather-mode tool schemas
# ---------------------------------------------------------------------------

# Tool schema for the shard manifest reader (reduce agent only).
# Lists shard paths + statuses; reads one shard's contents on demand.
_READ_SHARD_MANIFEST_SCHEMA: dict = {
    "name": "read_shard_manifest",
    "description": (
        "List all distilled .json shards for a gather run, including each entity's "
        "status and shard path. Optionally read one shard's JSON content by entity_key. "
        "Never accesses raw .source files."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "gather_run_id": {
                "type": "string",
                "description": "The gather run ID whose shards to list.",
            },
            "read_entity_key": {
                "type": "string",
                "description": (
                    "Optional: entity_key of a single shard to read. "
                    "If omitted, returns manifest listing only (no file content)."
                ),
            },
        },
        "required": ["gather_run_id"],
    },
}

# Extract agent: read .source file + write distilled .json shard.
# Network is blocked at the sandbox level (--network none); no fetch tool here.
GATHER_EXTRACT_TOOL_SCHEMAS: list[dict] = [
    TOOL_SCHEMAS[0],  # read_file
    TOOL_SCHEMAS[1],  # write_file (to write the .json shard)
]

# Reduce agent: read distilled .json shards + manifest listing.
# Explicitly excludes write_file, bash, git, and any network/fetch tool.
GATHER_REDUCE_TOOL_SCHEMAS: list[dict] = [
    TOOL_SCHEMAS[0],           # read_file (shard .json only -- enforced by ShardReadDispatcher)
    _READ_SHARD_MANIFEST_SCHEMA,  # manifest listing + single-shard read
]
