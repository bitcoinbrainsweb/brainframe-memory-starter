"""Structured output repair layer -- admin-structured-output-repair-v1.

One registry unifies validation of every model-produced JSON/verdict surface
(foreman_verdict, minor_fix_payload, findings_array, rationale_block). Each
consumer validates its model output against the registered schema BEFORE any
downstream branch executes.

Design constraint: this layer UNIFIES, it does not replace. ``_extract_verdict``
(transport.py) remains the authoritative verdict parser; it is registered here as
the foreman_verdict handler via :func:`register_verdict_handler`, with no change to
the parser's behavior. The verdict retry/park loop stays in
``bundle_runner._dispatch_verify`` -- this layer never double-retries verdicts.

Stdlib only (json + dataclasses); no new dependencies. This module is a leaf: it
imports nothing from the foreman package so no consumer can create an import cycle.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

_logger = logging.getLogger("foreman.output_schema")

# ---------------------------------------------------------------------------
# Status constants (returned as the second element of every validate* call)
# ---------------------------------------------------------------------------

VALID = "valid"            # parsed and schema-conformant
REPAIRED = "repaired"      # invalid on first parse, conformant after one repair
FALLBACK = "fallback"      # invalid after repair (or with no repair offered)
UNREGISTERED = "unregistered"  # no registry entry: passed through unvalidated

# Deterministic fallback sentinels per output type. Verdict paths park MALFORMED
# per existing Foreman convention; non-verdict payloads carry a FAILED sentinel.
FALLBACK_MINOR_FIX: dict[str, Any] = {"files": None, "commit_message": None}
FALLBACK_FINDINGS: list[dict[str, Any]] = [
    {"error": "structured-output-repair: unparseable findings", "verdict": "FAILED"}
]
FALLBACK_VERDICT: dict[str, str] = {"verdict": "MALFORMED", "findings": ""}
FALLBACK_RATIONALE: dict[str, str] = {
    "decision_rationale": "NONE_PROVIDED",
    "issues_encountered": "NONE_PROVIDED",
    "lessons": "NONE_PROVIDED",
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class SchemaEntry:
    output_type: str
    # validator: raw str -> (parsed, ok). MUST NOT raise; return (None, False) on any error.
    validator: Callable[[str], tuple[Any, bool]]
    fallback: Any
    description: str = ""


_REGISTRY: dict[str, SchemaEntry] = {}


def register(
    output_type: str,
    validator: Callable[[str], tuple[Any, bool]],
    fallback: Any,
    description: str = "",
) -> None:
    """Register (or overwrite) the handler for an output type."""
    _REGISTRY[output_type] = SchemaEntry(output_type, validator, fallback, description)


def is_registered(output_type: str) -> bool:
    return output_type in _REGISTRY


def registered_types() -> list[str]:
    return sorted(_REGISTRY)


# ---------------------------------------------------------------------------
# Lenient JSON extraction helpers (a JSON object/array embedded in prose)
# ---------------------------------------------------------------------------


def _loads_object(raw: str) -> dict | None:
    raw = (raw or "").strip()
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _loads_array(raw: str) -> list | None:
    raw = (raw or "").strip()
    try:
        arr = json.loads(raw)
        return arr if isinstance(arr, list) else None
    except (ValueError, TypeError):
        pass
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if m:
        try:
            arr = json.loads(m.group(0))
            return arr if isinstance(arr, list) else None
        except (ValueError, TypeError):
            return None
    return None


# ---------------------------------------------------------------------------
# Built-in validators
# ---------------------------------------------------------------------------


def _validate_minor_fix(raw: str) -> tuple[Any, bool]:
    """MINOR-FIX payload: {"files": [{"path": str, "content": str}, ...],
    "commit_message"?: str}. At least one file required."""
    obj = _loads_object(raw)
    if obj is None:
        return None, False
    files = obj.get("files")
    if not isinstance(files, list) or not files:
        return None, False
    for f in files:
        if not isinstance(f, dict):
            return None, False
        if not isinstance(f.get("path"), str) or not isinstance(f.get("content"), str):
            return None, False
    cm = obj.get("commit_message")
    if cm is not None and not isinstance(cm, str):
        return None, False
    return (
        {"files": files, "commit_message": obj.get("commit_message", "fix: automated MINOR-FIX")},
        True,
    )


def _validate_findings(raw: str) -> tuple[Any, bool]:
    """Findings array: list of finding objects. An empty list ('no findings') is
    valid and well-formed. Each element must be an object."""
    arr = _loads_array(raw)
    if arr is None:
        return None, False
    for f in arr:
        if not isinstance(f, dict):
            return None, False
    return arr, True


def _validate_rationale(raw: str) -> tuple[Any, bool]:
    """RATIONALE block (prompts.py): a JSON object with EXACTLY the three string keys
    decision_rationale / issues_encountered / lessons."""
    obj = _loads_object(raw)
    if obj is None:
        return None, False
    required = {"decision_rationale", "issues_encountered", "lessons"}
    if set(obj.keys()) != required:
        return None, False
    if not all(isinstance(obj[k], str) for k in required):
        return None, False
    return obj, True


# minor_fix_payload, findings_array, rationale_block registered at import time.
# foreman_verdict is registered by transport.py via register_verdict_handler so
# _extract_verdict stays the single source of truth for verdicts.
register("minor_fix_payload", _validate_minor_fix, FALLBACK_MINOR_FIX,
         "Autonomy MINOR-FIX file payload")
register("findings_array", _validate_findings, FALLBACK_FINDINGS,
         "Adversarial findings array")
register("rationale_block", _validate_rationale, FALLBACK_RATIONALE,
         "Three-string-key foreman-rationale receipt block (prompts.py)")


def register_verdict_handler(extract_verdict: Callable[[str], tuple[str, str]]) -> None:
    """Register transport._extract_verdict as the foreman_verdict handler.

    The parser is wrapped, not modified: a verdict of MALFORMED means not-ok so the
    layer surfaces the same three-class judgment. No retry happens here -- the
    verdict retry/park loop stays in bundle_runner._dispatch_verify (do not
    double-retry)."""

    def _validator(raw: str) -> tuple[Any, bool]:
        try:
            verdict, findings = extract_verdict(raw)
        except Exception:  # a parser fault is unresolvable output, not a real FAIL
            return None, False
        return {"verdict": verdict, "findings": findings}, verdict != "MALFORMED"

    register("foreman_verdict", _validator, FALLBACK_VERDICT,
             "Adversarial verify verdict (transport._extract_verdict)")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate(output_type: str, raw: str) -> tuple[Any, str]:
    """Validate raw against the registered schema for output_type.

    Returns (parsed, status):
      - registered + conformant  -> (parsed, VALID)
      - registered + malformed   -> (fallback_sentinel, FALLBACK)
      - unregistered             -> (raw, UNREGISTERED) and a coverage gap is logged

    No LLM call is ever made here (single local parse). Never raises.
    """
    entry = _REGISTRY.get(output_type)
    if entry is None:
        _logger.warning(
            "output_schema coverage gap: unregistered output_type %r passed through "
            "unvalidated", output_type,
        )
        return raw, UNREGISTERED
    parsed, ok = entry.validator(raw)
    if ok:
        return parsed, VALID
    return entry.fallback, FALLBACK


def build_repair_prompt(output_type: str, raw: str) -> str:
    """The one repair re-prompt: hand the model the schema and its malformed output
    and ask for a corrected version. Lives here so no prompt content leaks into the
    consumers (constraint: no prompt-content changes except this template)."""
    entry = _REGISTRY.get(output_type)
    schema_desc = entry.description if entry else output_type
    return (
        "Your previous response was not valid for the required output type "
        f"'{output_type}' ({schema_desc}).\n\n"
        "Re-emit ONLY the corrected output as strict JSON, with no prose before or "
        "after it. It must satisfy the schema for this output type.\n\n"
        "Your previous (malformed) output was:\n"
        "<<<BEGIN>>>\n"
        f"{raw}\n"
        "<<<END>>>\n"
    )


def validate_or_repair(
    output_type: str,
    raw: str,
    repair_fn: Callable[[str], str],
    on_fallback: Callable[[dict], None] | None = None,
) -> tuple[Any, str]:
    """Validate; on failure issue EXACTLY ONE repair re-prompt, then fall back.

    - valid on first parse          -> (parsed, VALID); repair_fn never called
    - invalid, valid after repair   -> (parsed, REPAIRED); repair_fn called once
    - invalid after repair          -> (fallback_sentinel, FALLBACK); on_fallback
                                       fired with both raw outputs
    - unregistered                  -> (raw, UNREGISTERED); repair_fn never called

    repair_fn receives the fully-built repair prompt (schema + malformed output) and
    returns the model's raw response. on_fallback records the double failure with
    both raw outputs, matching the caller's event-write convention. Never raises.
    """
    entry = _REGISTRY.get(output_type)
    if entry is None:
        _logger.warning(
            "output_schema coverage gap: unregistered output_type %r passed through "
            "unvalidated", output_type,
        )
        return raw, UNREGISTERED

    parsed, ok = entry.validator(raw)
    if ok:
        return parsed, VALID

    # Exactly one repair re-prompt.
    repaired_raw: str
    try:
        repaired_raw = repair_fn(build_repair_prompt(output_type, raw))
    except Exception as exc:
        _logger.error("output_schema repair call failed for %s: %s", output_type, exc)
        repaired_raw = ""

    parsed2, ok2 = entry.validator(repaired_raw)
    if ok2:
        return parsed2, REPAIRED

    if on_fallback is not None:
        try:
            on_fallback({
                "output_type": output_type,
                "status": FALLBACK,
                "raw": raw,
                "repaired_raw": repaired_raw,
            })
        except Exception as exc:  # event write must never break the consumer
            _logger.error("output_schema on_fallback hook failed: %s", exc)
    else:
        _logger.error(
            "output_schema double-failure for %s (no on_fallback recorder supplied)",
            output_type,
        )
    return entry.fallback, FALLBACK
