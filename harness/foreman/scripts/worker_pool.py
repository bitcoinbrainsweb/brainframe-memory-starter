"""Re-export of the worker pool from the subagents system (single source of truth).

Foreman and the subagents system share one WorkerPool implementation; the real
module lives at harness/subagents/worker_pool.py. This thin module re-exports it
so intra-Foreman imports (``from .worker_pool import ...``) resolve without
duplicating the code. See harness/subagents/SETUP.md for the implementation.
"""
from __future__ import annotations

try:  # normal case: the repo root is importable as a namespace package
    from harness.subagents.worker_pool import *  # noqa: F401,F403
    from harness.subagents.worker_pool import MEMORY_DISCIPLINE_PREAMBLE  # noqa: F401
except ImportError:  # fallback: load the sibling module directly by path
    import importlib.util as _ilu
    import pathlib as _pl

    _src = _pl.Path(__file__).resolve().parents[2] / "subagents" / "worker_pool.py"
    _spec = _ilu.spec_from_file_location("_foreman_worker_pool", _src)
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("_")})
    MEMORY_DISCIPLINE_PREAMBLE = _mod.MEMORY_DISCIPLINE_PREAMBLE
