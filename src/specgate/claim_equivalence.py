"""The equivalence check of a division or reformulation the agent wrote, run before anything else.

When the verification of a spec or of a ticket graph finds a lineage (a part that replaces an
original of the previous revision), it asks the Jev, with `jev_equivalence`, whether no part
says what the original does not and the original says nothing the parts leave out. One call
per original, judged against all of its parts together. The code reads each result the way it
reads a wording check.

- A check that passed the gate with a defect refuses the parts. The client raises
  `EquivalenceRejected`, the typed refusal of `specgate.wording`: no wording check of the
  parts, no evaluation and no research follow.
- A check that passed the gate without a defect accepts the division or the reformulation,
  and the verification records it with the original.
- Any other check (the mock, an error, a probability at the cutoff, a host with a manifest)
  keeps the parts in review.

The host repeats the last two rules from what the verification stored: a part with a lineage
and no accepted check, bound to this client's policy and recipe, is a gap, so a client that
skips the check never reaches ready or publishes. The Jev only judges: no field carries
suggested text, and the actions come from the defects. The record is the compact one of
`claim_wording`, keyed by the original instead of by a text.
"""

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import TypeAdapter

from specgate.claim_wording import (
    CHECK_BATCH_SIZE,
    Call,
    Kind,
    rebuilt,
    run_check,
    stored_entry,
)
from specgate.client import policy_verification_intact
from specgate.context import ContextPacket
from specgate.decision_lineage import Group
from specgate.recipe_inputs import request_revision
from specgate.slices.equivalence import DEFECTS, next_actions
from specgate.verification_refs import digest
from specgate.wording import EquivalenceRejected

TOOL = "jev_equivalence"
# What the check can allow next, so the context names what it will compare.
ALTERNATIVES = ("accept", "revise_parts")
MISSING = "equivalence_missing"
REVIEW = "equivalence_review"
ERROR = "decision_equivalence_error"
EQUIVALENCE = Kind(TOOL, DEFECTS, next_actions, ERROR)

State = Literal["accepted", "defective", "review", "missing"]


def identity(group: Group) -> dict[str, Any]:
    """What binds a stored check to the texts it judged: the digest of each."""
    return {
        "original_revision": digest(group.original_text),
        "parts": {part: digest(text) for part, text in group.parts},
    }


def judge(record: Any, group: Group) -> tuple[State, list[str]]:
    """What a stored verification says about the equivalence of a group, decided again.

    accepted: a real check that passed the gate under the policy, bound to this client's
    policy and recipe, found neither defect. defective: it passed with the defects returned.
    review: the check ran for this original and these parts but the gate stayed closed.
    missing: there is no check for this original and these parts, or one that claims to have
    passed does not hold, which the host reads as no check.
    """
    checks = record.get("checks") if isinstance(record, dict) else None
    entry = checks.get(group.original) if isinstance(checks, dict) else None
    if not isinstance(entry, dict) or any(
        entry.get(key) != value for key, value in identity(group).items()
    ):
        return "missing", []
    if entry.get("authority") is None:
        return "review", []
    result = rebuilt(record, entry, EQUIVALENCE)
    if result is None or not policy_verification_intact(
        result, result["context_revision"], tool=TOOL
    ):
        return "missing", []
    defects: list[str] = result["defects"]
    return ("defective", defects) if defects else ("accepted", [])


def equivalence_gaps(record: Any, groups: Sequence[Group]) -> list[str]:
    """One gap per part whose original has no accepted equivalence check."""
    gaps: list[str] = []
    for group in groups:
        state, _ = judge(record, group)
        if state != "accepted":
            code = REVIEW if state == "review" else MISSING
            gaps.extend(f"Jev: {code}:{part}" for part, _ in group.parts)
    return gaps


def lineage_view(
    lineage: Mapping[str, Any], groups: Sequence[Group], record: Any
) -> dict[str, dict[str, Any]]:
    """The lineage a reader sees from each part.

    Its original and kind, its sibling parts, the chain of ancestors the original descends from
    (the part inherits it) with the root of that chain, and what the equivalence check says.
    """
    resolved = {part: group for group in groups for part, _ in group.parts}
    view = {}
    for part, entry in lineage.items():
        group = resolved.get(part)
        ancestry = list(group.ancestry) if group and group.ancestry else [entry["original"]]
        view[part] = {
            "original": entry["original"],
            "kind": entry["kind"],
            "siblings": [
                other
                for other, found in lineage.items()
                if other != part and found["original"] == entry["original"]
            ],
            "ancestry": ancestry,
            "root": ancestry[-1],
            "equivalence": judge(record, group)[0] if group else "missing",
        }
    return view


def _finding(group: Group, defects: list[str], probabilities: Any) -> dict[str, Any]:
    """Copied from the check result: the original, the parts, the defects and the scores."""
    return {
        "original": group.original,
        "kind": group.kind,
        "parts": [part for part, _ in group.parts],
        "defects": list(defects),
        "next_actions": next_actions(defects),
        "probabilities": dict(zip(DEFECTS, probabilities, strict=True)),
    }


def defective(record: Any, groups: Sequence[Group]) -> list[dict[str, Any]]:
    """The finding of each group whose stored check passed the gate with a defect."""
    return [
        _finding(group, defects, record["checks"][group.original]["probabilities"])
        for group in groups
        for state, defects in [judge(record, group)]
        if state == "defective"
    ]


async def check_equivalence(
    call: Call,
    groups: Sequence[Group],
    context: Mapping[str, Any],
    prior: Any = None,
) -> dict[str, Any]:
    """Check each original against its parts; the record a verification stores.

    An accepted check stored for the very same request is kept. Raises
    `EquivalenceRejected` when a check passed the gate with a defect, before the caller
    spends any wording check or evaluation, and `PayloadTooLarge` when the host reports the
    request above the limit.
    """
    packet = {**context, "alternatives": list(ALTERNATIVES)}
    arguments = {
        group.original: {
            "original": group.original_text,
            "parts": [text for _, text in group.parts],
            "context": packet,
        }
        for group in groups
    }
    revisions = {
        group.original: request_revision(TOOL, arguments[group.original])
        for group in groups
    }
    record: dict[str, Any] = {
        "context_revision": TypeAdapter(ContextPacket).validate_python(packet).revision,
        "authorities": {},
        "checks": {},
    }
    before: Any = prior if isinstance(prior, dict) else {}
    kept: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    for group in groups:
        if judge(before, group)[0] == "accepted":
            entry = before["checks"][group.original]
            if (
                entry.get("request_revision") == revisions[group.original]
                and before.get("context_revision") == record["context_revision"]
            ):
                kept[group.original] = entry
                continue
        pending.append(group.original)
    results: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(pending), CHECK_BATCH_SIZE):
        batch = pending[offset : offset + CHECK_BATCH_SIZE]
        done = await asyncio.gather(
            *(run_check(call, arguments[original], EQUIVALENCE) for original in batch),
            return_exceptions=True,
        )
        for original, outcome in zip(batch, done, strict=True):
            if isinstance(outcome, BaseException):
                raise outcome
            results[original] = outcome
    for group in groups:
        if group.original in kept:
            entry = kept[group.original]
            authority = before["authorities"][entry["authority"]]
        else:
            entry, authority = stored_entry(
                results[group.original], identity(group), EQUIVALENCE
            )
        record["checks"][group.original] = entry
        if authority is not None:
            record["authorities"][entry["authority"]] = authority
    if findings := defective(record, groups):
        raise EquivalenceRejected(findings)
    return record
