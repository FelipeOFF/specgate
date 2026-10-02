"""Controlled Grok Build ACP client for explicit skills and structured questions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from specgate.codex_client import DecisionFn, mcp_question_decider
from specgate.cursor_client import (
    ControlledCursorTurn,
    CursorControlledClient,
    CursorOption,
    CursorProtocolError,
    CursorQuestion,
    CursorQuestionRequest,
    CursorSkill,
)
from specgate.routing import HumanReview, confirm_skill_loaded

ASK_USER_QUESTION_METHOD = "x.ai/ask_user_question"
VERIFIED_QUESTION_AGENT_VERSIONS = frozenset({"1.0.24"})


@dataclass(frozen=True)
class GrokBuildSkill(CursorSkill):
    """One skill whose exact revision was approved by the router."""


@dataclass(frozen=True)
class GrokBuildOption(CursorOption):
    description: str | None = None


def grok_build_skill_from_indication(
    indication: dict[str, Any],
    skill_id: str,
    revision: str,
    *,
    authorized_roots: list[Path],
    disabled_ids: Sequence[str] = (),
    review: HumanReview | None = None,
) -> GrokBuildSkill:
    """Recheck and load the exact skill revision reviewed by routing.

    Grok Build has no native hook: outside a validated manifest the skill loads only with the
    `review` of the person who chose this exact revision.
    """
    confirmed = confirm_skill_loaded(
        indication,
        skill_id,
        revision,
        authorized_roots=authorized_roots,
        disabled_ids=disabled_ids,
        review=review,
    )
    candidate = confirmed.get("candidate")
    if not isinstance(candidate, dict) or candidate.get("loaded") is not True:
        if isinstance(confirmed.get("loading"), dict):
            raise ValueError(
                "Loading this indication needs a native hook or a human review of "
                "this exact revision, and Grok Build has no hook; pass the review of the "
                "person who chose it."
            )
        raise ValueError("The selected skill does not match the reviewed revision.")
    instructions = candidate.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("The selected skill has no reviewed instructions.")
    return GrokBuildSkill(skill_id, revision, instructions)


class GrokBuildControlledClient(CursorControlledClient):
    """Own one ephemeral ``grok agent stdio`` process and its question channel."""

    question_method: ClassVar[str] = ASK_USER_QUESTION_METHOD
    auth_method: ClassVar[str] = "cached_token"
    client_mode: ClassVar[str] = "controlled_grok_build_acp"
    question_evidence: ClassVar[str] = "official_contract_protocol_double"
    review_methods: ClassVar[set[str]] = {
        "session/request_permission",
        "x.ai/plan_review",
    }

    def __init__(
        self,
        *,
        decide: DecisionFn,
        command: Sequence[str] = ("grok", "agent", "stdio"),
        timeout_seconds: float = 30,
        authenticate: bool = True,
    ) -> None:
        super().__init__(
            decide=decide,
            command=command,
            timeout_seconds=timeout_seconds,
            authenticate=authenticate,
        )

    async def start(self) -> None:
        """Negotiate ACP and require a runtime verified for native questions."""
        await super().start()
        agent_version = self.diagnostic.get("agent_version")
        if agent_version not in VERIFIED_QUESTION_AGENT_VERSIONS:
            await self.close()
            raise CursorProtocolError(
                "The Grok Build runtime does not expose a verified "
                "x.ai/ask_user_question contract."
            )
        self.diagnostic["structured_question_agent_version"] = agent_version

    def _parse_question(self, message: dict[str, Any]) -> CursorQuestionRequest:
        params = message.get("params")
        if not isinstance(params, dict):
            raise CursorProtocolError("Invalid x.ai/ask_user_question parameters.")
        tool_call_id = params.get("toolCallId")
        raw_questions = params.get("questions")
        if not isinstance(tool_call_id, str) or not tool_call_id.strip():
            raise CursorProtocolError("Invalid Grok Build tool call ID.")
        if not isinstance(raw_questions, list) or not raw_questions:
            raise CursorProtocolError("Grok Build question list is empty.")
        questions: list[CursorQuestion] = []
        question_ids: set[str] = set()
        prompts: set[str] = set()
        for question_index, raw in enumerate(raw_questions):
            if not isinstance(raw, dict):
                raise CursorProtocolError("Invalid Grok Build question.")
            raw_question_id = raw.get("id")
            question_id = (
                raw_question_id
                if isinstance(raw_question_id, str) and raw_question_id.strip()
                else f"question_{question_index}"
            )
            prompt = raw.get("question")
            raw_options = raw.get("options")
            if (
                raw_question_id is not None
                and (
                    not isinstance(raw_question_id, str)
                    or not raw_question_id.strip()
                )
            ) or (
                question_id in question_ids
                or not isinstance(prompt, str)
                or not prompt.strip()
                or prompt in prompts
                or not isinstance(raw_options, list)
            ):
                raise CursorProtocolError(
                    "Invalid or duplicate Grok Build question."
                )
            options: list[GrokBuildOption] = []
            option_ids: set[str] = set()
            option_labels: set[str] = set()
            for option_index, raw_option in enumerate(raw_options):
                if not isinstance(raw_option, dict):
                    raise CursorProtocolError("Invalid Grok Build question option.")
                raw_option_id = raw_option.get("id")
                option_id = (
                    raw_option_id
                    if isinstance(raw_option_id, str) and raw_option_id.strip()
                    else f"option_{option_index}"
                )
                label = raw_option.get("label")
                description = raw_option.get("description")
                if (
                    raw_option_id is not None
                    and (
                        not isinstance(raw_option_id, str)
                        or not raw_option_id.strip()
                    )
                ) or (
                    option_id in option_ids
                    or not isinstance(label, str)
                    or not label.strip()
                    or label in option_labels
                    or (
                        description is not None
                        and not isinstance(description, str)
                    )
                ):
                    raise CursorProtocolError(
                        "Invalid or duplicate Grok Build option."
                    )
                option_ids.add(option_id)
                option_labels.add(label)
                options.append(GrokBuildOption(option_id, label, description))
            question_ids.add(question_id)
            prompts.add(prompt)
            questions.append(
                CursorQuestion(
                    question_id,
                    prompt,
                    tuple(options),
                    raw.get("multiSelect") is True,
                )
            )
        mode = params.get("mode")
        if mode is not None and not isinstance(mode, str):
            raise CursorProtocolError("Invalid Grok Build question mode.")
        return CursorQuestionRequest(
            message["id"],
            tool_call_id,
            mode,
            tuple(questions),
        )

    def _decision_request(self, request: CursorQuestionRequest) -> dict[str, Any]:
        question = request.questions[0]
        return {
            "objective": question.prompt,
            "tool": "jev_decide",
            "arguments": {
                "question": question.prompt,
                "options": [
                    {
                        "id": option.id,
                        "text": (
                            f"{option.label}: {option.description}"
                            if isinstance(option, GrokBuildOption)
                            and option.description
                            else option.label
                        ),
                    }
                    for option in question.options
                ],
                "question_type": "single_choice",
            },
            "sources": [],
            "required": [],
            "artifact": question.prompt,
        }

    async def _respond(
        self,
        request: CursorQuestionRequest,
        answers: dict[str, list[str]],
        origin: Literal["automated", "human"],
    ) -> None:
        result: dict[str, list[str]] = {}
        for question in request.questions:
            by_id = {option.id: option.label for option in question.options}
            result[question.prompt] = [by_id[option_id] for option_id in answers[question.id]]
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": request.request_id,
                "result": {"outcome": "accepted", "answers": result},
            }
        )
        self._resolved_requests.add(request.request_id)
        self._question = None
        self._answer_origin = origin


GrokBuildProtocolError = CursorProtocolError
GrokBuildQuestionRequest = CursorQuestionRequest
ControlledGrokBuildTurn = ControlledCursorTurn

__all__ = [
    "ASK_USER_QUESTION_METHOD",
    "VERIFIED_QUESTION_AGENT_VERSIONS",
    "ControlledGrokBuildTurn",
    "GrokBuildControlledClient",
    "GrokBuildProtocolError",
    "GrokBuildQuestionRequest",
    "GrokBuildSkill",
    "grok_build_skill_from_indication",
    "mcp_question_decider",
]
