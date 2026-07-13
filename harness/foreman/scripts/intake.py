"""Spec approval gate for Phase 1 (single-spec intake).

resolve slug, proceed only if approved-for-build.
Multi-spec intake, ordering, and dependents are Phase 2.
"""
from __future__ import annotations

APPROVED_STATUSES = frozenset({
    "active",
    "approve-design",
    "approve-build",
    "approved",
    "approve-req",
    "approved-for-build",
    "council_cleared",
    "approve_design",
    "approve_build",
    "draft_active",
})


def is_approved(spec_row: dict) -> bool:
    """Return True if spec_row's status is in the approved-for-build set."""
    raw = (spec_row.get("status") or "").lower().strip()
    return raw in APPROVED_STATUSES
