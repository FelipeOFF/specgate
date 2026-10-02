"""Reuse of verdicts between the verifications of the same ticket graph.

A claim whose text, evidence (the approved spec and the whole graph) and context
are the ones an earlier verification judged keeps that verdict, strong or weak. A
verification stores the hashes of its evidence and context, not their text, and one
without a hash never matches: an unknown input is not an equal one.
A weak claim is never judged alone again without a change: it returns to Jev
only with new content in the claim, the evidence or the context, a different gate
authority (the confidence policy with the provider and model it bound, or an installed
manifest and its revision), or
through the symmetric confirmation. The host derives the record from the stored
rows, taking each claim from the latest stored verification that judged it, so a
submission that leaves the claim out does not erase what it was judged.
"""

from collections.abc import Sequence
from typing import Any, NamedTuple

from specgate.client import gate_authority
from specgate.ticket_claims import confirmation_rows, decided, near
from specgate.verification_refs import valid_hash

REUSE_MISMATCH = "Jev: verification_reuse_mismatch"
REUSE_RULE = {
    "name": "scoped_reuse",
    "unchanged": "claim text, evidence hash (approved spec and whole graph) and context hash equal to those of the latest stored verification that judged the claim",
    "reused": "a strong verdict of an unchanged claim is kept without a new evaluation",
    "retained": "a weak verdict of an unchanged claim is kept too: it returns to Jev only with new content in the claim, evidence or context, or once through the symmetric confirmation, never after a contradicted verdict",
    "held": "a row carried onto changed input, or a weak claim judged again without a change, holds the graph until its input changes",
}


class Inputs(NamedTuple):
    """What Jev is given for each claim of a verification; the rest as sha256 hashes."""

    texts: dict[str, str]
    evidence: str | None
    context: str | None


class Judged(NamedTuple):
    """The stored evaluations of one claim, with the provider results holding them."""

    request: Any
    first: dict[str, Any]
    second: dict[str, Any] | None
    result: dict[str, Any]
    pair: dict[str, Any] | None


class Memory(NamedTuple):
    """The latest stored judgment of a claim and the input it was judged on."""

    inputs: Inputs
    judged: Judged
    held: str | None
    entered: bool


def inputs(verification: dict[str, Any]) -> Inputs:
    claims = verification.get("claims")
    texts = {
        claim["id"]: claim["text"]
        for claim in (claims if isinstance(claims, list) else [])
        if isinstance(claim, dict)
        and isinstance(claim.get("id"), str)
        and isinstance(claim.get("text"), str)
    }
    return Inputs(
        texts,
        valid_hash(verification.get("evidence_revision")),
        valid_hash(verification.get("context_revision")),
    )


def change(before: Inputs, after: Inputs, claim_id: str) -> str | None:
    """Why a claim judged on `before` goes back to Jev on `after`; None if it does not."""
    if claim_id not in before.texts:
        return "new_claim"
    if before.texts[claim_id] != after.texts.get(claim_id):
        return "changed_claim"
    # A missing hash is no input at all: two missing hashes are not equal inputs.
    if before.evidence is None or before.evidence != after.evidence:
        return "changed_evidence"
    if before.context is None or before.context != after.context:
        return "changed_context"
    return None


def _wrapped(results: Any) -> dict[str, dict[str, Any]]:
    """Each stored row by claim ID, inside a provider result holding only that row."""
    return {
        row["id"]: {**result, "verdicts": [row]}
        for result in (results if isinstance(results, list) else [])
        if isinstance(result, dict) and isinstance(result.get("verdicts"), list)
        for row in result["verdicts"]
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }


def _judged(verification: dict[str, Any]) -> dict[str, Judged]:
    second = confirmation_rows(verification) or {}
    pairs = _wrapped(verification.get("confirmations"))
    return {
        claim_id: Judged(
            result.get("request_revision"),
            result["verdicts"][0],
            second.get(claim_id),
            result,
            pairs.get(claim_id),
        )
        for claim_id, result in _wrapped(verification.get("results")).items()
    }


def _entries(verification: dict[str, Any], key: str) -> list[dict[str, Any]]:
    record = verification.get("reuse")
    entries = record.get(key) if isinstance(record, dict) else None
    return [
        entry
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    ]


def history_of(state: dict[str, Any]) -> list[dict[str, Any]]:
    """The verifications of the earlier graphs of a run, oldest first."""
    return [
        entry.get("verification") or {} for entry in state.get("tickets_history", [])
    ]


