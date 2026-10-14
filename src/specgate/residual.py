"""What a human acceptance of verified residual claims shares, whatever the artifact.

The spec and the ticket graph both let the maintainer accept claims that stay
verified below the threshold once the automatic research cycles end. Each artifact
says which stored rows and which research question a claim has; this module holds
the rest: the contract of the list, the refusals, how the cycles of a question end
in the research ledger, and the all-or-none loop over the list. The host decides
from the rows it stored and from its ledger, never from what the list carries.
"""

from collections.abc import Callable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from specgate import lineage_budget
from specgate.grill_contracts import GrillError
from specgate.research_ledger import counted, exhausted, stop
from specgate.shared.domain.inputs import checked_text


class ResidualClaim(BaseModel):
    """A claim the maintainer accepts below the threshold, with its verdict and reason."""

    model_config = ConfigDict(extra="forbid", strict=True)
    claim_id: str = Field(min_length=1, max_length=200)
    verdict: Literal["verified", "unsupported", "contradicted"]
    reason: str = Field(min_length=1, max_length=2000)


# A persistent decision gap ends the automatic cycles; an operational stop or a
# contradicting source does not, so those leave the claim to the normal flow
# even after the attempt limit or the shared budget is reached. An artifact that drew
# its third rejection ends them too, for every weak claim it holds, and so does a derived
# claim whose only action is the maintainer's review of its derivation: no cycle runs for it.
ENDED = frozenset(
    {
        "research_limit",
        "research_budget_exhausted",
        "stagnation",
        lineage_budget.EXHAUSTED,
        lineage_budget.DERIVATION_REVIEW,
    }
)
# An artifact whose publication started has no cycle left to run. The value keeps the
# name this end had before the research ran without a manifest, so the records of
# the publications already started still read the same.
PUBLICATION_STARTED = "calibration_unconfigured"
# The research found nothing left to do for the weak claims but the agent's own actions.
AGENT_ACTION = "agent_action"
REFUSALS = {
    "residual_requires_human": "A aceitação de resíduo é um ato do mantenedor, não de uma delegação.",
    "residual_duplicate": "Cada claim aparece uma só vez na lista de resíduo.",
    "residual_contradicted": "Uma claim contradicted nunca pode ser aceita.",
    "residual_contradicted_by_research": "Uma fonte da pesquisa contradisse a claim; ela nunca pode ser aceita.",
    "residual_not_verified": "Só uma claim verified em todas as avaliações pode ser aceita.",
    "residual_verdict_mismatch": "O verdict informado difere do gravado na verificação.",
    "residual_cycles_open": "Os ciclos automáticos da claim ainda não se esgotaram.",
}


def refused(code: str, messages: Mapping[str, str] = REFUSALS) -> GrillError:
    return GrillError(code, messages[code])


def handoff_route(code: str | None, delegated: bool) -> str:
    """Where a research that ended goes next.

    An artifact that drew its third rejection goes to a person whatever the delegation says, and
    so does a claim the code allows nothing for or only the maintainer reviews. Any other end
    goes to a person too, unless a delegation is in force: then the cycles that ended are judged by
    the bounded adjudication, and the other stops are a delivery exception.
    """
    if not delegated or code in {
        lineage_budget.NO_ACTION,
        lineage_budget.EXHAUSTED,
        lineage_budget.DERIVATION_REVIEW,
    }:
        return "human_review"
    return "bounded_adjudication" if code in ENDED else "delivery_exception"


def research_stop(
    state: dict[str, Any],
    question: str,
    limit: int,
    budget: int,
    *,
    retry: bool = False,
) -> str | None:
    """Why no automatic cycle can run for a question; None while one can.

    The recorded stop of the question wins over the limit and the budget it may
    also reach, so a contradiction or an operational stop is never relabeled.
    With `retry`, an operational stop is no stop: it spent no cycle, so the next
    reservation researches the question again.

    The third rejection of the artifact wins over all of them: the claim is a person's
    (`lineage_exhausted`). The one end it leaves as it was is the research limit of the
    claim left alone weak, which a delegation still sends to the bounded adjudication.
    """
    recorded = (
        stop(state, question)
        if retry
        else state.get("research_stopped", {}).get(question)
    )
    return lineage_budget.end(
        state, question, recorded or exhausted(state, question, budget, limit=limit)
    )


def next_cycle(
    claim_ids: list[str],
    stop_of: Callable[[str], str | None],
    allowed: Mapping[str, list[str]] | None = None,
) -> tuple[str, str | None]:
    """The claim to research next and why it cannot be: (claim, None) while one can.

    `stop_of` says why no cycle can run for a claim. A claim whose cycles ended is
    skipped, so every residual claim runs its own. A stop that needs a person halts
    the research wherever the claim sits; when every claim ended, the first one
    reports its reason. A claim that stopped on an operational error is researched
    again, as the error spent no cycle.

    `allowed` are the actions the code lets the agent take on each weak claim: only a claim
    with `research` among them is researched, and the others are skipped, because the cycle
    would be refused as an action outside the set. When nothing is left to research, a claim
    with no action at all is a person's (`no_action_available`), one whose only action is the
    maintainer's review of its derivation is the maintainer's (`derivation_review`), and one whose actions are the
    agent's own (`agent_action`) goes back to the agent. None means every claim may be
    researched.
    """
    ended: tuple[str, str] | None = None
    unattended: tuple[str, str] | None = None
    waiting: tuple[str, str] | None = None
    for claim_id in claim_ids:
        reason = stop_of(claim_id)
        if reason is None:
            actions = None if allowed is None else allowed.get(claim_id, [])
            if actions is None or lineage_budget.RESEARCH in actions:
                return claim_id, None
            if actions and set(actions) <= lineage_budget.MAINTAINER_ACTIONS:
                # A diagnosed derivation is the maintainer's to review, not the agent's.
                unattended = unattended or (claim_id, lineage_budget.DERIVATION_REVIEW)
            elif actions:
                waiting = waiting or (claim_id, AGENT_ACTION)
            else:
                unattended = unattended or (claim_id, lineage_budget.NO_ACTION)
            continue
        if reason not in ENDED:
            return claim_id, reason
        ended = ended or (claim_id, reason)
    return ended or unattended or waiting or (claim_ids[0], None)


