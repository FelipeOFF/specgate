"""The budget of an artifact: how many rejections it draws before a person is called.

The spec and the ticket graph each keep one counter in the run state (`state["lineage_budget"]`,
under `spec` and under `tickets`). The counter belongs to the artifact, not to a claim, an id, a
label or a declared lineage, so renaming a decision, relabeling it, dividing it, dropping it and
bringing it back, or moving the grill under it leaves the count where it was. A count keyed on any
of those was reset by changing it.

- An evaluation is one the Jev made in real mode on every claim the host expects of the revision
  (the caller says so with `weak`); a mock provider, a fixture or a verification that leaves a
  claim out or judges another text judges nothing. The first evaluation does not count.
- Every later evaluation with new content that ends with a weak claim counts one rejection. The
  content is the digest of the whole artifact: the draft, without the records of the agent
  (`RECORDS`), and the evidence and the context it was judged on. A claim whose only action is the
  maintainer's review of its derivation is not the agent's and makes no rejection.
- On the third rejection every claim still weak goes to a person: the agent has no action left,
  and no revision of it, judged or not, replaces them. The cycles a person authorizes for a claim
  are that claim's only, counted from where the count stands; a revision they let through keeps
  every other claim of the person theirs, even one it judges strong.
- Only an act of a person starts the count over (the caller detects it). An evaluation that leaves
  every claim strong does not, so dropping a claim and bringing it back is no way out. The count
  starts over with the next evaluation, but the act gives the agent its research back at once:
  after a person approved the current revision, only a claim the person accepted as a residual is
  still the person's, and it keeps the end it was accepted under.
- The content of an evaluation that came out weak stays rejected until then: sending it again is
  refused (`identical_resubmission`) without counting. The same content judged again on purpose
  (the symmetric confirmation of the graph) is part of the evaluation it completes: once a claim,
  and never after the third rejection.

Before a new evaluation of an artifact whose last evaluation left weak claims, the agent registers
what it chose to do (`LineageAction`): a claim that was weak, an action inside the set the code
allows for it (`next_actions`) and the reason. An action outside the set is `action_not_allowed`,
and a revision that registers none is `action_not_registered`.

The record keeps the last evaluation (its content and the weak claims it left, with the ones a
person holds), so a revision the Jev did not judge in between changes nothing the next one answers
to. This module holds the rules and the shape of the record, and owns no gates and no storage. The
grill counts per question instead (`grill_budget.py`, ADR 0032) and reuses from here what is
generic: the action the agent registers, the refusal codes and the digest.
"""

import json
from collections.abc import Collection, Mapping, Sequence
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from specgate.grill_contracts import GrillError
from specgate.shared.domain.inputs import checked_text
from specgate.wording import (
    ACTION_NOT_ALLOWED,
    ACTION_NOT_REGISTERED,
    IDENTICAL_RESUBMISSION,
    AgentRefusal,
)

STATE_KEY = "lineage_budget"
# The rejection that calls a person: the third, as the research limit of a claim already was.
LIMIT = 3
# The artifacts with a counter; a research question is named `<artifact>:<claim>`.
SPEC = "spec"
TICKETS = "tickets"
EXHAUSTED = "lineage_exhausted"
# The research loop stops on a weak claim the code allows nothing for: a person decides it.
NO_ACTION = "no_action_available"
# A derived claim whose derivation the host diagnosed: its only action is the maintainer's.
DERIVATION_REVIEW = "derivation_review"
MAINTAINER_ACTIONS = frozenset({"review_derivation"})
# What a draft records about the agent's own work, not what the artifact says.
RECORDS = frozenset({"lineage_actions", "decision_lineage", "criteria_lineage"})
Action = Literal[
    "research", "reformulate", "split", "new_grill_question", "accept_contradiction"
]
RESEARCH = "research"

