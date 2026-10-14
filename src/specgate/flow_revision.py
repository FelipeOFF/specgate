"""A harness that revises: what it is offered when a text is refused, and what the flow checks.

A typed refusal (`action=agent_revision`) is the agent's to answer, not a person's. The flow
asks the harness, when it has this capability, to choose an action inside the set the code
allows (`next_actions`, or the `allowed` of an action refusal), to say why, and to bring the
content the action needs: the reformulated question or the parts of a division for the grill,
the revised spec with its lineage for the spec, the revised graph for the tickets. Nothing
else is asked: a research request comes from the harness callback that plans research, a
contradiction the agent accepts is corrected through the repair callbacks of the stage, and a
new question for the grill comes from the callback that lists the questions.

`FlowHarness` is a public protocol and gains no required method. The capability is a protocol
of its own, found with `isinstance`, so a harness written before it keeps working: the flow
then stops on the typed result, with its findings, as it always did.

This module holds the shape of the choice and the pure rules the flow checks it by. It calls
nothing: the flow sends the revision, and the host decides whether it holds.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, get_args, runtime_checkable

from specgate.grill_contracts import GrillQuestion
from specgate.lineage_budget import (
    NOT_ALLOWED_MESSAGE,
    NOT_REGISTERED_MESSAGE,
    Action,
    LineageAction,
)
from specgate.spec_contracts import SpecDraft
from specgate.ticket_contracts import TicketPlan
from specgate.wording import (
    ACTION_NOT_ALLOWED,
    ACTION_NOT_REGISTERED,
    AGENT_CODES,
    AGENT_REVISION,
    INVALID_LINEAGE,
    AgentRefusal,
)

Stage = Literal["grill", "spec", "tickets"]
ACTIONS: tuple[str, ...] = get_args(Action)
# A text the code refused and named no action for is revised: the agent rewrites or divides it.
REVISE: tuple[str, ...] = ("reformulate", "split")
# At most ten parts, as the host takes a division.
MAX_PARTS = 10
# Consecutive refusals of the revisions of one step before the flow hands the typed result back.
REVISION_LIMIT = 6
# The times the agent sends the grill a new question for one stage in a run: each one is accepted by
# the host, so nothing it counts ends a loop that comes back to the same text.
REOPEN_LIMIT = 3
PARTS_MESSAGE = (
    "A pergunta recusada não foi gravada, então a reformulação e as partes da divisão são perguntas "
    "novas, perguntadas uma a uma: cada uma precisa de um id que nenhuma outra parte e nenhuma outra "
    "pergunta do grill tenha."
)


@dataclass(frozen=True)
class Revision:
    """What the agent chose when a text was refused or weak: the action, why, and its content.

    `reformulate` and `split` carry the revised text of the stage in the field of the stage:
    `question` (a division adds the other parts in `siblings`), `spec` or `tickets`. The other
    actions carry nothing. `target` names the claim or the text the action answers, as the
    refusal names it; without it the action answers every target whose set holds it.
    """

    action: Action
    reason: str
    target: str | None = None
    question: GrillQuestion | None = None
    siblings: tuple[GrillQuestion, ...] = ()
    spec: SpecDraft | None = None
    tickets: TicketPlan | None = None

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"Ação desconhecida: {self.action}.")
        if not self.reason.strip():
            raise ValueError("Registre o motivo da ação escolhida.")
        given = [
            name
            for name in ("question", "spec", "tickets")
            if getattr(self, name) is not None
        ]
        if self.action not in REVISE:
            if given or self.siblings:
                raise ValueError(f"A ação {self.action} não traz conteúdo.")
            return
        if len(given) != 1:
            raise ValueError(
                f"A ação {self.action} traz o texto revisado: a pergunta, a spec ou os tickets."
            )
        if self.siblings and (self.question is None or self.action != "split"):
            raise ValueError("Só a divisão da pergunta traz as outras partes em siblings.")
        if self.question is not None and self.action == "split" and not self.siblings:
            raise ValueError("A divisão da pergunta traz duas ou mais partes.")
        if len(self.siblings) + 1 > MAX_PARTS:
            raise ValueError(f"Uma divisão tem no máximo {MAX_PARTS} partes.")

    @property
    def parts(self) -> list[GrillQuestion]:
        """The questions a reformulation or a division sends, the first one first."""
        return [self.question, *self.siblings] if self.question is not None else []


@runtime_checkable
class RevisingHarness(Protocol):
    """The optional capability of a harness that answers a typed refusal on its own."""

    async def revise(
        self,
        stage: Stage,
        refusal_or_weak: dict[str, Any],
        allowed: Mapping[str, Sequence[str]],
    ) -> Revision:
        """Choose one of the actions `allowed` holds (by target), say why, and bring the content.

        `refusal_or_weak` is the typed result the stage ended on, with its findings, or the
        reading of a weak question. The flow refuses an action outside `allowed` before it calls
        the host.
        """
        ...


def is_refusal(result: Mapping[str, Any]) -> bool:
    """Whether a result is the typed refusal of a text the agent wrote, never a person's."""
    error = result.get("error")
    return (
        result.get("action") == AGENT_REVISION
        and isinstance(error, Mapping)
        and error.get("code") in AGENT_CODES
    )


