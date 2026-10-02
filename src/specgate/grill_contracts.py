"""Portable grill inputs; persistence and evaluation belong to the private host."""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from specgate.context import ContextPacket
from specgate.shared.domain.inputs import Item
from specgate.slices.decide import QuestionType


class GrillQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=8000)
    options: list[Item] = Field(max_length=48)
    context: ContextPacket
    question_type: QuestionType = "single_choice"
    requires_authorization: bool = Field(default=False, strict=True)
    missing_personal_fact: bool = Field(default=False, strict=True)


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
