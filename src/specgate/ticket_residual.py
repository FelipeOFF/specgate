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

from typing import Any

from specgate.grill_contracts import GrillError
from specgate.research_ledger import counted, exhausted, limits, stop
from specgate.shared.domain.inputs import checked_text
from specgate.ticket_claims import (
    confirmation_rows,
    effective_cutoff,
    evaluations_of,
    first_rows,
    near,
    scored,
)

# A persistent decision gap ends the automatic cycles; an operational stop or a
# contradicting source does not, so those leave the claim to the normal flow
# even after the attempt limit or the shared budget is reached.
ENDED = frozenset({"research_limit", "research_budget_exhausted", "stagnation"})
# A graph whose publication started has no cycle left to run. The value keeps the
# name this end had before the research ran without a manifest, so the records of
# the publications already started still read the same.
PUBLICATION_STARTED = "calibration_unconfigured"
RESIDUAL_RULE = {
    "name": "human_accept_residual",
    "accepts": "a claim of the current graph whose every evaluation is verified, below the threshold",
    "requires": "the symmetric confirmation for a claim near the threshold, no automatic research cycle left (attempt limit, shared budget or stagnation, with or without a calibration manifest), and a human origin",
    "never": "a contradicted or unsupported evaluation, a claim a research source contradicted, or a structural gap of the verification",
    "ends": "with any new revision of the graph, a change of the threshold that leaves the claim unconfirmed, or, before the publication starts, an authorized continuation that reopens the cycles; once the publication starts the acceptance holds",
}
REFUSALS = {
    "residual_requires_human": "A aceitação de resíduo é um ato do mantenedor, não de uma delegação.",
    "residual_duplicate": "Cada claim aparece uma só vez na lista de resíduo.",
    "residual_not_a_gap": "Aceite só uma claim que ainda bloqueia a revisão atual do grafo.",
    "residual_contradicted": "Uma claim contradicted nunca pode ser aceita.",
    "residual_contradicted_by_research": "Uma fonte da pesquisa contradisse a claim; ela nunca pode ser aceita.",
    "residual_not_verified": "Só uma claim verified em todas as avaliações pode ser aceita.",
    "residual_verdict_mismatch": "O verdict informado difere do gravado na verificação.",
    "residual_unconfirmed": "A claim perto do limiar precisa da segunda avaliação da confirmação simétrica.",
    "residual_cycles_open": "Os ciclos automáticos da claim ainda não se esgotaram.",
}


def _refused(code: str) -> GrillError:
    return GrillError(code, REFUSALS[code])


def research_stop(
    state: dict[str, Any], claim_id: str, *, retry: bool = False
) -> str | None:
    """Why no automatic cycle can run for a claim; None while one can.

    The recorded stop of the claim wins over the limit and the budget it may
    also reach, so a contradiction or an operational stop is never relabeled.
    With `retry`, an operational stop is no stop: it spent no cycle, so the next
    reservation researches the claim again.
    """
    question = f"tickets:{claim_id}"
    limit, budget = limits(state, claim_id)
    recorded = (
        stop(state, question)
        if retry
        else state.get("research_stopped", {}).get(question)
    )
    return recorded or exhausted(state, question, budget, limit=limit)


def next_cycle(state: dict[str, Any], claim_ids: list[str]) -> tuple[str, str | None]:
    """The claim to research next and why it cannot be: (claim, None) while one can.

    A claim whose cycles ended is skipped, so every residual claim runs its own.
    A stop that needs a person halts the research wherever the claim sits; when
    every claim ended, the first one reports its reason. A claim that stopped on
    an operational error is researched again, as the error spent no cycle.
    """
    ended: tuple[str, str] | None = None
    for claim_id in claim_ids:
        reason = research_stop(state, claim_id, retry=True)
        if reason is None:
            return claim_id, None
        if reason not in ENDED:
            return claim_id, reason
        ended = ended or (claim_id, reason)
    return ended or (claim_ids[0], None)


