"""Portable grill inputs; persistence and evaluation belong to the private host."""

import json
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