NOT_ALLOWED_MESSAGE = (
    "A ação registrada está fora do conjunto permitido; registre, para uma claim que estava "
    "fraca, uma das next_actions dela e o motivo antes da nova avaliação."
)
NOT_REGISTERED_MESSAGE = (
    "A revisão anterior tinha claim fraca: registre em lineage_actions a ação escolhida "
    "dentro das next_actions dela e o motivo antes da nova avaliação."
)
IDENTICAL_MESSAGE = (
    "Este conteúdo já foi avaliado e rejeitado: mude o rascunho ou traga evidência nova. "
    "Reenviar o mesmo conteúdo não vale como tentativa."
)
DEFAULT_REASON = (
    "A claim está fraca e a formulação dela está limpa; pesquisar é a ação permitida, e o "
    "harness não informou outro motivo."
)
NO_ACTION_MESSAGE = (
    "Nenhuma ação está permitida ao agente para esta claim: a decisão vai para a revisão humana."
)
EXHAUSTED_MESSAGE = (
    "O artefato chegou à terceira rejeição: as claims fracas vão para a revisão humana, que "
    "aceita o resíduo ou aprova."
)


class LineageAction(BaseModel):
    """What the agent registers before a new evaluation: the weak claim it acts on, the action
    it chose inside the set the code allows, and why."""

    model_config = ConfigDict(extra="forbid", strict=True)
    target: str = Field(min_length=1, max_length=200)
    action: Action
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def written(cls, value: str) -> str:
        return checked_text(value)


def research_action(claim: str, reason: str = "") -> dict[str, str]:
    """The registration of a research cycle on a weak claim."""
    return {"target": claim, "action": RESEARCH, "reason": reason.strip() or DEFAULT_REASON}


def registered(actions: Sequence[LineageAction], claim: str) -> list[LineageAction]:
    """What a reservation of research registers: the actions the agent gave, or research with the
    reason of the host when it gave none, so a client from before the field still reserves."""
    return list(actions) or [LineageAction.model_validate(research_action(claim))]


def maintainer_only(actions: Collection[str]) -> bool:
    """Whether the maintainer's review is all there is for a claim: nothing is the agent's."""
    return bool(actions) and set(actions) <= MAINTAINER_ACTIONS


def counted(allowed: Mapping[str, Sequence[str]]) -> list[str]:
    """The weak claims that make a rejection: all but the ones only the maintainer acts on."""
    return [claim for claim, found in allowed.items() if not maintainer_only(found)]


