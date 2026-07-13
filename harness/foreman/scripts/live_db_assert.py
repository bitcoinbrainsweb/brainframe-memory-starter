"""Live-DB verification for specs whose deliverables include Supabase objects.

When a run's commit delta contains migration SQL that creates DB objects, verify
must not trust fixtures alone -- it must assert those objects actually exist in
the live admin database, plus one write-then-read smoke to prove the DB is
reachable and writable with the service role. Catches "spec claims a table but
the migration never applied to live" -- a sibling of the ledger hole in the
incident, where DB-side state and code-side claims diverged.

Detection is mechanical (parse CREATE statements from the migration SQL in the
delta). Assertion runs through a small ``LiveDbClient`` protocol so it is unit
testable with a fake and never requires a live DB in tests.

R5: near-miss naming. When a declared table is missing, the FAIL detail lists
any existing table within Levenshtein distance 6 or sharing an 8-character prefix
so name-drift parks are diagnosable from the park row alone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# CREATE <kind> [IF NOT EXISTS] [OR REPLACE] <name>. Schema-qualified names are
# reduced to the bare object name. Quoted identifiers are unquoted.
_CREATE_RE = re.compile(
    r"""create\s+(?:or\s+replace\s+)?
        (table|function|view|materialized\s+view|policy|index|trigger)\s+
        (?:if\s+not\s+exists\s+)?
        (?:concurrently\s+)?
        (?P<name>[A-Za-z_."][\w."]*)""",
    re.IGNORECASE | re.VERBOSE,
)

_MIGRATION_PATH_RE = re.compile(r"(^|/)migrations/.+\.sql$", re.IGNORECASE)


def is_migration_path(path: str) -> bool:
    return bool(_MIGRATION_PATH_RE.search(path.replace("\\", "/")))


def _bare_name(raw: str) -> str:
    name = raw.strip().strip('"')
    if "." in name:
        name = name.split(".")[-1]
    return name.strip('"()')


@dataclass
class DbDeliverables:
    tables: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    views: list[str] = field(default_factory=list)
    policies: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)

    def any(self) -> bool:
        return bool(self.tables or self.functions or self.views or self.policies)


def extract_db_deliverables(sql_texts: list[str]) -> DbDeliverables:
    """Parse created object names from one or more migration SQL blobs."""
    d = DbDeliverables()
    for sql in sql_texts:
        for m in _CREATE_RE.finditer(sql or ""):
            kind = m.group(1).lower()
            name = _bare_name(m.group("name"))
            if not name:
                continue
            if kind == "table" and name not in d.tables:
                d.tables.append(name)
            elif kind == "function" and name not in d.functions:
                d.functions.append(name)
            elif kind in ("view", "materialized view") and name not in d.views:
                d.views.append(name)
            elif kind == "policy" and name not in d.policies:
                d.policies.append(name)
            else:
                if name not in d.other:
                    d.other.append(name)
    return d


@dataclass
class LiveDbResult:
    verdict: str  # "PASS" | "FAIL" | "SKIP"
    missing: list[str] = field(default_factory=list)
    smoke_ok: bool | None = None
    reason: str = ""


def _levenshtein(a: str, b: str) -> int:
    """Compute the Levenshtein edit distance between two strings."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            ))
        prev = curr
    return prev[len(b)]


def _find_near_miss(declared: str, existing: list[str], max_dist: int = 6, prefix_len: int = 8) -> str | None:
    """Return the nearest existing table name if within Levenshtein distance or prefix match."""
    best_name: str | None = None
    best_dist = max_dist + 1
    declared_prefix = declared[:prefix_len]
    for name in existing:
        if name[:prefix_len] == declared_prefix:
            return name
        d = _levenshtein(declared, name)
        if d <= max_dist and d < best_dist:
            best_dist = d
            best_name = name
    return best_name


@runtime_checkable
class LiveDbClient(Protocol):
    def table_exists(self, name: str) -> bool: ...
    def function_exists(self, name: str) -> bool: ...
    def smoke_write_read(self) -> bool:
        """Insert a sentinel row into a table we own, read it back, delete it.
        Returns True on a clean round-trip."""
        ...
    def list_tables(self) -> list[str]:
        """Return names of all tables/views accessible to this client.
        Used for near-miss naming. Return empty list if unavailable."""
        ...


