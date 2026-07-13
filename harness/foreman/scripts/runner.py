"""Phase 1 Foreman runner: single-spec build-verify-commit loop.

Implements the inference subset of the spec.
Write-ahead ledger: every DB transition precedes its action.
Ground-truth gate is fully real: uses git ls-remote + merge-base.

Phase 3 additions:
- CI gate (verifying -> ci-gating -> merging | parked)
- U-02: GPG-signed merge commit + DCO sign-off on base_ref
- R11: heartbeat thread (started on build, stopped on terminal transition)
"""
from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .ci_gate import CIGate, CIOutcome, park_reason_for_ci_outcome
from .git_ops import (
    GPGKeyUnavailableError,
    GitError,
    OriginCheckResult,
    checkout_branch,
    check_origin_ground_truth,
    confirm_git_state,
    create_branch,
    ff_merge_local,
    get_local_sha,
    gpg_signed_ff_merge,
    post_push_check,
    push_branch,
    push_ref,
)
from .heartbeat import ForemanHeartbeat
from .intake import is_approved
from .ledger import LedgerBackend
from .models import (
    BUILDER_MODEL,
    SESSION_ID,
    VERIFIER_MODEL,
    SameFamilyError,
    assert_different_family,
)
from .prompts import render_build_prompt, render_verify_prompt
from .transport import AgentTransport


def _run_id() -> str:
    now = datetime.now(timezone.utc)
    suffix = uuid.uuid4().hex[:6]
    return f"fm-{now.strftime('%Y%m%d-%H%M')}-{suffix}"


@dataclass
class RunConfig:
    spec_slug: str
    repo: str
    base_ref: str
    remote_url: str
    working_dir: Path
    builder_model: str = BUILDER_MODEL
    verifier_model: str = VERIFIER_MODEL
    session_id: str = SESSION_ID


@dataclass
class RunResult:
    status: str          # "committed" | "parked" | "excluded" | "not-found" | "error"
    run_id: str = ""
    commit_sha: str = ""
    park_reason: str = ""
    message: str = ""
    no_op: bool = False  # True when ALREADY_CONTAINS outcome (commit was already there)


