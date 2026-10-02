"""Filter collected research with Jev while retaining rejected evidence."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from specgate.client import automatic_decision_valid
from specgate.context import ContextPacket, Evidence, _resolve_source
from specgate.payload import CODE, PayloadTooLarge
from specgate.privacy import ensure_safe_content
from specgate.product import mcp_api_key
from specgate.research import ResearchSource, save_research
from specgate.shared.domain.inputs import checked_text
from specgate.transport import call_tool


class CollectedEvidence(ResearchSource):
    model_config = ConfigDict(extra="allow", strict=True)

    text: str
    revision: str
    question: str
    claim: str


class CollectedPacket(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    schema_version: int = Field(alias="schema", ge=1, le=1)
    question: str
    claim: str
    claim_status: str
    status: Literal["collected", "needs_human"]
    selected_topic: str | None
    evidence: list[CollectedEvidence]


def read_packet(project: Path, relative: str) -> tuple[CollectedPacket, str]:
    path, _ = _resolve_source(project.resolve(), relative)
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Pacote de pesquisa ausente ou acima de 1 MB.")
    try:
        raw = path.read_bytes()
        packet = CollectedPacket.model_validate_json(raw)
    except (ValidationError, UnicodeError):
        raise ValueError("Pacote de pesquisa inválido.") from None
    if packet.claim_status != "unresolved":
        raise ValueError("A coleta deve manter a claim não resolvida.")
    checked_text(packet.question)
    checked_text(packet.claim, allow_empty=True)
    for item in packet.evidence:
        if packet.status != "collected" or item.topic != packet.selected_topic:
            raise ValueError("Evidência fora do tema coletado.")
        if item.question != packet.question or item.claim != packet.claim:
            raise ValueError("Evidência fora da pergunta ou claim do pacote.")
        if sha256(item.text.encode()).hexdigest() != item.revision:
            raise ValueError("Revisão de evidência não corresponde ao texto.")
    ensure_safe_content(packet.model_dump())
    return packet, sha256(raw).hexdigest()


def repeated(item: CollectedEvidence, earlier: list[CollectedEvidence]) -> str | None:
    for old in earlier:
        if item.revision == old.revision:
            return "Conteúdo idêntico a evidência anterior."
        if item.source == old.source:
            if item.source_revision == old.source_revision:
                return "Revisão da fonte já usada; novidade material não demonstrada."
            if item.text in old.text:
                return "Recorte de conteúdo já usado nesta fonte."
    return None


def contained(item: CollectedEvidence, known: Sequence[str]) -> str | None:
    """An excerpt of a source the caller already uses is not new, whatever its origin."""
    if any(item.text in text for text in known):
        return "Recorte de fonte já usada pela spec."
    return None


def _filter_context(item: CollectedEvidence) -> ContextPacket:
    return ContextPacket(
        objective=item.question,
        rules=("Avalie somente a fonte capturada para a pergunta e a claim.",),
        artifact=item.text,
        evidence=(Evidence(item.id, item.source, item.revision, item.text),),
        alternatives=(item.claim or item.question, "Evidência insuficiente"),
        gaps=(),
    )


def _valid_result(
    data: dict[str, Any], tool: str, arguments: dict[str, Any], context: ContextPacket
) -> bool:
    return _closed_review(data) or automatic_decision_valid(
        data, tool, arguments, context
    )


def gates_passed(entry: dict[str, Any]) -> bool:
    """Recheck that Screen and Verify passed their gate for the exact captured evidence.

    Either basis passes: the confidence policy or a validated manifest. Which one
    the host accepts is its own decision, made where the cycle is admitted.
    """
    try:
        item = CollectedEvidence.model_validate(entry)
        context = _filter_context(item)
        jev = entry["jev"]
        screen = jev["screen"]
        verify = jev["verify"]
        screen_args = {
            "text": item.text,
            "purpose": f"Pergunta: {item.question}\nClaim: {item.claim}",
            "context": asdict(context),
        }
        verify_args = {
            "claims": [{"id": item.id, "text": item.claim}],
            "evidence": item.text,
            "context": asdict(context),
        }
        return bool(
            item.claim
            and entry["result"] == "pertinent"
            and automatic_decision_valid(screen, "jev_screen", screen_args, context)
            and automatic_decision_valid(verify, "jev_verify", verify_args, context)
        )
    except (KeyError, TypeError, ValueError):
        return False


def overflow_report(
    question: str,
    claim: str,
    selected_topic: str | None,
    stage: str,
    source: str | None,
    overflow: PayloadTooLarge,
) -> dict[str, Any]:
    """A filtered packet that reports an overflow: sizes and ids, never evidence."""
    return {
        "schema": 1,
        "question": question,
        "claim": claim,
        "selected_topic": selected_topic,
        "collection_diagnostic": CODE,
        "claim_status": "unresolved",
        "status": "needs_human",
        "selected": [],
        "rejected": [],
        "overflow": {
            "stage": stage,
            "source": source,
            "size": overflow.size,
            "limit": overflow.limit,
        },
        "gaps": [
            "O pedido passou do limite de tamanho; reduza as fontes e retome o mesmo ciclo."
        ],
        "limitations": [
            "Nenhuma fonte foi julgada: o relato guarda tamanhos e identificadores, sem o conteúdo."
        ],
    }


async def filter_research(
    packet: CollectedPacket,
    previous: list[CollectedPacket],
    url: str,
    token: str,
    *,
    authorize_jev: bool = False,
    known: Sequence[str] = (),
    transport_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not authorize_jev:
        raise ValueError("Autorize o envio das evidências ao MCP para filtrar.")
    ensure_safe_content(packet.model_dump(), secrets=(token,))
    if any(
        (old.question, old.claim) != (packet.question, packet.claim) for old in previous
    ):
        raise ValueError("Pacote anterior pertence a outra pergunta ou claim.")
    earlier = [item for old in previous for item in old.evidence]
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in packet.evidence:
        result = item.model_dump()
        duplicate = repeated(item, earlier) or contained(item, known)
        if duplicate:
            rejected.append({**result, "result": "repeated", "reason": duplicate})
            continue
        stage = "jev_screen"
        try:
            context = _filter_context(item)
            screen_args = {
                "text": item.text,
                "purpose": f"Pergunta: {packet.question}\nClaim: {packet.claim}",
                "context": asdict(context),
            }
            screen = await call_tool(
                url,
                token,
                "jev_screen",
                screen_args,
                **(transport_options or {}),
            )
            judgment = screen.structured_content
            if overflow := PayloadTooLarge.from_result(judgment):
                raise overflow
            if (
                screen.is_error
                or not isinstance(judgment, dict)
                or not _valid_result(judgment, "jev_screen", screen_args, context)
            ):
                raise ValueError("Jev não avaliou a pertinência.")
            probabilities = judgment.get("probabilities")
            if (
                not isinstance(probabilities, dict)
                or not all(
                    type(probabilities.get(key)) in (float, int)
                    and 0 <= probabilities[key] <= 1
                    for key in ("injection", "substance", "relevance")
                )
                or not isinstance(judgment.get("decision"), str)
                or judgment["decision"] not in {"pass", "review", "block", "skip"}
            ):
                raise ValueError("Jev retornou screening inválido.")
            verdict = "unassessed"
            verify_result: dict[str, Any] | None = None
            if packet.claim and judgment["decision"] != "block":
                stage = "jev_verify"
                verify_args = {
                    "claims": [{"id": item.id, "text": packet.claim}],
                    "evidence": item.text,
                    "context": asdict(context),
                }
                verified = await call_tool(
                    url,
                    token,
                    "jev_verify",
                    verify_args,
                    **(transport_options or {}),
                )
                data = verified.structured_content
                if overflow := PayloadTooLarge.from_result(data):
                    raise overflow
                verdicts = data.get("verdicts") if isinstance(data, dict) else None
                if (
                    verified.is_error
                    or not isinstance(data, dict)
                    or not _valid_result(data, "jev_verify", verify_args, context)
                    or not isinstance(verdicts, list)
                    or len(verdicts) != 1
                    or not isinstance(verdicts[0], dict)
                    or verdicts[0].get("id") != item.id
                    or not isinstance(verdicts[0].get("verdict"), str)
                    or verdicts[0]["verdict"]
                    not in {"verified", "contradicted", "unsupported"}
                ):
                    raise ValueError("Jev não verificou a claim.")
                verdict = verdicts[0]["verdict"]
                verify_result = data
            pertinent = (
                judgment["decision"] == "pass" and probabilities["relevance"] >= 0.8
            )
            keep = judgment["decision"] != "block" and (
                pertinent or verdict == "contradicted"
            )
            reason = (
                "Screening bloqueou o conteúdo; exige revisão humana."
                if judgment["decision"] == "block"
                else "Contradiz a claim; preservada para revisão."
                if verdict == "contradicted"
                else "Jev julgou pertinente à lacuna."
                if pertinent
                else f"Jev julgou sem pertinência suficiente ({judgment['decision']})."
            )
            entry = {
                **result,
                "result": verdict
                if verdict == "contradicted"
                else "pertinent"
                if keep
                else "rejected",
                "reason": reason,
                "jev": {"screen": judgment, "verify": verify_result},
            }
            (selected if keep else rejected).append(entry)
        except PayloadTooLarge as overflow:
            # Not a source verdict: the cycle stops and the host keeps its reservation.
            return overflow_report(
                packet.question,
                packet.claim,
                packet.selected_topic,
                stage,
                item.id,
                overflow,
            )
        except (ValueError, OSError, ExceptionGroup):
            rejected.append(
                {
                    **result,
                    "result": "unavailable",
                    "reason": f"Avaliação {stage} inválida ou indisponível; exige revisão humana.",
                    "error": {"code": f"{stage}_invalid_or_unavailable"},
                }
            )
    captured = packet.model_dump()
    ranking = captured.get("prioritization", {})
    collection_diagnostic = (
        "operational_unavailable"
        if ranking.get("status") == "unavailable" or captured.get("unavailable")
        else "topic_selection_required"
        if packet.status != "collected"
        else None
    )
    output = {
        "schema": 1,
        "question": packet.question,
        "claim": packet.claim,
        "selected_topic": packet.selected_topic,
        "collection_diagnostic": collection_diagnostic,
        "claim_status": "unresolved",
        "status": "filtered" if selected else "needs_human",
        "selected": selected,
        "rejected": rejected,
        "gaps": []
        if selected
        else ["Nenhuma evidência nova e pertinente para a lacuna."],
        "limitations": [
            "Julgamentos Jev exigem revisão; scores não demonstram calibração ou autorização para avançar."
        ],
    }
    ensure_safe_content(output, secrets=(token,))
    return output


def _closed_review(data: dict[str, Any]) -> bool:
    return (
        data.get("mode") == "real"
        and data.get("action") == "review"
        and data.get("auto_advance") is False
        and data.get("calibrated") is False
    )


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("packet", help="Pacote JSON produzido por research-with-jev")
    parser.add_argument(
        "--previous",
        action="append",
        default=[],
        help="Pacote anterior da mesma lacuna",
    )
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--host", required=True)
    parser.add_argument("--output", default="docs/research/filtered")
    parser.add_argument("--authorize-jev", action="store_true")


def execute(args: argparse.Namespace) -> dict[str, str]:
    root = args.project.resolve()
    packet, input_revision = read_packet(root, args.packet)
    previous = [read_packet(root, path) for path in args.previous]
    host = args.host.rstrip("/")
    if not host.endswith(("/mcp", "/sse")):
        host += "/mcp"
    result = asyncio.run(
        filter_research(
            packet,
            [item[0] for item in previous],
            host,
            mcp_api_key(),
            authorize_jev=args.authorize_jev,
        )
    )
    result["history"] = {
        "input": {
            "path": args.packet,
            "revision": input_revision,
        },
        "previous": [
            {"path": path, "revision": revision}
            for path, (_, revision) in zip(args.previous, previous, strict=True)
        ],
        "selected_ids": [item["id"] for item in result["selected"]],
        "rejected_ids": [item["id"] for item in result["rejected"]],
    }
    return save_research(result, root, args.output)
