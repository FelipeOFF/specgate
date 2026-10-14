"""Run one unlocked ticket through a harness and reconcile its draft PR."""

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from specgate.client import automatic_decision_valid
from specgate.context import ContextPacket, build_context
from specgate.delegation import (
    publication_authority,
    review_authority_valid,
    verification_allows_review,
)
from specgate.delivery_dependencies import (
    DeliveryDependency,
    outcome_revision,
    same_references,
    same_repository,
    validate_dependency,
)
from specgate.grill_contracts import GrillError
from specgate.implementation_contracts import DraftPullRequest, PullRequestPublisher
from specgate.payload import PayloadTooLarge, oversize
from specgate.privacy import ensure_safe_content
from specgate.rewrite_note import BODY_LIMIT, rewrite_note
from specgate.spec_contracts import IssueReference
from specgate.spec_rewrite import rewrite_audit
from specgate.spec_verification import (
    interpret_verification,
    is_operational_claim,
    pack_evidence,
    ticket_claims,
)
from specgate.stack_guard import (
    WAITS,
    Stop,
    judge,
    legacy,
    marker,
    own,
    publication_of,
    rebased,
    shape,
    unreferenced,
)
from specgate.ticket_client import TicketClient
from specgate.ticket_contracts import require_verified_ticket
from specgate.tracker import TicketTracker, TrackerError


class RepositoryDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, arbitrary_types_allowed=True)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    root: Path
    base: str = Field(min_length=1, max_length=200)
    checks: tuple[tuple[str, ...], ...] = Field(min_length=1, max_length=50)


class DeliveryManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    delivery_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
    repositories: tuple[RepositoryDelivery, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_manifest(self) -> "DeliveryManifest":
        repositories = [item.repository.casefold() for item in self.repositories]
        roots = [item.root.expanduser().resolve() for item in self.repositories]
        if len(repositories) != len(set(repositories)) or len(roots) != len(set(roots)):
            raise ValueError("Delivery repositories and roots must be unique.")
        payload = self.model_dump(mode="json")
        ensure_safe_content(payload)
        for repository in self.repositories:
            for command in repository.checks:
                if not command or any(
                    re.search(
                        r"(?i)(?:api[_-]?key|token|secret|password)\s*=", argument
                    )
                    for argument in command
                ):
                    raise ValueError("Checks must not contain credentials.")
        return self

    def repository(self, name: str) -> RepositoryDelivery:
        matches = [
            item
            for item in self.repositories
            if item.repository.casefold() == name.casefold()
        ]
        if len(matches) != 1:
            raise ValueError("Repository is not authorized by the delivery manifest.")
        return matches[0]


class HarnessDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, arbitrary_types_allowed=True)
    branch: str = Field(pattern=r"^(feat|fix|hotfix|chore|docs|refactor|test|perf)/.+")
    worktree: Path
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    summary: str = Field(min_length=1, max_length=8000)


class ImplementationHarness(Protocol):
    async def implement(self, context: dict[str, Any]) -> HarnessDelivery: ...


async def _command(cwd: Path, command: Sequence[str]) -> tuple[int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as error:
        return 127, str(error)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 300)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124, "timeout"
    return process.returncode or 0, output.decode(errors="replace")[-8000:]


def _verification_note(result: dict[str, Any] | None) -> str:
    """What Jev judged and by which authority, for whoever reviews the draft.

    A score is a filter of uncertainty: the note says so next to the numbers.
    """
    if not result:
        return ""
    stored = result.get("gate")
    gate: dict[str, Any] = stored if isinstance(stored, dict) else {}
    approved = "aprovada" if gate.get("passed") is True else "não aprovada"
    lines = [
        (
            f"Verificação do Jev dos critérios de aceite: base do gate `{gate.get('basis') or 'ausente'}`, "
            f"{approved} pelo gate, calibrado: {'sim' if result.get('calibrated') is True else 'não'}. "
            "O score filtra incerteza e não mede acurácia."
        ),
        "",
        "| Critério | Veredito | Confiança | Suporte |",
        "| --- | --- | --- | --- |",
    ]
    for row in result.get("verdicts", []):
        support = (row.get("probabilities") or {}).get("supports")
        lines.append(
            f"| {row.get('id')} | {row.get('verdict')} | {row.get('confidence')} | {support} |"
        )
    return "\n".join(lines) + "\n\n"


def _review(state: dict[str, Any], code: str, **details: Any) -> dict[str, Any]:
    return {
        **state,
        "action": "needs_human",
        "auto_advance": False,
        "error": {
            "code": code,
            "message": "Revise a implementação antes de continuar.",
            **details,
        },
    }


