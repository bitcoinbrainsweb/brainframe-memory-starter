"""BundleRunner: Phase 2 orchestration layer.

Owns the build-verify-retry loop for each task in a bundle.
Phase 1 modules (runner.py, git_ops.py, prompts.py, transport.py) are consumed unchanged.
"""
from __future__ import annotations

import dataclasses
import sys
import time
from pathlib import Path

from .bundle import (
    BundleIntake,
    BundleResolution,
    _all_approved,
    _validate_attestation,
)
from .antislop_lint import format_lint_findings
from .gates import (
    InvariantHarness,
    NoOpHarness,
    run_antislop_lint_gate,
    verify_model_precondition,
)
from .git_ops import (
    GitError,
    OriginCheckResult,
    _git,
    checkout_branch,
    check_origin_ground_truth,
    confirm_git_state,
    create_branch,
    ff_merge_local,
    get_local_sha,
    git_ls_remote,
    post_push_check,
    push_branch,
    push_ref,
)
from .ledger import LedgerBackend, _redact_detail
from .manifest_lint import lint_spec
from .models import (
    BundleConfig,
    ForemanRunLocked,
    HaltChain,
    HaltRecord,
    ParkedRecord,
    TaskStatus,
)
from .prompts import render_build_prompt, render_verify_prompt
from .pty_harness import FOREMAN_SPEC_WALLCLOCK_CEILING
from .queue import TaskQueue
from .report import RunReport
from .transport import AgentTransport


# Bug 1: a MALFORMED verify verdict (unparseable/truncated output) is retried at
# the verify step only -- never a rebuild, never a build-attempt charge. Cap the
# extra verify attempts so a persistently-broken verifier still terminates.
_MAX_VERIFY_RETRIES = 2


class _VerifyMalformed(Exception):
    """Sentinel raised by _dispatch_verify when all MALFORMED retries are exhausted."""
    def __init__(self, last_result, elapsed_secs: float) -> None:
        self.last_result = last_result
        self.elapsed_secs = elapsed_secs


def _run_id() -> str:
    import uuid
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    suffix = uuid.uuid4().hex[:6]
    return f"fm-{now.strftime('%Y%m%d-%H%M')}-{suffix}"


def _fetch_live_sha(working_dir: Path, remote_url: str, base_ref: str) -> str:
    """Fetch origin/<base_ref> and return live HEAD SHA."""
    _git(
        ["fetch", remote_url, f"refs/heads/{base_ref}:refs/remotes/origin/{base_ref}"],
        cwd=working_dir,
        capture_output=True,
    )
    sha = git_ls_remote(remote_url, f"refs/heads/{base_ref}", working_dir)
    if sha is None:
        raise GitError(f"Cannot resolve live HEAD of {base_ref} from {remote_url}")
    return sha


def _delete_remote_branch(working_dir: Path, remote_url: str, branch: str) -> None:
    _git(
        ["push", remote_url, f":{branch}"],
        cwd=working_dir,
        capture_output=True,
    )


def _preserve_remote_branch(
    working_dir: Path, remote_url: str, branch: str, run_id: str, slug: str, attempt: int
) -> str | None:
    """never discard a build branch on the retry/wedge path. Rename the
    remote ref to wedged/{run_id}/{slug}/{attempt} (push old ref to the new name,
    then delete the old name). Degrades to leaving the original branch in place on
    any failure; a preservation hiccup must never take down the run. Returns the
    preserved ref name, or None if preservation failed (original left intact)."""
    preserved = f"wedged/{run_id}/{slug}/{attempt}"
    try:
        _git(
            ["push", remote_url, f"refs/remotes/origin/{branch}:refs/heads/{preserved}"],
            cwd=working_dir,
            capture_output=True,
        )
    except Exception:
        try:
            # Fallback: the local checkout may not have the remote-tracking ref;
            # fetch then push by sha.
            _git(["fetch", remote_url, branch], cwd=working_dir, capture_output=True)
            _git(
                ["push", remote_url, f"FETCH_HEAD:refs/heads/{preserved}"],
                cwd=working_dir,
                capture_output=True,
            )
        except Exception as exc:  # pragma: no cover - degrade, never crash
            print(f"[WARN] branch preservation failed for {branch}: {exc}", file=sys.stderr)
            return None
    try:
        _delete_remote_branch(working_dir, remote_url, branch)
    except Exception:
        pass
    return preserved


def _fetch_compare_diff(repo: str, base: str, branch: str) -> str | None:
    """Fetch the unified diff (concatenated file patches) for base...branch from
    the GitHub API, authenticated. Returns None on any failure so the verify
    prompt can degrade gracefully. Token resolved via the shared push-token
    resolver; never logged.
    """
    import httpx
    from .git_ops import _resolve_push_token

    # repo is "owner/name"; resolve a token that can actually reach it so a
    # token scoped to other repos is skipped rather than 404-ing the compare.
    token = _resolve_push_token(f"https://github.com/{repo}")
    if not token:
        return None
    url = f"https://api.github.com/repos/{repo}/compare/{base}...{branch}"
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"token {token}",
                     "Accept": "application/vnd.github+json"},
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        files = resp.json().get("files", [])
        chunks = []
        for f in files:
            patch = f.get("patch")
            if patch:
                chunks.append(f"--- {f.get('filename')}\n{patch}")
        return "\n\n".join(chunks) if chunks else None
    except Exception:
        return None


def _spec_demands_tests(spec: dict) -> bool:
    """Heuristic: does this spec require tests? Drives the anti-slop lint's
    comment-only-diff check (anti-slop static lint). A spec that
    mentions tests anywhere in its body, acceptance, scope, or test-slice channel
    is treated as test-demanding; the comment-only check is skipped otherwise."""
    for key in ("body", "acceptance", "test_slice", "scope"):
        val = spec.get(key)
        if val and "test" in str(val).lower():
            return True
    return False


# Host-side spec-body hydration. The verifier and builder must read the actual
# spec, not just the checklist. The specs table has no body column and the
# sandboxed agent cannot fetch your spec repo itself (unauthenticated gh), so
# the orchestrator fetches the spec markdown host-side and inlines it. A fetch
# failure parks operationally rather than silently proceeding checklist-only: a
# verifier that never read the spec can false-PASS a build that does not meet it.
_COMMS_REPO = "YOUR_ORG/YOUR_REPO"
_SPEC_BODY_CAP_BYTES = 32 * 1024
_SPEC_BODY_TRUNCATION_MARKER = (
    "\n[... spec body truncated by host-side hydration at 32KB; "
    "read the full spec at its comms_path ...]\n"
)


class SpecBodyUnavailable(Exception):
    """Host-side spec-body hydration failed (no usable token, 404, or network error).

    Raised so the orchestrator parks the task operationally (spec-body-unavailable)
    instead of silently proceeding checklist-only, which is the false-PASS defect
    this hydration path removes.
    """


def _fetch_spec_body_from_comms(comms_path: str) -> str:
    """Fetch spec markdown from your spec repo via the GitHub contents API.

    Uses the shared push-token resolver (env precedence first, then the secrets manager),
    read-only and validated against the comms repo so a token scoped elsewhere is
    skipped. Raises SpecBodyUnavailable on any failure. The token is never logged.
    """
    import httpx
    from .git_ops import _resolve_push_token

    token = _resolve_push_token(f"https://github.com/{_COMMS_REPO}")
    if not token:
        raise SpecBodyUnavailable(
            f"no usable token for {_COMMS_REPO} "
            "(checked env and secrets-manager push-token precedence)"
        )
    url = f"https://api.github.com/repos/{_COMMS_REPO}/contents/{comms_path}"
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"token {token}",
                     "Accept": "application/vnd.github.raw+json"},
            timeout=30,
        )
    except Exception as exc:
        raise SpecBodyUnavailable(f"network error fetching {comms_path}: {exc}") from exc
    if resp.status_code != 200:
        raise SpecBodyUnavailable(
            f"GitHub contents API returned {resp.status_code} for {comms_path}"
        )
    body = resp.text
    if not body.strip():
        raise SpecBodyUnavailable(f"spec body empty for {comms_path}")
    return body


def _cap_spec_body(body: str) -> str:
    """Cap the inlined spec body at 32KB with an explicit truncation marker so an
    oversized spec cannot blow the verify prompt. Under-cap bodies pass unchanged."""
    encoded = body.encode("utf-8")
    if len(encoded) <= _SPEC_BODY_CAP_BYTES:
        return body
    truncated = encoded[:_SPEC_BODY_CAP_BYTES].decode("utf-8", errors="ignore")
    return truncated + _SPEC_BODY_TRUNCATION_MARKER


