"""Human acceptance of verified residual claims of a spec once the automatic cycles end.

The maintainer lists the claims to accept. The host decides from the rows it
stored and from its research ledger, never from the verdicts the list carries: a
claim is accepted only while it is a gap of the current revision, verified, and
out of automatic cycles. A contradicted or unsupported claim, or one a research
source contradicted, is never accepted, and a new revision of the spec starts with
an empty list. The threshold and the support criterion stay what the spec was
verified under: an accepted claim is still listed as a gap, and the spec is ready
only when every gap is covered by an acceptance that holds.

The cycles of a spec claim are the ones the research of the spec runs (question
`spec:<claim>`): three attempts per claim and the shared budget of the policy, with
no continuation. Once the publication of the revision starts, no cycle runs on a
spec that left the review, so the acceptance holds whatever the ledger becomes.
"""

from collections.abc import Collection, Mapping
from typing import Any

from specgate import residual
from specgate.grill_contracts import GrillError
from specgate.residual import ENDED, PUBLICATION_STARTED, remaining
from specgate.ticket_claims import evaluations_of, scored

# The attempts a spec claim may draw; the research of the spec reserves with the same.
ATTEMPT_LIMIT = 3
KINDS = ("fidelity", "coverage")
# The questions of the research of the spec; the tickets research under `tickets:`.
QUESTIONS = "spec:"
RESIDUAL_RULE = {
    "name": "human_accept_residual",
    "accepts": "a claim of the current spec revision whose evaluation is verified, below the threshold",
    "requires": "no automatic research cycle left (attempt limit, shared budget or stagnation, with or without a calibration manifest) and a human origin",
    "never": "a contradicted or unsupported evaluation, a claim a research source contradicted, or a structural gap of the verification",
    "ends": "with any new revision of the spec or, while the spec is in review, a research cycle that reopens; once the publication starts and the spec is out of the review the acceptance holds",
}
REFUSALS = {
    **residual.REFUSALS,
    "residual_not_a_gap": "Aceite só uma claim que ainda bloqueia a revisão atual da spec.",
}


def _refused(code: str) -> GrillError:
    return residual.refused(code, REFUSALS)


def _question(claim_id: str) -> str:
    return f"{QUESTIONS}{claim_id}"


def _limits(state: dict[str, Any]) -> tuple[int, int]:
    """The attempt limit and the shared budget the research of the spec reserves under."""
    return ATTEMPT_LIMIT, state.get("workflow_policy", {}).get("research_budget", 12)


def research_stop(
    state: dict[str, Any], claim_id: str, *, retry: bool = False
) -> str | None:
    """Why no automatic cycle can run for a spec claim; None while one can."""
    limit, budget = _limits(state)
    return residual.research_stop(
        state, _question(claim_id), limit, budget, retry=retry
    )


def next_cycle(
    state: dict[str, Any],
    claim_ids: list[str],
    allowed: Mapping[str, list[str]] | None = None,
) -> tuple[str, str | None]:
    """The spec claim to research next and why it cannot be: (claim, None) while one can.

    `allowed` are the `next_actions` of the weak claims; only one with `research` is researched.
    """
    return residual.next_cycle(
        claim_ids, lambda claim_id: research_stop(state, claim_id, retry=True), allowed
    )


def publication_started(state: dict[str, Any], artifact: dict[str, Any]) -> bool:
    """Whether the publication of this spec revision started and no cycle can run on it.

    The research reserves cycles only on a spec in review, and the review can take
    a published spec back, so a spec that returned there has its cycles open again,
    whatever it published before.
    """
    publication = state.get("publication") or {}
    return bool(
        publication.get("artifact_revision") == artifact["revision"]
        and artifact.get("status") != "review"
    )


def rows_of(verification: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The stored verdict rows by claim, named as the gaps are: `fidelity:problem`."""
    return {
        f"{kind}:{row['id']}": row
        for kind in KINDS
        for row in (verification.get(kind) or {}).get("verdicts", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }


def _claim(
    state: dict[str, Any],
    entry: dict[str, Any],
    row: dict[str, Any] | None,
    started: bool,
    reviewed: bool,
) -> dict[str, Any]:
    """One accepted claim, or the refusal that keeps it out."""
    if row is None:
        raise _refused("residual_not_a_gap")
    residual.screen(entry, [row], scored=scored(row))
    limit, budget = _limits(state)
    ended = residual.settled(
        state,
        _question(entry["claim_id"]),
        limit,
        budget,
        publication_started=started,
        own=QUESTIONS,
        reviewed=reviewed,
    )
    return {
        "id": entry["claim_id"],
        "verdict": "verified",
        "reason": entry["reason"],
        **evaluations_of(row, None),
        "cycles": ended,
    }


def accept(
    state: dict[str, Any],
    artifact: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    origin: str,
    gaps: list[str],
    threshold: float,
    reviewed: Collection[str] = frozenset(),
) -> dict[str, Any] | None:
    """The record of the accepted claims; None for an empty list, else all or none.

    Raises GrillError with the first refusal. `gaps` are the verification gaps of
    the spec under the threshold, so a structural gap, which names no claim, cannot
    be accepted. A spec whose publication started has no cycle left to run and keeps
    the acceptance it was made under. `reviewed` are the claims whose only action is the
    maintainer's review of their derivation: no cycle runs for them, so their cycles ended.
    """
    if not entries:
        return None
    started = publication_started(state, artifact)
    rows = rows_of(artifact.get("verification", {}))
    open_claims = {gap.removeprefix("Jev: ") for gap in gaps}
    claims = residual.accepted(
        entries,
        origin=origin,
        claim_of=lambda entry: _claim(
            state,
            entry,
            rows.get(entry["claim_id"]) if entry["claim_id"] in open_claims else None,
            started,
            entry["claim_id"] in reviewed,
        ),
    )
    if claims is None:
        return None
    return {
        "rule": RESIDUAL_RULE,
        "artifact_revision": artifact["revision"],
        "threshold": float(max(0.8, threshold)),
        "claims": claims,
    }


__all__ = [
    "ENDED",
    "PUBLICATION_STARTED",
    "REFUSALS",
    "RESIDUAL_RULE",
    "accept",
    "next_cycle",
    "publication_started",
    "remaining",
    "research_stop",
    "rows_of",
]
