"""Harness flow: explicit sources -> context gate -> authenticated MCP."""

import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Literal

from mcp.shared.dispatcher import ProgressFnT
from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field

from specgate.context import (
    ContextPacket,
    ContextResult,
    assess_context,
    build_context,
    discover_skills,
)
from specgate.gate_policy import GatePolicy, candidate_eligible, recipe_versions
from specgate.recipe_inputs import request_revision
from specgate.shared.domain.decisions import question_revision
from specgate.shared.domain.inputs import Item, checked_text, closed_gate_review
from specgate.transport import MCPTransportError, call_tool


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    objective: str = Field(min_length=1, max_length=8000)
    tool: Literal["jev_verify", "jev_screen", "jev_find", "jev_decide"]
    arguments: dict[str, Any]
    sources: list[str] = Field(default_factory=list, max_length=50)
    required: list[str] = Field(default_factory=list, max_length=50)
    source_rounds: list[list[str]] = Field(default_factory=list, max_length=2)
    artifact: str = ""
    skill_roots: list[str] = Field(default_factory=list, max_length=10)
    gaps: list[str] = Field(default_factory=list, max_length=50)
    conflicts: list[str] = Field(default_factory=list, max_length=50)


class ContextUpdate(BaseModel):
    """Additional authorized sources and the harness's remaining known issues.

    Omitted gaps/conflicts preserve the previous assessment. Empty lists explicitly
    resolve it; new evidence is still required before another evaluation.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    sources: list[str] = Field(default_factory=list, max_length=50)
    gaps: list[str] | None = Field(default=None, max_length=50)
    conflicts: list[str] | None = Field(default=None, max_length=50)


def _human_question(packet: ContextPacket) -> str:
    issues = [*packet.gaps, *packet.conflicts]
    issues.extend(f"Fonte não examinada: {source}" for source in packet.unexamined)
    if issues:
        return "Quais evidências resolvem estas pendências? " + "; ".join(issues)
    return f"Você confirma a avaliação para o objetivo: {packet.objective}?"


def _review_reason(decision: dict[str, Any], arguments: dict[str, Any]) -> str:
    selected = decision.get("selected_option")
    label = next(
        (
            item.get("text")
            for item in arguments.get("options") or []
            if isinstance(item, dict) and item.get("id") == selected
        ),
        None,
    )
    gate = decision.get("gate")
    detail = gate.get("reason") if isinstance(gate, dict) else None
    if not isinstance(detail, str):
        fallback = decision.get("reason")
        detail = fallback if isinstance(fallback, str) else None
    named = closed_gate_review(
        selected, label if isinstance(label, str) else None, detail
    )
    if named:
        return named
    return "A avaliação exige revisão humana; consulte a política do gate."


async def review_request(
    request: ReviewRequest,
    project: Path,
    url: str,
    token: str,
    *,
    request_context: Callable[[ContextResult], Awaitable[ContextUpdate]] | None = None,
    transport: str = "streamable",
    ca_file: Path | None = None,
    timeout_seconds: float = 30,
    progress_callback: ProgressFnT | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Collect adaptively, then return an evaluation bound to its context revision.

    The harness callback receives the previous round and can supply sources that
    were not known when the request started. It runs at most twice. Callers must
    re-evaluate if the returned context revision changes after this call.
    """
    original_request = request.model_copy(deep=True)
    original_tool = original_request.tool
    original_arguments = original_request.arguments
    arguments = deepcopy(original_arguments)
    if original_tool == "jev_find" and request.skill_roots:
        arguments["candidates"] = discover_skills(
            [Path(root).expanduser() for root in request.skill_roots]
        )
    alternatives = {
        "jev_verify": ["supports", "contradicts", "unsupported"],
        "jev_screen": ["pass", "review", "block", "skip"],
        "jev_find": ["answered", "partial", "absent"],
        "jev_decide": [
            Item.model_validate(item).text for item in arguments.get("options", [])
        ]
        or ["Revisão humana"],
    }[original_tool]

    sources = list(request.sources)
    gaps, conflicts = list(request.gaps), list(request.conflicts)
    default_artifact = json.dumps(arguments, ensure_ascii=False)

    def collect() -> ContextPacket:
        return build_context(
            project,
            request.objective,
            list(dict.fromkeys(sources)),
            request.required,
            artifact=request.artifact
            or (
                default_artifact
                if request.arguments == original_arguments
                else json.dumps(request.arguments, ensure_ascii=False)
            ),
            alternatives=alternatives,
            gaps=gaps,
            conflicts=conflicts,
        )

    async def evaluate(context: ContextResult) -> dict[str, Any]:
        # These messages are returned to the user, not written to diagnostic logs.
        result = {
            "action": context.action,
            "reason": context.reason,
            "rounds": context.rounds,
            "context": asdict(context.packet),
            "context_revision": context.packet.revision,
            "auto_advance": False,
            "question": _human_question(context.packet),
        }
        if request != original_request:
            return {
                **result,
                "action": "needs_human",
                "reason": "O pedido mudou durante a coleta; solicite uma nova avaliação.",
                "error": {
                    "code": "request_changed",
                    "message": "O pedido mudou durante a coleta.",
                },
            }
        if context.action == "needs_human":
            return result
        if original_tool == "jev_verify":
            arguments["evidence"] = "\n\n".join(
                f"Fonte: {e.source}\n{e.text}" for e in context.packet.evidence
            )
        arguments["context"] = asdict(context.packet)
        try:
            checked_text(json.dumps(arguments, ensure_ascii=False, allow_nan=False))
        except ValueError:
            reason = "O contexto excede o orçamento do piloto; selecione fontes menores sem omitir evidências necessárias."
            packet = replace(context.packet, gaps=(*context.packet.gaps, reason))
            return {
                **result,
                "action": "needs_human",
                "reason": reason,
                "context": asdict(packet),
                "context_revision": packet.revision,
                "question": _human_question(packet),
            }
        try:
            response = await call_tool(
                url,
                token,
                original_tool,
                arguments,
                transport=transport,
                ca_file=ca_file,
                timeout_seconds=timeout_seconds,
                progress_callback=progress_callback,
                project_id=project_id,
            )
        except MCPTransportError as error:
            failure = {"code": error.code, "message": str(error)}
            if fallback := getattr(error, "fallback", None):
                failure["fallback"] = fallback
            return {
                **result,
                "action": "needs_human",
                "reason": str(error),
                "error": failure,
            }
        if (
            not isinstance(response, CallToolResult)
            or response.is_error
            or response.structured_content is None
        ):
            raise ValueError(
                "O MCP não retornou uma decisão válida; confira o pedido e as evidências."
            )
        decision = response.structured_content
        automatic = decision.get("action") == "auto"
        valid_policy = (
            decision.get("mode") in ("mock", "real")
            and decision.get("auto_advance") is False
            and decision.get("calibrated") is False
            and decision.get("action")
            in (
                {"review", "collect", "error"}
                if original_tool == "jev_decide"
                else {"review"}
            )
        )
        if automatic:
            try:
                gate = decision["gate"]
                policy = GatePolicy.model_validate(gate["policy"])
                binding = gate.get("binding")
                exam_bound = bool(
                    gate.get("artifact_revision")
                    and gate.get("report_sha256")
                    and isinstance(binding, dict)
                    and binding.get("tool") == original_tool
                    and binding.get("policy") == policy.revision
                    and binding.get("recipe") == recipe_versions()[original_tool]
                )
                confidence_bound = bool(
                    original_tool == "jev_decide"
                    and gate.get("reason") == "confidence_policy_satisfied"
                    and decision.get("reason") == "confidence_policy_satisfied"
                    and not gate.get("artifact_revision")
                    and not gate.get("report_sha256")
                    and binding is None
                )
                valid_policy = bool(
                    decision.get("mode") == "real"
                    and decision.get("calibrated") is True
                    and decision.get("auto_advance") is True
                    and gate["passed"] is True
                    and gate["scope"] == "recommendation"
                    and gate["execution_authorized"] is False
                    and (exam_bound or confidence_bound)
                    and decision.get("tool") == original_tool
                    and decision.get("context_revision") == context.packet.revision
                    and decision.get("request_revision")
                    == request_revision(original_tool, arguments)
                    and not arguments.get("requires_authorization")
                    and not arguments.get("missing_personal_fact")
                    and arguments.get("question_type", "single_choice")
                    == "single_choice"
                    and candidate_eligible(
                        original_tool,
                        {
                            **decision,
                            "action": "review",
                            "reason": "real_calibration_pending",
                        },
                        policy,
                    )
                )
            except (KeyError, TypeError, ValueError):
                valid_policy = False
        if not valid_policy:
            raise ValueError(
                "O MCP retornou uma política de decisão inválida; solicite revisão humana."
            )
        current = collect()
        if request != original_request:
            current = replace(
                current,
                gaps=(
                    *current.gaps,
                    "O pedido mudou durante a avaliação.",
                ),
            )
        if current.revision != context.packet.revision:
            return {
                **result,
                "action": "needs_human",
                "reason": "O contexto mudou durante a avaliação; solicite uma nova decisão.",
                "context": asdict(current),
                "context_revision": current.revision,
                "decision_revision": context.packet.revision,
                "question": "O artefato ou as evidências mudaram; qual revisão deve ser avaliada?",
            }
        if original_tool == "jev_decide":
            options = [
                Item.model_validate(item) for item in arguments.get("options", [])
            ]
            expected_revision = question_revision(
                arguments["question"],
                options,
                question_type=arguments.get("question_type", "single_choice"),
                requires_authorization=arguments.get("requires_authorization", False),
                missing_personal_fact=arguments.get("missing_personal_fact", False),
            )
            probabilities = decision.get("probabilities")
            option_ids = {item.id for item in options}
            selected = decision.get("selected_option")
            if (
                decision.get("tool") != original_tool
                or decision.get("context_revision") != context.packet.revision
                or decision.get("question_revision") != expected_revision
                or not isinstance(probabilities, dict)
                or (bool(probabilities) and probabilities.keys() != option_ids)
                or (
                    selected is not None
                    and (
                        not isinstance(selected, str)
                        or selected not in option_ids
                        or not probabilities
                    )
                )
            ):
                return {
                    **result,
                    "action": "needs_human",
                    "reason": "A resposta não corresponde ao pedido enviado; solicite uma nova avaliação.",
                    "error": {
                        "code": "decision_mismatch",
                        "message": "Revisão da pergunta, contexto ou opções incompatível.",
                    },
                }
        if decision["action"] == "error":
            return {
                **result,
                "action": "needs_human",
                "reason": "A avaliação falhou; solicite revisão humana.",
                "decision": decision,
                "error": {
                    "code": "decision_error",
                    "message": "Não foi possível obter uma decisão válida.",
                },
            }
        return {
            **result,
            "action": "auto" if automatic else "needs_human",
            "auto_advance": automatic,
            "origin": "automated" if automatic else "review",
            "execution_authorized": False,
            "question": None if automatic else result["question"],
            "reason": "Recomendação aceita pela política calibrada; não autoriza efeitos externos."
            if automatic
            else _review_reason(decision, arguments),
            "decision": decision,
            "decision_revision": context.packet.revision,
        }

    revisions: set[str] = set()
    evaluations: list[dict[str, Any]] = []
    semantic_gap: str | None = None
    for round_number in range(1, 4):
        packet = collect()
        if request != original_request:
            context = ContextResult(
                packet, round_number, "needs_human", "O pedido mudou durante a coleta."
            )
        elif (
            semantic_gap and not {item.revision for item in packet.evidence} - revisions
        ):
            context = ContextResult(
                replace(packet, gaps=(*packet.gaps, semantic_gap)),
                round_number,
                "needs_human",
                "A rodada não trouxe nenhum conteúdo novo.",
            )
        else:
            context = assess_context(packet, round_number, revisions)
        if context.action == "evaluate":
            result = await evaluate(context)
            decision = result.get("decision", {})
            if original_tool != "jev_decide" or decision.get("action") != "collect":
                return (
                    {
                        **result,
                        "evaluations": [
                            *evaluations,
                            *([decision] if decision else []),
                        ],
                    }
                    if original_tool == "jev_decide"
                    else result
                )
            evaluations.append(decision)
            semantic_gap = (
                f"Contexto insuficiente para decidir: {arguments['question']}"
            )
            context = assess_context(
                replace(packet, gaps=(*packet.gaps, semantic_gap)),
                round_number,
                revisions,
            )
        if context.action == "needs_human":
            result = await evaluate(context)
            return (
                {**result, "evaluations": evaluations}
                if original_tool == "jev_decide"
                else result
            )
        if round_number <= len(request.source_rounds):
            sources.extend(request.source_rounds[round_number - 1])
        if request_context is not None:
            update = await request_context(context)
            sources.extend(update.sources)
            if update.gaps is not None:
                gaps = update.gaps
            if update.conflicts is not None:
                conflicts = update.conflicts
    raise AssertionError("Context collection exceeded its round limit")
