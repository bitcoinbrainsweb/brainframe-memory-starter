"""Bounded-concurrency worker pool with a host-memory-budget cap.

Concurrency is not a fixed number. It is derived at run start from the host's
memory budget:

  cap = floor(HOST_MEMORY_BUDGET_MB / SUBAGENT_EST_FOOTPRINT_MB)
        clamped to [1, SUBAGENT_MAX_CONCURRENCY]

A new sub-worker is admitted only after a live worker exits (a semaphore, not
fixed-size batches). The cap is never a bare integer literal in the dispatch loop:
it is always derived from compute_cap() via _resolve_cap(), which reads live env at
run start so the cap reflects current host state, not a stale snapshot.
"""
from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable


# ---------------------------------------------------------------------------
# Memory-discipline preamble
#
# Injected into a sub-worker's prompt at the fan-out boundary, so every sub-worker
# is told the same three rules that keep a wide fan-out from exhausting host memory.
# ---------------------------------------------------------------------------

MEMORY_DISCIPLINE_PREAMBLE = """\
## Memory-Discipline Preamble

This task involves fan-out or bulk-data operations. The following constraints apply:

Rule 1: Host-budgeted concurrency cap.
Concurrency is derived at run start from the host memory budget:
  cap = floor(HOST_MEMORY_BUDGET_MB / SUBAGENT_EST_FOOTPRINT_MB)
  clamped to [1, SUBAGENT_MAX_CONCURRENCY]
Never assume a fixed concurrency limit. A new sub-worker is admitted only after a
live worker exits: the pool uses bounded live concurrency, not fixed-size groups.

Rule 2: Low-memory source default.
Prefer structured or streaming sources over bulk-document fetch:
- prefer a structured data API over a full HTML document,
- prefer ranged reads over whole-file fetches,
- prefer streaming parse over load-then-parse.
Free each document buffer before fetching the next. Bulk-document fetch is a
justified fallback: record the justification in your output if you choose it.

Rule 3: Shard-to-disk before worker exit.
Write your result to the run-scoped shard path before returning. The parent
assembles results from landed shards after each wave, and never holds all results
in memory simultaneously.\
"""


# ---------------------------------------------------------------------------
# Validation and cap computation
# ---------------------------------------------------------------------------

class MemoryBudgetConfigError(ValueError):
    """Raised when memory budget configuration is missing, zero, or non-positive."""


def compute_cap(budget_mb: float, footprint_mb: float, max_concurrency: int) -> int:
    """Derive the concurrency cap: floor(budget_mb / footprint_mb), clamped to [1, max_concurrency].

    Raises MemoryBudgetConfigError on:
    - budget_mb <= 0 or None
    - footprint_mb <= 0 or None
    - max_concurrency < 1

    When floor(budget_mb / footprint_mb) == 0, clamps to 1 (one worker always runs).
    Never divides by zero (footprint_mb is validated before the division).
    """
    if budget_mb is None or budget_mb <= 0:
        raise MemoryBudgetConfigError(
            f"HOST_MEMORY_BUDGET_MB must be present and > 0, got {budget_mb!r}"
        )
    if footprint_mb is None or footprint_mb <= 0:
        raise MemoryBudgetConfigError(
            f"SUBAGENT_EST_FOOTPRINT_MB must be present and > 0, got {footprint_mb!r}"
        )
    if max_concurrency < 1:
        raise MemoryBudgetConfigError(
            f"SUBAGENT_MAX_CONCURRENCY must be >= 1, got {max_concurrency!r}"
        )
    raw = math.floor(budget_mb / footprint_mb)
    return max(1, min(raw, max_concurrency))


# ---------------------------------------------------------------------------
# Audit record
# ---------------------------------------------------------------------------

@dataclass
class PoolRunRecord:
    """Audit record written to the pool run-ledger per execution.

    Recorded before dispatch so the cap, budget, and footprint are auditable
    after the run.
    """
    run_id: str
    cap: int
    budget_mb: float
    footprint_mb: float
    max_concurrency: int
    item_count: int


