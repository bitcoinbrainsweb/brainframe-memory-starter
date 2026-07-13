"""RunReport: consolidated per-run disposition for Phase 2."""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import ExclusionRecord, HaltChain, HaltRecord, ParkedRecord


@dataclass
class RunReport:
    run_id: str
    total_wall_s: float
    committed: list[str] = field(default_factory=list)
    parked: list[ParkedRecord] = field(default_factory=list)
    dependent_halted: list[HaltRecord] = field(default_factory=list)
    excluded: list[ExclusionRecord] = field(default_factory=list)
    dependent_halt_chains: list[HaltChain] = field(default_factory=list)
    # spec slugs that were appended to a live run (flagged appended=true).
    appended: list[str] = field(default_factory=list)
    # operational one-shot notes (e.g. throttled heartbeat failures).
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"Run: {self.run_id}  |  Wall: {self.total_wall_s:.1f}s",
            "",
            f"Committed ({len(self.committed)}): {', '.join(self.committed) or 'none'}",
        ]
        if self.parked:
            lines.append(f"Parked ({len(self.parked)}):")
            for p in self.parked:
                lines.append(f"  {p.spec_slug}: {p.park_reason}")
        if self.dependent_halted:
            lines.append(f"Dependent-halted ({len(self.dependent_halted)}):")
            for h in self.dependent_halted:
                lines.append(f"  {h.spec_slug} (because {h.halted_because} parked)")
        if self.excluded:
            lines.append(f"Excluded ({len(self.excluded)}):")
            for e in self.excluded:
                lines.append(f"  {e.spec_slug}: {e.reason}")
        if self.appended:
            lines.append(f"Appended ({len(self.appended)}): {', '.join(self.appended)}")
        if self.dependent_halt_chains:
            lines.append("Halt chains:")
            for chain in self.dependent_halt_chains:
                lines.append(f"  {chain.parked_slug} -> {chain.halted_slugs}")
        if self.notes:
            lines.append("Notes:")
            for n in self.notes:
                lines.append(f"  {n}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "total_wall_s": self.total_wall_s,
            "committed": self.committed,
            "parked": [
                {"spec_slug": p.spec_slug, "park_reason": p.park_reason, "failure_trail": p.failure_trail}
                for p in self.parked
            ],
            "dependent_halted": [
                {"spec_slug": h.spec_slug, "halted_because": h.halted_because}
                for h in self.dependent_halted
            ],
            "excluded": [
                {"spec_slug": e.spec_slug, "reason": e.reason}
                for e in self.excluded
            ],
            "dependent_halt_chains": [
                {"parked_slug": c.parked_slug, "halted_slugs": c.halted_slugs}
                for c in self.dependent_halt_chains
            ],
            "appended": list(self.appended),
            "notes": list(self.notes),
        }
