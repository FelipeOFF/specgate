"""Portable grill inputs; persistence and evaluation belong to the private host."""

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
)

from specgate.context import ContextPacket
from specgate.shared.domain.inputs import Item, checked_text
from specgate.slices.decide import QuestionType

QuestionLineageKind = Literal["reformulation", "split"]
# Why research cannot follow the evaluation of the active question (ADR 0037).
CONTEXT_INCOMPLETE = "context_incomplete"
EVALUATION_UNAVAILABLE = "evaluation_unavailable"
MOCK_EVALUATION = "mock_evaluation"
HUMAN_DECISION_REQUIRED = "human_decision_required"
# The actions the code allows for a question (`next_actions`), as the agent registers one.
QuestionActionName = Literal["research", "reformulate", "split", "new_grill_question"]


class QuestionLineage(BaseModel):
    """The agent's record that a question replaces one the grill already stored.

    `original` is the id of that question; `kind` says whether this question is its
    reformulation (one question, the same id or a new one) or one part of its division (two or
    more new questions, each pointing to the same original).
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    original: str = Field(min_length=1, max_length=200)
    kind: QuestionLineageKind


class QuestionAction(BaseModel):
    """What the agent registers before the new evaluation: the action it chose inside the set
    the code allows for the question, and why."""

    model_config = ConfigDict(extra="forbid", strict=True)
    action: QuestionActionName
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def written(cls, value: str) -> str:
        return checked_text(value)


class GrillQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=8000)
    options: list[Item] = Field(max_length=48)
    context: ContextPacket
    question_type: QuestionType = "single_choice"
    requires_authorization: bool = Field(default=False, strict=True)
    missing_personal_fact: bool = Field(default=False, strict=True)
    # The question this one reformulates or is a part of, and the action the agent chose and why.
    # Both are empty by default and left out of the dump when empty, so a question without a
    # lineage keeps the contract, and the stored turn, it always had.
    lineage: QuestionLineage | None = None
    lineage_action: QuestionAction | None = None

    @model_serializer(mode="wrap")
    def _without_empty_lineage(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for name in ("lineage", "lineage_action"):
            if data.get(name) is None:
                data.pop(name, None)
        return data


class HumanAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    selected_option: str | None = Field(default=None, max_length=200)
    text: str | None = Field(default=None, max_length=8000)
    authorization_granted: bool | None = None


class GrillError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def held_by_policy(evaluation: Mapping[str, Any]) -> bool:
    """Whether the policy held the evaluation before the Jev was called: the packet declared a
    gap, a conflict or an item not examined, or failed another readiness check."""
    return evaluation.get("origin") == "policy" and evaluation.get("action") == "collect"


def unresearchable(evaluation: Mapping[str, Any]) -> str | None:
    """The code that keeps research from following an evaluation, or None for the Jev's own.

    Only a provider that was called and failed is an unavailable evaluation. A packet the policy
    held is the agent's to repair and send again; the mock judges nothing; the policy answers for a
    question no judgment reaches (an authorization, a personal fact, another question type), and
    an evaluation of any other origin was not judged by the Jev either.
    """
    if evaluation.get("action") == "error":
        return EVALUATION_UNAVAILABLE
    origin = evaluation.get("origin")
    if origin == "jev":
        return None
    if held_by_policy(evaluation):
        return CONTEXT_INCOMPLETE
    if origin == "mock":
        return MOCK_EVALUATION
    return HUMAN_DECISION_REQUIRED


def context_findings(turn: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What held a turn before the Jev, as `packet_findings` names it for the stored packet."""
    question = turn.get("question") or {}
    reason = str((turn.get("evaluation") or {}).get("reason") or "")
    checks = reason.removeprefix("insufficient_context:").strip()
    return packet_findings(
        question.get("id"), question.get("context") or {}, checks.split("; ")
    )


def packet_findings(
    question_id: str | None, context: Mapping[str, Any], checks: Sequence[str]
) -> list[dict[str, Any]]:
    """The readiness checks a packet fails and the gaps, conflicts and items not examined it
    declares, never its evidence. No action of the agent's vocabulary repairs a packet, so the
    set it allows is empty."""
    return [
        {
            "question": question_id,
            "checks": [item for item in checks if item],
            "gaps": list(context.get("gaps") or ()),
            "conflicts": list(context.get("conflicts") or ()),
            "unexamined": list(context.get("unexamined") or ()),
            "allowed": [],
        }
    ]