# ---------------------------------------------------------------------------
# Bounded-concurrency pool
# ---------------------------------------------------------------------------

class WorkerPool:
    """Bounded-concurrency worker pool.

    The cap is resolved from live env at each call to run(), never cached from a
    prior run. A new worker is admitted only after a live worker exits
    (threading.Semaphore, not batches). At no instant are more than cap workers
    executing worker_fn simultaneously. Audit records are appended to run_ledger
    after each run() call.
    """

    def __init__(self) -> None:
        self._run_ledger: list[PoolRunRecord] = []

    def _resolve_cap(self) -> tuple[int, float, float, int]:
        """Read live env, compute the cap. Raises MemoryBudgetConfigError on bad config.

        Called at run start so the cap reflects current host state, never a prior snapshot.
        """
        budget_key = "HOST_MEMORY_BUDGET_MB"
        footprint_key = "SUBAGENT_EST_FOOTPRINT_MB"
        concurrency_key = "SUBAGENT_MAX_CONCURRENCY"

        if budget_key not in os.environ:
            raise MemoryBudgetConfigError(f"{budget_key} is not set")
        if footprint_key not in os.environ:
            raise MemoryBudgetConfigError(f"{footprint_key} is not set")
        if concurrency_key not in os.environ:
            raise MemoryBudgetConfigError(f"{concurrency_key} is not set")

        try:
            budget_mb = float(os.environ[budget_key])
        except ValueError as exc:
            raise MemoryBudgetConfigError(f"{budget_key} is not a valid number") from exc

        try:
            footprint_mb = float(os.environ[footprint_key])
        except ValueError as exc:
            raise MemoryBudgetConfigError(f"{footprint_key} is not a valid number") from exc

        try:
            max_concurrency = int(os.environ[concurrency_key])
        except ValueError as exc:
            raise MemoryBudgetConfigError(f"{concurrency_key} is not a valid integer") from exc

        cap = compute_cap(budget_mb, footprint_mb, max_concurrency)
        return cap, budget_mb, footprint_mb, max_concurrency

    def run(
        self,
        run_id: str,
        items: Iterable[Any],
        worker_fn: Callable[[Any], Any],
    ) -> list[Any]:
        """Execute worker_fn(item) for each item with bounded live concurrency.

        The cap is resolved from live env at call time, never hardcoded. A new
        worker is admitted only after a live worker exits. At no instant are more
        than cap workers live. All items are processed before return (wave drain).

        Worker exceptions are re-raised after all threads complete.
        """
        cap, budget_mb, footprint_mb, max_concurrency = self._resolve_cap()

        item_list = list(items)
        self._run_ledger.append(PoolRunRecord(
            run_id=run_id,
            cap=cap,
            budget_mb=budget_mb,
            footprint_mb=footprint_mb,
            max_concurrency=max_concurrency,
            item_count=len(item_list),
        ))

        results: list[Any] = [None] * len(item_list)
        errors: list[BaseException | None] = [None] * len(item_list)
        semaphore = threading.Semaphore(cap)
        threads: list[threading.Thread] = []

        def _run_item(index: int, item: Any) -> None:
            # Block until a slot is free (admit a new worker after a live one exits).
            semaphore.acquire()
            try:
                results[index] = worker_fn(item)
            except BaseException as exc:
                errors[index] = exc
            finally:
                # Release unconditionally so the semaphore count is always restored.
                semaphore.release()

        for i, item in enumerate(item_list):
            t = threading.Thread(target=_run_item, args=(i, item), daemon=True)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        for err in errors:
            if err is not None:
                raise err

        return results

    @property
    def run_ledger(self) -> list[PoolRunRecord]:
        """Read-only view of per-run audit records (cap, budget, footprint)."""
        return list(self._run_ledger)