class BundleRunner:
    def __init__(
        self,
        config: BundleConfig,
        transport: AgentTransport,
        ledger: LedgerBackend,
        harness: InvariantHarness | None = None,
        model_api_key: str | None = None,
    ) -> None:
        self.config = config
        self._transport = transport
        self._ledger = ledger
        # Hydrate the spec body once per spec per run; cache in memory so retries
        # and multiple attempts never refetch. Keyed by comms_path.
        self._spec_body_cache: dict[str, str] = {}
        self._harness = harness or NoOpHarness()
        self._api_key = model_api_key
        # F4: optional injected LiveDbClient (tests). Live path builds one from env.
        self._live_db_client_override = None

        # H-CREDIT-001: Reject any transport that calls the Anthropic API directly.
        # InProcessDispatcher / ApiAgentTransport burn pay-per-token credits.
        # CliAgentTransport runs via the Claude CLI on subscription (zero marginal cost).
        # To allow non-CLI transports in unit tests: set FOREMAN_ALLOW_API_TRANSPORT=1.
        import os as _os
        from .transport import CliAgentTransport as _CliAgentTransport
        if not isinstance(transport, _CliAgentTransport):
            if _os.environ.get("FOREMAN_ALLOW_API_TRANSPORT", "0").strip() != "1":
                raise RuntimeError(
                    "H-CREDIT-001: BundleRunner transport is not CliAgentTransport. "
                    "Non-CLI transports call the Anthropic API and burn credits. "
                    "Use harness/foreman/scripts/run_bundle.py (CliAgentTransport). "
                    "To override for tests only: FOREMAN_ALLOW_API_TRANSPORT=1"
                )

    # expose for testing
    def _fetch_live_base_sha(self) -> str:
        return _fetch_live_sha(
            self.config.working_dir,
            self.config.remote_url,
            self.config.base_ref,
        )

    def _check_substance_delta(self, base_sha: str, commit_sha: str | None, spec: dict,
                               branch: str | None = None):
        """F3: PASS/FAIL/INCONCLUSIVE the base..commit deliverable delta.

        In a cross-repo run the working tree is an ephemeral clone whose fetched
        refs make a naive ``base_sha..commit`` diff empty. There the base is
        resolved as the merge-base of the builder branch and the target default
        branch inside that clone (Bug 2); an unresolvable base is an evaluation
        error -> INCONCLUSIVE, never a FAIL.
        """
        from .substance_delta import (
            DeltaResult,
            delta_from_git,
            evaluate_deliverable_delta,
            resolve_delta_base,
        )
        if not commit_sha:
            return DeltaResult("FAIL", [], [], "no-commit-sha")
        effective_base = base_sha
        if self.config.is_xrepo and branch:
            resolved = resolve_delta_base(
                self.config.working_dir, branch, self.config.base_ref
            )
            if resolved is None:
                return DeltaResult(
                    "INCONCLUSIVE", [], [],
                    "xrepo-base-unresolvable (merge-base of builder branch and "
                    "default branch could not be resolved in the clone)",
                )
            effective_base = resolved
        try:
            files = delta_from_git(self.config.working_dir, effective_base, commit_sha)
        except Exception as exc:
            # Fail closed: a build whose delta cannot be measured does not merge.
            return DeltaResult("FAIL", [], [], f"delta-compute-error:{exc}")
        required = spec.get("deliverable_globs") or None
        return evaluate_deliverable_delta(files, required_deliverables=required)

    def _scaffolding_in_build(self, base_sha: str, commit_sha: str | None) -> list[str]:
        """Bug 4: return any harness scaffolding paths the commit set touched.

        Best-effort: an unmeasurable delta returns [] (the substance gate remains
        the authority on the delta); a measured delta containing scaffolding is
        reported so the caller can reject the build.
        """
        from .git_ops import _scaffolding_leaks
        from .substance_delta import delta_from_git
        if not commit_sha:
            return []
        try:
            files = delta_from_git(self.config.working_dir, base_sha, commit_sha)
        except Exception:
            return []
        return _scaffolding_leaks([cf.path for cf in files])

    def _live_db_client(self):
        """F4: injected client (tests) or one built from your DB env creds."""
        if self._live_db_client_override is not None:
            return self._live_db_client_override
        import os as _os
        url = _os.environ.get("SUPABASE_URL")
        key = _os.environ.get("SUPABASE_SERVICE_KEY")
        if not url or not key:
            return None
        from .live_db_assert import SupabaseLiveDbClient
        return SupabaseLiveDbClient(url, key)

    def _check_live_db(self, base_sha: str, commit_sha: str | None):
        """F4: when the delta ships migration SQL creating Supabase objects,
        assert they exist live + one write-then-read smoke. SKIP otherwise."""
        from .live_db_assert import (
            LiveDbResult,
            assert_live_db,
            extract_db_deliverables,
            is_migration_path,
        )
        from .substance_delta import delta_from_git
        if not commit_sha:
            return LiveDbResult("SKIP", reason="no-commit")
        try:
            files = delta_from_git(self.config.working_dir, base_sha, commit_sha)
        except Exception as exc:
            return LiveDbResult("SKIP", reason=f"delta-compute-error:{exc}")
        migs = [cf.path for cf in files if is_migration_path(cf.path) and cf.status != "D"]
        if not migs:
            return LiveDbResult("SKIP", reason="no-migration-in-delta")
        sql_texts: list[str] = []
        for p in migs:
            try:
                res = _git(["show", f"{commit_sha}:{p}"], cwd=self.config.working_dir, capture_output=True, text=True)
                sql_texts.append(getattr(res, "stdout", "") or "")
            except Exception:
                continue
        deliv = extract_db_deliverables(sql_texts)
        if not deliv.any():
            return LiveDbResult("SKIP", reason="no-db-objects-created-in-migration")
        client = self._live_db_client()
        if client is None:
            return LiveDbResult("SKIP", reason="no-db-credentials")
        return assert_live_db(deliv, client)

    def _check_conformance(self, base_sha: str, commit_sha: str | None, spec: dict, run_id: str):
        """H7: mechanical conformance gate for Checklists A/B.

        Runs the deterministic subset (max-width/token-override/grid/density;
        vendor-self-source ratio/uniform-value/sample-size) as a HARD gate. The
        cold verifier's read of A/B is a semantic supplement, not the sole
        mechanism. SKIP when the delta ships no UI files and the spec declares no
        write batch. Fails closed if the delta cannot be measured."""
        from .conformance import ConformanceResult, run_conformance_gate
        from .substance_delta import delta_from_git
        if not commit_sha:
            return ConformanceResult("SKIP", reason="no-commit-sha")
        try:
            files = delta_from_git(self.config.working_dir, base_sha, commit_sha)
        except Exception as exc:
            return ConformanceResult("FAIL", reason=f"conformance-delta-compute-error:{exc}")

        def _load_batch(path: str) -> str:
            res = _git(["show", f"{commit_sha}:{path}"], cwd=self.config.working_dir,
                       capture_output=True, text=True)
            return getattr(res, "stdout", "") or ""

        return run_conformance_gate(spec, files, run_id, batch_loader=_load_batch)

    def _emit(
        self,
        run_id: str,
        event: str,
        *,
        task_id: str | None = None,
        spec_slug: str | None = None,
        detail: dict | None = None,
    ) -> None:
        """Fire-and-forget event emit. Redacts secrets and never propagates exceptions."""
        import sys
        try:
            safe_detail = _redact_detail(detail) if detail else None
            self._ledger.emit_run_event(run_id, event, task_id=task_id, spec_slug=spec_slug, detail=safe_detail)
        except Exception as exc:
            print(f"[run-event] emit failed: {exc}", file=sys.stderr)

    def _append_gate_reason(self, slug: str, spec: dict, committed_slugs: list[str]) -> str | None:
        """apply the intake gates to an appended spec. Returns the standard
        per-spec exclusion reason if it fails a gate, or None if it passes.

        Mirrors BundleIntake.resolve (approval, attestation, already-committed)
        so an appended spec is held to the same bar as an intake-time spec.
        """
        if not spec:
            return "spec not found"
        if slug in committed_slugs:
            return "already committed in this run"
        if not _all_approved(spec):
            return ("missing approval: requires requirements_approved AND "
                    "design_approved AND tasks_approved")
        attestation_err = _validate_attestation(spec)
        if attestation_err:
            return attestation_err
        return None

    def _make_heartbeat_sink(self, run_id: str, spec_slug: str):
        """Build the last_heartbeat_at writer for one task's liveness watchdog.

        The sink writes the same column the single-flight guard and stale-claim recovery
        read. It may raise (e.g. a DB blip); the watchdog catches, counts, and
        throttles so a failed heartbeat never interrupts the build.
        """
        from datetime import datetime, timezone

        def _sink() -> None:
            self._ledger.patch_heartbeat(
                run_id, spec_slug, datetime.now(timezone.utc).isoformat()
            )

        return _sink

    _LOCK_STATUSES = frozenset({"queued", "building", "verifying", "merging"})
    # Operational parks are retryable on the next run and are excluded from the
    # repeat-failure circuit breaker. verify-malformed joins this set:
    # a spec whose verifier only ever emitted unreadable output was never
    # genuinely judged, so it must be retryable, not terminally broken (Bug 1).
    # spec-body-unavailable joins it too: a transient bad token / 404 / network
    # error is an environment fault, not a spec that deterministically fails.
    # manifest-lint-failed joins it as well: a body/self-containment refusal means
    # host-side hydration produced a pointer/empty body rather than content -- a
    # hydration-plane fault of the same kind as spec-body-unavailable, retryable on
    # the next run, not a deterministic spec defect the breaker should trip on.
    _OPERATIONAL_PARK_REASONS = frozenset({
        "orphaned-stale-claim", "verify-malformed", "spec-body-unavailable",
        "manifest-lint-failed",
    })

    def _hydrate_spec_body(self, comms_path: str) -> str:
        """Return the spec markdown for comms_path, fetching from your spec repo
        once per run and caching in memory (never refetched per attempt). Raises
        SpecBodyUnavailable on fetch failure so the caller parks the task."""
        cached = self._spec_body_cache.get(comms_path)
        if cached is not None:
            return cached
        body = _cap_spec_body(_fetch_spec_body_from_comms(comms_path))
        self._spec_body_cache[comms_path] = body
        return body

    def _circuit_broken(self, run_id: str, slug: str) -> str | None:
        """R4: if the last N parks for this spec in this run share a non-operational
        park_reason, return that reason (the breaker trips). Else None.
        N from FOREMAN_CIRCUIT_BREAKER_N (default 3). Operational reasons
        and prior-run parks (query is run-scoped) do not count.
        """
        import os as _os
        n = int(_os.environ.get("FOREMAN_CIRCUIT_BREAKER_N", "3"))
        fn = getattr(self._ledger, "recent_dispositions", None)
        if not callable(fn):
            return None
        reasons = [
            r for r in fn(run_id, slug, n)
            if r not in self._OPERATIONAL_PARK_REASONS
            and not r.startswith("operational-")
        ]
        if len(reasons) >= n and len(set(reasons)) == 1:
            return reasons[0]
        return None

    @staticmethod
    def _get_stale_threshold() -> int:
        import os as _os
        raw = _os.environ.get("FOREMAN_RUN_LOCK_STALE_SECONDS", "900")
        try:
            return int(raw)
        except (ValueError, TypeError):
            return 900

    def _reconcile_killed_runs(self) -> None:
        """Close build_runs stranded 'running' by a hard kill (F5 kill-reconciliation).

        Delegates to reconcile.reconcile_killed_runs, keyed off task heartbeats so a
        just-killed run is caught. Fire-and-forget: never propagates an exception into
        the run (a reconcile failure must not block a fresh build)."""
        import sys
        try:
            from .reconcile import reconcile_killed_runs
            reconciled = reconcile_killed_runs(self._ledger)
            for r in reconciled:
                self._emit(
                    r.get("run_id") or "",
                    "run_reconciled_aborted",
                    detail={"stale_tasks": r.get("stale_tasks"),
                            "last_task_activity": r.get("last_task_activity")},
                )
        except Exception as exc:
            print(f"[reconcile] startup kill-sweep failed: {exc}", file=sys.stderr)

    def _check_single_flight(self, spec_slugs: list[str]) -> None:
        """Single-flight guard. Raises ForemanRunLocked on live in-flight tasks.

        Stale tasks (updated_at older than FOREMAN_RUN_LOCK_STALE_SECONDS) are reaped
        and RESET TO QUEUED (retryable), not terminally parked -- a run killed
        mid-build never failed, so its spec must be buildable on the next run without
        DB surgery. Only fresh, actively-claimed in-flight tasks (building/verifying/
        merging) block; a reset/queued task is unclaimed work and never blocks.
        """
        import sys
        from datetime import datetime, timezone

        rows = self._ledger.query_non_terminal_tasks(spec_slugs)
        if not rows:
            return

        threshold_s = self._get_stale_threshold()
        now = datetime.now(timezone.utc)
        _IN_FLIGHT = {"building", "verifying", "merging"}

        stale: list[dict] = []
        live: list[dict] = []
        for row in rows:
            updated_at_str = row.get("updated_at", "")
            try:
                updated_at = datetime.fromisoformat(
                    updated_at_str.replace("Z", "+00:00")
                )
                age_s = (now - updated_at).total_seconds()
                is_stale = age_s > threshold_s
            except (ValueError, AttributeError):
                is_stale = True
            if is_stale:
                stale.append(row)
            elif row.get("status") in _IN_FLIGHT:
                # Fresh AND actively claimed: a real in-flight worker. Blocks.
                live.append(row)
            # Fresh but 'queued' (unclaimed): holds no claim -> never blocks.

        if stale:
            by_run: dict[str, list[str]] = {}
            for row in stale:
                rid = row["run_id"]
                task_id = row.get("id", "")
                by_run.setdefault(rid, []).append(task_id)
            for row in stale:
                try:
                    # Reset to queued (retryable), clearing the dead claim -- NOT a
                    # terminal park. The killed-mid-build spec is picked up again.
                    self._ledger.update_task_status(
                        row["run_id"],
                        row["spec_slug"],
                        "queued",
                        claimed_by=None,
                        claimed_at=None,
                    )
                except Exception as exc:
                    print(f"[run-lock] reap failed for {row.get('id')}: {exc}", file=sys.stderr)
            for rid, task_ids in by_run.items():
                self._emit(rid, "stale_lock_reaped", detail={"task_ids": task_ids})

        if not live:
            return

        blocking_run_ids = sorted({row["run_id"] for row in live})
        blocking_slugs = sorted({row["spec_slug"] for row in live})
        slug_args = " ".join(f"--spec {s}" for s in spec_slugs)
        msg = (
            "ForemanRunLocked: a run is already in flight for the requested specs.\n"
            f"Blocking run_id(s): {', '.join(blocking_run_ids)}\n"
            f"Blocking spec(s): {', '.join(blocking_slugs)}\n"
            "\n"
            "To clear the lock (reap the stale run), wait for it to finish or set\n"
            "FOREMAN_RUN_LOCK_STALE_SECONDS=0 and re-run to reap immediately.\n"
            "\n"
            "To force override:\n"
            f"  python harness/foreman/scripts/run_bundle.py {slug_args} --force-unlock"
        )
        raise ForemanRunLocked(msg)

    def run(self, spec_slugs: list[str], *, force_unlock: bool = False) -> RunReport:
        cfg = self.config
        t_start = time.monotonic()

        # Startup reconciliation sweep: close any build_runs stranded 'running' by a
        # hard kill (finalize runs in a finally-block a SIGKILL skips) before we do
        # anything else, so dashboards and the single-flight lock stop treating dead
        # runs as live. Never raises into the run.
        self._reconcile_killed_runs()

        # Single-flight guard (R1, R2, R3): refuse to start if another run is in flight.
        if force_unlock:
            # emit override event before proceeding; run_id not yet minted so use placeholder.
            # We emit after run_id is minted below; store the flag and emit then.
            pass
        else:
            self._check_single_flight(spec_slugs)

        # Step 1: resolve + commit_intake
        intake = BundleIntake(self._ledger, cfg.session_id)
        try:
            resolution = intake.resolve(spec_slugs)
        except Exception as exc:
            return RunReport(
                run_id="",
                total_wall_s=time.monotonic() - t_start,
                excluded=[],
            )

        run_id = _run_id()
        run_row = self._ledger.create_run(run_id, spec_slugs, cfg.session_id)
        run_uuid: str = run_row["id"]

        # F5: finalize build_runs.status in a finally-path so no run is left
        # stuck in 'running' (incident left 59 zombies). 'failed' unless the loop
        # completes normally, then 'completed'. Individual task parks do not fail
        # the run; only a crash / intake failure does. final_report is attached to
        # the finalizing status write so a zero-intake run carries its exclusions.
        final_status = "failed"
        final_report: dict | None = None
        try:
            if force_unlock:
                self._emit(run_id, "run_lock_forced", detail={"spec_slugs": spec_slugs})

            try:
                intake.commit_intake(run_id, resolution)
            except Exception as exc:
                return RunReport(
                    run_id=run_id,
                    total_wall_s=time.monotonic() - t_start,
                    excluded=resolution.excluded,
                )

            self._emit(run_id, "intake_resolved", detail={"queued": len(resolution.ordered)})

            # Step 2: stale-claim recovery (handles crash resume)
            queue = TaskQueue(self._ledger, run_id, cfg.session_id)
            queue.recover_stale()
            queue.reap_orphans()  # R5: park any in-flight tasks left stale by a prior kill

            # Tracking for report
            committed_slugs: list[str] = []
            parked_records: list[ParkedRecord] = []
            halt_records: list[HaltRecord] = []
            halt_chains: list[HaltChain] = []
            appended_slugs: list[str] = []
            run_notes: list[str] = []
            hb_failure_noted = False
            # R1: slugs committed to the queue at intake time. Anything the loop later
            # picks up that is NOT one of these was appended to the live run.
            initial_slugs: set[str] = {s["slug"] for s in resolution.ordered}
            appended_seen: set[str] = set()

            # Step 3: main task loop
            while True:
                task = queue.next_queued()
                if task is None:
                    break

                slug = task["spec_slug"]
                task_id = task.get("id")
                spec = self._ledger.fetch_spec(slug) or {}

                # a task the loop picks up that was not part of the
                # intake resolution was appended to the live run. Apply the same intake
                # gates now (approval, attestation, already-committed); a gate failure
                # parks with the standard exclusion reason rather than being ignored.
                if slug not in initial_slugs and slug not in appended_seen:
                    appended_seen.add(slug)
                    appended_slugs.append(slug)
                    gate_reason = self._append_gate_reason(slug, spec, committed_slugs)
                    if gate_reason is not None:
                        halts = queue.park(task, gate_reason, {"appended": True})
                        self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                   detail={"park_reason": gate_reason,
                                           "failure_trail": {"appended": True}})
                        parked_records.append(ParkedRecord(spec_slug=slug, park_reason=gate_reason))
                        halt_records.extend(halts)
                        if halts:
                            halt_chains.append(HaltChain(parked_slug=slug,
                                                         halted_slugs=[h.spec_slug for h in halts]))
                        continue

                # model precondition gate
                if self._api_key:
                    pre = verify_model_precondition(cfg.builder_model, self._api_key)
                    if not pre.ok:
                        park_reason = (
                            "model-precondition-unreachable"
                            if "unreachable" in pre.reason
                            else "model-precondition-failed"
                        )
                        halts = queue.park(task, park_reason, {"precondition": pre.reason})
                        self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={"park_reason": park_reason, "failure_trail": {"precondition": pre.reason}})
                        parked_records.append(ParkedRecord(spec_slug=slug, park_reason=park_reason))
                        halt_records.extend(halts)
                        if halts:
                            halt_chains.append(HaltChain(parked_slug=slug, halted_slugs=[h.spec_slug for h in halts]))
                        continue

                # H1 invariant gate (if spec declares write_invariant)
                if spec.get("write_invariant"):
                    invariant_id = spec["write_invariant"]
                    try:
                        inv = self._harness.check_invariant(
                            slug,
                            invariant_id=invariant_id,
                            run_id=run_id,
                            mode="precondition",
                        )
                    except Exception as exc:
                        halts = queue.park(task, "db-invariant-unreachable", {"error": str(exc)})
                        self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={"park_reason": "db-invariant-unreachable", "failure_trail": {"error": str(exc)}})
                        parked_records.append(ParkedRecord(spec_slug=slug, park_reason="db-invariant-unreachable"))
                        halt_records.extend(halts)
                        if halts:
                            halt_chains.append(HaltChain(parked_slug=slug, halted_slugs=[h.spec_slug for h in halts]))
                        continue
                    if not inv.ok or inv.violation_count > 0:
                        halts = queue.park(task, "db-invariant-violated", {
                            "violation_count": inv.violation_count,
                            "reason": inv.reason,
                        })
                        self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={"park_reason": "db-invariant-violated", "failure_trail": {"violation_count": inv.violation_count, "reason": inv.reason}})
                        parked_records.append(ParkedRecord(spec_slug=slug, park_reason="db-invariant-violated"))
                        halt_records.extend(halts)
                        if halts:
                            halt_chains.append(HaltChain(parked_slug=slug, halted_slugs=[h.spec_slug for h in halts]))
                        continue

                # Host-side spec-body hydration (before dispatching any build or
                # verify). Fetch the spec markdown from your spec repo and inline
                # it so the cold verifier reads the real spec, not just the checklist.
                # On failure, park operationally (spec-body-unavailable) rather than
                # silently proceeding checklist-only: a verifier that never read the
                # spec can false-PASS a build that does not meet it.
                _comms_path = spec.get("comms_path") or f"specs/{slug}.md"
                try:
                    # The {**spec, ...} merge preserves the scope / test_slice channel
                    # (pre-dispatch manifest lint) alongside the newly
                    # hydrated body, so render_build_prompt carries scope into the
                    # builder context unchanged.
                    spec = {**spec, "body": self._hydrate_spec_body(_comms_path)}
                except SpecBodyUnavailable as exc:
                    trail = {"comms_path": _comms_path, "error": str(exc)}
                    halts = queue.park(task, "spec-body-unavailable", trail)
                    self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                               detail={"park_reason": "spec-body-unavailable",
                                       "failure_trail": trail})
                    parked_records.append(ParkedRecord(spec_slug=slug,
                                                       park_reason="spec-body-unavailable"))
                    halt_records.extend(halts)
                    if halts:
                        halt_chains.append(HaltChain(parked_slug=slug,
                                                     halted_slugs=[h.spec_slug for h in halts]))
                    continue

                # Pre-dispatch manifest lint (pre-dispatch manifest lint).
                # This is the LIVE gate point: post-hydration, where the spec body
                # exists. Resolve time carries no body/scope, which is why the
                # BundleIntake flag path stays dormant. Only a body / self-containment
                # violation refuses a builder token here -- host-side hydration is what
                # is responsible for producing a self-contained body, so a pointer or
                # empty body means hydration did not deliver, exactly like
                # spec-body-unavailable. Acceptance and scope/test_slice violations are
                # ADVISORY (emitted, never parked): scope-less specs are a supported case
                # (they render byte-identically, see docs/build-log.md) and acceptance may
                # live outside the hydrated body, so they must not gate a builder token.
                lint_res = lint_spec(spec)
                _body_violations = [v for v in lint_res.violations if v.field == "body"]
                _advisory = [v for v in lint_res.violations if v.field != "body"]
                if _body_violations:
                    trail = {
                        "violations": [{"field": v.field, "message": v.message}
                                       for v in _body_violations],
                        "advisory": [{"field": v.field, "message": v.message}
                                     for v in _advisory],
                        "spec_hash": lint_res.spec_hash,
                    }
                    halts = queue.park(task, "manifest-lint-failed", trail)
                    self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                               detail={"park_reason": "manifest-lint-failed",
                                       "failure_trail": trail})
                    parked_records.append(ParkedRecord(spec_slug=slug,
                                                       park_reason="manifest-lint-failed"))
                    halt_records.extend(halts)
                    if halts:
                        halt_chains.append(HaltChain(parked_slug=slug,
                                                     halted_slugs=[h.spec_slug for h in halts]))
                    continue
                if _advisory:
                    # Not a refusal: surface the non-body gaps so a scope-less or
                    # acceptance-thin spec is visible in the run event trail.
                    self._emit(run_id, "manifest_lint_advisory", task_id=task_id, spec_slug=slug,
                               detail={"advisory": [{"field": v.field, "message": v.message}
                                                    for v in _advisory],
                                       "spec_hash": lint_res.spec_hash})
                if lint_res.clean:
                    # Record the spec hash for a fully-clean lint pass via the optional
                    # ledger hook (getattr pattern, mirrors bundle.py); a ledger without
                    # the hook simply skips it. Fire-and-forget: never fail a build here.
                    _record_lint = getattr(self._ledger, "record_manifest_lint", None)
                    if callable(_record_lint):
                        try:
                            _record_lint(run_id, [slug], lint_res.spec_hash)
                        except Exception as exc:
                            print(f"[manifest-lint] ledger record failed: {exc}", file=sys.stderr)

                # Claim (write-ahead: foreman_tasks -> building BEFORE branch creation)
                ok = queue.claim(task)
                if not ok:
                    continue  # another worker claimed; skip and re-poll

                self._emit(run_id, "task_claimed", task_id=task_id, spec_slug=slug)

                # R4: circuit breaker on deterministic repeat failure.
                broken_reason = self._circuit_broken(run_id, slug)
                if broken_reason is not None:
                    recent = self._ledger.recent_dispositions(run_id, slug, 3) if hasattr(self._ledger, "recent_dispositions") else []
                    halts = queue.park(task, "circuit-break-repeat-failure", {
                        "repeated_park_reason": broken_reason,
                        "consecutive": len(recent),
                    })
                    self._emit(run_id, "halted", task_id=task_id, spec_slug=slug, detail={
                        "park_reason": "circuit-break-repeat-failure",
                        "diagnosis": broken_reason,
                    })
                    parked_records.append(ParkedRecord(spec_slug=slug, park_reason="circuit-break-repeat-failure"))
                    halt_records.extend(halts)
                    continue

                # Multi-spec: bind the transport to this task's spec/run before dispatch.
                # Duck-typed so transports without per-task binding (fakes, API path) are
                # unaffected.
                set_task = getattr(self._transport, "set_task", None)
                if callable(set_task):
                    set_task(slug, run_id)

                # bind the liveness heartbeat sink for this task so the watchdog
                # writes last_heartbeat_at driven by transport output activity.
                set_sink = getattr(self._transport, "set_heartbeat_sink", None)
                if callable(set_sink):
                    set_sink(self._make_heartbeat_sink(run_id, slug))

                # Run the build-verify-retry loop
                result = self._run_task(task, run_id, run_uuid, spec, queue)

                # heartbeat write failures are throttled and noted once per run.
                hb_failures = int(getattr(self._transport, "last_heartbeat_failures", 0) or 0)
                if hb_failures and not hb_failure_noted:
                    run_notes.append(
                        f"heartbeat writes failed during this run (throttled); "
                        f"last session saw {hb_failures} consecutive failures"
                    )
                    hb_failure_noted = True

                if result["status"] == "committed":
                    committed_slugs.append(slug)
                else:
                    park_reason = result.get("park_reason", "unknown")
                    trail = result.get("failure_trail", {})
                    parked_records.append(ParkedRecord(spec_slug=slug, park_reason=park_reason, failure_trail=trail))
                    for h in result.get("halts", []):
                        halt_records.append(h)
                    if result.get("halts"):
                        halt_chains.append(HaltChain(
                            parked_slug=slug,
                            halted_slugs=[h.spec_slug for h in result["halts"]],
                        ))

            self._emit(
                run_id,
                "run_exited",
                detail={
                    "committed": len(committed_slugs),
                    "parked": len(parked_records),
                    "halted": len(halt_records),
                },
            )

            if not resolution.ordered and not appended_slugs:
                # Zero specs queued at intake (e.g. all excluded by the XOR
                # attestation gate) and nothing appended mid-run. This is NOT a normal
                # completion -- mark the run 'no_intake' and record every exclusion so it
                # is not a silent 0/0/0 (run fm-20260705-2346 exited 'completed' with
                # report=NULL).
                final_status = "no_intake"
                final_report = {
                    "outcome": "no_intake",
                    "queued": 0,
                    "excluded": [
                        {"spec_slug": e.spec_slug, "reason": e.reason}
                        for e in resolution.excluded
                    ],
                }
            else:
                final_status = "completed"

            if appended_slugs:
                # appended specs are reported alongside intake-time specs.
                final_report = {
                    **(final_report or {}),
                    "appended": list(appended_slugs),
                }
            if run_notes:
                final_report = {**(final_report or {}), "notes": list(run_notes)}

            return RunReport(
                run_id=run_id,
                total_wall_s=time.monotonic() - t_start,
                committed=committed_slugs,
                parked=parked_records,
                dependent_halted=halt_records,
                excluded=resolution.excluded,
                dependent_halt_chains=halt_chains,
                appended=appended_slugs,
                notes=run_notes,
            )
        finally:
            # F5: close out build_runs so it never lingers in 'running'.
            try:
                self._ledger.update_run_status(run_uuid, final_status, report=final_report)
            except Exception as exc:
                import sys as _sys
                print(f"[finalize] update_run_status({final_status}) failed: {exc}", file=_sys.stderr)

    @staticmethod
    def _count_findings(findings: str | None) -> int:
        """Count the number of individual findings in the verifier output.

        Findings are delimited by lines starting with '- ' or numbered '1.' etc.
        Falls back to treating the whole text as one finding when no list markers appear."""
        if not findings:
            return 0
        text = findings.strip()
        lines = text.splitlines()
        count = sum(
            1 for ln in lines
            if ln.lstrip().startswith("- ") or (ln.lstrip()[:2].rstrip(".").isdigit())
        )
        return count if count > 0 else (1 if text else 0)

    def _dispatch_verify(
        self,
        spec: dict,
        run_id: str,
        branch: str,
        attempt: int,
        prior_findings: str | None,
        task_id: str | None,
        slug: str,
    ):
        """Dispatch the cold verify agent with malformed-verdict retries.

        Returns (verify_result, spec_wallclock_delta_secs) on success, or
        raises _VerifyMalformed (a sentinel) when retries are exhausted."""
        cfg = self.config
        _diff_text = _fetch_compare_diff(cfg.repo, "main", branch)
        verify_prompt = render_verify_prompt(
            spec, run_id, branch, attempt, prior_findings, diff_text=_diff_text
        )
        verify_result = None
        elapsed = 0.0
        for _vretry in range(_MAX_VERIFY_RETRIES + 1):
            _verify_t0 = time.monotonic()
            verify_result = self._transport.verify(verify_prompt, cfg.verifier_model)
            elapsed += time.monotonic() - _verify_t0
            # persist raw verifier output tail in the verify_result event.
            raw_tail = (verify_result.raw_output or "")[-4000:]
            self._emit(run_id, "verify_result", task_id=task_id, spec_slug=slug,
                       detail={
                           "verdict": verify_result.verdict,
                           "findings": verify_result.findings,
                           "raw_output_tail": raw_tail,
                           "verify_retry": _vretry,
                       })
            if verify_result.verdict != "MALFORMED":
                break
        else:
            raise _VerifyMalformed(verify_result, elapsed)
        return verify_result, elapsed

    def _run_task(
        self,
        task: dict,
        run_id: str,
        run_uuid: str,
        spec: dict,
        queue: TaskQueue,
    ) -> dict:
        """Inner build-verify-retry loop for one task. Returns disposition dict."""
        cfg = self.config
        slug = task["spec_slug"]
        task_id = task.get("id")
        failure_trail: dict = {}

        # hydrate prior findings from the most recent failed run for this spec.
        # Only injected into attempt-0; subsequent rebuild attempts carry live findings.
        _prior_fn = getattr(self._ledger, "fetch_prior_verify_findings", None)
        cross_run_findings: str | None = None
        if callable(_prior_fn):
            try:
                raw_prior = _prior_fn(slug, run_id)
                if raw_prior:
                    cross_run_findings = raw_prior[:4000]
                    if len(raw_prior) > 4000:
                        cross_run_findings += "\n[... prior findings truncated at 4000 chars ...]"
            except Exception:
                pass

        prior_findings: str | None = cross_run_findings
        # Anti-slop static-lint findings carried into a retry build prompt
        # (anti-slop static lint). Set on a first-attempt lint FAIL.
        lint_findings: str | None = None
        spec_wallclock_secs: float = 0.0

        # fix_budget_remaining: tracks how many fix passes remain for this task this run.
        # fix passes consume fix_budget, not build attempts.
        import os as _os
        fix_forward_max = int(_os.environ.get("FOREMAN_FIX_FORWARD_MAX_FINDINGS", "3"))
        fix_budget_remaining = int(_os.environ.get("FOREMAN_FIX_BUDGET", "2"))
        # The commit_sha from the most recently completed build; used by fix-forward lane.
        last_build_sha: str | None = None
        last_build_branch: str | None = None

        for attempt in range(2):
            # Ceiling check ONLY before dispatching a new BUILD attempt
            if spec_wallclock_secs >= FOREMAN_SPEC_WALLCLOCK_CEILING:
                halts = queue.park(task, "spec-wallclock-ceiling-exceeded",
                                   {"accumulated_secs": spec_wallclock_secs})
                self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={
                    "park_reason": "spec-wallclock-ceiling-exceeded",
                    "accumulated_secs": spec_wallclock_secs,
                })
                return {"status": "parked", "park_reason": "spec-wallclock-ceiling-exceeded",
                        "failure_trail": {"accumulated_secs": spec_wallclock_secs}, "halts": halts}

            # Fetch live origin/main HEAD (never cached between attempts)
            base_sha = self._fetch_live_base_sha()

            branch = f"build/{run_id}/{slug}/{attempt}"

            # Create and push feature branch
            try:
                confirm_git_state(cfg.working_dir, cfg.remote_url)
                create_branch(cfg.working_dir, branch, base_sha)
                checkout_branch(cfg.working_dir, branch)
                push_branch(cfg.working_dir, cfg.remote_url, branch)
            except GitError as exc:
                halts = queue.park(task, "build-git-error", {"error": str(exc), "attempt": attempt})
                self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={"park_reason": "build-git-error", "failure_trail": {"error": str(exc)}})
                return {"status": "parked", "park_reason": "build-git-error", "failure_trail": {"error": str(exc)}, "halts": halts}

            # Build agent
            build_prompt = render_build_prompt(
                spec, run_id, branch, attempt, cfg.repo, prior_findings,
                lint_findings=lint_findings,
            )
            self._emit(run_id, "build_dispatched", task_id=task_id, spec_slug=slug, detail={"model": cfg.builder_model, "branch": branch})
            _build_t0 = time.monotonic()
            build_result = self._transport.build(build_prompt, cfg.builder_model)
            spec_wallclock_secs += time.monotonic() - _build_t0

            if build_result.commit_sha is None:
                ec = build_result.error_class
                if ec == "builder-wedged":
                    # the liveness watchdog killed a wedged / over-cap
                    # session. Record the trail (wedge duration, last output excerpt)
                    # and retry once under the existing budget; a second wedge parks
                    # with park_reason='builder-wedged'.
                    wd = build_result.wedge_detail or {}
                    attempt_trail = {
                        "attempt": attempt,
                        "event": "builder-wedged",
                        "reason": build_result.reason,
                        **wd,
                    }
                    failure_trail[f"attempt_{attempt}"] = attempt_trail
                    self._emit(run_id, "build_result", task_id=task_id, spec_slug=slug,
                               detail={"status": "failed", "error_class": "builder-wedged",
                                       "wedge_reason": build_result.reason,
                                       "wedge_seconds": wd.get("wedge_seconds")})
                    if attempt == 1:
                        halts = queue.park(task, "builder-wedged", failure_trail)
                        self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                   detail={"park_reason": "builder-wedged",
                                           "failure_trail": failure_trail})
                        return {"status": "parked", "park_reason": "builder-wedged",
                                "failure_trail": failure_trail, "halts": halts}
                    # First wedge: retry once. The kill happened while the task was still
                    # 'building' (no verify transition), so we stay in 'building' and let
                    # the loop open a fresh attempt branch -- the claim is retained.
                    preserved = _preserve_remote_branch(
                        cfg.working_dir, cfg.remote_url, branch, run_id, slug, attempt)
                    if preserved:
                        self._emit(run_id, "branch_preserved", task_id=task_id, spec_slug=slug,
                                   detail={"from": branch, "to": preserved, "path": "builder-wedged"})
                    self._ledger.update_data(run_uuid, slug, {"attempt": 1})
                    continue
                if ec == "build-api-error":
                    trail = {
                        "attempt": attempt,
                        "status_code": build_result.status_code,
                        "error_body": build_result.error_body,
                    }
                    self._emit(run_id, "build_result", task_id=task_id, spec_slug=slug, detail={"status": "failed", "error_class": ec, "status_code": build_result.status_code, "error_body": build_result.error_body})
                    halts = queue.park(task, "build-api-error", trail)
                    self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={"park_reason": "build-api-error", "failure_trail": trail})
                    return {"status": "parked", "park_reason": "build-api-error", "failure_trail": trail, "halts": halts}
                elif ec == "build-push-failed":
                    trail = {"attempt": attempt, "agent_output": build_result.raw_output[-3000:] if build_result.raw_output else ""}
                    self._emit(run_id, "build_result", task_id=task_id, spec_slug=slug, detail={"status": "failed", "error_class": ec})
                    halts = queue.park(task, "build-push-failed", trail)
                    self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={"park_reason": "build-push-failed", "failure_trail": trail})
                    return {"status": "parked", "park_reason": "build-push-failed", "failure_trail": trail, "halts": halts}
                elif ec == "build-unpushed":
                    # The builder made commits but the orchestrator push did not land
                    # them on the remote (push denied/rejected/timed out). This is NOT
                    # an empty branch -- reserve build-no-commit for that. Record the
                    # push error so the failure is diagnosable, never silently dropped.
                    trail = {
                        "attempt": attempt,
                        "push_error": build_result.reason,
                        "agent_output": build_result.raw_output[-3000:] if build_result.raw_output else "",
                    }
                    self._emit(run_id, "build_result", task_id=task_id, spec_slug=slug, detail={"status": "failed", "error_class": ec})
                    halts = queue.park(task, "build-unpushed", trail)
                    self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={"park_reason": "build-unpushed", "failure_trail": trail})
                    return {"status": "parked", "park_reason": "build-unpushed", "failure_trail": trail, "halts": halts}
                else:
                    trail = {"attempt": attempt, "agent_output": build_result.raw_output[-3000:] if build_result.raw_output else ""}
                    self._emit(run_id, "build_result", task_id=task_id, spec_slug=slug, detail={"status": "failed", "error_class": ec or "build-no-commit"})
                    halts = queue.park(task, "build-no-commit", trail)
                    self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={"park_reason": "build-no-commit", "failure_trail": trail})
                    return {"status": "parked", "park_reason": "build-no-commit", "failure_trail": trail, "halts": halts}

            # Build succeeded
            last_build_sha = build_result.commit_sha
            last_build_branch = branch
            self._emit(run_id, "build_result", task_id=task_id, spec_slug=slug, detail={"status": "ok", "commit_sha": build_result.commit_sha})

            # Post-build scaffolding guard (Bug 4): a builder must never commit
            # harness scaffolding (emit_trace.sh, ruleforge_check.py). Reject the
            # build if any leaked into the commit set. Best-effort: a delta that
            # cannot be measured here is left to the substance gate, not blocked.
            leak = self._scaffolding_in_build(base_sha, build_result.commit_sha)
            if leak:
                trail = {"attempt": attempt, "scaffolding": leak}
                halts = queue.park(task, "scaffolding-leak", trail)
                self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                           detail={"park_reason": "scaffolding-leak", "failure_trail": trail})
                return {"status": "parked", "park_reason": "scaffolding-leak",
                        "failure_trail": trail, "halts": halts}

            # Anti-slop static lint (anti-slop static lint). Pure,
            # deterministic pre-verify gate: the build commit exists and its diff is
            # available, but no verify token has been spent yet. On FAIL we skip
            # verify for this attempt and route into the existing single-retry flow
            # with lint findings injected into the retry prompt. The task is still in
            # 'building' here (no verify transition yet), so a retry mirrors the
            # wedged-retry path: preserve the branch, carry findings, bump attempt.
            _lint_diff = _fetch_compare_diff(cfg.repo, "main", branch)
            if not _lint_diff:
                # Diff unavailable (compare API failure or empty): the lint cannot
                # run this attempt. Deliberately fail-open (the verify agent still
                # runs), but emit so the skip is observable in the run ledger.
                self._emit(run_id, "antislop_lint_skipped", task_id=task_id, spec_slug=slug,
                           detail={"reason": "diff-unavailable", "branch": branch})
            if _lint_diff:
                lint_res = run_antislop_lint_gate(
                    _lint_diff, _spec_demands_tests(spec), cfg.antislop
                )
                self._emit(run_id, "antislop_lint_result", task_id=task_id, spec_slug=slug,
                           detail={"verdict": lint_res.verdict,
                                   "findings": [dataclasses.asdict(f) for f in lint_res.findings],
                                   "error": lint_res.error})
                if lint_res.verdict == "FAIL":
                    _lint_text = format_lint_findings(lint_res)
                    attempt_findings = {
                        "attempt": attempt,
                        "verdict": "ANTISLOP_FAIL",
                        "findings": _lint_text,
                        "error": lint_res.error,
                        "lint_findings": [dataclasses.asdict(f) for f in lint_res.findings],
                    }
                    failure_trail[f"attempt_{attempt}_antislop"] = attempt_findings
                    if attempt == 1:
                        # Second failure: park (blocks the verify + merge path).
                        halts = queue.park(task, "antislop-lint-failed", failure_trail)
                        self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                   detail={"park_reason": "antislop-lint-failed",
                                           "failure_trail": failure_trail})
                        return {"status": "parked", "park_reason": "antislop-lint-failed",
                                "failure_trail": failure_trail, "halts": halts}
                    # First failure: retry once with lint findings injected.
                    lint_findings = _lint_text
                    preserved = _preserve_remote_branch(
                        cfg.working_dir, cfg.remote_url, branch, run_id, slug, attempt)
                    if preserved:
                        self._emit(run_id, "branch_preserved", task_id=task_id, spec_slug=slug,
                                   detail={"from": branch, "to": preserved, "path": "antislop-retry"})
                    self._ledger.update_data(run_uuid, slug, {"attempt": 1})
                    continue

            # Write-ahead: verifying (before verify dispatch)
            queue.advance(task, TaskStatus.VERIFYING, commit_sha=build_result.commit_sha)

            # NO ceiling check here. Once a build completes, verify runs
            # regardless of accumulated wall-clock. The ceiling only gates the
            # NEXT build attempt dispatch (top of the for loop).

            # Verify agent. Fetch the diff authenticated on the orchestrator side
            # and inline it (plus the spec body) so the cold verifier never depends
            # on unauthenticated gh in the sandbox.
            try:
                verify_result, _verify_elapsed = self._dispatch_verify(
                    spec, run_id, branch, attempt, prior_findings, task_id, slug
                )
            except _VerifyMalformed as _vm:
                spec_wallclock_secs += _vm.elapsed_secs
                _vr = _vm.last_result
                trail = {
                    "attempt": attempt,
                    "verify_retries": _MAX_VERIFY_RETRIES,
                    "raw_output_tail": (_vr.raw_output or "")[-4000:],
                }
                failure_trail[f"attempt_{attempt}_verify_malformed"] = trail
                halts = queue.park(task, "verify-malformed", trail)
                self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                           detail={"park_reason": "verify-malformed", "failure_trail": trail})
                return {"status": "parked", "park_reason": "verify-malformed",
                        "failure_trail": trail, "halts": halts}
            spec_wallclock_secs += _verify_elapsed

            if verify_result.verdict == "PASS":
                # H7: mechanical conformance gate. The
                # deterministic subset of Checklists A (max-width/token-override/
                # grid/density) and B (vendor-self-source ratio/uniform-value/
                # sample-size) is a HARD gate here, not LLM inference. The cold
                # verifier's checklist read above remains a semantic supplement.
                # A FAIL routes to the standard R5 park/retry path (
                # direct-push mode).
                from .conformance import format_conformance_findings
                conf = self._check_conformance(base_sha, build_result.commit_sha, spec, run_id)
                self._emit(run_id, "conformance_result", task_id=task_id, spec_slug=slug,
                           detail={"verdict": conf.verdict, "checklist": conf.checklist,
                                   "applied_items": conf.applied_items,
                                   "violations": [{"item": v.item, "detail": v.detail}
                                                  for v in conf.violations],
                                   "reason": conf.reason})
                if conf.verdict == "FAIL":
                    conf_findings = format_conformance_findings(conf)
                    attempt_findings = {
                        "attempt": attempt,
                        "verdict": "CONFORMANCE_FAIL",
                        "checklist": conf.checklist,
                        "findings": conf_findings,
                        "violations": [{"item": v.item, "detail": v.detail}
                                       for v in conf.violations],
                    }
                    failure_trail[f"attempt_{attempt}"] = attempt_findings
                    if attempt == 1:
                        # Second failure: park. Blocks the merge transition.
                        halts = queue.park(task, "conformance-checklist-failed", failure_trail)
                        self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                   detail={"park_reason": "conformance-checklist-failed",
                                           "failure_trail": failure_trail})
                        return {"status": "parked", "park_reason": "conformance-checklist-failed",
                                "failure_trail": failure_trail, "halts": halts}
                    # First failure: retry once with findings injected.
                    prior_findings = conf_findings
                    preserved = _preserve_remote_branch(
                        cfg.working_dir, cfg.remote_url, branch, run_id, slug, attempt)
                    if preserved:
                        self._emit(run_id, "branch_preserved", task_id=task_id, spec_slug=slug,
                                   detail={"from": branch, "to": preserved, "path": "conformance-retry"})
                    queue.advance(task, TaskStatus.BUILDING, attempt_no=1)
                    continue

                # F3: deliverable-delta substance gate. Even when the cold verifier
                # passes, a hollow commit set (docs/build-log only, or pre-existing
                # test scaffolding / import-only edits) must FAIL. Measures the
                # base_sha..commit delta, never repo-wide or test-outcome signals.
                substance = self._check_substance_delta(
                    base_sha, build_result.commit_sha, spec, branch=branch
                )
                self._emit(run_id, "substance_result", task_id=task_id, spec_slug=slug,
                           detail={"verdict": substance.verdict, "reason": substance.reason,
                                   "substantive": substance.substantive,
                                   "considered": substance.considered})
                if substance.verdict == "FAIL":
                    trail = {"attempt": attempt, "substance_reason": substance.reason,
                             "considered": substance.considered}
                    halts = queue.park(task, "substance-empty-delta", trail)
                    self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                               detail={"park_reason": "substance-empty-delta", "failure_trail": trail})
                    return {"status": "parked", "park_reason": "substance-empty-delta",
                            "failure_trail": trail, "halts": halts}
                # INCONCLUSIVE never vetoes a verifier PASS: the discriminator
                # evaluated nothing (empty considered set / unresolvable xrepo
                # base), which is absence of evidence, not a hollow commit. The
                # substance_result event above is the hollow-commit audit trail.
                # (Bug 2, Bug 3.)

                # F4: if the delta ships Supabase objects, assert them live.
                dbres = self._check_live_db(base_sha, build_result.commit_sha)
                self._emit(run_id, "live_db_result", task_id=task_id, spec_slug=slug,
                           detail={"verdict": dbres.verdict, "reason": dbres.reason,
                                   "missing": dbres.missing})
                if dbres.verdict == "FAIL":
                    trail = {"attempt": attempt, "missing": dbres.missing, "reason": dbres.reason}
                    halts = queue.park(task, "live-db-assert-failed", trail)
                    self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                               detail={"park_reason": "live-db-assert-failed", "failure_trail": trail})
                    return {"status": "parked", "park_reason": "live-db-assert-failed",
                            "failure_trail": trail, "halts": halts}

                # R9: origin ground-truth gate
                try:
                    confirm_git_state(cfg.working_dir, cfg.remote_url)
                    origin_outcome, confirmed_sha = check_origin_ground_truth(
                        working_dir=cfg.working_dir,
                        remote_url=cfg.remote_url,
                        branch_name=branch,
                        claimed_sha=build_result.commit_sha,
                        base_sha=base_sha,
                    )
                except GitError as exc:
                    halts = queue.park(task, "r9-gate-error", {"error": str(exc)})
                    self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={"park_reason": "r9-gate-error", "failure_trail": {"error": str(exc)}})
                    return {"status": "parked", "park_reason": "r9-gate-error", "failure_trail": {"error": str(exc)}, "halts": halts}

                if origin_outcome == OriginCheckResult.PHANTOM_COMPLETION:
                    halts = queue.park(task, "phantom-completion", {"attempt": attempt})
                    self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={"park_reason": "phantom-completion", "failure_trail": {"attempt": attempt}})
                    return {"status": "parked", "park_reason": "phantom-completion", "failure_trail": {}, "halts": halts}

                # Write-ahead: merging. F1 -- record the verify PASS + models into
                # the ledger row here so build_run_specs carries verify_result=PASS
                # BEFORE the committed transition (which the DB trigger requires).
                _verify_fields = {
                    "verify_result": "PASS",
                    "verifier_model": cfg.verifier_model,
                    "builder_model": cfg.builder_model,
                    "verify_report": {"verdict": "PASS", "findings": verify_result.findings or ""},
                    "commit_sha": build_result.commit_sha,
                    "branch_name": branch,
                    "base_sha": base_sha,
                    "attempt": attempt,
                }
                queue.advance(task, TaskStatus.MERGING, **_verify_fields)

                if origin_outcome == OriginCheckResult.ALREADY_CONTAINS:
                    # Clean no-op
                    queue.advance(task, TaskStatus.COMMITTED, commit_sha=confirmed_sha or "")
                    self._emit(run_id, "merge_confirmed", task_id=task_id, spec_slug=slug, detail={"merged_sha": confirmed_sha or ""})
                    return {"status": "committed", "commit_sha": confirmed_sha or ""}

                # ff-merge
                try:
                    confirm_git_state(cfg.working_dir, cfg.remote_url)
                    checkout_branch(cfg.working_dir, cfg.base_ref)
                    ff_ok = ff_merge_local(cfg.working_dir, branch)
                except GitError as exc:
                    halts = queue.park(task, "ff-merge-error", {"error": str(exc)})
                    self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={"park_reason": "ff-merge-error", "failure_trail": {"error": str(exc)}})
                    return {"status": "parked", "park_reason": "ff-merge-error", "failure_trail": {"error": str(exc)}, "halts": halts}

                if not ff_ok:
                    halts = queue.park(task, "ff-merge-failed", {})
                    self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={"park_reason": "ff-merge-failed", "failure_trail": {}})
                    return {"status": "parked", "park_reason": "ff-merge-failed", "failure_trail": {}, "halts": halts}

                merged_sha = get_local_sha(cfg.working_dir, "HEAD")

                try:
                    push_ref(cfg.working_dir, cfg.remote_url, cfg.base_ref)
                    push_ok = post_push_check(
                        working_dir=cfg.working_dir,
                        remote_url=cfg.remote_url,
                        base_ref=cfg.base_ref,
                        merged_sha=merged_sha,
                    )
                except GitError as exc:
                    halts = queue.park(task, "push-error", {"error": str(exc)})
                    self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={"park_reason": "push-error", "failure_trail": {"error": str(exc)}})
                    return {"status": "parked", "park_reason": "push-error", "failure_trail": {"error": str(exc)}, "halts": halts}

                if not push_ok:
                    halts = queue.park(task, "post-push-ref-not-advanced", {})
                    self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={"park_reason": "post-push-ref-not-advanced", "failure_trail": {}})
                    return {"status": "parked", "park_reason": "post-push-ref-not-advanced", "failure_trail": {}, "halts": halts}

                queue.advance(task, TaskStatus.COMMITTED, commit_sha=merged_sha)
                self._emit(run_id, "merge_confirmed", task_id=task_id, spec_slug=slug, detail={"merged_sha": merged_sha})
                return {"status": "committed", "commit_sha": merged_sha}

            else:
                # FAIL
                attempt_findings = {
                    "attempt": attempt,
                    "verdict": verify_result.verdict,
                    "findings": verify_result.findings,
                }
                failure_trail[f"attempt_{attempt}"] = attempt_findings

                # R2: fix-forward lane. When findings count <= threshold and a build
                # commit exists, dispatch a fix pass instead of a rebuild.
                # fix passes are NOT blocked by wall-clock ceiling; check
                # fix-forward eligibility BEFORE the ceiling-exceeded-FAIL park.
                findings_count = self._count_findings(verify_result.findings)
                if (
                    findings_count <= fix_forward_max
                    and last_build_sha is not None
                    and last_build_branch is not None
                    and fix_budget_remaining > 0
                ):
                    # fix pass; patch existing implementation, not rebuild.
                    fix_budget_remaining -= 1
                    prior_findings = verify_result.findings
                    # Transition back to building before fix pass dispatch (write-ahead).
                    queue.advance(task, TaskStatus.BUILDING, attempt_no=attempt)
                    # fix pass prompt instructs the builder to patch (not rebuild).
                    fix_prompt = render_build_prompt(
                        spec, run_id, last_build_branch, attempt, cfg.repo,
                        prior_findings=prior_findings,
                        fix_forward=True,
                    )
                    self._emit(run_id, "fix_pass_dispatched", task_id=task_id, spec_slug=slug,
                               detail={"model": cfg.builder_model, "branch": last_build_branch,
                                       "findings_count": findings_count,
                                       "fix_budget_remaining": fix_budget_remaining})
                    _fix_t0 = time.monotonic()
                    fix_result = self._transport.build(fix_prompt, cfg.builder_model)
                    # fix passes are NOT gated by wall-clock ceiling; we do not
                    # add elapsed to spec_wallclock_secs for ceiling-gating purposes.
                    _fix_elapsed = time.monotonic() - _fix_t0

                    if fix_result.commit_sha is None:
                        # Fix pass failed to produce a commit; treat as fix budget exhausted.
                        fix_trail = {
                            "attempt": attempt, "fix_pass": True,
                            "error_class": fix_result.error_class or "fix-no-commit",
                        }
                        failure_trail[f"attempt_{attempt}_fix"] = fix_trail
                        # Fall through to rebuild or terminal park below.
                    else:
                        # Fix commit landed; run full verify on it.
                        # Update tracking for subsequent fix passes if needed.
                        last_build_sha = fix_result.commit_sha
                        queue.advance(task, TaskStatus.VERIFYING, commit_sha=fix_result.commit_sha)
                        try:
                            fix_vr, _fix_velapsed = self._dispatch_verify(
                                spec, run_id, last_build_branch, attempt,
                                prior_findings, task_id, slug
                            )
                        except _VerifyMalformed as _vm:
                            spec_wallclock_secs += _vm.elapsed_secs
                            _fvr = _vm.last_result
                            trail = {
                                "attempt": attempt, "fix_pass": True,
                                "verify_retries": _MAX_VERIFY_RETRIES,
                                "raw_output_tail": (_fvr.raw_output or "")[-4000:],
                            }
                            failure_trail[f"attempt_{attempt}_fix_verify_malformed"] = trail
                            halts = queue.park(task, "verify-malformed", trail)
                            self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                       detail={"park_reason": "verify-malformed",
                                               "failure_trail": trail})
                            return {"status": "parked", "park_reason": "verify-malformed",
                                    "failure_trail": trail, "halts": halts}

                        if fix_vr.verdict == "PASS":
                            # Fix pass succeeded; proceed through normal PASS gates.
                            # Fall through by setting verify_result and continuing.
                            verify_result = fix_vr
                            # Re-enter PASS handling via inner goto: build_result.commit_sha
                            # is the fix commit. We redirect to the post-verify PASS path.
                            # Update build_result so downstream gate calls use the fix SHA.
                            build_result = type(build_result)(
                                commit_sha=fix_result.commit_sha,
                                raw_output=fix_result.raw_output,
                                error_class=fix_result.error_class,
                                reason=fix_result.reason,
                            )
                            # Jump to PASS processing; break out of FAIL branch.
                            # We do this by overriding verify_result.verdict; the
                            # `if verify_result.verdict == "PASS"` block above this `else`
                            # will be executed on the next iteration. Instead, we inline
                            # the PASS gate chain here to avoid restructuring the loop.
                            # Inline PASS gate: conformance -> substance -> live_db -> R9 -> merge
                            _inline_pass = True
                        else:
                            # Fix verify returned FAIL or MALFORMED-after-retries.
                            fix_attempt_findings = {
                                "attempt": attempt, "fix_pass": True,
                                "verdict": fix_vr.verdict,
                                "findings": fix_vr.findings,
                            }
                            failure_trail[f"attempt_{attempt}_fix"] = fix_attempt_findings
                            prior_findings = fix_vr.findings
                            _inline_pass = False

                        if _inline_pass:
                            # Run conformance gate on fix commit.
                            from .conformance import format_conformance_findings
                            conf = self._check_conformance(base_sha, fix_result.commit_sha, spec, run_id)
                            self._emit(run_id, "conformance_result", task_id=task_id, spec_slug=slug,
                                       detail={"verdict": conf.verdict, "checklist": conf.checklist,
                                               "applied_items": conf.applied_items,
                                               "violations": [{"item": v.item, "detail": v.detail}
                                                              for v in conf.violations],
                                               "reason": conf.reason})
                            if conf.verdict == "FAIL":
                                conf_findings = format_conformance_findings(conf)
                                fix_trail_conf = {
                                    "attempt": attempt, "fix_pass": True,
                                    "verdict": "CONFORMANCE_FAIL",
                                    "checklist": conf.checklist,
                                    "findings": conf_findings,
                                    "violations": [{"item": v.item, "detail": v.detail}
                                                   for v in conf.violations],
                                }
                                failure_trail[f"attempt_{attempt}_fix_conf"] = fix_trail_conf
                                halts = queue.park(task, "conformance-checklist-failed", failure_trail)
                                self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                           detail={"park_reason": "conformance-checklist-failed",
                                                   "failure_trail": failure_trail})
                                return {"status": "parked",
                                        "park_reason": "conformance-checklist-failed",
                                        "failure_trail": failure_trail, "halts": halts}

                            substance = self._check_substance_delta(
                                base_sha, fix_result.commit_sha, spec, branch=last_build_branch
                            )
                            self._emit(run_id, "substance_result", task_id=task_id, spec_slug=slug,
                                       detail={"verdict": substance.verdict, "reason": substance.reason,
                                               "substantive": substance.substantive,
                                               "considered": substance.considered})
                            if substance.verdict == "FAIL":
                                s_trail = {"attempt": attempt, "fix_pass": True,
                                           "substance_reason": substance.reason,
                                           "considered": substance.considered}
                                halts = queue.park(task, "substance-empty-delta", s_trail)
                                self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                           detail={"park_reason": "substance-empty-delta",
                                                   "failure_trail": s_trail})
                                return {"status": "parked", "park_reason": "substance-empty-delta",
                                        "failure_trail": s_trail, "halts": halts}

                            dbres = self._check_live_db(base_sha, fix_result.commit_sha)
                            self._emit(run_id, "live_db_result", task_id=task_id, spec_slug=slug,
                                       detail={"verdict": dbres.verdict, "reason": dbres.reason,
                                               "missing": dbres.missing})
                            if dbres.verdict == "FAIL":
                                db_trail = {"attempt": attempt, "fix_pass": True,
                                            "missing": dbres.missing, "reason": dbres.reason}
                                halts = queue.park(task, "live-db-assert-failed", db_trail)
                                self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                           detail={"park_reason": "live-db-assert-failed",
                                                   "failure_trail": db_trail})
                                return {"status": "parked", "park_reason": "live-db-assert-failed",
                                        "failure_trail": db_trail, "halts": halts}

                            # R9 gate on fix commit.
                            try:
                                confirm_git_state(cfg.working_dir, cfg.remote_url)
                                fix_origin_outcome, fix_confirmed_sha = check_origin_ground_truth(
                                    working_dir=cfg.working_dir,
                                    remote_url=cfg.remote_url,
                                    branch_name=last_build_branch,
                                    claimed_sha=fix_result.commit_sha,
                                    base_sha=base_sha,
                                )
                            except GitError as exc:
                                halts = queue.park(task, "r9-gate-error", {"error": str(exc)})
                                self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                           detail={"park_reason": "r9-gate-error",
                                                   "failure_trail": {"error": str(exc)}})
                                return {"status": "parked", "park_reason": "r9-gate-error",
                                        "failure_trail": {"error": str(exc)}, "halts": halts}

                            if fix_origin_outcome == OriginCheckResult.PHANTOM_COMPLETION:
                                halts = queue.park(task, "phantom-completion", {"attempt": attempt, "fix_pass": True})
                                self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                           detail={"park_reason": "phantom-completion",
                                                   "failure_trail": {"attempt": attempt, "fix_pass": True}})
                                return {"status": "parked", "park_reason": "phantom-completion",
                                        "failure_trail": {}, "halts": halts}

                            _fix_verify_fields = {
                                "verify_result": "PASS",
                                "verifier_model": cfg.verifier_model,
                                "builder_model": cfg.builder_model,
                                "verify_report": {"verdict": "PASS",
                                                  "findings": fix_vr.findings or ""},
                                "commit_sha": fix_result.commit_sha,
                                "branch_name": last_build_branch,
                                "base_sha": base_sha,
                                "attempt": attempt,
                            }
                            queue.advance(task, TaskStatus.MERGING, **_fix_verify_fields)

                            if fix_origin_outcome == OriginCheckResult.ALREADY_CONTAINS:
                                queue.advance(task, TaskStatus.COMMITTED,
                                              commit_sha=fix_confirmed_sha or "")
                                self._emit(run_id, "merge_confirmed", task_id=task_id,
                                           spec_slug=slug,
                                           detail={"merged_sha": fix_confirmed_sha or ""})
                                return {"status": "committed",
                                        "commit_sha": fix_confirmed_sha or ""}

                            # ff-merge for fix commit.
                            try:
                                confirm_git_state(cfg.working_dir, cfg.remote_url)
                                checkout_branch(cfg.working_dir, cfg.base_ref)
                                fix_ff_ok = ff_merge_local(cfg.working_dir, last_build_branch)
                            except GitError as exc:
                                halts = queue.park(task, "ff-merge-error", {"error": str(exc)})
                                self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                           detail={"park_reason": "ff-merge-error",
                                                   "failure_trail": {"error": str(exc)}})
                                return {"status": "parked", "park_reason": "ff-merge-error",
                                        "failure_trail": {"error": str(exc)}, "halts": halts}

                            if not fix_ff_ok:
                                halts = queue.park(task, "ff-merge-failed", {})
                                self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                           detail={"park_reason": "ff-merge-failed",
                                                   "failure_trail": {}})
                                return {"status": "parked", "park_reason": "ff-merge-failed",
                                        "failure_trail": {}, "halts": halts}

                            fix_merged_sha = get_local_sha(cfg.working_dir, "HEAD")
                            try:
                                push_ref(cfg.working_dir, cfg.remote_url, cfg.base_ref)
                                fix_push_ok = post_push_check(
                                    working_dir=cfg.working_dir,
                                    remote_url=cfg.remote_url,
                                    base_ref=cfg.base_ref,
                                    merged_sha=fix_merged_sha,
                                )
                            except GitError as exc:
                                halts = queue.park(task, "push-error", {"error": str(exc)})
                                self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                           detail={"park_reason": "push-error",
                                                   "failure_trail": {"error": str(exc)}})
                                return {"status": "parked", "park_reason": "push-error",
                                        "failure_trail": {"error": str(exc)}, "halts": halts}

                            if not fix_push_ok:
                                halts = queue.park(task, "post-push-ref-not-advanced", {})
                                self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                           detail={"park_reason": "post-push-ref-not-advanced",
                                                   "failure_trail": {}})
                                return {"status": "parked",
                                        "park_reason": "post-push-ref-not-advanced",
                                        "failure_trail": {}, "halts": halts}

                            queue.advance(task, TaskStatus.COMMITTED, commit_sha=fix_merged_sha)
                            self._emit(run_id, "merge_confirmed", task_id=task_id, spec_slug=slug,
                                       detail={"merged_sha": fix_merged_sha})
                            return {"status": "committed", "commit_sha": fix_merged_sha}

                        # Fix pass FAIL: if budget remains, loop back (stay on attempt=0,
                        # fix_budget_remaining already decremented).
                        # If budget is 0 or findings exceeded threshold, fall through to rebuild.
                        if fix_budget_remaining > 0 and self._count_findings(prior_findings) <= fix_forward_max:
                            # Re-run the verify loop using the same build branch.
                            # Dispatch another fix pass on the next iteration of this inner loop.
                            # We mimic "continue outer loop" by recursing the fix-pass dispatch.
                            # For simplicity, re-enter via a fix-pass-only inner loop:
                            _fix_inner_limit = fix_budget_remaining
                            for _fi in range(_fix_inner_limit):
                                if fix_budget_remaining <= 0:
                                    break
                                fix_budget_remaining -= 1
                                _fp2 = render_build_prompt(
                                    spec, run_id, last_build_branch, attempt, cfg.repo,
                                    prior_findings=prior_findings,
                                    fix_forward=True,
                                )
                                self._emit(run_id, "fix_pass_dispatched", task_id=task_id,
                                           spec_slug=slug,
                                           detail={"model": cfg.builder_model,
                                                   "branch": last_build_branch,
                                                   "fix_budget_remaining": fix_budget_remaining})
                                queue.advance(task, TaskStatus.BUILDING, attempt_no=attempt)
                                fp2_result = self._transport.build(_fp2, cfg.builder_model)
                                if fp2_result.commit_sha is None:
                                    break
                                last_build_sha = fp2_result.commit_sha
                                queue.advance(task, TaskStatus.VERIFYING,
                                              commit_sha=fp2_result.commit_sha)
                                try:
                                    fp2_vr, _ = self._dispatch_verify(
                                        spec, run_id, last_build_branch, attempt,
                                        prior_findings, task_id, slug
                                    )
                                except _VerifyMalformed:
                                    break
                                if fp2_vr.verdict == "PASS":
                                    # Succeeded on subsequent fix pass; treat as build_result update.
                                    build_result = type(build_result)(
                                        commit_sha=fp2_result.commit_sha,
                                        raw_output=fp2_result.raw_output,
                                        error_class=fp2_result.error_class,
                                        reason=fp2_result.reason,
                                    )
                                    verify_result = fp2_vr
                                    # Signal to outer code to use PASS path.
                                    # The loop structure forces us to park or continue.
                                    # We park here as "committed-via-fix" is not a separate
                                    # status; we continue through the outer PASS block by
                                    # falling through. Actually we cannot jump into the PASS
                                    # block. We must park this sub-result and return committed.
                                    # Inline the merge path again for the second fix pass.
                                    from .conformance import format_conformance_findings as _fcf2
                                    conf2 = self._check_conformance(base_sha, fp2_result.commit_sha, spec, run_id)
                                    if conf2.verdict == "FAIL":
                                        halts = queue.park(task, "conformance-checklist-failed", failure_trail)
                                        self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                                   detail={"park_reason": "conformance-checklist-failed",
                                                           "failure_trail": failure_trail})
                                        return {"status": "parked",
                                                "park_reason": "conformance-checklist-failed",
                                                "failure_trail": failure_trail, "halts": halts}
                                    sub2 = self._check_substance_delta(base_sha, fp2_result.commit_sha, spec, branch=last_build_branch)
                                    if sub2.verdict == "FAIL":
                                        s2t = {"attempt": attempt, "fix_pass": True, "fix_index": _fi + 2}
                                        halts = queue.park(task, "substance-empty-delta", s2t)
                                        self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                                   detail={"park_reason": "substance-empty-delta", "failure_trail": s2t})
                                        return {"status": "parked", "park_reason": "substance-empty-delta",
                                                "failure_trail": s2t, "halts": halts}
                                    db2 = self._check_live_db(base_sha, fp2_result.commit_sha)
                                    if db2.verdict == "FAIL":
                                        db2t = {"attempt": attempt, "fix_pass": True, "fix_index": _fi + 2}
                                        halts = queue.park(task, "live-db-assert-failed", db2t)
                                        self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                                   detail={"park_reason": "live-db-assert-failed", "failure_trail": db2t})
                                        return {"status": "parked", "park_reason": "live-db-assert-failed",
                                                "failure_trail": db2t, "halts": halts}
                                    try:
                                        confirm_git_state(cfg.working_dir, cfg.remote_url)
                                        fp2_oc, fp2_sha = check_origin_ground_truth(
                                            working_dir=cfg.working_dir, remote_url=cfg.remote_url,
                                            branch_name=last_build_branch,
                                            claimed_sha=fp2_result.commit_sha, base_sha=base_sha,
                                        )
                                    except GitError as exc:
                                        halts = queue.park(task, "r9-gate-error", {"error": str(exc)})
                                        self._emit(run_id, "parked", task_id=task_id, spec_slug=slug,
                                                   detail={"park_reason": "r9-gate-error", "failure_trail": {"error": str(exc)}})
                                        return {"status": "parked", "park_reason": "r9-gate-error",
                                                "failure_trail": {"error": str(exc)}, "halts": halts}
                                    if fp2_oc == OriginCheckResult.PHANTOM_COMPLETION:
                                        halts = queue.park(task, "phantom-completion", {})
                                        return {"status": "parked", "park_reason": "phantom-completion", "failure_trail": {}, "halts": halts}
                                    _fp2_vf = {"verify_result": "PASS", "verifier_model": cfg.verifier_model,
                                               "builder_model": cfg.builder_model,
                                               "verify_report": {"verdict": "PASS", "findings": fp2_vr.findings or ""},
                                               "commit_sha": fp2_result.commit_sha, "branch_name": last_build_branch,
                                               "base_sha": base_sha, "attempt": attempt}
                                    queue.advance(task, TaskStatus.MERGING, **_fp2_vf)
                                    if fp2_oc == OriginCheckResult.ALREADY_CONTAINS:
                                        queue.advance(task, TaskStatus.COMMITTED, commit_sha=fp2_sha or "")
                                        return {"status": "committed", "commit_sha": fp2_sha or ""}
                                    try:
                                        confirm_git_state(cfg.working_dir, cfg.remote_url)
                                        checkout_branch(cfg.working_dir, cfg.base_ref)
                                        fp2_ff = ff_merge_local(cfg.working_dir, last_build_branch)
                                    except GitError as exc:
                                        halts = queue.park(task, "ff-merge-error", {"error": str(exc)})
                                        return {"status": "parked", "park_reason": "ff-merge-error",
                                                "failure_trail": {"error": str(exc)}, "halts": halts}
                                    if not fp2_ff:
                                        halts = queue.park(task, "ff-merge-failed", {})
                                        return {"status": "parked", "park_reason": "ff-merge-failed", "failure_trail": {}, "halts": halts}
                                    fp2_merged = get_local_sha(cfg.working_dir, "HEAD")
                                    try:
                                        push_ref(cfg.working_dir, cfg.remote_url, cfg.base_ref)
                                        fp2_push = post_push_check(
                                            working_dir=cfg.working_dir, remote_url=cfg.remote_url,
                                            base_ref=cfg.base_ref, merged_sha=fp2_merged,
                                        )
                                    except GitError as exc:
                                        halts = queue.park(task, "push-error", {"error": str(exc)})
                                        return {"status": "parked", "park_reason": "push-error",
                                                "failure_trail": {"error": str(exc)}, "halts": halts}
                                    if not fp2_push:
                                        halts = queue.park(task, "post-push-ref-not-advanced", {})
                                        return {"status": "parked", "park_reason": "post-push-ref-not-advanced", "failure_trail": {}, "halts": halts}
                                    queue.advance(task, TaskStatus.COMMITTED, commit_sha=fp2_merged)
                                    self._emit(run_id, "merge_confirmed", task_id=task_id, spec_slug=slug,
                                               detail={"merged_sha": fp2_merged})
                                    return {"status": "committed", "commit_sha": fp2_merged}
                                else:
                                    prior_findings = fp2_vr.findings
                            # Budget exhausted or fix passes all failed; fall through to rebuild.

                # findings above threshold OR fix budget exhausted.
                # Check ceiling AFTER fix-forward eligibility: if ceiling
                # exceeded, park ceiling-exceeded carrying findings; otherwise rebuild.
                if spec_wallclock_secs >= FOREMAN_SPEC_WALLCLOCK_CEILING:
                    park_detail = {
                        "accumulated_secs": spec_wallclock_secs,
                        "verify_verdict": verify_result.verdict,
                        "findings": verify_result.findings,
                        "failure_trail": failure_trail,
                    }
                    halts = queue.park(task, "spec-wallclock-ceiling-exceeded", park_detail)
                    self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={
                        "park_reason": "spec-wallclock-ceiling-exceeded",
                        "failure_trail": park_detail,
                    })
                    return {"status": "parked", "park_reason": "spec-wallclock-ceiling-exceeded",
                            "failure_trail": park_detail, "halts": halts}

                if attempt == 1:
                    # Second fail: park
                    halts = queue.park(task, "verify-failed-retry", failure_trail)
                    self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={"park_reason": "verify-failed-retry", "failure_trail": failure_trail})
                    return {"status": "parked", "park_reason": "verify-failed-retry", "failure_trail": failure_trail, "halts": halts}

                # First fail: retry -- claim is retained, no reclaim
                prior_findings = verify_result.findings
                preserved = _preserve_remote_branch(
                    cfg.working_dir, cfg.remote_url, branch, run_id, slug, attempt)
                if preserved:
                    self._emit(run_id, "branch_preserved", task_id=task_id, spec_slug=slug,
                               detail={"from": branch, "to": preserved, "path": "verify-retry"})
                # Write-ahead: back to building with incremented attempt_no (claim retained)
                queue.advance(task, TaskStatus.BUILDING, attempt_no=1)

        # Should not reach here
        halts = queue.park(task, "unexpected-loop-exit", {})
        self._emit(run_id, "parked", task_id=task_id, spec_slug=slug, detail={"park_reason": "unexpected-loop-exit", "failure_trail": {}})
        return {"status": "parked", "park_reason": "unexpected-loop-exit", "failure_trail": {}, "halts": halts}