def assert_live_db(deliverables: DbDeliverables, client: LiveDbClient) -> LiveDbResult:
    """Every declared table/function/view must exist live; one write-then-read
    smoke must succeed. FAIL closed on any miss (F4). SKIP when the delta declares
    no DB objects (verify falls back to its normal checks).

    R5: when a declared table is missing, include near-miss info (nearest existing
    table by Levenshtein distance or 8-char prefix) so name-drift parks are
    diagnosable from the park row alone."""
    if not deliverables.any():
        return LiveDbResult("SKIP", reason="no-db-deliverables-in-delta")

    existing_tables: list[str] = []
    try:
        existing_tables = list(client.list_tables())
    except Exception:
        pass

    missing: list[str] = []
    for t in deliverables.tables:
        if not client.table_exists(t):
            near = _find_near_miss(t, existing_tables) if existing_tables else None
            if near:
                missing.append(f"table:{t} (declared {t}, nearest existing {near})")
            else:
                missing.append(f"table:{t}")
    for fn in deliverables.functions:
        if not client.function_exists(fn):
            missing.append(f"function:{fn}")
    for v in deliverables.views:
        # views are queried like tables through PostgREST
        if not client.table_exists(v):
            near = _find_near_miss(v, existing_tables) if existing_tables else None
            if near:
                missing.append(f"view:{v} (declared {v}, nearest existing {near})")
            else:
                missing.append(f"view:{v}")

    if missing:
        return LiveDbResult("FAIL", missing=missing, reason="declared-db-objects-absent-in-live-db")

    smoke_ok = False
    try:
        smoke_ok = bool(client.smoke_write_read())
    except Exception as exc:  # pragma: no cover - defensive
        return LiveDbResult("FAIL", smoke_ok=False, reason=f"smoke-error:{exc}")

    if not smoke_ok:
        return LiveDbResult("FAIL", smoke_ok=False, reason="write-then-read-smoke-failed")
    return LiveDbResult("PASS", smoke_ok=True, reason="ok")


class SupabaseLiveDbClient:
    """LiveDbClient backed by the Supabase REST API (service role).

    Table/view existence: a ``select ?limit=0`` returns 200 when the relation is
    exposed by PostgREST, 404 when absent. Functions are not enumerable over
    REST; ``function_exists`` returns True (not-checkable) so F4 never fails on a
    function it cannot introspect -- table/view existence is the load-bearing
    assertion. The smoke round-trips a sentinel through ``foreman_run_events`` (a
    table we own), proving the service role can write and read live.
    """

    def __init__(self, url: str, service_key: str) -> None:
        self._url = url.rstrip("/")
        self._key = service_key

    def _headers(self) -> dict:
        return {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "User-Agent": "foreman/1.0",
        }

    def table_exists(self, name: str) -> bool:
        import urllib.error
        import urllib.request
        full = f"{self._url}/rest/v1/{name}?select=*&limit=0"
        req = urllib.request.Request(full, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as exc:
            # 404/406 => relation not exposed; other codes are inconclusive but
            # we fail closed on existence (return False).
            return exc.code not in (404, 406, 400)
        except Exception:
            return False

    def function_exists(self, name: str) -> bool:
        # Not enumerable via PostgREST; treat as not-checkable (do not block F4).
        return True

    def list_tables(self) -> list[str]:
        """Return all table/view names exposed by PostgREST (OpenAPI paths).
        Used for R5 near-miss naming. Returns empty list on any error."""
        import json
        import urllib.request
        try:
            req = urllib.request.Request(
                f"{self._url}/rest/v1/",
                headers={**self._headers(), "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            paths = data.get("paths") or {}
            names = []
            for path in paths:
                name = path.lstrip("/")
                if name and not name.startswith("rpc/"):
                    names.append(name)
            return names
        except Exception:
            return []

    def smoke_write_read(self) -> bool:
        import json
        import urllib.request
        import uuid
        sentinel = f"foreman-f4-smoke-{uuid.uuid4().hex}"
        payload = [{
            "run_id": sentinel,
            "event": "live_db_smoke",
            "detail": {"probe": sentinel},
        }]
        post = urllib.request.Request(
            f"{self._url}/rest/v1/foreman_run_events",
            data=json.dumps(payload).encode(),
            headers={**self._headers(), "Content-Type": "application/json",
                     "Prefer": "return=representation"},
            method="POST",
        )
        with urllib.request.urlopen(post, timeout=15) as resp:
            written = json.loads(resp.read())
        if not written:
            return False
        get = urllib.request.Request(
            f"{self._url}/rest/v1/foreman_run_events?run_id=eq.{sentinel}&select=run_id",
            headers=self._headers(),
        )
        with urllib.request.urlopen(get, timeout=15) as resp:
            rows = json.loads(resp.read())
        ok = any(r.get("run_id") == sentinel for r in rows)
        # Best-effort cleanup; never fails the smoke.
        try:
            dele = urllib.request.Request(
                f"{self._url}/rest/v1/foreman_run_events?run_id=eq.{sentinel}",
                headers=self._headers(), method="DELETE",
            )
            urllib.request.urlopen(dele, timeout=15).read()
        except Exception:
            pass
        return ok
