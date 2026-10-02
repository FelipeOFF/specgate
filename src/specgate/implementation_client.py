"""Run one unlocked ticket through a harness and reconcile its draft PR."""

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from specgate.client import automatic_decision_valid
from specgate.context import ContextPacket, build_context
from specgate.delegation import verification_allows_review
from specgate.grill_contracts import GrillError
from specgate.implementation_contracts import DraftPullRequest
from specgate.payload import PayloadTooLarge, oversize
from specgate.privacy import ensure_safe_content
from specgate.spec_contracts import IssueReference
from specgate.spec_verification import (
    interpret_verification,
    is_operational_claim,
    pack_evidence,
    ticket_claims,
)
from specgate.ticket_client import TicketClient
from specgate.ticket_contracts import require_verified_ticket
from specgate.tracker import TicketTracker


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


class PullRequestPublisher(Protocol):
    repository: str

    async def find(self, marker: str) -> list[DraftPullRequest]: ...
    async def create(
        self,
        title: str,
        body: str,
        head: str,
        base: str,
        commit_sha: str,
    ) -> DraftPullRequest: ...
    async def merged(self, number: int) -> bool: ...
    async def retarget(self, number: int, base: str) -> DraftPullRequest: ...


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


def _review(state: dict[str, Any], code: str) -> dict[str, Any]:
    return {
        **state,
        "action": "needs_human",
        "auto_advance": False,
        "error": {
            "code": code,
            "message": "Revise a implementação antes de continuar.",
        },
    }


class ImplementationClient(TicketClient):
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
    ) -> dict[str, Any]:
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
            return state
        if not checks or any(not command for command in checks):
            return _review(state, "invalid_check")
        frontier = await self.frontier(run_id, tracker)
        if "error" in frontier:
            return frontier
        if ticket_id not in frontier["frontier"]:
            return _review(state, "ticket_blocked")
        graph = state["tickets"]
        ticket = next(
            item for item in graph["draft"]["tickets"] if item["id"] == ticket_id
        )
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
        if not existing:
            begin_base = base
            stack_after = None
            root_base = None
            for blocker in ticket.get("blocked_by") or []:
                parent = state.get("implementations", {}).get(blocker)
                if not isinstance(parent, dict):
                    continue
                pull_request = parent.get("pull_request") or {}
                delivery = parent.get("delivery") or {}
                head = pull_request.get("head")
                if (
                    parent.get("status") in {"draft", "ready"}
                    and isinstance(head, str)
                    and head
                ):
                    stack_after = blocker
                    begin_base = head
                    root = delivery.get("root_base") or delivery.get("base")
                    root_base = root if isinstance(root, str) else base
                    break
            state = await self._call(
                "implementation_begin",
                run_id=run_id,
                expected_revision=state["revision"],
                idempotency_key=sha256(
                    f"{idempotency_key}:begin:{ticket_id}".encode()
                ).hexdigest(),
                ticket_id=ticket_id,
                observations=await self._observations(state, tracker),
                base=begin_base,
                delivery_id=delivery_id,
                stack_after=stack_after,
                root_base=root_base,
            )
            if "error" in state:
                return state
            existing = state["implementations"][ticket_id]
        if (
            delivery_id is not None
            and existing.get("delivery_id", run_id) != delivery_id
        ):
            return _review(state, "delivery_mismatch")
        if base != existing["delivery"]["base"]:
            return _review(state, "base_mismatch")
        if (
            publisher.repository.casefold()
            != existing["issue"]["repository"].casefold()
        ):
            return _review(state, "repository_mismatch")
        marker = (
            f"<!-- dev-decision-implementation:{run_id}:"
            f"{ticket_id}:{existing['id']} -->"
        )
        if existing["status"] == "publishing":
            try:
                matches = await publisher.find(marker)
            except (OSError, ValueError):
                return _review(state, "publication_unknown")
            if len(matches) > 1:
                return _review(state, "ambiguous_delivery")
            if not matches:
                return _review(state, "publication_unknown")
            resumed_outcome = {
                **existing["outcome"],
                "pull_request": matches[0].model_dump(),
            }
            return await self._call(
                "implementation_record",
                run_id=run_id,
                expected_revision=state["revision"],
                idempotency_key=sha256(
                    f"{idempotency_key}:record:{existing['id']}:{matches[0].number}".encode()
                ).hexdigest(),
                ticket_id=ticket_id,
                implementation_id=existing["id"],
                outcome=resumed_outcome,
            )
        delivery = await harness.implement(
            {
                "spec": state["spec"],
                "ticket": ticket,
                "issue": existing["issue"],
                "delivery_id": existing.get("delivery_id", run_id),
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
        if not publication_authorized:
            from specgate.delegation import publication_authority

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

        marker = (
            f"<!-- dev-decision-implementation:{run_id}:"
            f"{ticket_id}:{existing['id']} -->"
        )
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
            matches = await publisher.find(marker)
        except (OSError, ValueError):
            return _review(state, "publication_unknown")
        if len(matches) > 1:
            return _review(state, "ambiguous_delivery")
        body = (
            f"## Summary\n\n{delivery.summary}\n\n"
            f"## Changes\n\nImplementa {existing['issue']['url']}.\n\n"
            f"Delivery: `{existing.get('delivery_id', run_id)}`.\n\n"
            "## Test plan\n\nTodos os checks configurados passaram no commit informado.\n\n"
            f"{_verification_note(verified.get('result'))}"
            f"{marker}\n"
        )
        try:
            if not publication_authorized:
                from specgate.delegation import review_authority_valid

                if not review_authority_valid(
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
                    base,
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

    async def retarget(
        self,
        run_id: str,
        ticket_id: str,
        worktree: Path,
        publisher: PullRequestPublisher,
        *,
        checks: Sequence[Sequence[str]],
        idempotency_key: str,
    ) -> dict[str, Any]:
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
        if (
            not isinstance(existing, dict)
            or existing.get("status") not in {"draft", "checks_failed"}
            or not isinstance(stored, dict)
            or not isinstance(parent_pr, dict)
            or publisher.repository.casefold()
            != existing["issue"]["repository"].casefold()
        ):
            return _review(state, "retarget_unavailable")
        if not await publisher.merged(int(parent_pr["number"])):
            return _review(state, "base_still_open")
        commit_result, commit = await _command(worktree, ("git", "rev-parse", "HEAD"))
        if commit_result or commit.strip() != stored["commit_sha"]:
            return _review(state, "history_rewrite_refused")
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
        """Implement each currently unlocked ticket in its authorized repository."""
        frontier = await self.frontier(run_id, trackers)
        if "error" in frontier:
            return frontier
        graph = frontier["tickets"]
        try:
            for ticket in graph["draft"]["tickets"]:
                manifest.repository(ticket["repository"])
        except ValueError:
            return _review(frontier, "repository_not_authorized")
        results: dict[str, dict[str, Any]] = {}
        for ticket_id in frontier["frontier"]:
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
                results[ticket_id] = await self.implement(
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
                )
            except (OSError, ValueError, TimeoutError):
                results[ticket_id] = _review(
                    await self.get(run_id), "implementation_failed"
                )
        return {
            "delivery_id": manifest.delivery_id,
            "frontier": list(frontier["frontier"]),
            # Tickets whose issue closed as completed: no blocker waits on them any more.
            "completed": list(frontier.get("completed_tickets", [])),
            "results": results,
        }
