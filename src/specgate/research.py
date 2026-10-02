"""Collect authorized tool snapshots for a gap; never resolve it by collection."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from specgate.client import automatic_decision_valid
from specgate.context import ContextPacket, Evidence, _resolve_source, build_context
from specgate.privacy import ensure_safe_content
from specgate.product import mcp_api_key
from specgate.shared.domain.inputs import Item, checked_text, item_map
from specgate.transport import call_tool


class ResearchSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    topic: str
    source: str
    source_revision: str
    collected_at: str
    snapshot: str | None = None
    relation: str
    kind: Literal["source", "inference", "requirement"] = "source"
    independent: bool = False
    limitation: str = ""

    @field_validator("id", "topic", "source", "source_revision", "relation")
    @classmethod
    def nonempty(cls, value: str) -> str:
        return checked_text(value)

    @field_validator("source")
    @classmethod
    def public_source(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "Fonte deve ter referência sem credenciais, query string ou fragmento."
            )
        return value

    @field_validator("collected_at")
    @classmethod
    def dated(cls, value: str) -> str:
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("Data de coleta exige timezone explícito.")
        return value


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    question: str
    claim: str = ""
    factual: bool = True
    human_topic: str | None = None
    topics: list[Item] = Field(min_length=2, max_length=48)
    sources: list[ResearchSource] = Field(max_length=100)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_scope(self) -> Self:
        checked_text(self.question)
        checked_text(self.claim, allow_empty=True)
        topics = item_map(self.topics)
        if self.human_topic is not None and self.human_topic not in topics:
            raise ValueError("Tema humano deve pertencer ao catálogo.")
        ids = [source.id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("ID de fonte duplicado.")
        if any(source.topic not in topics for source in self.sources):
            raise ValueError("Toda fonte deve indicar um tema do catálogo.")
        ensure_safe_content(self.model_dump())
        return self


async def prioritize_topics(
    request: ResearchRequest, url: str, token: str, *, authorize_jev: bool = False,
    transport_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Choose one research topic, without recursively researching the ranking."""
    fallback: dict[str, Any] = {
        "status": "not_authorized",
        "selected_option": None,
        "action": "review",
        "auto_advance": False,
        "calibrated": False,
        "origin": "policy",
    }
    if request.human_topic is not None:
        return {
            **fallback,
            "status": "selected",
            "origin": "human",
            "selected_option": request.human_topic,
        }
    if not authorize_jev:
        return fallback
    scope = json.dumps(
        {"question": request.question, "claim": request.claim},
        ensure_ascii=False,
        sort_keys=True,
    )
    context = ContextPacket(
        objective="Escolher um tema para pesquisar a lacuna declarada.",
        rules=(
            (
                "Priorize apenas um tema do catálogo. A pergunta e a claim são "
                "escopo declarado, não evidência factual. Não resolva a claim."
            ),
        ),
        artifact=json.dumps(
            [topic.model_dump() for topic in request.topics], ensure_ascii=False
        ),
        evidence=(
            Evidence(
                "research-scope",
                "invocation",
                sha256(scope.encode()).hexdigest(),
                scope,
            ),
        ),
        alternatives=tuple(topic.text for topic in request.topics),
        gaps=(),
    )
    arguments = {
        "question": "Qual tema deve ser pesquisado primeiro para responder: "
        + request.question,
        "options": [topic.model_dump() for topic in request.topics],
        "context": asdict(context),
    }
    try:
        result = await call_tool(url, token, "jev_decide", arguments, **(transport_options or {}))
        data = result.structured_content
        if result.is_error or not isinstance(data, dict):
            raise ValueError("Priorização indisponível.")
        ranking = {
            key: data.get(key)
            for key in (
                "selected_option",
                "mode",
                "origin",
                "confidence",
                "action",
                "reason",
                "auto_advance",
                "calibrated",
                "context_revision",
                "evaluation_id",
            )
        }
        # The packet states which authority advanced the topic, never a bare flag.
        gate = data.get("gate")
        ranking["gate_basis"] = gate.get("basis") if isinstance(gate, dict) else None
        selected = (
            isinstance(data.get("selected_option"), str)
            and data.get("selected_option") in {topic.id for topic in request.topics}
            and data.get("mode") == "real"
            and data.get("origin") == "jev"
            and data.get("action") == "auto"
            and data.get("auto_advance") is True
            and automatic_decision_valid(data, "jev_decide", arguments, context)
        )
        ranking["status"] = "selected" if selected else "needs_human"
        ensure_safe_content(ranking, secrets=(token,))
        return ranking
    except (ValueError, OSError, ExceptionGroup):
        return {**fallback, "status": "unavailable"}