def pending(allowed: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """The weak claims the agent can act on now, with their sets. A claim with no action is a
    person's, and one whose only action is the maintainer's is the maintainer's."""
    return {
        claim: list(found)
        for claim, found in allowed.items()
        if found and not maintainer_only(found)
    }


# The field a person approves each artifact with; the run state keeps it under the same name.
APPROVALS = {SPEC: "seams_approved", TICKETS: "decomposition_approved"}


def approved_by_person(authorization: Mapping[str, Any] | None, approval: str) -> bool:
    """Whether a person, not a delegation, approved a revision (`approval` names the field of the
    artifact: `seams_approved`, `decomposition_approved`) or accepted a residual of it."""
    found = authorization or {}
    return found.get("origin", "human") == "human" and bool(
        found.get(approval) or found.get("residual")
    )


def answered_since(state: Mapping[str, Any], artifact: str) -> bool:
    """Whether a person answered the grill after the counter of the artifact last took a revision
    in. The agent reopens the grill and the Jev resolves a question on its own: only a turn a
    person answered (`origin` human) is the person asking for something else, and it counts once,
    however many revisions the spec writes from the same point of the grill."""
    record = _record(state, artifact)
    return record is not None and any(
        turn.get("origin") == "human"
        and int(turn.get("answered_revision") or 0) > int(record.get("revision", 0))
        for turn in state.get("turns", [])
    )


# --- The record -------------------------------------------------------------------------------


def _record(state: Mapping[str, Any], artifact: str) -> dict[str, Any] | None:
    records = state.get(STATE_KEY)
    found = records.get(artifact) if isinstance(records, dict) else None
    return found if isinstance(found, dict) else None


def rejections(state: Mapping[str, Any], artifact: str) -> int:
    record = _record(state, artifact)
    return int(record.get("rejections", 0)) if record else 0


def _ceiling(record: Mapping[str, Any] | None) -> dict[str, int]:
    """The count at which each claim a person extended goes back to them, since the count began."""
    found = (record or {}).get("ceiling")
    return {claim: int(n) for claim, n in found.items()} if isinstance(found, dict) else {}


def limit(state: Mapping[str, Any], artifact: str) -> int:
    """Three rejections, or the highest count a person extended a claim to since the count began."""
    return max([LIMIT, *_ceiling(_record(state, artifact)).values()])


def _spent(record: Mapping[str, Any], claim: str) -> bool:
    """Whether the count reached the limit of a claim: three, or the count a person extended that
    claim to. The cycles authorized for one claim never raise the limit of another."""
    return int(record.get("rejections", 0)) >= _ceiling(record).get(claim, LIMIT)


def released(state: Mapping[str, Any], artifact: str) -> bool:
    """Whether a person approved the current revision of the artifact or accepted a residual of it.
    The count starts over with the next evaluation; until then the act already gives the research
    back for every claim the person did not accept."""
    current = state.get(artifact)
    return isinstance(current, dict) and approved_by_person(
        current.get("authorization"), APPROVALS[artifact]
    )


def _accepted(state: Mapping[str, Any], artifact: str) -> set[str]:
    authorization = (state.get(artifact) or {}).get("authorization") or {}
    return {
        str(entry.get("claim_id"))
        for entry in authorization.get("residual") or ()
        if isinstance(entry, dict)
    }


def held(state: Mapping[str, Any], artifact: str, claim: str) -> bool:
    """Whether a weak claim of the artifact is a person's: the count reached the limit of the claim
    and no person acted on the current revision since, or the person who did accepted the claim as
    a residual, whose cycles end where the person accepted them."""
    record = _record(state, artifact)
    if record is None or not _spent(record, claim):
        return False
    return not released(state, artifact) or claim in _accepted(state, artifact)


def exhausted(state: Mapping[str, Any], artifact: str) -> bool:
    """Whether a weak claim of the last evaluation reached its limit and no person acted since."""
    record = _record(state, artifact)
    return (
        record is not None
        and not released(state, artifact)
        and any(_spent(record, claim) for claim in counted(record.get("weak", {})))
    )


def held_claims(state: Mapping[str, Any], artifact: str) -> list[str]:
    """The weak claims of the last evaluation that are a person's (`held`), the ones a later
    revision judged strong included: the artifact waits for the person on each of them."""
    record = _record(state, artifact)
    found = counted(record.get("weak", {})) if record else []
    return [claim for claim in found if held(state, artifact, claim)]


def stop(state: Mapping[str, Any], question: str) -> str | None:
    """`lineage_exhausted` for a research question whose claim is a person's (`held`); None for any
    other question, the grill's included."""
    artifact, separator, claim = question.partition(":")
    counted = separator and artifact in APPROVALS
    return EXHAUSTED if counted and held(state, artifact, claim) else None


def alone(state: Mapping[str, Any], question: str) -> bool:
    """Whether the claim of a research question is the only weak claim that counts in the last
    evaluation of its artifact. On the third rejection that claim's own research limit is still
    the bounded adjudication's to judge under a delegation (ADR 0019), because the repair it
    recommends replaces no other claim a person was called for; every other end, and every
    claim of an artifact with more than one weak claim left, is the person's."""
    artifact, _, claim = question.partition(":")
    record = _record(state, artifact)
    return record is not None and counted(record.get("weak", {})) == [claim]


def end(state: Mapping[str, Any], question: str, ended: str | None) -> str | None:
    """How the research of a question ends, given the end its ledger names (`ended`):
    `lineage_exhausted` comes first, but for the research limit of the claim left alone weak,
    which a delegation still sends to the bounded adjudication (ADR 0019)."""
    if ended == "research_limit" and alone(state, question):
        return ended
    return stop(state, question) or ended


def view(state: Mapping[str, Any], artifact: str) -> dict[str, Any] | None:
    """What a reader sees: the rejections, the limit, whether a person is called and what each
    rejection was, with the reasons the agent registered. Derived on every read."""
    record = _record(state, artifact)
    if record is None:
        return None
    return {
        "rejections": rejections(state, artifact),
        "limit": limit(state, artifact),
        "exhausted": exhausted(state, artifact),
        "attempts": list(record.get("attempts", [])),
    }


# --- The content ------------------------------------------------------------------------------


def content_digest(draft: Mapping[str, Any], *inputs: Any) -> str:
    """The digest of what an evaluation was given: the draft without the records of the agent,
    and the evidence and the context it was judged on."""
    content = {name: value for name, value in draft.items() if name not in RECORDS}
    raw = json.dumps([content, *inputs], ensure_ascii=False, sort_keys=True)
    return sha256(raw.encode()).hexdigest()


def rejected(state: Mapping[str, Any], artifact: str, digest: str) -> bool:
    """Whether an evaluation of the artifact already came out weak on this content."""
    record = _record(state, artifact)
    return record is not None and digest in record.get("seen", ())


def refuse_identical(artifact: str, digest: str) -> AgentRefusal:
    return AgentRefusal(
        IDENTICAL_RESUBMISSION,
        IDENTICAL_MESSAGE,
        [{"target": artifact, "content_digest": digest}],
    )


# --- What the agent registers -----------------------------------------------------------------


def check_actions(actions: Sequence[LineageAction], allowed: Mapping[str, Sequence[str]]) -> None:
    """Refuse an action outside the set of its target, or a revision that registers none while a
    claim was weak. `allowed` holds the weak claims and their sets: a target missing from it is
    no weak claim, and an empty set allows nothing."""
    outside = [
        {"target": item.target, "action": item.action, "allowed": list(allowed.get(item.target, ()))}
        for item in actions
        if item.action not in allowed.get(item.target, ())
    ]
    if outside:
        raise AgentRefusal(ACTION_NOT_ALLOWED, NOT_ALLOWED_MESSAGE, outside)
    if allowed and not actions:
        raise AgentRefusal(
            ACTION_NOT_REGISTERED,
            NOT_REGISTERED_MESSAGE,
            [{"target": claim, "allowed": list(found)} for claim, found in allowed.items()],
        )


def check_research(
    actions: Sequence[LineageAction], allowed: Mapping[str, Sequence[str]], claim: str
) -> None:
    """Refuse a reservation of research whose registration is outside the set, or that does not
    register research for the claim it researches."""
    check_actions(actions, allowed)
    if not any(item.target == claim and item.action == RESEARCH for item in actions):
        raise AgentRefusal(
            ACTION_NOT_REGISTERED,
            NOT_REGISTERED_MESSAGE,
            [{"target": claim, "required": RESEARCH, "allowed": list(allowed.get(claim, ()))}],
        )


def not_researchable(target: str, allowed: Sequence[str]) -> AgentRefusal:
    """The refusal of a research the set of the claim does not hold: its actions are the
    agent's own, and a person is not called for them."""
    return AgentRefusal(
        ACTION_NOT_ALLOWED,
        NOT_ALLOWED_MESSAGE,
        [{"target": target, "action": RESEARCH, "allowed": list(allowed)}],
    )


# --- Settling a revision ----------------------------------------------------------------------


def evaluate(
    state: dict[str, Any],
    artifact: str,
    content: str,
    *,
    weak: Mapping[str, Sequence[str]] | None,
    acted: bool,
    governed: bool,
    attempt: Mapping[str, Any],
    on_purpose: Collection[str] = (),
    adjudicated: str | None = None,
) -> None:
    """Refuse a new revision the budget of the artifact does not let through, then record it.

    `content` is the digest of the revision, and `weak` what the agent may do with each weak claim
    the Jev left in it (its `next_actions`), or None when the Jev did not judge every claim of it in
    real mode: a mock, a fixture or a verification that leaves a claim out counts nothing, and it
    replaces nothing a person was called for either.
    `acted` says whether a person acted since the previous revision, `governed` whether something
    other than a registration answers for this one (the research cycle that registered its own, an
    adjudication, a grant), `attempt` what the evaluation is (`{kind, actions}`), and `on_purpose`
    the claims the same content judged again as part of the evaluation it completes (none when it
    judged a claim again any other way). Each claim comes back that way once per evaluation, and
    none after the third rejection.
    `adjudicated` is the claim a bounded adjudication judged on the previous revision: its repair
    is the one revision the third rejection lets through, and only while that claim is the only
    weak one left (`alone`).

    Every check is made against the last evaluation the record holds, never against a revision
    the Jev did not judge in between. Raises the refusal, and nothing is recorded: any revision
    once the count reached the limit of every weak claim left (a claim a person extended has its
    own), the content already rejected, an action outside the set, or none registered. A revision
    an extension lets through keeps the other claims a person holds in the record, whatever it
    judged them.
    """
    if acted:
        reset(state, artifact)
    record = _record(state, artifact)
    if record is None or record.get("last") is None:
        # The first evaluation opens the record and makes no rejection.
        if weak is not None:
            _settle(_stored(state, artifact), state, content, weak, None)
        return
    left = counted(record.get("weak", {}))
    if left and left != [adjudicated] and all(_spent(record, claim) for claim in left):
        # The weak claims are a person's: no revision of the agent replaces them, judged or not.
        raise GrillError(EXHAUSTED, EXHAUSTED_MESSAGE)
    # The claims a person holds stay theirs, whatever the revision an extension let through says.
    kept = [claim for claim in left if claim != adjudicated and _spent(record, claim)]
    same = content == record.get("last")
    again = set(record.get("rejudged", ()))
    if weak is not None and same and on_purpose and again.isdisjoint(on_purpose):
        # Part of the evaluation that counted already: it completes what that one left, once a claim.
        _settle(record, state, content, weak, None, kept)
        record["rejudged"] = sorted(again.union(on_purpose))
        return
    if weak is None:
        record["revision"] = state.get("revision", 0)
        return
    if rejected(state, artifact, content):
        raise refuse_identical(artifact, content)
    if not (acted or governed):
        actions = [LineageAction.model_validate(item) for item in attempt.get("actions", [])]
        # A claim whose limit the count reached has no action left for the agent.
        owed = {
            claim: found
            for claim, found in record.get("weak", {}).items()
            if not _spent(record, claim)
        }
        check_actions(actions, pending(owed))
    # The same content judged again brings nothing new: kept, not counted.
    _settle(record, state, content, weak, None if same else attempt, kept)


def _sets(weak: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    return {claim: list(found) for claim, found in weak.items()}


def _settle(
    record: dict[str, Any],
    state: Mapping[str, Any],
    digest: str,
    weak: Mapping[str, Sequence[str]],
    attempt: Mapping[str, Any] | None,
    kept: Sequence[str] = (),
) -> None:
    """Record an evaluation: its content and the weak claims it left, which the next revision
    answers to, with the claims a person holds (`kept`) whatever it judged them. A weak content
    stays rejected, and with an `attempt` it is also a rejection; a claim kept for the person is
    no weakness of this evaluation."""
    if digest != record.get("last"):
        record["rejudged"] = []
    held_sets = {claim: list(record["weak"][claim]) for claim in kept if claim not in weak}
    record.update(
        last=digest, weak={**_sets(weak), **held_sets}, revision=state.get("revision", 0)
    )
    if not counted(weak):
        return
    if digest not in record["seen"]:
        record["seen"].append(digest)
    if attempt is not None:
        record["rejections"] += 1
        record["attempts"].append(dict(attempt))


def reset(state: dict[str, Any], artifact: str) -> None:
    """A person acted: the count starts over, and so do what it held as rejected, the weak claims
    the agent answered to, the claims judged again on purpose and the cycles a person had
    authorized on top of the limit."""
    record = _record(state, artifact)
    if record is not None:
        record.update(rejections=0, ceiling={}, attempts=[], seen=[], weak={}, rejudged=[])


def extend(state: dict[str, Any], artifact: str, claim: str, cycles: int) -> None:
    """A person authorized more cycles for a claim: it goes back to them when the count grows by
    that many from where it stands, or from the limit of the claim not reached yet. The cycles are
    that claim's only, until the count starts over."""
    record = _stored(state, artifact)
    ceiling = _ceiling(record)
    start = max(int(record.get("rejections", 0)), ceiling.get(claim, LIMIT))
    record["ceiling"] = {**ceiling, claim: start + cycles}


def _stored(state: dict[str, Any], artifact: str) -> dict[str, Any]:
    """The record of the artifact in the run state, created empty the first time."""
    records = state.setdefault(STATE_KEY, {})
    record: dict[str, Any] = records.setdefault(
        artifact,
        {
            "last": None,
            "weak": {},
            "revision": 0,
            "rejections": 0,
            "ceiling": {},
            "attempts": [],
            "seen": [],
            "rejudged": [],
        },
    )
    return record