class RunForeman:
    """Single-spec sequential build-verify-commit runner.

    Phase 1 scope:
    - Single spec (no bundle ordering, no dependents)
    - Build agent + cold verify agent from a different model family
    - R9 ground-truth gate (fully real git ls-remote + merge-base)
    - ff-only merge + post-push ref check
    - Write-ahead ledger throughout
    """

    def __init__(
        self,
        config: RunConfig,
        transport: AgentTransport,
        ledger: LedgerBackend,
    ) -> None:
        self.config = config
        self.transport = transport
        self.ledger = ledger

    def run(self) -> RunResult:
        cfg = self.config

        # --- Guard: model families must differ ---
        # Done before any DB write so a same-family error is a clean pre-run failure
        try:
            assert_different_family(cfg.builder_model, cfg.verifier_model)
        except SameFamilyError as exc:
            return RunResult(status="error", message=str(exc))

        # --- Intake: resolve spec and check approval ---
        spec = self.ledger.fetch_spec(cfg.spec_slug)
        if spec is None:
            return RunResult(
                status="not-found",
                message=f"Spec '{cfg.spec_slug}' not found in specs table",
            )
        if not is_approved(spec):
            return RunResult(
                status="excluded",
                message=(
                    f"Spec '{cfg.spec_slug}' is not approved for build "
                    f"(status={spec.get('status')!r})"
                ),
            )

        # Fetch spec body so render_build_prompt can inline it (avoids unauthenticated gh fetch in sandbox)
        comms_path = spec.get("comms_path") or f"specs/{cfg.spec_slug}.md"
        spec_body = self.ledger.fetch_spec_body(comms_path)
        if spec_body is not None:
            spec = {**spec, "body": spec_body}
        # If None: prompt will degrade gracefully (falls back to gh fetch instruction)

        run_id = _run_id()

        # --- Create run and spec ledger rows (queued) ---
        run_row = self.ledger.create_run(run_id, [cfg.spec_slug], cfg.session_id)
        run_uuid: str = run_row["id"]
        self.ledger.create_spec_row(run_uuid, run_id, spec, position=0)

        # --- Confirm git state before any git op ---
        confirm_git_state(cfg.working_dir, cfg.remote_url)

        # Get base SHA from local git (we treat base_ref as already fetched/checked-out)
        base_sha = get_local_sha(cfg.working_dir, f"refs/heads/{cfg.base_ref}")

        branch_name = f"build/{run_id}/{cfg.spec_slug}/0"

        # --- Write-ahead: building (precedes branch creation + build dispatch) ---
        self.ledger.transition(
            run_uuid, run_id, cfg.spec_slug,
            new_status="building",
            prior_status="queued",
            branch_name=branch_name,
            base_sha=base_sha,
            attempt=0,
            builder_model=cfg.builder_model,
            session_id=cfg.session_id,
        )

        # --- R11: start heartbeat thread after claim (building is the first non-queued state) ---
        heartbeat = ForemanHeartbeat()
        heartbeat.start(run_id, cfg.spec_slug, self.ledger)

        # --- Create feature branch and push to origin ---
        confirm_git_state(cfg.working_dir, cfg.remote_url)
        create_branch(cfg.working_dir, branch_name, base_sha)
        checkout_branch(cfg.working_dir, branch_name)
        push_branch(cfg.working_dir, cfg.remote_url, branch_name)

        # --- Dispatch build agent ---
        build_prompt_text = render_build_prompt(
            spec, run_id, branch_name, 0, cfg.repo
        )
        build_result = self.transport.build(build_prompt_text, cfg.builder_model)

        if build_result.commit_sha is None:
            heartbeat.stop()
            self.ledger.transition(
                run_uuid, run_id, cfg.spec_slug,
                new_status="parked",
                prior_status="building",
                park_reason="build-no-sha",
                session_id=cfg.session_id,
            )
            self.ledger.update_run_status(run_uuid, "failed")
            return RunResult(
                run_id=run_id,
                status="parked",
                park_reason="build-no-sha",
                message="Build agent returned no commit SHA",
            )

        # --- Write-ahead: verifying (precedes verify dispatch) ---
        self.ledger.transition(
            run_uuid, run_id, cfg.spec_slug,
            new_status="verifying",
            prior_status="building",
            commit_sha=build_result.commit_sha,
            verifier_model=cfg.verifier_model,
            session_id=cfg.session_id,
        )

        # --- Dispatch cold verify agent ---
        verify_prompt_text = render_verify_prompt(
            spec, run_id, branch_name, 0
        )
        verify_result = self.transport.verify(verify_prompt_text, cfg.verifier_model)

        self.ledger.update_data(run_uuid, cfg.spec_slug, {
            "verify_result": verify_result.verdict,
            "verifier_findings": {
                "verdict": verify_result.verdict,
                "findings": verify_result.findings,
                "attempt": 0,
            },
        })

        if verify_result.verdict != "PASS":
            heartbeat.stop()
            self.ledger.transition(
                run_uuid, run_id, cfg.spec_slug,
                new_status="parked",
                prior_status="verifying",
                verify_result=verify_result.verdict,
                verifier_findings={
                    "verdict": verify_result.verdict,
                    "findings": verify_result.findings,
                },
                park_reason="verify-failed",
                session_id=cfg.session_id,
            )
            self.ledger.update_run_status(run_uuid, "failed")
            return RunResult(
                run_id=run_id,
                status="parked",
                park_reason="verify-failed",
                message=f"Verify returned {verify_result.verdict}",
            )

        # --- R9: origin ground-truth gate (fully real) ---
        confirm_git_state(cfg.working_dir, cfg.remote_url)
        origin_outcome, confirmed_sha = check_origin_ground_truth(
            working_dir=cfg.working_dir,
            remote_url=cfg.remote_url,
            branch_name=branch_name,
            claimed_sha=build_result.commit_sha,
            base_sha=base_sha,
        )

        if origin_outcome == OriginCheckResult.PHANTOM_COMPLETION:
            heartbeat.stop()
            self.ledger.transition(
                run_uuid, run_id, cfg.spec_slug,
                new_status="parked",
                prior_status="verifying",
                park_reason="phantom-completion",
                session_id=cfg.session_id,
            )
            self.ledger.update_run_status(run_uuid, "failed")
            return RunResult(
                run_id=run_id,
                status="parked",
                park_reason="phantom-completion",
                message=(
                    "R9: claimed SHA not confirmed on origin at target ref -- "
                    "builder did not push or lied about SHA"
                ),
            )

        if origin_outcome == OriginCheckResult.ALREADY_CONTAINS:
            # Clean no-op: commit was already in history before the build ran
            heartbeat.stop()
            self.ledger.transition(
                run_uuid, run_id, cfg.spec_slug,
                new_status="committed",
                prior_status="verifying",
                commit_sha=confirmed_sha,
                session_id=cfg.session_id,
            )
            self.ledger.update_run_status(run_uuid, "completed")
            return RunResult(
                run_id=run_id,
                status="committed",
                commit_sha=confirmed_sha or "",
                message="R9: already-contains -- commit was already in origin history",
                no_op=True,
            )

        # CONFIRMED -- CI gate before merge
        # Write-ahead: ci-gating (precedes polling)
        self.ledger.transition(
            run_uuid, run_id, cfg.spec_slug,
            new_status="ci-gating",
            prior_status="verifying",
            session_id=cfg.session_id,
        )

        owner_repo = cfg.repo  # expected: "owner/repo"
        owner, _, repo_name = owner_repo.partition("/")
        if os.environ.get("FOREMAN_UNSAFE_INPROCESS") == "1":
            # In-process integration tests run against a local origin with a fake
            # owner/repo; there is no live GitHub Check-Runs API to poll. Treat as
            # no-CI (PASS_NO_CI) so the gate's write-ahead/verdict contract is
            # exercised without a network call. Production never sets this flag.
            ci_outcome = CIOutcome.PASS_NO_CI
            ci_verdict = {
                "ci_configured": False,
                "checks": [],
                "conclusion": "pass-no-ci",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        else:
            ci_gate = CIGate()
            ci_outcome, ci_verdict = ci_gate.poll(owner, repo_name, build_result.commit_sha)

        # Write ci_verdict before any merge transition (write-ahead)
        self.ledger.update_data(run_uuid, cfg.spec_slug, {"ci_verdict": ci_verdict})

        if ci_outcome not in (CIOutcome.PASS, CIOutcome.PASS_NO_CI):
            heartbeat.stop()
            park_reason = park_reason_for_ci_outcome(ci_outcome)
            self.ledger.transition(
                run_uuid, run_id, cfg.spec_slug,
                new_status="parked",
                prior_status="ci-gating",
                park_reason=park_reason,
                session_id=cfg.session_id,
            )
            self.ledger.update_run_status(run_uuid, "failed")
            return RunResult(
                run_id=run_id,
                status="parked",
                park_reason=park_reason,
                message=f"CI gate: {ci_outcome.value} (conclusion={ci_verdict.get('conclusion')})",
            )

        # CI passed -- proceed to merge
        # --- Write-ahead: merging (precedes the merge op) ---
        self.ledger.transition(
            run_uuid, run_id, cfg.spec_slug,
            new_status="merging",
            prior_status="ci-gating",
            session_id=cfg.session_id,
        )

        # --- Checkout base ref and merge feature branch (GPG-signed if key available) ---
        confirm_git_state(cfg.working_dir, cfg.remote_url)
        checkout_branch(cfg.working_dir, cfg.base_ref)

        gpg_key_id = os.environ.get("FOREMAN_GPG_KEY_ID", "")
        if gpg_key_id:
            # U-02: GPG-signed merge commit with DCO sign-off
            try:
                gpg_signed_ff_merge(cfg.working_dir, branch_name, gpg_key_id)
            except GPGKeyUnavailableError:
                heartbeat.stop()
                self.ledger.transition(
                    run_uuid, run_id, cfg.spec_slug,
                    new_status="parked",
                    prior_status="merging",
                    park_reason="gpg-key-unavailable",
                    session_id=cfg.session_id,
                )
                self.ledger.update_run_status(run_uuid, "failed")
                return RunResult(
                    run_id=run_id,
                    status="parked",
                    park_reason="gpg-key-unavailable",
                    message="GPG key unavailable or expired (U-02.AC3); set FOREMAN_GPG_KEY_ID in your secrets manager",
                )
            except GitError as exc:
                heartbeat.stop()
                self.ledger.transition(
                    run_uuid, run_id, cfg.spec_slug,
                    new_status="parked",
                    prior_status="merging",
                    park_reason="ff-merge-failed",
                    session_id=cfg.session_id,
                )
                self.ledger.update_run_status(run_uuid, "failed")
                return RunResult(
                    run_id=run_id,
                    status="parked",
                    park_reason="ff-merge-failed",
                    message=f"GPG-signed merge failed: {exc}",
                )
            ff_ok = True
        else:
            ff_ok = ff_merge_local(cfg.working_dir, branch_name)

        if not ff_ok:
            heartbeat.stop()
            self.ledger.transition(
                run_uuid, run_id, cfg.spec_slug,
                new_status="parked",
                prior_status="merging",
                park_reason="ff-merge-failed",
                session_id=cfg.session_id,
            )
            self.ledger.update_run_status(run_uuid, "failed")
            return RunResult(
                run_id=run_id,
                status="parked",
                park_reason="ff-merge-failed",
                message="Fast-forward merge failed (base ref advanced since branch creation)",
            )

        merged_sha = get_local_sha(cfg.working_dir, "HEAD")

        # --- Push to origin ---
        confirm_git_state(cfg.working_dir, cfg.remote_url, expected_branch=cfg.base_ref)
        push_ref(cfg.working_dir, cfg.remote_url, cfg.base_ref)

        # --- post-push ref-advanced check ---
        confirm_git_state(cfg.working_dir, cfg.remote_url)
        push_ok = post_push_check(
            working_dir=cfg.working_dir,
            remote_url=cfg.remote_url,
            base_ref=cfg.base_ref,
            merged_sha=merged_sha,
        )

        if not push_ok:
            heartbeat.stop()
            self.ledger.transition(
                run_uuid, run_id, cfg.spec_slug,
                new_status="parked",
                prior_status="merging",
                park_reason="post-push-ref-not-advanced",
                session_id=cfg.session_id,
            )
            self.ledger.update_run_status(run_uuid, "failed")
            return RunResult(
                run_id=run_id,
                status="parked",
                park_reason="post-push-ref-not-advanced",
                message="remote ref did not advance after push (local-only push)",
            )

        # --- Write-ahead: committed (precedes nothing -- this is terminal) ---
        heartbeat.stop()
        self.ledger.transition(
            run_uuid, run_id, cfg.spec_slug,
            new_status="committed",
            prior_status="merging",
            commit_sha=merged_sha,
            session_id=cfg.session_id,
        )
        self.ledger.update_run_status(run_uuid, "completed")

        return RunResult(
            run_id=run_id,
            status="committed",
            commit_sha=merged_sha,
            message=f"Committed {merged_sha[:12]} to {cfg.base_ref}",
        )
