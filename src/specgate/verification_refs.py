"""A stored verification keeps claims, verdicts, source refs and hashes, never research text."""

import json
import re
from collections.abc import Iterable, Mapping
from hashlib import sha256
from typing import Any

from specgate.grill_contracts import GrillError

REF_KEYS = ("id", "source", "revision")
KINDS = ("fidelity", "coverage")
# What a jev_verify result and each of its verdicts carry; the rest is client text.
RESULT_KEYS = (
    "tool",
    "mode",
    "action",
    "reason",
    "auto_advance",
    "calibrated",
    "gate",
    "summary",
    "provider_calls",
    "context_revision",
    "request_revision",
    "error",
)
VERDICT_KEYS = ("id", "verdict", "confidence", "probabilities", "would_auto_accept")
_HASH = re.compile(r"[a-f0-9]{64}")


def digest(text: str) -> str:
    return sha256(text.encode()).hexdigest()


def context_digest(context: Mapping[str, Any]) -> str:
    """The same hash as `ContextPacket.revision` for the packet that was sent."""
    return digest(json.dumps(context, ensure_ascii=False, sort_keys=True))


def source_refs(sources: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    return [{key: item[key] for key in REF_KEYS} for item in sources]


def checked_refs(sources: Any) -> list[dict[str, str]]:
    rows = sources if isinstance(sources, list) else None
    if rows is None or any(
        not isinstance(row, dict)
        or any(not isinstance(row.get(key), str) for key in REF_KEYS)
        for row in rows
    ):
        raise GrillError(
            "invalid_verification",
            "Informe cada fonte da verificação com id, origem e revisão.",
        )
    return source_refs(rows)


def _result(value: dict[str, Any]) -> dict[str, Any]:
    kept = {key: value[key] for key in RESULT_KEYS if key in value}
    if isinstance(value.get("verdicts"), list):
        kept["verdicts"] = [
            {key: row[key] for key in VERDICT_KEYS if key in row}
            for row in value["verdicts"]
            if isinstance(row, dict)
        ]
    return kept


def valid_hash(value: Any) -> str | None:
    """The sha256 a stored verification names, or None: nothing is reused without one."""
    return value if isinstance(value, str) and _HASH.fullmatch(value) else None


def _hashes(value: Any) -> dict[str, str]:
    # A malformed hash is dropped: without it no verdict can be reused.
    if not isinstance(value, dict):
        return {}
    return {kind: value[kind] for kind in KINDS if valid_hash(value.get(kind))}


def compact_spec_verification(verification: dict[str, Any]) -> dict[str, Any]:
    """Drop context, evidence, history and source text before a spec stores it."""
    if not verification:
        return verification
    stored: dict[str, Any] = {}
    if "claims" in verification:
        stored["claims"] = verification["claims"]
    for kind in KINDS:
        if isinstance(verification.get(kind), dict):
            stored[kind] = _result(verification[kind])
    if "sources" in verification:
        stored["sources"] = checked_refs(verification["sources"])
    for name in ("context_revisions", "evidence_revisions"):
        if hashes := _hashes(verification.get(name)):
            stored[name] = hashes
    return stored


def evidence_revision(verification: dict[str, Any]) -> str | None:
    """The hash of the planning evidence a graph verification judged.

    A graph stored before the hashes kept the evidence text itself.
    """
    stored = verification.get("evidence_revision")
    if stored is None and isinstance(verification.get("evidence"), str):
        return digest(verification["evidence"])
    return valid_hash(stored)


def _results(value: Any) -> list[dict[str, Any]]:
    """Each result by its keys; what the host cannot read stays unreadable.

    An entry that is not a result, or a verdict row that is not an object, is
    dropped and an empty entry takes its place: the host reads a list holding it
    as malformed, as it did before the text was dropped.
    """
    rows = value if isinstance(value, list) else []
    kept = [_result(row) for row in rows if isinstance(row, dict)]
    readable = isinstance(value, list) and all(
        isinstance(row, dict)
        and (
            not isinstance(row.get("verdicts"), list)
            or all(isinstance(item, dict) for item in row["verdicts"])
        )
        for row in rows
    )
    return kept if readable else [*kept, {}]


def compact_ticket_verification(verification: dict[str, Any]) -> dict[str, Any]:
    """Drop context, evidence, history and source text before a graph stores it."""
    if not verification:
        return verification
    stored: dict[str, Any] = {}
    if "claims" in verification:
        stored["claims"] = verification["claims"]
    for name in ("results", "confirmations"):
        if name in verification:
            stored[name] = _results(verification[name])
    if "sources" in verification:
        stored["sources"] = checked_refs(verification["sources"])
    for name in ("evidence_revision", "context_revision"):
        if found := valid_hash(verification.get(name)):
            stored[name] = found
    return stored
