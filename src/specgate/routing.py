"""Recommend authorized skills through bounded MCP evaluations; never execute them."""

import json
from collections.abc import Awaitable, Callable, Collection, Iterator
from copy import deepcopy
from math import isclose
from pathlib import Path
from typing import Any

from mcp.shared.dispatcher import ProgressFnT

from specgate.client import ContextUpdate, ReviewRequest, review_request
from specgate.context import (
    ContextResult,
    SkillSource,
    discover_skill_catalog,
    read_skill,
)
from specgate.shared.domain.inputs import checked_text

# Leave room for the explicit project context within the 64 KB MCP payload limit.
MAX_CANDIDATE_BYTES = 16000


def _batches(skills: list[SkillSource]) -> Iterator[list[SkillSource]]:
    batch: list[SkillSource] = []
    size = 2
    for skill in skills:
        item_size = (
            len(
                json.dumps(
                    {"id": skill.id, "text": skill.text}, ensure_ascii=False
                ).encode()
            )
            + 2
        )
        if batch and (len(batch) == 50 or size + item_size > MAX_CANDIDATE_BYTES):
            yield batch
            batch, size = [], 2
        batch.append(skill)
        size += item_size
    if batch:
        yield batch


async def route_skills(
    request: ReviewRequest,
    project: Path,
    url: str,
    token: str,
    *,
    authorized_roots: list[Path],
    disabled_ids: Collection[str] = (),
    request_context: Callable[[ContextResult], Awaitable[ContextUpdate]] | None = None,
    transport: str = "streamable",
    ca_file: Path | None = None,
    timeout_seconds: float = 30,
    progress_callback: ProgressFnT | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Rank descriptions in batches, then screen and assess complete instructions.

    Each batch contributes its returned top candidates. Evaluate the next one when
    a candidate fails; once one fits, only description ties require another check.
    Coverage names skipped candidates. Only backend-approved evaluations may auto-select.
    """
    if request.tool != "jev_find":
        raise ValueError("O roteamento exige um pedido jev_find.")
    query = checked_text(request.arguments.get("query", ""))
    result: dict[str, Any] = {
        "action": "needs_human",
        "auto_advance": False,
        "execution_authorized": False,
        "status": "absent",
        "reason": "Nenhuma skill adequada foi encontrada.",
        "candidate": None,
        "evaluations": [],
        "coverage": {
            "discovered": [],
            "disabled": [],
            "descriptions_examined": [],
            "instructions_examined": [],
            "not_shortlisted": [],
            "unexamined": [],
            "rejected": [],
        },
    }
    coverage = result["coverage"]
    roots = [project / Path(root).expanduser() for root in request.skill_roots]
    try:
        catalog = discover_skill_catalog(roots, authorized_roots=authorized_roots)
    except PermissionError as error:
        return {**result, "status": "unauthorized", "reason": str(error)}
    except (OSError, ValueError) as error:
        return {**result, "status": "incomplete", "reason": str(error)}
    coverage["discovered"] = [skill.id for skill in catalog]
    active = []
    for skill in catalog:
        if set(skill.aliases) & set(disabled_ids):
            coverage["disabled"].append(skill.id)
        elif (
            skill.issue
            or not skill.text.strip()
            or len(skill.text.encode()) > MAX_CANDIDATE_BYTES
        ):
            coverage["unexamined"].append(
                {
                    "id": skill.id,
                    "reason": skill.issue
                    or "Descrição ausente ou acima do limite de bytes.",
                }
            )
        else:
            active.append(skill)

    original_request = request.model_copy(deep=True)
    base = original_request.model_copy(deep=True)
    base.skill_roots = []
    base.artifact = request.artifact or query
    context_snapshot: str | None = None

    async def evaluate(
        phase: str, ids: list[str], tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        nonlocal context_snapshot
        evaluation_request = ReviewRequest.model_validate(
            {
                **base.model_dump(),
                "tool": tool,
                "arguments": arguments,
            }
        )
        evaluation = await review_request(
            evaluation_request,
            project,
            url,
            token,
            request_context=request_context if context_snapshot is None else None,
            transport=transport,
            ca_file=ca_file,
            timeout_seconds=timeout_seconds,
            progress_callback=progress_callback,
            project_id=project_id,
        )
        result["evaluations"].append({"phase": phase, "ids": ids, "result": evaluation})
        result["context"] = evaluation["context"]
        if request != original_request:
            result.update(
                status="incomplete",
                reason="O pedido mudou durante a avaliação; refaça o roteamento.",
            )
            return None
        if "decision" not in evaluation:
            result.update(status="incomplete", reason=evaluation["reason"])
            return None
        context = evaluation["context"]
        snapshot = json.dumps(
            {key: value for key, value in context.items() if key != "alternatives"},
            sort_keys=True,
        )
        if context_snapshot is not None and context_snapshot != snapshot:
            result.update(
                status="incomplete",
                reason="O contexto mudou entre etapas; refaça o roteamento.",
            )
            return None
        if context_snapshot is None:
            context_snapshot = snapshot
            result["context_revision"] = evaluation["context_revision"]
            base.sources = [item["source"] for item in context["evidence"]]
            base.source_rounds = []
            base.gaps, base.conflicts = [], []
        decision: dict[str, Any] = evaluation["decision"]
        return decision

    shortlist: list[tuple[SkillSource, int, float]] = []
    for batch_index, batch in enumerate(_batches(active)):
        ids = [skill.id for skill in batch]
        decision = await evaluate(
            "catalog",
            ids,
            "jev_find",
            {
                "query": query,
                "candidates": [{"id": skill.id, "text": skill.text} for skill in batch],
            },
        )
        if decision is None:
            coverage["unexamined"].extend(
                {"id": skill.id, "reason": result["reason"]}
                for skill in active
                if skill.id not in coverage["descriptions_examined"]
            )
            return result
        coverage["descriptions_examined"].extend(ids)
        top = decision["top"] if decision["status"] != "absent" else []
        shortlisted_ids = {item["id"] for item in top}
        by_id = {skill.id: skill for skill in batch}
        shortlist.extend(
            (by_id[item["id"]], batch_index, item["fit"])
            for item in sorted(
                top, key=lambda item: (-item["fit"], -item["probability"], item["id"])
            )
        )
        coverage["not_shortlisted"].extend(
            skill.id for skill in batch if skill.id not in shortlisted_ids
        )

    suitable: list[tuple[float, dict[str, Any], SkillSource]] = []
    accepted: dict[int, float] = {}
    for skill, batch_index, description_fit in shortlist:
        if batch_index in accepted and not isclose(
            description_fit, accepted[batch_index]
        ):
            coverage["not_shortlisted"].append(skill.id)
            continue
        try:
            instructions = read_skill(skill, authorized_roots)
            if len(instructions.encode()) > MAX_CANDIDATE_BYTES:
                raise ValueError(
                    "Instruções completas excedem o limite de bytes; avaliação não realizada."
                )
        except (OSError, ValueError) as error:
            coverage["unexamined"].append({"id": skill.id, "reason": str(error)})
            continue
        screening = await evaluate(
            "screen", [skill.id], "jev_screen", {"text": instructions, "purpose": query}
        )
        if screening is None:
            return result
        if screening["decision"] != "pass":
            coverage["rejected"].append(
                {"id": skill.id, "reason": screening["decision"]}
            )
            continue
        decision = await evaluate(
            "instructions",
            [skill.id],
            "jev_find",
            {
                "query": query,
                "candidates": [{"id": skill.id, "text": instructions}],
                "top_k": 1,
            },
        )
        if decision is None:
            return result
        coverage["instructions_examined"].append(skill.id)
        if decision["status"] == "answered":
            accepted[batch_index] = description_fit
            suitable.append(
                (
                    decision["top"][0]["fit"],
                    {
                        "id": skill.id,
                        "source": skill.source,
                        "reference": skill.reference,
                        "revision": skill.revision,
                        "aliases": list(skill.aliases),
                        "instructions": instructions,
                        "loaded": False,
                    },
                    skill,
                )
            )
        else:
            coverage["rejected"].append({"id": skill.id, "reason": decision["status"]})

    if coverage["unexamined"]:
        return {
            **result,
            "status": "incomplete",
            "reason": "Há fontes não examinadas; revise o catálogo antes de escolher.",
        }
    if not suitable:
        return {
            **result,
            "status": "incompatible" if coverage["rejected"] else "absent",
            "reason": (
                "As candidatas foram retidas pelo screening ou pela avaliação de adequação; "
                "consulte coverage.rejected e os gates das avaliações."
                if coverage["rejected"]
                else result["reason"]
            ),
        }
    suitable.sort(key=lambda item: (-item[0], item[1]["id"]))
    if len(suitable) > 1 and isclose(suitable[0][0], suitable[1][0]):
        return {
            **result,
            "status": "ambiguous",
            "reason": "Skills igualmente adequadas exigem escolha humana.",
        }
    candidate, source = suitable[0][1:]
    if set(candidate["aliases"]) & set(disabled_ids):
        return {
            **result,
            "status": "unauthorized",
            "reason": "A skill foi desativada durante a avaliação.",
        }
    try:
        read_skill(source, authorized_roots)
    except (OSError, ValueError) as error:
        coverage["unexamined"].append({"id": source.id, "reason": str(error)})
        return {**result, "status": "incomplete", "reason": str(error)}
    automated = bool(result["evaluations"]) and all(
        evaluation["result"].get("action") == "auto"
        and evaluation["result"].get("auto_advance") is True
        for evaluation in result["evaluations"]
    )
    return {
        **result,
        "candidate": candidate,
        "action": "auto" if automated else "needs_human",
        "auto_advance": automated,
        "origin": "automated" if automated else "review",
        "status": "selected" if automated else "suggested",
        "reason": "Skill selecionada por gates calibrados; o harness ainda deve confirmar o carregamento."
        if automated
        else "Indicação pendente de revisão e confirmação de carregamento pelo harness; consulte a política das avaliações.",
    }


def confirm_skill_loaded(
    indication: dict[str, Any],
    skill_id: str,
    revision: str,
    *,
    authorized_roots: list[Path],
    disabled_ids: Collection[str] = (),
) -> dict[str, Any]:
    """Record an explicit harness acknowledgment without authorizing execution."""
    result = deepcopy(indication)
    candidate = result.get("candidate")
    if (
        not candidate
        or candidate["id"] != skill_id
        or candidate["revision"] != revision
        or set(candidate["aliases"]) & set(disabled_ids)
    ):
        return {
            **result,
            "candidate": None,
            "action": "needs_human",
            "auto_advance": False,
            "execution_authorized": False,
            "status": "incomplete",
            "reason": "O carregamento não corresponde à indicação autorizada.",
        }
    source = SkillSource(
        skill_id,
        "",
        candidate["source"],
        candidate["reference"],
        revision,
        tuple(candidate["aliases"]),
    )
    try:
        read_skill(source, authorized_roots)
    except (OSError, ValueError) as error:
        return {
            **result,
            "candidate": None,
            "action": "needs_human",
            "auto_advance": False,
            "execution_authorized": False,
            "status": "incomplete",
            "reason": str(error),
        }
    candidate["loaded"] = True
    result["execution_authorized"] = False
    return result