def _stopped(state: dict[str, Any], stop: Stop) -> dict[str, Any]:
    """The review a stop is: its code, the blockers that say so, and no effect."""
    details = dict(stop.details)
    if stop.dependencies:
        details["dependencies"] = stop.dependencies
    return _review(state, stop.code, **details)


def _actionable(rows: Mapping[str, dict[str, Any]], ticket_id: str) -> None:
    """Level 5: the issue of the ticket itself is still open for the agent."""
    row = rows.get(ticket_id)
    if row is None or row["state"] != "open" or row.get("actionable") is False:
        raise Stop("ticket_not_actionable")


def _references(
    state: dict[str, Any],
    blockers: Sequence[str],
    observed: Mapping[str, DraftPullRequest],
    delivery_id: str,
) -> list[DeliveryDependency]:
    """The reference to the delivery each open blocker has now, as the host recorded it."""
    references = []
    for blocker in blockers:
        entry = state["implementations"][blocker]
        dependency = DeliveryDependency(
            ticket_id=blocker,
            implementation_id=entry["id"],
            graph_revision=state["tickets"]["revision"],
            outcome_revision=outcome_revision(entry),
            pull_request=observed[blocker],
        )
        try:
            validate_dependency(state, dependency, delivery_id)
        except GrillError as error:
            raise Stop(error.code, dependencies=[blocker]) from error
        references.append(dependency)
    return references


class Start(NamedTuple):
    """What a delivery without a record begins with, once its blockers were judged."""

    observations: list[dict[str, Any]]
    references: list[DeliveryDependency]
    base: str
    stack_after: str | None
    root_base: str | None


def _misplaced(
    state: dict[str, Any],
    existing: dict[str, Any],
    publisher: PullRequestPublisher,
    base: str,
) -> dict[str, Any] | None:
    if base != (existing.get("root_base") or existing["delivery"]["base"]):
        return _review(state, "base_mismatch")
    if publisher.repository.casefold() != existing["issue"]["repository"].casefold():
        return _review(state, "repository_mismatch")
    return None


def _dependency_note(
    state: dict[str, Any], ticket_id: str, entry: dict[str, Any]
) -> str:
    """Tells the reviewer what the draft rests on, since a draft is not a merge.

    Only the reference the PR is stacked on (`stack_after`) is its base and has a retarget.
    The others of the same repository are deliveries already in that stack, and those of
    another repository are consumed, never stacked.
    """
    references = entry.get("dependencies", [])
    parent = next(
        (item for item in references if item["ticket_id"] == entry.get("stack_after")),
        None,
    )
    lines = []
    for reference in references:
        pull_request = reference["pull_request"]
        commit = pull_request["commit_sha"][:12]
        if reference is parent:
            lines.append(
                f"Empilhada sobre {pull_request['url']} (draft, commit `{commit}`): a base é "
                f"`{pull_request['head']}` até o merge dela e volta para "
                f"`{entry['root_base']}` depois, sem reescrever histórico."
            )
        elif parent is not None and same_repository(
            state, ticket_id, pull_request["repository"]
        ):
            lines.append(
                f"Inclui a entrega de {pull_request['url']} (draft, commit `{commit}`), "
                f"já na stack de {parent['pull_request']['url']}."
            )
        else:
            lines.append(
                f"Consome a entrega de {pull_request['url']} (draft, commit `{commit}`), "
                "de outro repositório, sem empilhar branch."
            )
    return "\n\n".join(lines) + "\n\n" if lines else ""