def allowed_of(result: Mapping[str, Any]) -> dict[str, list[str]]:
    """The actions the code allows, by target, as the typed refusal names them.

    A finding names its set in `next_actions` (the checks) or in `allowed` (a refused action,
    which can be empty: nothing is the agent's). What is not an action of the vocabulary
    (`revise_parts`) names none, and a finding with no set (an identical resubmission, a lineage
    that does not hold) is a text to revise.
    """
    error = result.get("error")
    findings = error.get("findings") if isinstance(error, Mapping) else None
    sets: dict[str, list[str]] = {}
    for finding in findings or ():
        if not isinstance(finding, Mapping):
            continue
        target = str(
            finding.get("target")
            or finding.get("original")
            or finding.get("part")
            or finding.get("question")
            or "*"
        )
        if "allowed" in finding:
            found = [item for item in finding["allowed"] if item in ACTIONS]
        else:
            named = [item for item in finding.get("next_actions", ()) if item in ACTIONS]
            found = named or list(REVISE)
        sets[target] = list(dict.fromkeys([*sets.get(target, []), *found]))
    return sets or {"*": list(REVISE)}


def refuse_choice(revision: Revision, allowed: Mapping[str, Sequence[str]]) -> AgentRefusal | None:
    """The refusal of an action outside the set, made before the host is called; None if it holds."""
    pool = (
        {revision.target: allowed.get(revision.target, ())}
        if revision.target is not None
        else dict(allowed)
    )
    if any(revision.action in actions for actions in pool.values()):
        return None
    return AgentRefusal(
        ACTION_NOT_ALLOWED,
        NOT_ALLOWED_MESSAGE,
        [
            {"target": target, "action": revision.action, "allowed": list(actions)}
            for target, actions in (pool or {"*": ()}).items()
        ],
    )


def refuse_content(revision: Revision, stage: Stage) -> AgentRefusal | None:
    """The refusal of a revision that brought no text for the stage it answers, or that accepts a
    contradiction where no claim exists: the grill judges questions, and contradicts none."""
    if revision.action == "accept_contradiction" and stage == "grill":
        return AgentRefusal(
            ACTION_NOT_ALLOWED,
            NOT_ALLOWED_MESSAGE,
            [{"target": revision.target or "*", "action": revision.action, "allowed": []}],
        )
    held = {"grill": revision.question, "spec": revision.spec, "tickets": revision.tickets}
    if revision.action not in REVISE or held[stage] is not None:
        return None
    return AgentRefusal(
        ACTION_NOT_REGISTERED,
        NOT_REGISTERED_MESSAGE,
        [{"target": revision.target or "*", "required": stage, "action": revision.action}],
    )


def refuse_parts(revision: Revision, taken: Collection[str]) -> AgentRefusal | None:
    """The refusal of the parts of a question the grill stored nowhere that repeat an id: among
    themselves, or one of `taken`, the ids the other questions of the grill have. None if none does.

    Those parts are new questions, asked one at a time, so the host never sees them together: a
    repeated id would reach it as that question asked again with no new evidence, which is a
    person's to decide. The problems are the ones the host names for the parts of a lineage.
    """
    names = [part.id for part in revision.parts]
    problems = [
        {"part": name, "problem": "duplicate_part"}
        for name in dict.fromkeys(names)
        if names.count(name) > 1
    ]
    problems += [
        {"part": name, "problem": "id_in_use"} for name in dict.fromkeys(names) if name in taken
    ]
    return AgentRefusal(INVALID_LINEAGE, PARTS_MESSAGE, problems) if problems else None


def without_reopening(allowed: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """The set without the new grill question: the host takes none while a question waits for a
    person or a cycle of research is open, so the agent is not offered what the host refuses."""
    return {
        target: [item for item in actions if item != "new_grill_question"]
        for target, actions in allowed.items()
    }


def refuse_reopening(revision: Revision) -> AgentRefusal:
    """The refusal of one more new question for a stage after `REOPEN_LIMIT`, made before the host."""
    return AgentRefusal(
        ACTION_NOT_ALLOWED,
        NOT_ALLOWED_MESSAGE,
        [
            {
                "target": revision.target or "*",
                "action": revision.action,
                "allowed": [],
                "problem": "reopen_limit",
            }
        ],
    )


def targets(weak: Mapping[str, Sequence[str]], revision: Revision) -> list[str]:
    """The weak claims of the stored artifact the action answers: the ones whose set holds it,
    and the one the revision names, as the claim or as the text the refusal named."""
    found = [claim for claim, actions in weak.items() if revision.action in actions]
    if revision.target is None:
        return found
    return [
        claim for claim in found if revision.target in {claim, claim.partition(":")[2]}
    ]


def own_actions(
    carried: Sequence[LineageAction], stored: Sequence[LineageAction]
) -> list[LineageAction]:
    """What a draft declares for this revision: the actions it carries beyond the ones the stored
    artifact already holds. Those were registered for an earlier revision and name claims that may
    not be weak any more, so a draft the agent built from the stored one does not declare them."""
    return [item for item in carried if item not in stored]


def registered(
    drafted: Sequence[LineageAction], weak: Mapping[str, Sequence[str]], revision: Revision
) -> list[LineageAction]:
    """What the revision registers for the host: the actions the draft already carries, or the
    action and the reason the agent gave for each weak claim it answers. A stored artifact with
    no weak claim has nothing to register, and a refused first draft stored nothing."""
    if drafted:
        return list(drafted)
    return [
        LineageAction(target=claim, action=revision.action, reason=revision.reason)
        for claim in targets(weak, revision)
    ]


def report(
    stage: Stage, revision: Revision, code: str, *, rejected: bool = False
) -> dict[str, Any]:
    """What the run reports of a choice: the stage, the action, why, and the refusal it answered."""
    return {
        "stage": stage,
        "action": revision.action,
        "reason": revision.reason,
        "answers": code,
        **({"target": revision.target} if revision.target else {}),
        **({"refused": True} if rejected else {}),
    }
