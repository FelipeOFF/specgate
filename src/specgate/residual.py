"""What a human acceptance of verified residual claims shares, whatever the artifact.

The spec and the ticket graph both let the maintainer accept claims that stay
verified below the threshold once the automatic research cycles end. Each artifact
says which stored rows and which research question a claim has; this module holds
the rest: the contract of the list, the refusals, how the cycles of a question end
in the research ledger, and the all-or-none loop over the list. The host decides
from the rows it stored and from its ledger, never from what the list carries.

The same list accepts the wording of a text the agent wrote whose check stayed outside the
gate with no defect above the cutoff (`wording:<target>`, ADR 0036). The host decides it from
the check it stored for the text and the scope it derives itself: the gap is current, the
check is in review and kept the five probabilities, and none of them is above the cutoff.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from specgate import lineage_budget
from specgate.claim_wording import REVIEW, judge, present, review_probabilities
from specgate.grill_contracts import GrillError
from specgate.research_ledger import counted, exhausted, stop
from specgate.shared.domain.inputs import checked_text


class ResidualClaim(BaseModel):
    """A claim the maintainer accepts below the threshold, with its verdict and reason.

    A claim of the evidence is `fidelity:<id>`, `coverage:<id>` or a claim of the graph, with
    the verdict the host stored. The wording of a text is `wording:<target>`, with the verdict
    `review`: its check stayed outside the gate, and the person accepts it as it was written.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    claim_id: str = Field(min_length=1, max_length=200)
    verdict: Literal["verified", "unsupported", "contradicted", "review"]
    reason: str = Field(min_length=1, max_length=2000)


# The residual of the wording of a text, and the verdict it is listed under.
WORDING = "wording:"
WORDING_VERDICT = "review"


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
        lineage_budget.WORDING_RESIDUAL,
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
    "residual_wording_defect": (
        "A checagem de formulação tem defeito acima de 0,80: o agente reformula ou divide o "
        "texto, e o resíduo não o aceita."
    ),
    "residual_wording_unscored": (
        "A checagem de formulação não guardou as cinco probabilidades: só uma nova "
        "verificação resolve o texto."
    ),
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
        lineage_budget.WORDING_RESIDUAL,
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
    maintainer's is the maintainer's (`derivation_review` for the review of its derivation,
    `wording_residual` for the acceptance of its wording), and one whose actions are the
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
            if lineage_budget.maintainer_only(actions):
                # A diagnosed derivation or a wording outside the gate is the maintainer's.
                unattended = unattended or (claim_id, lineage_budget.maintainer_end(actions))
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
    reviewed: bool | str = False,
) -> dict[str, Any] | None:
    """How the automatic research cycles of a question ended; None while one remains.

    The ledger names the end. An operational stop it recorded keeps the cycles
    open. Once the publication started no cycle runs, so a question the ledger
    never ended counts as ended by the publication. `own` is the prefix of the
    questions of the artifact; the default owns every question, so any reserved
    cycle keeps them open. A claim `reviewed` has an action of the maintainer as its
    only one, so the research never opens a cycle for it and the review is where its
    cycles end: `reviewed` names that end (`maintainer_end`), and True is the review of
    its derivation.
    """
    if reserved(state, own, publication_started=publication_started):
        return None
    reason = research_stop(state, question, limit, budget)
    if reason is None and publication_started:
        reason = PUBLICATION_STARTED
    if reason is None and reviewed:
        reason = reviewed if isinstance(reviewed, str) else lineage_budget.DERIVATION_REVIEW
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
    reviewed: bool | str = False,
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


@dataclass(frozen=True)
class Wordings:
    """The texts the agent wrote, as the host derives them, with the record of their checks.

    `texts` maps each target (`decision-<id>`, `<ticket>:ac-N`) to its text, and `scope` is
    what the check judged them against. The host builds both from the draft and the grill or
    the approved spec it holds, never from the claims the client sent.
    """

    record: Any
    texts: Mapping[str, str]
    scope: str


def wording_claim(
    entry: dict[str, Any],
    wordings: Wordings | None,
    refuse: Callable[[str], GrillError] = refused,
) -> dict[str, Any]:
    """One accepted wording residual, or the refusal that keeps it out.

    The gap is current: the target is a text of this revision and its check, for this text and
    scope, stayed outside the gate. It kept the five probabilities (`residual_wording_unscored`
    otherwise, as an error or the transport leave it), none above the cutoff
    (`residual_wording_defect` otherwise: the agent rewrites the text), and the list names it
    under the verdict `review`.
    """
    target = entry["claim_id"].removeprefix(WORDING)
    text = wordings.texts.get(target) if wordings else None
    if (
        wordings is None
        or text is None
        or judge(wordings.record, target, text, wordings.scope)[0] != "review"
    ):
        raise refuse("residual_not_a_gap")
    probabilities = review_probabilities(wordings.record, target, text, wordings.scope)
    if probabilities is None:
        raise refuse("residual_wording_unscored")
    if present(probabilities):
        raise refuse("residual_wording_defect")
    if entry["verdict"] != WORDING_VERDICT:
        raise refuse("residual_verdict_mismatch")
    reason = wordings.record["checks"][target].get("reason")
    return {
        "id": entry["claim_id"],
        "target": target,
        "verdict": WORDING_VERDICT,
        "reason": entry["reason"],
        "probabilities": probabilities,
        **({"check_reason": reason} if isinstance(reason, str) else {}),
    }


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


def lists_wording(authorization: Mapping[str, Any] | None) -> bool:
    """Whether the residual of an approval lists the wording of a text."""
    return any(
        isinstance(entry, dict) and str(entry.get("claim_id", "")).startswith(WORDING)
        for entry in (authorization or {}).get("residual") or ()
    )


def accepted_wordings(record: Mapping[str, Any] | None) -> frozenset[str]:
    """The targets whose wording an acceptance that holds covers."""
    return frozenset(
        claim["target"]
        for claim in (record or {}).get("claims", [])
        if str(claim.get("id", "")).startswith(WORDING)
    )


def gap_of(claim_id: str) -> str:
    """The gap a residual claim covers: its own, or the wording gap of the text it names."""
    if claim_id.startswith(WORDING):
        return f"Jev: {REVIEW}:{claim_id.removeprefix(WORDING)}"
    return f"Jev: {claim_id}"


def remaining(gaps: list[str], record: dict[str, Any] | None) -> list[str]:
    """The gaps no accepted claim covers."""
    covered = {gap_of(claim["id"]) for claim in (record or {}).get("claims", [])}
    return [gap for gap in gaps if gap not in covered]


def with_exits(refusal: dict[str, Any], exits: Any) -> dict[str, Any]:
    """A review refusal with what the host derived on the read (`review_exits`): the gaps that
    block the artifact, each with who can lift it, and the message that says so."""
    if not isinstance(exits, dict) or not isinstance(exits.get("blocking"), list):
        return refusal
    error = {**refusal["error"], "blocking": exits["blocking"]}
    if isinstance(exits.get("message"), str):
        error["message"] = exits["message"]
    return {**refusal, "error": error}
