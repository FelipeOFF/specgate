"""Human acceptance of verified residual claims once the automatic cycles end.

The maintainer lists the claims to accept. The host decides from the rows it
stored and from its research ledger, never from the verdicts the list carries: a
claim is accepted only while it is a gap of the current graph, verified on every
evaluation, decided by the symmetric confirmation when it is near the threshold,
and out of automatic cycles. A contradicted claim, or one a research source
contradicted, is never accepted, and a new graph revision starts with an empty
list.

The automatic research runs on every host: with no calibration manifest it
admits evidence by the confidence policy, so the cycles of a claim end only as
the research ledger records (attempt limit, shared budget or stagnation), never
for want of a manifest. Once the publication starts, no cycle runs on a graph
that left the review, so the acceptance holds whatever the ledger or the host's
gates become.
"""

from collections.abc import Collection, Mapping
from typing import Any

from specgate import residual
from specgate.grill_contracts import GrillError
from specgate.research_ledger import limits
from specgate.residual import ENDED, PUBLICATION_STARTED, remaining
from specgate.ticket_claims import (
    confirmation_rows,
    effective_cutoff,
    evaluations_of,
    first_rows,
    near,
    scored,
)

RESIDUAL_RULE = {
    "name": "human_accept_residual",
    "accepts": "a claim of the current graph whose every evaluation is verified, below the threshold",
    "requires": "the symmetric confirmation for a claim near the threshold, no automatic research cycle left (attempt limit, shared budget or stagnation, with or without a calibration manifest), and a human origin",
    "never": "a contradicted or unsupported evaluation, a claim a research source contradicted, or a structural gap of the verification",
    "ends": "with any new revision of the graph, a change of the threshold that leaves the claim unconfirmed, or, before the publication starts, an authorized continuation that reopens the cycles; once the publication starts the acceptance holds",
}
REFUSALS = {
    **residual.REFUSALS,
    "residual_not_a_gap": "Aceite só uma claim que ainda bloqueia a revisão atual do grafo.",
    "residual_unconfirmed": "A claim perto do limiar precisa da segunda avaliação da confirmação simétrica.",
}


def _refused(code: str) -> GrillError:
    return residual.refused(code, REFUSALS)


def _question(claim_id: str) -> str:
    return f"tickets:{claim_id}"


def research_stop(
    state: dict[str, Any], claim_id: str, *, retry: bool = False
) -> str | None:
    """Why no automatic cycle can run for a claim; None while one can.

    The ledger of a claim is its attempt limit and the shared budget, which an
    authorized continuation raises; the rest is the rule both artifacts share.
    """
    limit, budget = limits(state, claim_id)
    return residual.research_stop(
        state, _question(claim_id), limit, budget, retry=retry
    )


def next_cycle(
    state: dict[str, Any],
    claim_ids: list[str],
    allowed: Mapping[str, list[str]] | None = None,
) -> tuple[str, str | None]:
    """The claim to research next and why it cannot be: (claim, None) while one can.

    `allowed` are the `next_actions` of the weak claims; only one with `research` is researched.
    """
    return residual.next_cycle(
        claim_ids, lambda claim_id: research_stop(state, claim_id, retry=True), allowed
    )


def contradicted_by_research(state: dict[str, Any], claim_id: str) -> bool:
    return residual.contradicted_by_research(state, _question(claim_id))


def cycles(
    state: dict[str, Any], claim_id: str, *, publication_started: bool
) -> dict[str, Any] | None:
    """How the automatic research cycles of a claim ended; None while one remains."""
    limit, budget = limits(state, claim_id)
    return residual.cycles(
        state,
        _question(claim_id),
        limit,
        budget,
        publication_started=publication_started,
    )


def _claim(
    state: dict[str, Any],
    entry: dict[str, Any],
    first: dict[str, Any] | None,
    second: dict[str, Any] | None,
    threshold: float,
    publication_started: bool,
    reviewed: bool,
) -> dict[str, Any]:
    """One accepted claim, or the refusal that keeps it out."""
    if first is None:
        raise _refused("residual_not_a_gap")
    rows = [first, *([second] if second else [])]
    residual.screen(entry, rows, scored=all(scored(row) for row in rows))
    if near(first, threshold) and second is None:
        raise _refused("residual_unconfirmed")
    limit, budget = limits(state, entry["claim_id"])
    ended = residual.settled(
        state,
        _question(entry["claim_id"]),
        limit,
        budget,
        publication_started=publication_started,
        reviewed=reviewed,
    )
    return {
        "id": entry["claim_id"],
        "verdict": "verified",
        "reason": entry["reason"],
        **evaluations_of(first, second),
        "cycles": ended,
    }


def publication_started(state: dict[str, Any], graph: dict[str, Any]) -> bool:
    """Whether a publication of this graph revision started, at any destination.

    A change of destination archives the publications of the graph and leaves it
    with none, but the publication it made is no less started.
    """
    return bool(graph.get("publications")) or any(
        row.get("revision") == graph["revision"] and row.get("publications")
        for row in state.get("destination_history", [])
    )


def accept(
    state: dict[str, Any],
    graph: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    origin: str,
    gaps: list[str],
    threshold: float,
    reviewed: Collection[str] = frozenset(),
) -> dict[str, Any] | None:
    """The record of the accepted claims; None for an empty list, else all or none.

    Raises GrillError with the first refusal. `gaps` are the verification gaps of
    the graph, so a structural gap, which names no claim, cannot be accepted. The
    research runs while the graph is in review, so a graph whose publication started
    has no cycle left to run and keeps the acceptance it was made under. `reviewed` are
    the claims whose only action is the maintainer's review of their derivation: no cycle
    runs for them, so their cycles ended.
    """
    if not entries:
        return None
    started = publication_started(state, graph)
    verification = graph.get("verification", {})
    first = {row.get("id"): row for row in first_rows(verification)}
    second = confirmation_rows(verification) or {}
    open_claims = {gap.removeprefix("Jev: ") for gap in gaps}
    claims = residual.accepted(
        entries,
        origin=origin,
        claim_of=lambda entry: _claim(
            state,
            entry,
            first.get(entry["claim_id"])
            if entry["claim_id"] in open_claims
            else None,
            second.get(entry["claim_id"]),
            threshold,
            started,
            entry["claim_id"] in reviewed,
        ),
    )
    if claims is None:
        return None
    return {
        "rule": RESIDUAL_RULE,
        "artifact_revision": graph["revision"],
        "threshold": effective_cutoff(threshold),
        "claims": claims,
    }


__all__ = [
    "ENDED",
    "PUBLICATION_STARTED",
    "REFUSALS",
    "RESIDUAL_RULE",
    "accept",
    "contradicted_by_research",
    "cycles",
    "next_cycle",
    "publication_started",
    "remaining",
    "research_stop",
]