class ImplementationClient(TicketClient):
    async def _rows(
        self,
        state: dict[str, Any],
        tracker: TicketTracker | Mapping[str, TicketTracker],
    ) -> dict[str, dict[str, Any]]:
        """The issue of every ticket as its tracker shows it now; an outage is a stop."""
        try:
            observed = await self._observations(state, tracker)
        except (TrackerError, OSError, ValueError) as error:
            raise Stop("tracker_unavailable") from error
        return {row["ticket_id"]: row for row in observed}

    def _grant(
        self,
        state: dict[str, Any],
        blockers: Sequence[str],
        observed: Mapping[str, DraftPullRequest],
        publication_authorized: bool,
    ) -> None:
        """Level 9: the grant a blocker was drafted under still holds, or a person authorizes."""
        if publication_authorized:
            return
        for blocker in blockers:
            outcome = state["implementations"][blocker].get("outcome") or {}
            authority = outcome.get("publication_authorization")
            if authority and not review_authority_valid(
                authority,
                host=self.url,
                repository=observed[blocker].repository,
                operation="draft_pr",
                home=self.home,
            ):
                raise Stop("dependency_authorization_changed", dependencies=[blocker])

    async def _gate(
        self,
        state: dict[str, Any],
        existing: dict[str, Any],
        publisher: PullRequestPublisher,
        publishers: Mapping[str, PullRequestPublisher],
        tracker: TicketTracker | Mapping[str, TicketTracker],
        *,
        publication_authorized: bool,
    ) -> None:
        """Whether a record with no published PR may go on; a stop says why not.

        It is asked before the harness and again before the claim, and it only ever
        reads: a record never starts again, whatever its blockers did.
        """
        ticket_id = existing["ticket"]
        if existing["ticket_revision"] != state["tickets"]["revision"]:
            raise Stop(
                "implementation_stale",
                **await publication_of(state, existing, publisher),
            )
        if existing.get("pull_request"):
            # A PR the host recorded would be erased by a new claim.
            raise Stop(existing["status"], reason="pull_request_recorded")
        if legacy(existing):
            # The master stacked this record: it has no references to judge.
            raise Stop("legacy_stack")
        frontier = await self.frontier(state["id"], tracker)
        if "error" in frontier:
            raise Stop(frontier["error"]["code"])
        completed = frontier["completed_tickets"]
        references = existing.get("dependencies") or []
        if not references:
            if ticket_id not in frontier["frontier"]:
                raise Stop("ticket_blocked")
            return
        done = [item["ticket_id"] for item in references if item["ticket_id"] in completed]
        if done:
            raise Stop(
                "dependency_completed",
                dependencies=done,
                **await publication_of(state, existing, publisher),
            )
        rows = await self._rows(state, tracker)
        _actionable(rows, ticket_id)
        blockers = [item["ticket_id"] for item in references]
        observed = await judge(
            state,
            [(item["ticket_id"], item) for item in references],
            ticket_id,
            completed=completed,
            rows=rows,
            publishers=publishers,
        )
        self._grant(state, blockers, observed, publication_authorized)
        fresh = _references(
            state, blockers, observed, existing.get("delivery_id", state["id"])
        )
        if not same_references(references, fresh):
            raise Stop("dependency_changed", dependencies=blockers)
        unreferenced(state, ticket_id, references, completed)

    async def _plan(
        self,
        state: dict[str, Any],
        ticket: dict[str, Any],
        publishers: Mapping[str, PullRequestPublisher],
        tracker: TicketTracker | Mapping[str, TicketTracker],
        *,
        base: str,
        delivery_id: str,
        publication_authorized: bool,
    ) -> Start:
        """Where a ticket without a record begins: the trunk, or a stack on verified drafts.

        The ticket outside the frontier starts only when every blocker it still waits for
        is a verified draft the forge confirms, and the shape of the stack is one this
        version supports. Everything else is a stop.
        """
        frontier = await self.frontier(state["id"], tracker)
        if "error" in frontier:
            raise Stop(frontier["error"]["code"])
        completed = frontier["completed_tickets"]
        pending = [key for key in ticket["blocked_by"] if key not in completed]
        if not pending and ticket["id"] not in frontier["frontier"]:
            raise Stop("ticket_blocked")
        rows = await self._rows(state, tracker)
        observations = list(rows.values())
        if not pending:
            return Start(observations, [], base, None, None)
        _actionable(rows, ticket["id"])
        observed = await judge(
            state,
            [(key, None) for key in pending],
            ticket["id"],
            completed=completed,
            rows=rows,
            publishers=publishers,
        )
        self._grant(state, pending, observed, publication_authorized)
        references = _references(state, pending, observed, delivery_id)
        parent = shape(state, ticket, completed, references)
        if parent is None:
            return Start(observations, references, base, None, None)
        return Start(observations, references, observed[parent].head, parent, base)

    async def _draft(
        self,
        state: dict[str, Any],
        ticket_id: str,
        tracker: TicketTracker | Mapping[str, TicketTracker],
        publisher: PullRequestPublisher,
        publishers: Mapping[str, PullRequestPublisher],
    ) -> dict[str, Any]:
        """A recorded draft stays as it is; one that no longer holds is held for review.

        Nothing is discarded or rewritten: the draft keeps its record and its PR, and the
        review names the blockers that stop it.
        """
        entry = state["implementations"][ticket_id]
        if not entry.get("dependencies"):
            return state
        try:
            found = await own(
                state,
                ticket_id,
                publisher,
                trunk=entry["root_base"] if entry.get("stack_after") else None,
            )
            if entry["ticket_revision"] != state["tickets"]["revision"]:
                raise Stop("implementation_stale")
            frontier = await self.frontier(state["id"], tracker)
            if "error" in frontier:
                raise Stop(frontier["error"]["code"])
            completed = frontier["completed_tickets"]
            rebased(entry, found, completed)
            rows = await self._rows(state, tracker)
            _actionable(rows, ticket_id)
            await judge(
                state,
                [(item["ticket_id"], item) for item in entry["dependencies"]],
                ticket_id,
                completed=completed,
                rows=rows,
                publishers=publishers,
            )
        except Stop as stop:
            held = _stopped(state, stop)
            if stop.code == "dependency_changed":
                held["stale_dependencies"] = stop.dependencies
            return held
        return state

    async def _observations(
        self,
        state: dict[str, Any],
        tracker: TicketTracker | Mapping[str, TicketTracker],
    ) -> list[dict[str, Any]]:
        observations = []
        graph = state["tickets"]
        for ticket_id in graph["order"]:
            issue = graph["publications"].get(ticket_id, {}).get("issue")
            if issue is None:
                continue
            current = await self._tracker(
                issue["repository"], tracker
            ).issue_state(IssueReference.model_validate(issue).lookup_id)
            observations.append(
                {
                    "ticket_id": ticket_id,
                    "issue_id": current.id,
                    "number": current.number,
                    "external_id": current.external_id,
                    "state": current.state,
                    "state_reason": current.state_reason,
                    **({"actionable": False} if not current.actionable else {}),
                }
            )
        return observations

    async def _verify_ticket(
        self,
        state: dict[str, Any],
        ticket: dict[str, Any],
        worktree: Path,
        check_results: list[dict[str, Any]],
        *,
        context: ContextPacket | None = None,
        require_automatic: bool = False,
        record: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Hold the delivery for review unless the evidence supports every claim.

        Without a human authorization the verdict is the whole decision to publish, so it
        also has to clear the predicate of its authority, bound to this request.
        """
        claims = ticket_claims(ticket)
        code_claims = [
            claim for claim in claims if not is_operational_claim(claim.text)
        ]
        operational = [claim for claim in claims if is_operational_claim(claim.text)]
        if not code_claims:
            reviewed = _review(state, "verification_operational")
            reviewed["verification"] = {
                "claims": [claim.id for claim in operational],
                "auto_advance": False,
            }
            return reviewed
        try:
            evidence = pack_evidence(worktree, check_results, ticket)
        except ValueError as error:
            if (overflow := oversize(error)) is None:
                raise
            return overflow.review(state)
        arguments: dict[str, Any] = {
            "claims": [claim.model_dump() for claim in code_claims],
            "evidence": evidence,
            **({"context": asdict(context)} if require_automatic and context else {}),
        }
        verification = await self._call("jev_verify", **arguments)
        if overflow := PayloadTooLarge.from_result(verification):
            return overflow.review(state)
        if record is not None:
            record["result"] = verification
        verdicts = verification.get("verdicts")
        if not isinstance(verdicts, list):
            return _review(state, "verification_failed")
        verdicts = [
            *verdicts,
            *({"id": claim.id, "verdict": "unsupported"} for claim in operational),
        ]
        decision = interpret_verification(verdicts, claims)
        if decision.action == "publish":
            if not require_automatic or (
                context is not None
                and automatic_decision_valid(
                    verification, "jev_verify", arguments, context
                )
                # The run's threshold binds this decision like the spec and the tickets.
                and verification_allows_review(
                    [verification],
                    state.get("workflow_policy", {}).get("confidence", 0.8),
                    contexts=[context.revision],
                )
            ):
                return None
            reviewed = _review(state, "verification_gate_closed")
            reviewed["verification"] = verification
            return reviewed
        reviewed = _review(state, decision.code)
        reviewed["verification"] = verification
        reviewed["auto_advance"] = False
        return reviewed

    async def implement(
        self,
        run_id: str,
        ticket_id: str,
        project: Path,
        tracker: TicketTracker | Mapping[str, TicketTracker],
        harness: ImplementationHarness,
        publisher: PullRequestPublisher,
        *,
        sources: Sequence[str],
        required: Sequence[str],
        checks: Sequence[Sequence[str]],
        base: str,
        publication_authorized: bool,
        idempotency_key: str,
        delivery_id: str | None = None,
        dependency_publishers: Mapping[str, PullRequestPublisher] | None = None,
    ) -> dict[str, Any]:
        """Open the draft PR of one ticket; `base` is the trunk of its repository.

        A ticket whose blockers are all complete starts from that trunk. One that waits
        only on blockers with a verified draft starts before their merge: in the same
        repository it is stacked on the draft PR of its parent, and a blocker of another
        repository is consumed as a reference, never as a branch. Anything outside that
        path stops for a person with a code of its own and no effect (ADR 0023).
        """
        publishers = dependency_publishers or {publisher.repository: publisher}
        state = await self.get(run_id)
        if "error" in state:
            return state
        if state.get("tickets"):
            try:
                require_verified_ticket(state["tickets"], ticket_id)
            except GrillError as error:
                return _review(state, error.code)
        existing = state.get("implementations", {}).get(ticket_id)
        if (
            existing
            and delivery_id is not None
            and existing.get("delivery_id", run_id) != delivery_id
        ):
            return _review(state, "delivery_mismatch")
        if existing and existing["status"] == "draft":
            return await self._draft(state, ticket_id, tracker, publisher, publishers)
        if not checks or any(not command for command in checks):
            return _review(state, "invalid_check")
        if existing and existing["status"] == "publishing":
            return await self._publishing(
                state, existing, publisher, base=base, idempotency_key=idempotency_key
            )
        graph = state.get("tickets") or {}
        ticket = next(
            (
                item
                for item in graph.get("draft", {}).get("tickets", [])
                if item["id"] == ticket_id
            ),
            None,
        )
        if ticket is None:
            # Outside the graph, or no graph: the frontier says which, as before.
            frontier = await self.frontier(run_id, tracker)
            return frontier if "error" in frontier else _review(state, "ticket_blocked")
        start: Start | None = None
        try:
            if existing:
                if misplaced := _misplaced(state, existing, publisher, base):
                    return misplaced
                await self._gate(
                    state,
                    existing,
                    publisher,
                    publishers,
                    tracker,
                    publication_authorized=publication_authorized,
                )
            else:
                start = await self._plan(
                    state,
                    ticket,
                    publishers,
                    tracker,
                    base=base,
                    delivery_id=delivery_id or run_id,
                    publication_authorized=publication_authorized,
                )
        except Stop as stop:
            return _stopped(state, stop)
        packet = build_context(
            project,
            ticket["behavior"],
            list(sources),
            list(required),
            artifact=state["spec"]["body"],
            alternatives=["implementar", "revisar ticket"],
        )
        if packet.gaps or packet.conflicts:
            return _review(state, "implementation_context_incomplete")
        if start is not None:
            state = await self._call(
                "implementation_begin",
                run_id=run_id,
                expected_revision=state["revision"],
                idempotency_key=sha256(
                    f"{idempotency_key}:begin:{ticket_id}".encode()
                ).hexdigest(),
                ticket_id=ticket_id,
                observations=start.observations,
                base=start.base,
                delivery_id=delivery_id,
                stack_after=start.stack_after,
                root_base=start.root_base,
                **(
                    {"dependencies": [item.model_dump() for item in start.references]}
                    if start.references
                    else {}
                ),
            )
            if "error" in state:
                return state
            existing = state["implementations"][ticket_id]
        if (
            delivery_id is not None
            and existing.get("delivery_id", run_id) != delivery_id
        ):
            return _review(state, "delivery_mismatch")
        if misplaced := _misplaced(state, existing, publisher, base):
            return misplaced
        # The PR opens on the parent's branch when stacked, on the trunk otherwise.
        pull_request_base = existing["delivery"]["base"]
        delivery = await harness.implement(
            {
                "spec": state["spec"],
                "ticket": ticket,
                "issue": existing["issue"],
                "delivery_id": existing.get("delivery_id", run_id),
                "base": pull_request_base,
                "dependencies": existing.get("dependencies", []),
                "context": asdict(packet),
                "directive": "Implemente somente este ticket em worktree isolado e execute os checks informados.",
            }
        )
        worktree = delivery.worktree.resolve()
        if worktree == project.resolve() or not worktree.is_dir():
            return _review(state, "invalid_worktree")
        project_result, project_common = await _command(
            project, ("git", "rev-parse", "--git-common-dir")
        )
        worktree_result, worktree_common = await _command(
            worktree, ("git", "rev-parse", "--git-common-dir")
        )
        project_git = Path(project_common.strip())
        worktree_git = Path(worktree_common.strip())
        if (
            project_result
            or worktree_result
            or (project / project_git).resolve() != (worktree / worktree_git).resolve()
        ):
            return _review(state, "invalid_worktree")
        branch_result, branch = await _command(
            worktree, ("git", "branch", "--show-current")
        )
        commit_result, commit = await _command(worktree, ("git", "rev-parse", "HEAD"))
        if (
            branch_result
            or commit_result
            or branch.strip() != delivery.branch
            or commit.strip() != delivery.commit_sha
        ):
            return _review(state, "invalid_worktree")
        # A stacked PR must really carry the verified commit of its parent.
        for reference in existing.get("dependencies", []):
            commit_sha = reference["pull_request"]["commit_sha"]
            if not same_repository(
                state, ticket_id, reference["pull_request"]["repository"]
            ):
                continue
            ancestor, _ = await _command(
                worktree, ("git", "merge-base", "--is-ancestor", commit_sha, "HEAD")
            )
            if ancestor:
                return _review(state, "dependency_not_integrated", reason="not_carried")
        check_results = []
        for command in checks:
            if not command:
                return _review(state, "invalid_check")
            returncode, _ = await _command(worktree, command)
            check_results.append({"name": command[0], "passed": returncode == 0})
        outcome: dict[str, Any] = {
            "branch": delivery.branch,
            "commit_sha": delivery.commit_sha,
            "summary": delivery.summary,
            "checks": check_results,
            "pull_request": None,
        }
        if not all(check["passed"] for check in check_results):
            return await self._call(
                "implementation_record",
                run_id=run_id,
                expected_revision=state["revision"],
                idempotency_key=sha256(
                    f"{idempotency_key}:checks:{existing['id']}:{delivery.commit_sha}".encode()
                ).hexdigest(),
                ticket_id=ticket_id,
                implementation_id=existing["id"],
                outcome=outcome,
            )
        verified: dict[str, Any] = {}
        verification = await self._verify_ticket(
            state,
            ticket,
            worktree,
            check_results,
            context=packet,
            require_automatic=not publication_authorized,
            record=verified,
        )
        if verification is not None:
            return verification
        if existing.get("dependencies"):
            # The harness and the checks took time: the stack must still be what was recorded.
            current = await self.get(run_id)
            try:
                await self._gate(
                    current,
                    current["implementations"][ticket_id],
                    publisher,
                    publishers,
                    tracker,
                    publication_authorized=publication_authorized,
                )
            except Stop as stop:
                return _stopped(current, stop)
        if not publication_authorized:
            authority = publication_authority(
                project=project,
                host=self.url,
                repositories=[publisher.repository],
                operation="draft_pr",
                home=self.home,
            )
            if not authority:
                return _review(state, "publication_authorization_required")
            outcome["publication_authorization"] = authority

        claim = marker(run_id, ticket_id, existing["id"])
        state = await self._call(
            "implementation_record",
            run_id=run_id,
            expected_revision=state["revision"],
            idempotency_key=sha256(
                f"{idempotency_key}:publication:{existing['id']}:{delivery.commit_sha}".encode()
            ).hexdigest(),
            ticket_id=ticket_id,
            implementation_id=existing["id"],
            outcome=outcome,
        )
        if "error" in state:
            return state
        if not state.get("publication_claimed"):
            return _review(state, "publication_unknown")
        try:
            matches = await publisher.find(claim)
        except ValidationError:
            return _review(state, "publication_left_draft")
        except (OSError, ValueError):
            return _review(state, "publication_unknown")
        if len(matches) > 1:
            return _review(state, "ambiguous_delivery")
        opening = (
            f"## Summary\n\n{delivery.summary}\n\n"
            f"## Changes\n\nImplementa {existing['issue']['url']}.\n\n"
            f"{_dependency_note(state, ticket_id, existing)}"
            f"Delivery: `{existing.get('delivery_id', run_id)}`.\n\n"
            "## Test plan\n\nTodos os checks configurados passaram no commit informado.\n\n"
            f"{_verification_note(verified.get('result'))}"
        )
        closing = f"{claim}\n"
        # The rewrites the delegation made behind the spec this delivery rests on, each next to
        # its original; none, and the body is the one it always was. The marker stays last.
        body = (
            opening
            + rewrite_note(
                rewrite_audit(state),
                BODY_LIMIT - len((opening + closing).encode()),
            )
            + closing
        )
        try:
            if not publication_authorized and not review_authority_valid(
                outcome["publication_authorization"],
                host=self.url,
                repository=publisher.repository,
                operation="draft_pr",
                home=self.home,
            ):
                return _review(state, "publication_authorization_changed")
            pull_request = (
                matches[0]
                if matches
                else await publisher.create(
                    ticket["title"],
                    body,
                    delivery.branch,
                    pull_request_base,
                    delivery.commit_sha,
                )
            )
        except (OSError, ValueError):
            return _review(state, "publication_unknown")
        outcome["pull_request"] = pull_request.model_dump()
        return await self._call(
            "implementation_record",
            run_id=run_id,
            expected_revision=state["revision"],
            idempotency_key=sha256(
                f"{idempotency_key}:record:{existing['id']}:{pull_request.number}".encode()
            ).hexdigest(),
            ticket_id=ticket_id,
            implementation_id=existing["id"],
            outcome=outcome,
        )

    async def _publishing(
        self,
        state: dict[str, Any],
        existing: dict[str, Any],
        publisher: PullRequestPublisher,
        *,
        base: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Record the draft PR a claimed publication already created on the forge.

        It does not ask what became of the blockers: the effect already happened. A forge
        that shows none, shows more than one or does not answer is a review, and so is a
        record of an earlier graph, which the host would refuse.
        """
        if misplaced := _misplaced(state, existing, publisher, base):
            return misplaced
        if existing["ticket_revision"] != state["tickets"]["revision"]:
            return _review(
                state,
                "implementation_stale",
                **await publication_of(state, existing, publisher),
            )
        try:
            matches = await publisher.find(
                marker(state["id"], existing["ticket"], existing["id"])
            )
        except ValidationError:
            return _review(state, "publication_left_draft")
        except (OSError, ValueError):
            return _review(state, "publication_unknown")
        if len(matches) > 1:
            return _review(state, "ambiguous_delivery")
        if not matches:
            return _review(state, "publication_unknown")
        resumed_outcome = {**existing["outcome"], "pull_request": matches[0].model_dump()}
        return await self._call(
            "implementation_record",
            run_id=state["id"],
            expected_revision=state["revision"],
            idempotency_key=sha256(
                f"{idempotency_key}:record:{existing['id']}:{matches[0].number}".encode()
            ).hexdigest(),
            ticket_id=existing["ticket"],
            implementation_id=existing["id"],
            outcome=resumed_outcome,
        )

    async def retarget(
        self,
        run_id: str,
        ticket_id: str,
        worktree: Path,
        publisher: PullRequestPublisher,
        *,
        tracker: TicketTracker | Mapping[str, TicketTracker],
        checks: Sequence[Sequence[str]],
        idempotency_key: str,
        dependency_publishers: Mapping[str, PullRequestPublisher] | None = None,
    ) -> dict[str, Any]:
        """Move the PR of a stacked ticket to its original base once its parent merged.

        The parent has to be completed in the tracker and merged as a merge commit of the
        commit this PR was built on, into the trunk, as its forge says. Every other state
        is a stop and the forge is not touched.
        """
        publishers = dependency_publishers or {publisher.repository: publisher}
        state = await self.get(run_id)
        if "error" in state:
            return state
        if state.get("tickets"):
            try:
                require_verified_ticket(state["tickets"], ticket_id)
            except GrillError as error:
                return _review(state, error.code)
        existing = state.get("implementations", {}).get(ticket_id)
        parent_id = existing.get("stack_after") if isinstance(existing, dict) else None
        parent = (
            state.get("implementations", {}).get(parent_id)
            if isinstance(parent_id, str)
            else None
        )
        stored = existing.get("pull_request") if isinstance(existing, dict) else None
        parent_pr = parent.get("pull_request") if isinstance(parent, dict) else None
        references = existing.get("dependencies") if isinstance(existing, dict) else None
        if (
            not isinstance(existing, dict)
            or existing.get("status") not in {"draft", "checks_failed"}
            or not isinstance(stored, dict)
            or not isinstance(parent_pr, dict)
            or publisher.repository.casefold()
            != existing["issue"]["repository"].casefold()
        ):
            return _review(state, "retarget_unavailable")
        # The master stacked a record when no reference names its parent.
        by_master = not any(item["ticket_id"] == parent_id for item in references or [])
        try:
            # The base on the trunk is explained by a merge the references confirm, which a
            # record of the master has none of: for it, any base but the recorded one stops.
            found = await own(
                state,
                ticket_id,
                publisher,
                trunk=None if by_master else str(existing["root_base"]),
            )
            if existing["ticket_revision"] != state["tickets"]["revision"]:
                raise Stop("implementation_stale")
            if by_master:
                raise Stop("legacy_stack")
            frontier = await self.frontier(run_id, tracker)
            if "error" in frontier:
                raise Stop(frontier["error"]["code"])
            completed = frontier["completed_tickets"]
            forged = rebased(existing, found, completed)
            rows = await self._rows(state, tracker)
            _actionable(rows, ticket_id)
            await judge(
                state,
                [(item["ticket_id"], item) for item in references or []],
                ticket_id,
                completed=completed,
                rows=rows,
                publishers=publishers,
            )
            if parent_id not in completed:
                raise Stop("base_still_open", dependencies=[str(parent_id)])
            unreferenced(state, ticket_id, references or [], completed)
        except Stop as stop:
            return _stopped(state, stop)
        commit_result, commit = await _command(worktree, ("git", "rev-parse", "HEAD"))
        if commit_result or commit.strip() != stored["commit_sha"]:
            return _review(state, "history_rewrite_refused")
        updated = found
        if not forged:
            try:
                updated = await publisher.retarget(
                    int(stored["number"]), str(existing["root_base"])
                )
            except (OSError, ValueError):
                return _review(state, "publication_unknown")
        if (
            updated.head != stored["head"]
            or updated.commit_sha != stored["commit_sha"]
            or updated.base != existing["root_base"]
            or updated.number != stored["number"]
        ):
            return _review(state, "history_rewrite_refused")
        check_results = []
        for command in checks:
            if not command:
                return _review(state, "invalid_check")
            returncode, _output = await _command(worktree, command)
            check_results.append({"name": command[0], "passed": returncode == 0})
        return await self._call(
            "implementation_retarget",
            run_id=run_id,
            expected_revision=state["revision"],
            idempotency_key=sha256(
                f"{idempotency_key}:retarget:{existing['id']}:{updated.number}:{updated.base}".encode()
            ).hexdigest(),
            ticket_id=ticket_id,
            implementation_id=existing["id"],
            pull_request=updated.model_dump(),
            checks=check_results,
        )

    async def implement_workspace(
        self,
        run_id: str,
        manifest: DeliveryManifest,
        trackers: Mapping[str, TicketTracker],
        harnesses: Mapping[str, ImplementationHarness],
        publishers: Mapping[str, PullRequestPublisher],
        *,
        sources: Sequence[str],
        required: Sequence[str],
        publication_authorized: bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Implement each unlocked ticket in its authorized repository, then those it unlocks.

        A ticket whose blockers are not merged joins the same pass once every one of
        them has a verified draft. The frontier still watches completed tickets only. A
        ticket that has to wait (`ticket_blocked`, `stack_unsupported`) is not a result;
        every other stop is, so a person sees it.
        """
        frontier = await self.frontier(run_id, trackers)
        if "error" in frontier:
            return frontier
        graph = frontier["tickets"]
        try:
            for ticket in graph["draft"]["tickets"]:
                manifest.repository(ticket["repository"])
        except ValueError:
            return _review(frontier, "repository_not_authorized")
        completed = list(frontier.get("completed_tickets", []))
        results: dict[str, dict[str, Any]] = {}
        stacked: list[str] = []
        waiting = [
            key
            for key in graph["order"]
            if key not in completed and key not in frontier["frontier"]
        ]
        for ticket_id in [*frontier["frontier"], *waiting]:
            ticket = next(
                item for item in graph["draft"]["tickets"] if item["id"] == ticket_id
            )
            repository = manifest.repository(ticket["repository"])
            harness = harnesses.get(repository.repository)
            publisher = publishers.get(repository.repository)
            tracker = trackers.get(repository.repository)
            if harness is None or publisher is None or tracker is None:
                results[ticket_id] = _review(
                    await self.get(run_id), "repository_adapter_missing"
                )
                continue
            try:
                outcome = await self.implement(
                    run_id,
                    ticket_id,
                    repository.root,
                    trackers,
                    harness,
                    publisher,
                    sources=sources,
                    required=required,
                    checks=repository.checks,
                    base=repository.base,
                    publication_authorized=publication_authorized,
                    idempotency_key=f"{idempotency_key}:{ticket_id}",
                    delivery_id=manifest.delivery_id,
                    dependency_publishers=publishers,
                )
            except (OSError, ValueError, TimeoutError):
                outcome = _review(await self.get(run_id), "implementation_failed")
            if ticket_id not in frontier["frontier"]:
                if (outcome.get("error") or {}).get("code") in WAITS:
                    # No verified stack to rest on yet: the ticket waits for a merge.
                    continue
                if ticket_id in (outcome.get("implementations") or {}):
                    stacked.append(ticket_id)
            results[ticket_id] = outcome
        return {
            "delivery_id": manifest.delivery_id,
            "frontier": list(frontier["frontier"]),
            # Started before the merge of their blockers, on a verified draft of each.
            "stacked": stacked,
            # Tickets whose issue closed as completed: no blocker waits on them any more.
            "completed": completed,
            "results": results,
        }
