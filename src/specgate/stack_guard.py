"""A pre-merge stack fails closed: the happy path moves on and every other state stops.

The judgments read the tracker rows, the host state and the forge, and answer with the
blockers that hold a delivery up. They make no effect and keep no state. A stop carries a
code: only `ticket_blocked` and `stack_unsupported` wait, every other one is a review a
person does (ADR 0023). Nothing here starts a record again, redirects a PR or reads the
local trunk.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from specgate.delivery_dependencies import (
    DeliveryDependency,
    delivered,
    pull_request_identity,
)
from specgate.grill_contracts import GrillError
from specgate.implementation_contracts import (
    DraftPullRequest,
    MergedPullRequest,
    PullRequestPublisher,
)
from specgate.ticket_contracts import require_verified_ticket

# The only codes that wait: nothing a person does is needed for them to pass.
WAITS = frozenset({"ticket_blocked", "stack_unsupported"})


class Stop(GrillError):
    """Why a stack cannot go on, with the blockers that say so and no effect made."""

    def __init__(
        self,
        code: str,
        *,
        dependencies: Sequence[str] = (),
        **details: Any,
    ) -> None:
        super().__init__(code, "A stack não pode seguir sem revisão.")
        self.dependencies = list(dependencies)
        self.details = details


@dataclass(frozen=True)
class Link:
    """One blocker a delivery rests on, and the delivery that rests on it."""

    ticket: str
    consumer: str
    # What the consumer recorded of the blocker; None while the consumer has no record.
    reference: dict[str, Any] | None


def marker(run_id: str, ticket_id: str, implementation_id: str) -> str:
    return f"<!-- dev-decision-implementation:{run_id}:{ticket_id}:{implementation_id} -->"


def publisher_for(
    publishers: Mapping[str, PullRequestPublisher], repository: str
) -> PullRequestPublisher | None:
    matches = [
        item
        for name, item in publishers.items()
        if name.casefold() == repository.casefold()
        and item.repository.casefold() == repository.casefold()
    ]
    return matches[0] if len(matches) == 1 else None


def repository_of(state: dict[str, Any], ticket_id: str) -> str:
    return str(state["tickets"]["publications"][ticket_id]["issue"]["repository"])


def legacy(entry: Mapping[str, Any] | None) -> bool:
    """Whether the record is one the master stacked: it names a parent, with no reference.

    Nothing says what it rests on, so nothing judges its parent, and no state of the forge
    can be explained for it.
    """
    return bool(entry and entry.get("stack_after") and not entry.get("dependencies"))


def chain(
    state: dict[str, Any],
    roots: Sequence[tuple[str, dict[str, Any] | None]],
    consumer: str,
    completed: Sequence[str],
) -> list[Link]:
    """The blockers up the stack, nearest first, with those they rest on in turn.

    A blocker that completed ends its branch: its own references are in the trunk now.
    """
    records = state.get("implementations") or {}
    links: list[Link] = []
    seen: set[tuple[str, str]] = set()
    queue = [Link(ticket, consumer, reference) for ticket, reference in roots]
    while queue:
        link = queue.pop(0)
        if (link.ticket, link.consumer) in seen:
            continue
        seen.add((link.ticket, link.consumer))
        links.append(link)
        if link.ticket in completed:
            continue
        for reference in (records.get(link.ticket) or {}).get("dependencies") or []:
            queue.append(Link(reference["ticket_id"], link.ticket, reference))
    return links


def _hold(failing: Sequence[tuple[Stop, Link]]) -> None:
    """Raise the first stop, in chain order, naming every blocker that stops the same way."""
    if not failing:
        return
    first = failing[0][0]
    blockers = [link.ticket for stop, link in failing if stop.code == first.code]
    raise Stop(first.code, dependencies=list(dict.fromkeys(blockers)), **first.details)


def _issues(
    links: Sequence[Link], completed: Sequence[str], rows: Mapping[str, dict[str, Any]]
) -> None:
    """Level 6: a blocker the tracker no longer offers to the agent holds the stack."""
    failing: list[tuple[Stop, Link]] = []
    for link in links:
        if link.ticket in completed:
            continue
        row = rows.get(link.ticket)
        if row is None:
            failing.append((Stop("tracker_unavailable"), link))
        elif row["state"] != "open":
            failing.append((Stop("dependency_abandoned"), link))
        elif row.get("actionable") is False:
            failing.append((Stop("dependency_not_actionable"), link))
    _hold(failing)


def _records(
    state: dict[str, Any], links: Sequence[Link], completed: Sequence[str]
) -> None:
    """Level 7: the host record of each open blocker is a verified draft of this graph.

    A blocker that still has no PR waits, but never hides one that needs a person. A record
    the master stacked is one of them, whatever its PR shows: the chain does not go down
    from it, so nothing confirms what it rests on, and no pass would ever release it.
    """
    graph = state["tickets"]
    records = state.get("implementations") or {}
    failing: list[tuple[Stop, Link]] = []
    for link in links:
        if link.ticket in completed:
            continue
        try:
            require_verified_ticket(graph, link.ticket)
            if legacy(records.get(link.ticket)):
                raise Stop("legacy_stack")
            delivered(state, link.ticket)
        except GrillError as error:
            failing.append((Stop(error.code), link))
    _hold([item for item in failing if item[0].code not in WAITS] or failing)


async def _merged(publisher: PullRequestPublisher, number: int) -> MergedPullRequest | None:
    try:
        return await publisher.merged_pull_request(number)
    except (OSError, ValueError):
        return None


def moved(entry: dict[str, Any], found: DraftPullRequest, completed: Sequence[str]) -> bool:
    """Whether the forge itself re-pointed a stacked PR to the trunk after its parent merged.

    GitHub does it when the merge deletes the branch of the parent. It is the retarget this
    version asks for, made by the forge, so it counts only once the tracker shows the parent
    completed, and the same judgment confirms the merge. Any other base is a person's doing,
    and so is every base of a record the master stacked, which has no reference to confirm it.
    """
    return (
        not legacy(entry)
        and found.base == entry.get("root_base")
        and entry.get("stack_after") in completed
    )


def rebased(
    entry: dict[str, Any], found: DraftPullRequest, completed: Sequence[str]
) -> bool:
    """Whether the PR of a delivery was moved to the trunk by the forge; any other base stops."""
    if found.base == entry["pull_request"]["base"]:
        return False
    if moved(entry, found, completed):
        return True
    raise Stop("delivery_base_changed")


async def _observe(
    state: dict[str, Any],
    link: Link,
    completed: Sequence[str],
    publishers: Mapping[str, PullRequestPublisher],
) -> DraftPullRequest:
    """The draft PR of an open blocker as its forge shows it now, found by its own marker."""
    entry = state["implementations"][link.ticket]
    recorded = entry["pull_request"]
    publisher = publisher_for(publishers, recorded["repository"])
    if publisher is None:
        raise Stop("dependency_unavailable")
    number = int(recorded["number"])
    shown = False
    try:
        matches = await publisher.find(marker(state["id"], link.ticket, entry["id"]))
    except ValidationError:
        # The forge shows the PR, but not as a draft: it merged or left review.
        matches, shown = [], True
    except (OSError, ValueError) as error:
        raise Stop("dependency_unavailable") from error
    if len(matches) > 1:
        raise Stop("dependency_ambiguous")
    if matches:
        found = matches[0]
        if pull_request_identity(found.model_dump()) != pull_request_identity(recorded):
            raise Stop("dependency_changed")
        if found.base != recorded["base"] and not moved(entry, found, completed):
            raise Stop("dependency_base_changed")
        return found
    try:
        merged = await publisher.merged(number)
    except (OSError, ValueError) as error:
        raise Stop("dependency_unavailable") from error
    if not merged:
        raise Stop("dependency_left_draft" if shown else "dependency_missing")
    facts = await _merged(publisher, number)
    if facts is None:
        raise Stop("dependency_unavailable")
    built_on = (link.reference or {}).get("pull_request") or recorded
    if facts.merged_head != built_on["commit_sha"]:
        raise Stop("dependency_changed")
    # Merged as built, with the issue still open: the stack waits for a person to close it.
    raise Stop("dependency_integrated")


async def _merge_proof(
    state: dict[str, Any],
    link: Link,
    publishers: Mapping[str, PullRequestPublisher],
) -> None:
    """A completed blocker went into the trunk as a merge of the commit the dependent has."""
    entry = (state.get("implementations") or {}).get(link.ticket) or {}
    recorded = entry.get("pull_request")
    if not isinstance(recorded, dict):
        raise Stop("dependency_unavailable")
    publisher = publisher_for(publishers, recorded["repository"])
    if publisher is None:
        raise Stop("dependency_unavailable")
    number = int(recorded["number"])
    try:
        merged = await publisher.merged(number)
    except (OSError, ValueError) as error:
        raise Stop("dependency_unavailable") from error
    if not merged:
        raise Stop("dependency_not_integrated", reason="not_merged")
    facts = await _merged(publisher, number)
    if facts is None:
        raise Stop("dependency_unavailable")
    built_on = (link.reference or {}).get("pull_request") or recorded
    if facts.merged_head != built_on["commit_sha"]:
        raise Stop("dependency_changed")
    if facts.method != "merge":
        # Squash and rebase leave the commit the dependent was built on out of the trunk.
        raise Stop("dependency_not_integrated", reason=facts.method)
    if facts.base != entry.get("root_base"):
        raise Stop("dependency_not_integrated", reason="other_base")


async def _forge(
    state: dict[str, Any],
    links: Sequence[Link],
    completed: Sequence[str],
    publishers: Mapping[str, PullRequestPublisher],
) -> dict[str, DraftPullRequest]:
    """Level 8: what the forge shows of each blocker is what the host recorded."""
    observed: dict[str, DraftPullRequest] = {}
    failing: list[tuple[Stop, Link]] = []
    for link in links:
        if link.ticket in observed:
            continue
        try:
            if link.ticket in completed:
                await _merge_proof(state, link, publishers)
            else:
                observed[link.ticket] = await _observe(state, link, completed, publishers)
        except Stop as stop:
            failing.append((stop, link))
    _hold(failing)
    return observed


async def judge(
    state: dict[str, Any],
    roots: Sequence[tuple[str, dict[str, Any] | None]],
    consumer: str,
    *,
    completed: Sequence[str],
    rows: Mapping[str, dict[str, Any]],
    publishers: Mapping[str, PullRequestPublisher],
) -> dict[str, DraftPullRequest]:
    """The draft of each open blocker up the stack, or the stop that holds it.

    Levels 6 to 8 of the precedence: the tracker, then the host record, then the forge.
    The same judgment serves a delivery that begins, one that goes on and a draft.
    """
    links = chain(state, roots, consumer, completed)
    _issues(links, completed, rows)
    _records(state, links, completed)
    return await _forge(state, links, completed, publishers)


async def own(
    state: dict[str, Any],
    ticket_id: str,
    publisher: PullRequestPublisher,
    *,
    trunk: str | None = None,
) -> DraftPullRequest:
    """The PR of a delivery is still the draft the host recorded, as its forge shows it.

    `trunk` is the one other base the forge may show; `rebased` says whether it counts.
    """
    entry = state["implementations"][ticket_id]
    recorded = entry["pull_request"]
    try:
        matches = await publisher.find(marker(state["id"], ticket_id, entry["id"]))
    except ValidationError as error:
        raise Stop("delivery_mismatch", reason="left_draft") from error
    except (OSError, ValueError) as error:
        raise Stop("publication_unknown") from error
    if len(matches) > 1:
        raise Stop("ambiguous_delivery")
    if not matches:
        raise Stop("delivery_mismatch", reason="missing")
    found = matches[0]
    if pull_request_identity(found.model_dump()) != pull_request_identity(recorded):
        raise Stop("delivery_mismatch", reason="changed")
    if found.base not in {recorded["base"], trunk}:
        raise Stop("delivery_base_changed")
    return found


async def publication_of(
    state: dict[str, Any], entry: dict[str, Any], publisher: PullRequestPublisher
) -> dict[str, Any]:
    """What the forge holds of a publication that may have been claimed, for the review.

    A record that claimed its publication may have a PR the host never heard of, and the
    record does not always show it: the checks of a later pass replace the passed ones that
    came with the claim. So every record that went past `pending` and has no PR asks the
    forge, which is a read. The review says what it found and creates nothing, since only a
    person can settle it.
    """
    if entry.get("pull_request") or entry.get("status") == "pending":
        return {}
    try:
        matches = await publisher.find(marker(state["id"], entry["ticket"], entry["id"]))
    except ValidationError:
        return {"publication": "left_draft"}
    except (OSError, ValueError):
        return {"publication": "unknown"}
    if len(matches) > 1:
        return {"publication": "ambiguous"}
    if matches:
        return {"publication": "found", "pull_request": matches[0].url}
    return {"publication": "none"}


def unreferenced(
    state: dict[str, Any],
    ticket_id: str,
    references: Sequence[dict[str, Any]],
    completed: Sequence[str],
) -> None:
    """A blocker that was complete when the record began, left out of its references, is open again.

    No reference names it and the frontier no longer holds the ticket, so what the record
    rests on is not what it was. The ticket waits, as one with no references does.
    """
    ticket = next(
        item for item in state["tickets"]["draft"]["tickets"] if item["id"] == ticket_id
    )
    held = {item["ticket_id"] for item in references}
    reopened = [
        key for key in ticket["blocked_by"] if key not in completed and key not in held
    ]
    if reopened:
        raise Stop("ticket_blocked", dependencies=reopened)


def shape(
    state: dict[str, Any],
    ticket: dict[str, Any],
    completed: Sequence[str],
    references: Sequence[DeliveryDependency],
) -> str | None:
    """The parent a new delivery is stacked on, None when it only consumes references.

    Every open blocker of the same repository has to be in the stack of one of them, which
    is then the base, and every completed one has to be in that stack too: the proof of its
    merge came with the chain. Anything else is a shape this version does not support.
    """
    records = state["implementations"]
    own_repository = repository_of(state, ticket["id"]).casefold()
    open_same = [
        item.ticket_id
        for item in references
        if item.pull_request.repository.casefold() == own_repository
    ]
    if not open_same:
        return None
    done_same = [
        key
        for key in ticket["blocked_by"]
        if key in completed and repository_of(state, key).casefold() == own_repository
    ]
    parent = next(
        (
            key
            for key in open_same
            if set(open_same) <= {*records[key].get("stack", []), key}
        ),
        None,
    )
    if parent is None or not set(done_same) <= {*records[parent].get("stack", []), parent}:
        raise Stop("stack_unsupported", dependencies=open_same)
    return parent