def contradicted_by_research(state: dict[str, Any], claim_id: str) -> bool:
    """Whether a research source ever contradicted the claim.

    Read from the attempts, which an extension keeps while it erases the stop.
    """
    question = f"tickets:{claim_id}"
    return any(
        row["question_id"] == question and row.get("reason") == "contradiction"
        for row in state.get("research_attempts", [])
    )


def cycles(
    state: dict[str, Any], claim_id: str, *, publication_started: bool
) -> dict[str, Any] | None:
    """How the automatic research cycles of a claim ended; None while one remains.

    The ledger names the end. An operational stop it recorded keeps the cycles
    open. Once the publication started no cycle runs, so a claim the ledger never
    ended counts as ended by the publication.
    """
    # A reserved cycle is still running and can spend the shared budget, so no
    # claim has exhausted its cycles until it is settled.
    if state.get("research_pending"):
        return None
    reason = research_stop(state, claim_id)
    if reason is None and publication_started:
        reason = PUBLICATION_STARTED
    if reason not in {*ENDED, PUBLICATION_STARTED}:
        return None
    limit, _ = limits(state, claim_id)
    return {
        "exhausted": reason,
        "attempts": len(counted(state, f"tickets:{claim_id}")),
        "limit": limit,
    }


def _claim(
    state: dict[str, Any],
    entry: dict[str, Any],
    first: dict[str, Any] | None,
    second: dict[str, Any] | None,
    threshold: float,
    publication_started: bool,
) -> dict[str, Any]:
    """One accepted claim, or the refusal that keeps it out."""
    if first is None:
        raise _refused("residual_not_a_gap")
    rows = [first, *([second] if second else [])]
    verdicts = {row.get("verdict") for row in rows}
    if "contradicted" in verdicts or entry["verdict"] == "contradicted":
        raise _refused("residual_contradicted")
    if verdicts != {"verified"} or not all(scored(row) for row in rows):
        raise _refused("residual_not_verified")
    if entry["verdict"] != "verified":
        raise _refused("residual_verdict_mismatch")
    if near(first, threshold) and second is None:
        raise _refused("residual_unconfirmed")
    if contradicted_by_research(state, entry["claim_id"]):
        raise _refused("residual_contradicted_by_research")
    ended = cycles(state, entry["claim_id"], publication_started=publication_started)
    if ended is None:
        raise _refused("residual_cycles_open")
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
) -> dict[str, Any] | None:
    """The record of the accepted claims; None for an empty list, else all or none.

    Raises GrillError with the first refusal. `gaps` are the verification gaps of
    the graph, so a structural gap, which names no claim, cannot be accepted. The
    research runs while the graph is in review, so a graph whose publication started
    has no cycle left to run and keeps the acceptance it was made under.
    """
    if not entries:
        return None
    if origin != "human":
        raise _refused("residual_requires_human")
    started = publication_started(state, graph)
    verification = graph.get("verification", {})
    first = {row.get("id"): row for row in first_rows(verification)}
    second = confirmation_rows(verification) or {}
    open_claims = {gap.removeprefix("Jev: ") for gap in gaps}
    listed: set[str] = set()
    claims = []
    for entry in entries:
        claim_id = entry["claim_id"]
        if claim_id in listed:
            raise _refused("residual_duplicate")
        listed.add(claim_id)
        checked_text(entry["reason"])
        claims.append(
            _claim(
                state,
                entry,
                first.get(claim_id) if claim_id in open_claims else None,
                second.get(claim_id),
                threshold,
                started,
            )
        )
    return {
        "rule": RESIDUAL_RULE,
        "artifact_revision": graph["revision"],
        "threshold": effective_cutoff(threshold),
        "claims": claims,
    }


def remaining(gaps: list[str], record: dict[str, Any] | None) -> list[str]:
    """The gaps no accepted claim covers."""
    accepted = {f"Jev: {claim['id']}" for claim in (record or {}).get("claims", [])}
    return [gap for gap in gaps if gap not in accepted]