def contradicted_by_research(state: dict[str, Any], question: str) -> bool:
    """Whether a research source ever contradicted the claim of the question.

    Read from the attempts, which an extension keeps while it erases the stop.
    """
    return any(
        row["question_id"] == question and row.get("reason") == "contradiction"
        for row in state.get("research_attempts", [])
    )


def reserved(state: dict[str, Any], own: str, *, publication_started: bool) -> bool:
    """Whether a reserved cycle still keeps the cycles of the artifact open.

    A reserved cycle is still running and can spend the shared budget, so no claim
    has exhausted its cycles until it is settled. Once the publication started the
    acceptance holds whatever the ledger becomes, so a cycle that belongs to another
    artifact (a question outside `own`, like the research of the tickets that runs
    after the spec was published) spends nothing the acceptance rests on.
    """
    pending = state.get("research_pending")
    if not pending:
        return False
    return not publication_started or pending["question_id"].startswith(own)


def cycles(
    state: dict[str, Any],
    question: str,
    limit: int,
    budget: int,
    *,
    publication_started: bool,
    own: str = "",
    reviewed: bool = False,
) -> dict[str, Any] | None:
    """How the automatic research cycles of a question ended; None while one remains.

    The ledger names the end. An operational stop it recorded keeps the cycles
    open. Once the publication started no cycle runs, so a question the ledger
    never ended counts as ended by the publication. `own` is the prefix of the
    questions of the artifact; the default owns every question, so any reserved
    cycle keeps them open. A claim `reviewed` has the maintainer's review of its
    derivation as its only action, so the research never opens a cycle for it and
    the review is where its cycles end.
    """
    if reserved(state, own, publication_started=publication_started):
        return None
    reason = research_stop(state, question, limit, budget)
    if reason is None and publication_started:
        reason = PUBLICATION_STARTED
    if reason is None and reviewed:
        reason = lineage_budget.DERIVATION_REVIEW
    if reason not in {*ENDED, PUBLICATION_STARTED}:
        return None
    return {
        "exhausted": reason,
        "attempts": len(counted(state, question)),
        "limit": limit,
    }


def screen(entry: dict[str, Any], rows: list[dict[str, Any]], *, scored: bool) -> None:
    """Refuse a claim whose stored rows or listed verdict keep it out of acceptance.

    Only a claim verified on every evaluation, with valid scores, can be accepted,
    and only under the verdict the host stored for it.
    """
    verdicts = {row.get("verdict") for row in rows}
    if "contradicted" in verdicts or entry["verdict"] == "contradicted":
        raise refused("residual_contradicted")
    if verdicts != {"verified"} or not scored:
        raise refused("residual_not_verified")
    if entry["verdict"] != "verified":
        raise refused("residual_verdict_mismatch")


def settled(
    state: dict[str, Any],
    question: str,
    limit: int,
    budget: int,
    *,
    publication_started: bool,
    own: str = "",
    reviewed: bool = False,
) -> dict[str, Any]:
    """The end of the cycles of a claim the research did not contradict, or the refusal."""
    if contradicted_by_research(state, question):
        raise refused("residual_contradicted_by_research")
    ended = cycles(
        state,
        question,
        limit,
        budget,
        publication_started=publication_started,
        own=own,
        reviewed=reviewed,
    )
    if ended is None:
        raise refused("residual_cycles_open")
    return ended


def accepted(
    entries: list[dict[str, Any]],
    *,
    origin: str,
    claim_of: Callable[[dict[str, Any]], dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """The accepted claims; None for an empty list, else all or none.

    Raises GrillError with the first refusal: a list with any claim the host cannot
    accept stores nothing. Only a person accepts, so a delegation never does.
    """
    if not entries:
        return None
    if origin != "human":
        raise refused("residual_requires_human")
    listed: set[str] = set()
    claims = []
    for entry in entries:
        claim_id = entry["claim_id"]
        if claim_id in listed:
            raise refused("residual_duplicate")
        listed.add(claim_id)
        checked_text(entry["reason"])
        claims.append(claim_of(entry))
    return claims


def remaining(gaps: list[str], record: dict[str, Any] | None) -> list[str]:
    """The gaps no accepted claim covers."""
    covered = {f"Jev: {claim['id']}" for claim in (record or {}).get("claims", [])}
    return [gap for gap in gaps if gap not in covered]