async def collect_research(
    request: ResearchRequest,
    project: Path,
    url: str,
    token: str,
    *,
    authorize_jev: bool = False,
    capture: Callable[[ResearchSource], Awaitable[None]] | None = None,
    transport_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture explicit snapshots from any harness, independently of setup/filtering."""
    root = project.resolve()
    ensure_safe_content(request.model_dump(), secrets=(token,))
    paths = {
        source.id: _resolve_source(root, source.snapshot)[1]
        for source in request.sources
        if source.snapshot is not None
    }
    ranking = await prioritize_topics(request, url, token, authorize_jev=authorize_jev,
                                      transport_options=transport_options)
    selected = ranking["selected_option"] if ranking["status"] == "selected" else None
    sources = [source for source in request.sources if source.topic == selected]
    failed: set[str] = set()
    if capture is not None:
        for source in sources:
            try:
                await capture(source)
            except (ValueError, OSError, TimeoutError):
                failed.add(source.id)
    context = build_context(
        root,
        request.question,
        [
            paths[source.id]
            for source in sources
            if source.id in paths and source.id not in failed
        ],
        [],
    )
    evidence_by_path = {item.source: item for item in context.evidence}
    evidence: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    limitations = [
        *request.limitations,
        "Coleta e ranking não resolvem a claim nem demonstram acurácia ou calibração.",
        (
            "Independência, revisão e data da origem são declaradas pelo coletor; "
            "o sha256 verifica apenas o texto capturado."
        ),
    ]
    if selected is not None and not sources:
        limitations.append("Nenhuma fonte autorizada para o tema selecionado.")
    if selected is None:
        limitations.append(
            "Priorização requer decisão humana; não coletamos fontes nem repetimos pesquisa."
        )
    for source in sources:
        item = (
            None
            if source.id in failed
            else evidence_by_path.get(paths.get(source.id, ""))
        )
        if source.limitation:
            limitations.append(f"{source.id}: {source.limitation}")
        if item is None:
            unavailable.append(
                {
                    "id": source.id,
                    "topic": source.topic,
                    "source": source.source,
                    "question": request.question,
                    "reason": source.limitation or "Snapshot ausente ou ilegível.",
                }
            )
            continue
        evidence.append(
            {
                **source.model_dump(exclude={"snapshot", "id"}),
                **asdict(item),
                "id": source.id,
                "source": source.source,
                "snapshot": paths[source.id],
                "question": request.question,
                "claim": request.claim,
            }
        )
    # Read diagnostics only; decision readiness requires artifacts/rules this flow doesn't.
    limitations.extend(
        gap for gap in context.gaps if gap.startswith(("Fonte ", "Conteúdo "))
    )
    independent = [
        item["id"]
        for item in evidence
        if item["kind"] == "source" and item["independent"]
    ]
    if request.factual and not independent:
        limitations.append(
            "Pergunta factual sem fonte independente do artefato proposto."
        )
    packet = {
        "schema": 1,
        "question": request.question,
        "claim": request.claim,
        "status": "collected" if selected is not None else "needs_human",
        "selected_topic": selected,
        "not_collected": [
            source.id for source in request.sources if source.topic != selected
        ],
        "factual": request.factual,
        "claim_status": "unresolved",
        "topics": [topic.model_dump() for topic in request.topics],
        "authorized_sources": [source.model_dump() for source in request.sources],
        "prioritization": ranking,
        "evidence": evidence,
        "unavailable": unavailable,
        "independent_factual_sources": independent,
        "limitations": limitations,
    }
    ensure_safe_content(packet, secrets=(token,))
    return packet


def save_research(packet: dict[str, Any], project: Path, output: str) -> dict[str, str]:
    """Content-addressed files retain earlier captures and never overwrite one."""
    root = project.resolve()
    directory, _ = _resolve_source(root, output)
    ensure_safe_content(packet)
    raw = (
        json.dumps(
            packet, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        )
        + "\n"
    )
    revision = sha256(raw.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{revision}.json"
    try:
        with path.open("x", encoding="utf-8") as file:
            file.write(raw)
    except FileExistsError:
        if path.read_bytes() != raw.encode():
            raise ValueError(
                "Pacote existente não corresponde ao hash; preserve e investigue."
            ) from None
    return {"path": path.relative_to(root).as_posix(), "revision": revision}


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "request", type=Path, help="Pergunta, catálogo e fontes autorizadas em JSON"
    )
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--host", required=True)
    parser.add_argument(
        "--output", default="docs/research", help="Diretório versionável no projeto"
    )
    parser.add_argument(
        "--authorize-jev",
        action="store_true",
        help="Autoriza enviar pergunta, claim e catálogo ao host/provider",
    )


def execute(args: argparse.Namespace) -> dict[str, str]:
    try:
        request = ResearchRequest.model_validate_json(args.request.read_text())
    except ValidationError:
        raise ValueError(
            "Pedido de pesquisa inválido; confira o contrato da skill."
        ) from None
    host = args.host.rstrip("/")
    if not host.endswith(("/mcp", "/sse")):
        host += "/mcp"
    packet = asyncio.run(
        collect_research(
            request,
            args.project,
            host,
            mcp_api_key(),
            authorize_jev=args.authorize_jev,
        )
    )
    return save_research(packet, args.project, args.output)
