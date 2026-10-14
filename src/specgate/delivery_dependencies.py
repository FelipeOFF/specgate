"""Pre-merge prerequisites: verified draft deliveries consumed before their ticket is done.

A reference pins one delivery of a blocker (PR, commit, graph revision, outcome) and
never says the ticket is complete or merged. The functions read a run state and decide;
they hold no state of their own and no credentials.
"""

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from specgate.grill_contracts import GrillError
from specgate.implementation_contracts import DraftPullRequest
from specgate.spec_contracts import _fingerprint
from specgate.ticket_contracts import TicketObservation, require_verified_ticket

DELIVERED = frozenset({"draft", "ready"})


class DeliveryDependency(BaseModel):
    """The delivery of one pending blocker, as the forge shows it now."""

    model_config = ConfigDict(extra="forbid", strict=True)
    ticket_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    implementation_id: str = Field(min_length=1, max_length=128)
    graph_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    outcome_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    pull_request: DraftPullRequest


def pull_request_identity(pull_request: dict[str, Any]) -> tuple[Any, ...]:
    """What names a PR and its commit; a title or body edit does not change it."""
    return (
        str(pull_request["repository"]).casefold(),
        pull_request["number"],
        pull_request["url"],
        pull_request["head"],
        pull_request["commit_sha"],
    )


def same_references(
    recorded: Sequence[dict[str, Any]], observed: Sequence[DeliveryDependency]
) -> bool:
    """Whether the references a ticket recorded are the ones the forge shows now."""

    def key(reference: dict[str, Any]) -> tuple[Any, ...]:
        return (
            reference["ticket_id"],
            reference["implementation_id"],
            reference["graph_revision"],
            reference["outcome_revision"],
            pull_request_identity(reference["pull_request"]),
        )

    return [key(item) for item in recorded] == [
        key(item.model_dump()) for item in observed
    ]


def outcome_revision(entry: dict[str, Any]) -> str:
    """The outcome a dependent was built on, apart from its checks.

    A retarget runs the checks again and replaces them; `delivered` already requires that
    every one of them passed, so the checks do not tell a different delivery.
    """
    outcome = entry.get("outcome") or {}
    return _fingerprint({key: value for key, value in outcome.items() if key != "checks"})


def delivered(state: dict[str, Any], ticket_id: str) -> dict[str, Any]:
    """The recorded delivery of a blocker when a ticket may rely on it before the merge.

    It needs a PR, the commit the checks ran on, the current graph revision and every
    check passed. The forge and the grant are the caller's to observe.
    """
    graph = state["tickets"]
    entry = (state.get("implementations") or {}).get(ticket_id)
    pull_request = entry.get("pull_request") if isinstance(entry, dict) else None
    if not isinstance(entry, dict) or not isinstance(pull_request, dict):
        raise GrillError(
            "ticket_blocked", "O bloqueador ainda não tem uma entrega verificada."
        )
    if entry.get("status") not in DELIVERED:
        raise GrillError(
            "dependency_unverified", "O registro do bloqueador não está em draft."
        )
    outcome = entry.get("outcome") or {}
    checks = outcome.get("checks") or []
    if entry.get("ticket_revision") != graph["revision"]:
        # No push to the PR explains it and none will undo it: the graph was replaced.
        raise GrillError(
            "dependency_outdated",
            "A entrega do bloqueador é de uma revisão anterior do grafo.",
        )
    if outcome.get("commit_sha") != pull_request["commit_sha"]:
        raise GrillError(
            "dependency_changed", "A entrega do bloqueador não é a revisão atual."
        )
    if not checks or any(check.get("passed") is not True for check in checks):
        raise GrillError(
            "dependency_unverified", "A entrega do bloqueador não passou nos checks."
        )
    return entry


def validate_dependency(
    state: dict[str, Any], dependency: DeliveryDependency, delivery_id: str
) -> dict[str, Any]:
    """The recorded delivery a reference names, or why it no longer holds."""
    graph = state["tickets"]
    require_verified_ticket(graph, dependency.ticket_id)
    entry = delivered(state, dependency.ticket_id)
    if (
        dependency.graph_revision != graph["revision"]
        or entry["id"] != dependency.implementation_id
        or entry.get("delivery_id", state["id"]) != delivery_id
        or pull_request_identity(entry["pull_request"])
        != pull_request_identity(dependency.pull_request.model_dump())
        or dependency.outcome_revision != outcome_revision(entry)
    ):
        raise GrillError(
            "dependency_changed", "A entrega do bloqueador mudou; verifique de novo."
        )
    return entry


