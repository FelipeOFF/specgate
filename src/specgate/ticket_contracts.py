"""Portable ticket plans, authorizations and tracker markers; no state or credentials."""

import re
from graphlib import CycleError, TopologicalSorter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from specgate.grill_contracts import GrillError
from specgate.shared.domain.inputs import checked_text


class TicketDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    title: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n]+$")
    behavior: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=30)
    blocked_by: list[str] = Field(default_factory=list, max_length=50)
    repository: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    integration: bool = False


class TicketPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tickets: list[TicketDraft] = Field(min_length=1, max_length=50)
    gaps: list[str] = Field(default_factory=list, max_length=50)

    def order(self) -> list[str]:
        ids = [ticket.id for ticket in self.tickets]
        if len(set(ids)) != len(ids):
            raise GrillError("duplicate_ticket", "Há IDs duplicados na decomposição.")
        for ticket in self.tickets:
            if not set(ticket.blocked_by) <= set(ids):
                raise GrillError(
                    "missing_blocker", "Um bloqueador não existe no grafo."
                )
            if len(set(ticket.blocked_by)) != len(ticket.blocked_by):
                raise GrillError("duplicate_blocker", "Um bloqueador está repetido.")
            for text in (ticket.title, ticket.behavior, *ticket.acceptance_criteria):
                checked_text(text)
                if re.search(r"(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]+\b|```", text):
                    raise GrillError(
                        "implementation_paths",
                        "Descreva comportamento sem caminhos ou código.",
                    )
            if ticket.integration:
                blocker_repositories = {
                    candidate.repository
                    for candidate in self.tickets
                    if candidate.id in ticket.blocked_by
                }
                if None in blocker_repositories or len(blocker_repositories) < 2:
                    raise GrillError(
                        "invalid_integration_ticket",
                        "Um ticket de integração deve reunir ao menos dois repos.",
                    )
        if len(self.model_dump_json().encode()) > 32_000:
            raise GrillError("tickets_too_large", "A decomposição excede 32.000 bytes.")
        try:
            return list(
                TopologicalSorter(
                    {t.id: t.blocked_by for t in self.tickets}
                ).static_order()
            )
        except CycleError:
            raise GrillError(
                "dependency_cycle", "As dependências contêm um ciclo."
            ) from None


class ResidualClaim(BaseModel):
    """A claim the maintainer accepts below the threshold, with its verdict and reason."""

    model_config = ConfigDict(extra="forbid", strict=True)
    claim_id: str = Field(min_length=1, max_length=200)
    verdict: Literal["verified", "unsupported", "contradicted"]
    reason: str = Field(min_length=1, max_length=2000)


class TicketAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decomposition_approved: bool = False
    origin: Literal["human", "automated"] = "human"
    delegation_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    project: str | None = None
    repositories: list[str] = Field(default_factory=list)
    publication_authorized: bool = False
    reference: str = Field(default="", max_length=2000)
    residual: list[ResidualClaim] = Field(default_factory=list, max_length=100)


class TicketRelations(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    native_sub_issues: bool = True
    native_dependencies: bool = True


def require_native_relations(destination: str, relations: dict[str, Any]) -> None:
    """Validate publication policy; GrillError messages are shown to the user."""
    if destination in {"github", "jira", "beads"} and not all(
        relations.get(name) is True
        for name in ("native_sub_issues", "native_dependencies")
    ):
        raise GrillError(
            "native_relations_required",
            "A publicação exige vínculo da spec e dependências nativas habilitadas.",
        )


class TicketObservation(BaseModel):
    actionable: bool = True
    model_config = ConfigDict(extra="forbid", strict=True)
    ticket_id: str
    issue_id: int | None = Field(default=None, gt=0)
    number: int | None = Field(default=None, gt=0)
    external_id: str | None = Field(default=None, min_length=1)
    state: Literal["open", "closed"]
    state_reason: (
        Literal["completed", "not_planned", "reopened", "duplicate"] | None
    ) = None


def ticket_marker(state: dict[str, Any], ticket_id: str) -> str:
    graph = state["tickets"]
    epoch = len(state.get("destination_history") or [])
    suffix = f":{epoch}" if epoch else ""
    return (
        f"<!-- dev-decision-ticket:{state['id']}:{graph['revision']}:"
        f"{ticket_id}{suffix} -->"
    )


def _reference_text(reference: dict[str, Any]) -> str:
    return str(
        reference.get("comment_url")
        or reference.get("url")
        or f"{reference['tracker']}:{reference['project']}:{reference['external_id']}"
    )


def ticket_body(state: dict[str, Any], ticket_id: str) -> str:
    graph = state["tickets"]
    ticket = next(t for t in graph["draft"]["tickets"] if t["id"] == ticket_id)
    parent = graph["parent"]
    blockers = [
        _reference_text(graph["publications"][key]["issue"])
        for key in ticket["blocked_by"]
    ]
    criteria = "\n".join(f"- [ ] {text}" for text in ticket["acceptance_criteria"])
    blocked = (
        "\n".join(f"- {url}" for url in blockers) or "None (can start immediately)."
    )
    previous_urls = [
        _reference_text(entry["issue"])
        for previous in state.get("tickets_history", [])
        if (entry := previous["publications"].get(ticket_id)) and entry.get("issue")
    ]
    previous_urls += [
        _reference_text(entry["issue"])
        for archived in state.get("destination_history", [])
        if (entry := archived["publications"].get(ticket_id)) and entry.get("issue")
    ]
    previous_refs = (
        "## Previous artifacts\n\n"
        + "\n".join(f"- {url}" for url in dict.fromkeys(previous_urls))
        + "\n\n"
        if previous_urls
        else ""
    )
    return (
        f"## Parent\n\n{_reference_text(parent)}\n\n"
        f"Revisão da spec: {graph['spec_revision']}\n\n"
        f"## What to build\n\n{ticket['behavior']}\n\n"
        f"## Acceptance criteria\n\n{criteria}\n\n## Blocked by\n\n{blocked}\n\n"
        f"{previous_refs}"
        f"{ticket_marker(state, ticket_id)}\n"
    )


def review_blocked(graph: dict[str, Any]) -> set[str]:
    blocked: set[str] = set()
    for key in graph["order"]:
        ticket = next(t for t in graph["draft"]["tickets"] if t["id"] == key)
        if graph["publications"].get(key, {}).get("status") == "review" or (
            blocked.intersection(ticket["blocked_by"])
        ):
            blocked.add(key)
    return blocked


def require_verified_ticket(graph: dict[str, Any], ticket_id: str) -> None:
    require_native_relations(graph.get("destination") or "github", graph["relations"])
    if ticket_id in review_blocked(graph):
        raise GrillError(
            "publication_review_required",
            "Reconcilie o ticket e seus bloqueadores antes de implementar.",
        )
