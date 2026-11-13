"""A stored verification keeps claims, verdicts, source refs and hashes, never research text."""

import json
import re
from collections.abc import Iterable, Mapping
from hashlib import sha256
from typing import Any

from specgate.slices.equivalence import MAX_PARTS
from specgate.wording import AgentRefusal

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
# What a stored wording check keeps: the shared record, the authority that judged and, for
# each text, the digest of the text it checked and what the host needs to read it again.
AUTHORITY_KEYS = (
    "policy",
    "recipe",
    "backend",
    "provider",
    "requested_model",
    "resolved_model",
)
_HASH = re.compile(r"[a-f0-9]{64}")
# A division has at most as many parts as the equivalence tool takes.
PART_LIMIT = MAX_PARTS
INVALID_VERIFICATION = "invalid_verification"
# The top-level keys the host reads: those `SpecClient._verify_draft` and
# `TicketClient._verify_plan` send.
SPEC_KEYS = frozenset(
    {
        "claims",
        *KINDS,
        "wording",
        "equivalence",
        "sources",
        "context_revisions",
        "evidence_revisions",
    }
)
GRAPH_KEYS = frozenset(
    {
        "claims",
        "results",
        "confirmations",
        "wording",
        "equivalence",
        "sources",
        "evidence_revision",
        "context_revision",
    }
)
# What a client sent with the texts before the hashes; the host takes it and drops it.
LEGACY_KEYS = frozenset({"context", "evidence", "history"})
# The records the host writes in a stored graph and derives again on every submit: a graph
# read back and sent again carries them.
GRAPH_DERIVED_KEYS = frozenset({"confirmation", "reuse"})
_NAME_LIMIT = 40
_NAMES_SHOWN = 10


class InvalidVerification(AgentRefusal):
    """A verification the client did not build, refused before anything is stored.

    It is the agent's to correct, by submitting the verification the client builds, and no
    person is called. It stays out of `AGENT_CODES`, which name a text the composed flow
    revises: the flow builds every verification with the client, so there it can only mean
    a broken client, which the flow hands to a person.
    """

    def __init__(self, message: str, findings: list[dict[str, Any]]) -> None:
        super().__init__(INVALID_VERIFICATION, message, findings)


def checked_keys(
    verification: dict[str, Any], read: frozenset[str], kept: frozenset[str], client: str
) -> None:
    """Refuse a verification with a top-level key no client sends, or with none the host
    reads, which compacted would be stored empty. `kept` are the keys the host takes and
    then drops or replaces. Only the names, truncated, go back to the agent."""
    known = read | kept
    unknown = [str(key) for key in verification if key not in known]
    if unknown:
        shown = [
            key if len(key) <= _NAME_LIMIT else key[:_NAME_LIMIT] + "…"
            for key in unknown[:_NAMES_SHOWN]
        ]
        omitted = len(unknown) - len(shown)
        more = f" e mais {omitted}" if omitted else ""
        found = f"Chaves não reconhecidas: {', '.join(shown)}{more}."
        finding: dict[str, Any] = {"problem": "unknown_keys", "keys": shown}
        if omitted:
            finding["omitted"] = omitted
    elif not any(key in read for key in verification):
        found, finding = "Nenhuma chave que o host lê.", {"problem": "no_known_key"}
    else:
        return
    raise InvalidVerification(
        "A verificação não tem o formato do cliente do Specgate, e nada foi gravado. "
        f"Monte-a com {client} ou com `specgate flow` do cliente público, sem editar o "
        f"que ele devolve, e envie de novo. {found}",
        [finding],
    )


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
        raise InvalidVerification(
            "Informe cada fonte da verificação com id, origem e revisão.",
            [{"problem": "source_ref"}],
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


_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,80}")


def _short(value: Any, limit: int) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def _compact_authorities(value: Any) -> dict[str, dict[str, str]]:
    """The authorities a stored check names, each with the six fields and nothing else."""
    return {
        key: {
            name: found
            for name in AUTHORITY_KEYS
            if (found := _short(record.get(name), 200))
        }
        for key, record in (value if isinstance(value, dict) else {}).items()
        if valid_hash(key) and isinstance(record, dict)
    }


def _compact_outcome(entry: dict[str, Any]) -> dict[str, Any]:
    """What a check kept of how it was judged: the request, the authority, the scores and,
    when the gate stayed closed, the reason and the error."""
    row: dict[str, Any] = {
        name: found
        for name in ("request_revision", "authority")
        if (found := valid_hash(entry.get(name)))
    }
    scores = entry.get("probabilities")
    if isinstance(scores, list) and len(scores) <= 8:
        row["probabilities"] = [
            item
            for item in scores
            if isinstance(item, (int, float)) and not isinstance(item, bool)
        ]
    if isinstance(entry.get("reason"), str) and _CODE.fullmatch(entry["reason"]):
        row["reason"] = entry["reason"]
    error = entry.get("error")
    if isinstance(error, dict):
        row["error"] = {
            name: found
            for name, limit in (("code", 80), ("message", 300))
            if (found := _short(error.get(name), limit))
        }
    return row