def same_repository(state: dict[str, Any], ticket_id: str, other: str) -> bool:
    issue = state["tickets"]["publications"][ticket_id]["issue"]
    return bool(issue["repository"].casefold() == other.casefold())


def dependency_order(
    state: dict[str, Any],
    ticket_id: str,
    completed: list[str],
    dependencies: list[DeliveryDependency],
    base: str,
    stack_after: str | None,
    delivery_id: str,
    observations: Sequence[TicketObservation],
) -> list[str]:
    """The same-repository stack a ticket joins, or [] when it only consumes references.

    Every blocker that is not complete needs a verified reference and an open issue: the
    draft of an abandoned ticket is no delivery. Only a blocker of the same repository
    can be the base of the PR; one of another repository is read from its commit and its
    branch is never stacked.
    """
    graph = state["tickets"]
    ticket = next(item for item in graph["draft"]["tickets"] if item["id"] == ticket_id)
    pending = [key for key in ticket["blocked_by"] if key not in completed]
    if len(dependencies) != len(pending) or {d.ticket_id for d in dependencies} != set(
        pending
    ):
        raise GrillError(
            "ticket_blocked", "Cada bloqueador pendente precisa de uma entrega verificada."
        )
    open_issues = {
        item.ticket_id for item in observations if item.state == "open" and item.actionable
    }
    if not set(pending) <= open_issues:
        raise GrillError(
            "ticket_blocked", "Um bloqueador pendente não está aberto para o agente."
        )
    entries = {
        item.ticket_id: validate_dependency(state, item, delivery_id)
        for item in dependencies
    }
    same = [
        item.ticket_id
        for item in dependencies
        if same_repository(state, ticket_id, item.pull_request.repository)
    ]
    if not same:
        if stack_after:
            raise GrillError(
                "invalid_stack", "Dependências entre repos não compartilham branch."
            )
        return []
    parent = next((item for item in dependencies if item.ticket_id == stack_after), None)
    if parent is None or parent.ticket_id not in same or parent.pull_request.head != base:
        raise GrillError(
            "invalid_stack", "A stack exige a PR de um bloqueador do mesmo repo como base."
        )
    order = [*entries[parent.ticket_id].get("stack", []), parent.ticket_id]
    if ticket_id in order or not set(same) <= set(order):
        raise GrillError("ticket_blocked", "Há bloqueadores fora da stack autorizada.")
    return list(dict.fromkeys([*order, ticket_id]))


def dependencies_gap(
    state: dict[str, Any], entry: dict[str, Any], authority: dict[str, Any] | None
) -> GrillError | None:
    """Why a started ticket may no longer go out on its recorded references, if so.

    The commit of a PR is the forge's to tell; the client observes it again before it
    publishes. This is what the host itself can see: the delivery it recorded, its
    graph revision, its checks and the grant each side published under.
    """
    graph = state["tickets"]
    authority = authority or {}
    for reference in entry.get("dependencies") or []:
        try:
            require_verified_ticket(graph, reference["ticket_id"])
            parent = delivered(state, reference["ticket_id"])
        except GrillError as error:
            return error
        if (
            parent["id"] != reference["implementation_id"]
            or outcome_revision(parent) != reference["outcome_revision"]
            or pull_request_identity(parent["pull_request"])
            != pull_request_identity(reference["pull_request"])
        ):
            return GrillError(
                "dependency_changed", "A entrega do bloqueador mudou; verifique de novo."
            )
        parent_authority = (parent.get("outcome") or {}).get("publication_authorization")
        if (
            authority.get("origin") == "automated"
            and parent_authority
            and parent_authority.get("delegation_revision")
            != authority.get("delegation_revision")
        ):
            return GrillError(
                "dependency_authorization_changed",
                "A autorização do bloqueador não é a mesma do ticket.",
            )
    return None