def lineage(
    prior: dict[str, Any] | None, history: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The stored verifications, newest first; `history` is oldest first."""
    return [found for found in (prior, *reversed(history)) if found]


def memories(chain: Sequence[dict[str, Any]]) -> dict[str, Memory]:
    """The latest stored judgment of each claim, from the newest verification back."""
    found: dict[str, Memory] = {}
    for verification in chain:
        seen = inputs(verification)
        held = {
            entry["id"]: str(entry.get("reason"))
            for entry in _entries(verification, "held")
        }
        entered = {
            entry["id"]
            for entry in _entries(verification, "evaluated")
            if entry.get("reason") == "symmetric_confirmation"
        }
        for claim_id, judged in _judged(verification).items():
            found.setdefault(
                claim_id,
                Memory(seen, judged, held.get(claim_id), claim_id in entered),
            )
    return found


def reentry(past: Memory, threshold: float) -> bool:
    """Whether a near claim whose pair is unusable may come back as a new pair.

    It may once per input, never after a contradicted verdict and never held.
    """
    first = past.judged.first
    return (
        past.judged.second is None
        and near(first, threshold)
        and first.get("verdict") != "contradicted"
        and not past.entered
        and past.held is None
    )


def _regated(now: Judged, was: Judged) -> bool:
    """Another gate authority judged the claim than the one that judged it before."""
    before, after = gate_authority(was.result), gate_authority(now.result)
    return before is not None and after is not None and before != after


def _carried(now: Judged, was: Judged) -> bool:
    """The first evaluation is the very one an earlier verification stored."""
    return (
        isinstance(now.request, str)
        and bool(now.request)
        and (now.request, now.first) == (was.request, was.first)
    )


def _unchanged(now: Judged, was: Judged, threshold: float) -> bool:
    """The same evaluations; a pair the current band no longer asks for may be gone."""
    return (now.request, now.first) == (was.request, was.first) and (
        now.second == was.second
        or (now.second is None and not near(now.first, threshold))
    )


def with_reuse(
    verification: dict[str, Any],
    prior: dict[str, Any] | None,
    threshold: float,
    history: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Replace any client-provided record with the one the host derives.

    The record compares the raw rows of the verification with the stored rows of
    the earlier ones. It is absent when there is no earlier verification.
    """
    raw = {key: value for key, value in verification.items() if key != "reuse"}
    chain = lineage(prior, history)
    if not chain:
        return raw
    after = inputs(raw)
    current = _judged(raw)
    memory = memories(chain)
    reused: list[str] = []
    retained: list[str] = []
    evaluated: list[dict[str, str]] = []
    held: list[dict[str, str]] = []
    for claim_id in after.texts:
        now = current.get(claim_id)
        if now is None:
            continue
        past = memory.get(claim_id)
        if past is None:
            # No stored verification judged it: this is its first judgment.
            evaluated.append({"id": claim_id, "reason": "new_claim"})
            continue
        was = past.judged
        reason = change(past.inputs, after, claim_id)
        if reason and _carried(now, was):
            # A verdict judged on another input cannot stand for this one.
            held.append({"id": claim_id, "reason": "stale_input"})
        elif reason:
            evaluated.append({"id": claim_id, "reason": reason})
        elif _regated(now, was):
            # Another gate authority judged it: that is a new input, not a repeat.
            evaluated.append({"id": claim_id, "reason": "changed_gate"})
        elif past.held is not None:
            held.append({"id": claim_id, "reason": past.held})
        elif _unchanged(now, was, threshold):
            (reused if decided(now.first, now.second, threshold) else retained).append(
                claim_id
            )
        elif decided(was.first, was.second, threshold):
            evaluated.append({"id": claim_id, "reason": "reevaluated"})
        elif reentry(past, threshold) and (
            now.second is not None or not near(now.first, threshold)
        ):
            # The pair the host could not use is confirmed again, as a new pair.
            evaluated.append({"id": claim_id, "reason": "symmetric_confirmation"})
        else:
            held.append({"id": claim_id, "reason": "reevaluated_without_change"})
    return {
        **raw,
        "reuse": {
            "rule": REUSE_RULE,
            "reused": reused,
            "retained": retained,
            "evaluated": evaluated,
            "held": held,
        },
    }


def reuse_gaps(verification: dict[str, Any]) -> list[str]:
    return [REUSE_MISMATCH] if _entries(verification, "held") else []