def _compact_checks(checks: Any) -> dict[str, dict[str, Any]]:
    """The checks of a wording record, each reduced to the shape it must have."""
    kept: dict[str, dict[str, Any]] = {}
    for target, entry in (checks if isinstance(checks, dict) else {}).items():
        if not isinstance(target, str) or not isinstance(entry, dict):
            continue
        row: dict[str, Any] = {
            name: found
            for name in ("text_revision",)
            if (found := valid_hash(entry.get(name)))
        }
        kept[target] = {**row, **_compact_outcome(entry)}
    return kept


def compact_wording(value: Any) -> dict[str, Any]:
    """The wording record a verification stores; whatever is not part of it is dropped.

    Each field keeps only the shape it has: a digest is a sha256, a reason is a short code,
    a probability is a number. A text whose check is dropped reads as unchecked, and a
    faulty client cannot smuggle research text into the stored artifact through it.
    """
    if not isinstance(value, dict):
        return {}
    stored: dict[str, Any] = {
        key: found
        for key in ("scope_revision", "context_revision")
        if (found := valid_hash(value.get(key)))
    }
    stored["authorities"] = _compact_authorities(value.get("authorities"))
    stored["checks"] = _compact_checks(value.get("checks"))
    # The checks of the claims the host derived: the ones it reads, and one digest of the rest.
    if derived := _compact_checks(value.get("derived")):
        stored["derived"] = derived
    clean = value.get("clean")
    if isinstance(clean, dict) and (requests := valid_hash(clean.get("requests"))):
        keys = clean.get("authorities")
        judges = [key for key in (keys if isinstance(keys, list) else []) if valid_hash(key)]
        if judges:
            stored["clean"] = {"authorities": judges, "requests": requests}
    return stored


def compact_equivalence(value: Any) -> dict[str, Any]:
    """The equivalence record a verification stores, one check per original.

    A check keeps the digest of the original and of each part it judged, so the host reads
    it against the texts it has, and how it was judged, as a wording check does. An original
    whose check is dropped reads as unchecked.
    """
    if not isinstance(value, dict):
        return {}
    stored: dict[str, Any] = {
        key: found
        for key in ("context_revision",)
        if (found := valid_hash(value.get(key)))
    }
    stored["authorities"] = _compact_authorities(value.get("authorities"))
    checks = value.get("checks")
    kept: dict[str, dict[str, Any]] = {}
    for original, entry in (checks if isinstance(checks, dict) else {}).items():
        if not isinstance(original, str) or not isinstance(entry, dict):
            continue
        row: dict[str, Any] = {}
        if found := valid_hash(entry.get("original_revision")):
            row["original_revision"] = found
        parts = entry.get("parts")
        if isinstance(parts, dict) and len(parts) <= PART_LIMIT:
            row["parts"] = {
                part: found
                for part, digested in parts.items()
                if isinstance(part, str) and (found := valid_hash(digested))
            }
        kept[original] = {**row, **_compact_outcome(entry)}
    stored["checks"] = kept
    return stored


def valid_hash(value: Any) -> str | None:
    """The sha256 a stored verification names, or None: nothing is reused without one."""
    return value if isinstance(value, str) and _HASH.fullmatch(value) else None


def _hashes(value: Any) -> dict[str, str]:
    # A malformed hash is dropped: without it no verdict can be reused.
    if not isinstance(value, dict):
        return {}
    return {kind: value[kind] for kind in KINDS if valid_hash(value.get(kind))}


def compact_spec_verification(verification: dict[str, Any]) -> dict[str, Any]:
    """Drop context, evidence, history and source text before a spec stores it.

    A key no client sends is refused, never dropped (`checked_keys`).
    """
    if not verification:
        return verification
    checked_keys(verification, SPEC_KEYS, LEGACY_KEYS, "SpecClient.synthesize")
    stored: dict[str, Any] = {}
    if "claims" in verification:
        stored["claims"] = verification["claims"]
    for kind in KINDS:
        if isinstance(verification.get(kind), dict):
            stored[kind] = _result(verification[kind])
    if "wording" in verification:
        stored["wording"] = compact_wording(verification["wording"])
    if "equivalence" in verification:
        stored["equivalence"] = compact_equivalence(verification["equivalence"])
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
    """Drop context, evidence, history and source text before a graph stores it.

    A key no client sends is refused, never dropped (`checked_keys`).
    """
    if not verification:
        return verification
    checked_keys(
        verification,
        GRAPH_KEYS,
        LEGACY_KEYS | GRAPH_DERIVED_KEYS,
        "TicketClient.synthesize",
    )
    stored: dict[str, Any] = {}
    if "claims" in verification:
        stored["claims"] = verification["claims"]
    for name in ("results", "confirmations"):
        if name in verification:
            stored[name] = _results(verification[name])
    if "wording" in verification:
        stored["wording"] = compact_wording(verification["wording"])
    if "equivalence" in verification:
        stored["equivalence"] = compact_equivalence(verification["equivalence"])
    if "sources" in verification:
        stored["sources"] = checked_refs(verification["sources"])
    for name in ("evidence_revision", "context_revision"):
        if found := valid_hash(verification.get(name)):
            stored[name] = found
    return stored
