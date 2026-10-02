"""Compose the existing clients into one resumable run to draft pull requests.

The MCP owns the run, its revisions, the research ledger and every review. This module
only picks the next call from the state it reads, so a restart continues where the run
stands. It holds no authority: a grant, an approval of the exact revision and the
verification of each stage decide what runs, and the run stops at drafts. Nothing here
can merge, deploy or rewrite history.
"""

import argparse
import asyncio
import importlib
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from specgate.context import SkillSource, build_context, read_skill
from specgate.delegation import (
    load_delegation,
    publication_authority,
    review_authority_valid,
)
from specgate.gate_policy import BASIS_MANIFEST
from specgate.grill_client import GrillClient
from specgate.grill_contracts import GrillQuestion
from specgate.implementation_client import (
    DeliveryManifest,
    ImplementationClient,
    ImplementationHarness,
    PullRequestPublisher,
)
from specgate.product import mcp_api_key
from specgate.research import ResearchRequest, ResearchSource, save_research
from specgate.research_ledger import OPERATIONAL, applicable_adjudication, counted
from specgate.spec_claims import weak_claims
from specgate.spec_client import SpecClient
from specgate.spec_contracts import SpecAuthorization, SpecDraft, _fingerprint
from specgate.ticket_claims import weak
from specgate.ticket_client import TicketClient
from specgate.ticket_contracts import TicketAuthorization, TicketPlan
from specgate.tracker import TicketTracker, Tracker

STEP_LIMIT = 200
Operation = Literal["spec", "tickets", "draft_pr"]
# The human alternatives of a gap the research and the adjudication could not close.
HUMAN_ALTERNATIVES = ("repair_with_evidence", "review_scope", "keep_unpublished")
# Errors that may come from a missing grant; the grant itself tells whether they do.
AUTHORITY_CODES = frozenset(
    {
        "delegated_review_unavailable",
        "publication_authorization_changed",
        "publication_authorization_required",
    }
)


class FlowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    objective: str = Field(min_length=1, max_length=8000)
    delivery: DeliveryManifest
    sources: list[str] = Field(min_length=1, max_length=100)
    research_sources: list[str] = Field(min_length=1, max_length=100)
    authorized_skill_roots: list[str] = Field(min_length=1, max_length=50)
    run_id: str | None = None
    # An approval binds one artifact revision; a changed artifact inherits none.
    spec_reviews: dict[str, SpecAuthorization] = Field(default_factory=dict)
    ticket_reviews: dict[str, TicketAuthorization] = Field(default_factory=dict)


class FlowHarness(Protocol):
    async def questions(self, state: dict[str, Any]) -> list[GrillQuestion]: ...
    async def skill(self, stage: str) -> dict[str, Any]:
        """The routing result of the stage's skill, after `confirm_skill_loaded`."""
        ...
    async def spec(self, context: dict[str, Any]) -> SpecDraft: ...
    async def tickets(self, context: dict[str, Any]) -> TicketPlan: ...
    async def research(self, state: dict[str, Any], claim: str) -> ResearchRequest: ...


@dataclass(frozen=True)
class FlowAdapters:
    harness: FlowHarness
    spec_tracker: Tracker
    trackers: Mapping[str, TicketTracker]
    implementations: Mapping[str, ImplementationHarness]
    publishers: Mapping[str, PullRequestPublisher]
    capture: Callable[[ResearchSource], Awaitable[None]] | None = None


class FlowJournal:
    """Immutable audit snapshots and a run locator; never an authorization store."""

    def __init__(self, project: Path, delivery_id: str, host: str) -> None:
        self.project, self.host = project.resolve(), host
        self.relative = f"docs/flows/{delivery_id}"
        self.directory = self.project / self.relative
        if not self.directory.resolve().is_relative_to(self.project):
            raise ValueError("O journal precisa permanecer no projeto.")
        self.locator = self.directory / "run.json"

    def locate(self, request: FlowRequest) -> str | None:
        if not self.locator.exists():
            return request.run_id
        stored = json.loads(self.locator.read_text())
        if (
            stored["host"] != self.host
            or stored["objective_revision"] != _fingerprint(request.objective)
            or (request.run_id and request.run_id != stored["run_id"])
        ):
            raise ValueError(
                "O journal pertence a outro escopo; use outro delivery_id."
            )
        return str(stored["run_id"])

    def capture(
        self, state: dict[str, Any], request: FlowRequest, event: str
    ) -> dict[str, str]:
        artifact = save_research(
            {
                "schema": 1,
                "event": event,
                "delivery_id": request.delivery.delivery_id,
                "request_revision": _fingerprint(request.model_dump(mode="json")),
                "state": state,
            },
            self.project,
            self.relative,
        )
        if state.get("id"):
            locator = {
                "host": self.host,
                "run_id": state["id"],
                "objective_revision": _fingerprint(request.objective),
                "snapshot": artifact,
            }
            temporary = self.directory / f"{artifact['revision']}.locator"
            temporary.write_text(json.dumps(locator, indent=2) + "\n")
            os.replace(temporary, self.locator)
        return artifact


def _same_question(question: GrillQuestion, turn: dict[str, Any]) -> bool:
    old = turn["question"]
    current = question.model_dump(mode="json")
    if any(
        old.get(k) != current[k]
        for k in (
            "question",
            "options",
            "question_type",
            "requires_authorization",
            "missing_personal_fact",
        )
    ):
        return False
    if any(
        old["context"].get(key) != current["context"].get(key)
        for key in (
            "objective",
            "rules",
            "artifact",
            "alternatives",
            "gaps",
            "conflicts",
        )
    ):
        return False
    references = {
        (e["id"], e["source"], e["revision"]) for e in old["context"]["evidence"]
    }
    return all(
        (e.id, e.source, e.revision) in references for e in question.context.evidence
    )


class _Stop(Exception):
    """The run ends here, with these exceptions, and keeps what it has done."""

    def __init__(self, state: dict[str, Any], *exceptions: dict[str, Any]) -> None:
        super().__init__("flow stopped")
        self.state, self.exceptions = state, list(exceptions)


@dataclass
class _Plan:
    """What a run holds across steps; none of it is state the MCP does not own."""

    request: FlowRequest
    adapters: FlowAdapters
    journal: FlowJournal
    project: Path
    questions: list[GrillQuestion]
    authorize_jev: bool
    publication_authorized: bool
    spec: SpecClient
    tickets: TicketClient
    implementation: ImplementationClient
    skills: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def roots(self) -> list[Path]:
        return [Path(value) for value in self.request.authorized_skill_roots]


def confirmed_skill(selection: Any, roots: list[Path]) -> Path | None:
    """The skill file a routing result confirmed as loaded, or None.

    Routing only selects. The harness confirms the load (its native hook, or a
    person's review), and the result then says `loaded`. Instructions the file no
    longer holds, or a file outside the authorized roots, confirm nothing.
    """
    candidate = selection.get("candidate") if isinstance(selection, dict) else None
    if (
        not isinstance(candidate, dict)
        or candidate.get("loaded") is not True
        or selection.get("execution_authorized") is not False
        or not candidate.get("revision")
    ):
        return None
    try:
        read_skill(
            SkillSource(
                candidate["id"],
                "",
                candidate["source"],
                candidate["reference"],
                candidate["revision"],
                tuple(candidate["aliases"]),
            ),
            roots,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return Path(candidate["source"])


class FlowClient(GrillClient):
    def _client(self, cls: type[GrillClient]) -> Any:
        return cls(
            self.url,
            self.token,
            transport=self.transport,
            ca_file=self.ca_file,
            timeout_seconds=self.timeout_seconds,
            progress_callback=self.progress_callback,
            project_id=self.project_id,
            home=self.home,
            project=self.project,
        )

    async def run(
        self,
        request: FlowRequest,
        adapters: FlowAdapters,
        *,
        authorize_jev: bool,
        publication_authorized: bool = False,
    ) -> dict[str, Any]:
        project = self.project or Path.cwd()
        if not authorize_jev:
            return {
                "action": "needs_human",
                "auto_advance": False,
                "error": {"code": "jev_authorization_required"},
            }
        journal = FlowJournal(project, request.delivery.delivery_id, self.url)
        run_id = journal.locate(request)
        if run_id:
            state = await self.get(run_id)
        else:
            context = build_context(
                project,
                request.objective,
                request.sources,
                request.sources,
                artifact=request.objective,
                alternatives=["implementar a entrega", "revisar o escopo"],
            )
            if context.gaps or context.conflicts:
                return {
                    "action": "needs_human",
                    "auto_advance": False,
                    "error": {"code": "flow_context_incomplete"},
                }
            state = await self.start(
                request.objective,
                context,
                idempotency_key=f"flow:{request.delivery.delivery_id}:start",
            )
        if state.get("error"):
            return state
        if state.get("objective") != request.objective:
            return {
                "action": "needs_human",
                "auto_advance": False,
                "error": {"code": "run_scope_mismatch"},
            }
        journal.capture(state, request, "reconciled")
        questions = await adapters.harness.questions(state)
        if len(questions) > 100 or len({q.id for q in questions}) != len(questions):
            raise ValueError("O grill exige perguntas únicas dentro do limite de 100.")
        plan = _Plan(
            request,
            adapters,
            journal,
            project,
            questions,
            authorize_jev,
            publication_authorized,
            self._client(SpecClient),
            self._client(TicketClient),
            self._client(ImplementationClient),
        )
        try:
            return await self._loop(state["id"], plan)
        except _Stop as stop:
            return self._stop(plan, stop.state, *stop.exceptions)

    async def _loop(self, run_id: str, plan: _Plan) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for _ in range(STEP_LIMIT):
            # The ticket read derives the staleness of the spec and of the graph.
            state = await plan.tickets.get(run_id)
            if state.get("error"):
                raise _Stop(state)
            result = await self._advance(state, plan)
            if result.get("delivery_results"):
                return self._stop(plan, result)
            if (
                result.get("research_handoff", {}).get("route")
                == "bounded_adjudication"
            ):
                result = await self._adjudicate(result, plan)
            plan.journal.capture(result, plan.request, "stage_result")
            if (
                result.get("error")
                or result.get("research_handoff")
                or result.get("reconcile")
                or result.get("diagnostic")
            ):
                raise _Stop(result)
            # The error of an operational stop is the result of the research: another
            # cycle here would only draw another score.
            if self._operational(result):
                raise _Stop(result)
            if result.get("revision") == state["revision"]:
                raise _Stop(result, {"stage": state.get("stage"), "code": "flow_no_progress"})
        raise _Stop(state, {"stage": state.get("stage"), "code": "flow_step_limit"})

    @staticmethod
    def _operational(state: dict[str, Any]) -> str | None:
        active = (state.get("pending") or {}).get("question", {}).get("id")
        reason: str | None = state.get("research_stopped", {}).get(active)
        return reason if reason in OPERATIONAL else None

    async def _advance(self, state: dict[str, Any], plan: _Plan) -> dict[str, Any]:
        """One call, chosen from the stage and the artifacts the MCP reports."""
        key = f"flow:{plan.request.delivery.delivery_id}:{state['revision']}"
        stage = state["stage"]
        if state.get("pending") or stage == "grill":
            return await self._grill(state, key, plan)
        if state.get("spec", {}).get("status") in {None, "stale"}:
            return await self._synthesize_spec(state, key, plan)
        if stage == "spec_review":
            return await self._review_spec(state, key, plan)
        if stage == "spec_ready" and not self._holds(state["spec"], "spec", plan):
            return await self._delegate("spec", state, f"{key}:renew", plan)
        if stage in {"spec_ready", "spec_publishing"}:
            return await plan.spec.publish(
                state["id"], plan.adapters.spec_tracker, idempotency_key=key
            )
        if state.get("tickets", {}).get("status") in {None, "stale"}:
            return await self._synthesize_tickets(state, key, plan)
        if stage == "tickets_review":
            return await self._review_tickets(state, key, plan)
        if stage == "tickets_ready" and not self._holds(state["tickets"], "tickets", plan):
            return await self._delegate("tickets", state, f"{key}:renew", plan)
        if stage in {"tickets_ready", "tickets_publishing", "tickets_publication_review"}:
            return await plan.tickets.publish(
                state["id"], plan.adapters.trackers, idempotency_key=key, project=plan.project
            )
        if stage == "tickets_published":
            return await self._deliver(state, key, plan)
        raise _Stop(state, {"stage": stage, "code": "flow_stage_unsupported"})

    async def _grill(self, state: dict[str, Any], key: str, plan: _Plan) -> dict[str, Any]:
        run_id = state["id"]
        if pending := state.get("pending"):
            question = pending["question"]
            if question.get("requires_authorization") or question.get(
                "missing_personal_fact"
            ):
                raise _Stop(
                    state,
                    {"stage": "grill", "code": "human_decision_required", "question": question["id"]},
                )
            prompt = await plan.adapters.harness.research(state, question["id"])
            return await self.research_until_resolved(
                run_id,
                [prompt],
                idempotency_key=key,
                authorize_jev=plan.authorize_jev,
                capture=plan.adapters.capture,
            )
        resolved = {t["question"]["id"]: t for t in state.get("decisions", [])}
        question_to_ask = next(
            (
                q
                for q in plan.questions
                if q.id not in resolved or not _same_question(q, resolved[q.id])
            ),
            None,
        )
        if question_to_ask:
            return await self.continue_run(
                run_id, state["revision"], idempotency_key=key, question=question_to_ask
            )
        return await self.continue_run(
            run_id, state["revision"], idempotency_key=key, finish=True
        )

    async def _skill(self, stage: str, state: dict[str, Any], plan: _Plan) -> Path:
        """The skill of a synthesis, only once the harness confirmed it loaded."""
        selection = await plan.adapters.harness.skill(stage)
        path = confirmed_skill(selection, plan.roots)
        if path is None:
            loading = selection.get("loading") if isinstance(selection, dict) else None
            raise _Stop(
                state,
                {
                    "stage": stage,
                    "code": "skill_load_unconfirmed",
                    "loading": loading,
                    "reason": selection.get("reason") if isinstance(selection, dict) else None,
                },
            )
        candidate = selection["candidate"]
        plan.skills[stage] = {
            "id": candidate["id"],
            "revision": candidate["revision"],
            "gate_basis": selection.get("gate_basis"),
            "loading": selection.get("loading"),
        }
        return path

    async def _synthesize_spec(self, state: dict[str, Any], key: str, plan: _Plan) -> dict[str, Any]:
        skill = await self._skill("spec", state, plan)
        return await plan.spec.synthesize(
            state["id"],
            state["revision"],
            idempotency_key=key,
            project=plan.project,
            skill_path=skill,
            authorized_roots=plan.roots,
            harness=plan.adapters.harness.spec,
            research_sources=plan.request.research_sources,
        )

    async def _review_spec(self, state: dict[str, Any], key: str, plan: _Plan) -> dict[str, Any]:
        spec = state["spec"]
        threshold = state["workflow_policy"]["confidence"]
        if weak_claims(spec.get("verification", {}), threshold):
            return await plan.spec.research_spec(
                state["id"],
                plan=plan.adapters.harness.research,
                idempotency_key=key,
                authorize_jev=plan.authorize_jev,
                repair=plan.adapters.harness.spec,
                capture=plan.adapters.capture,
            )
        if approval := plan.request.spec_reviews.get(spec["revision"]):
            return await plan.spec.review(
                state["id"],
                state["revision"],
                idempotency_key=key,
                artifact_revision=spec["revision"],
                authorization=approval,
            )
        return await self._delegate("spec", state, key, plan)

    def _holds(self, artifact: dict[str, Any], operation: Operation, plan: _Plan) -> bool:
        """Whether the grant a review recorded still covers the effect it gates.

        A person's approval always does. A delegation review is a revision of the
        grant, and the effect checks that revision again right before it runs.
        """
        authorization = artifact.get("authorization") or {}
        repositories = (
            [plan.adapters.spec_tracker.repository]
            if operation == "spec"
            else sorted({row["repository"] for row in artifact["draft"]["tickets"]})
        )
        return authorization.get("origin") != "automated" or all(
            review_authority_valid(
                authorization,
                host=self.url,
                repository=repository,
                operation=operation,
                home=self.home,
            )
            for repository in repositories
        )

    async def _delegate(
        self, artifact: str, state: dict[str, Any], key: str, plan: _Plan
    ) -> dict[str, Any]:
        """Review the revision on the grant as it stands now; the effect still checks it."""
        if artifact == "spec":
            return await plan.spec.review_delegated(
                state["id"],
                project=plan.project,
                repository=plan.adapters.spec_tracker.repository,
                idempotency_key=key,
            )
        graph = state["tickets"]
        stored = graph.get("verification", {})
        return await plan.tickets.review_delegated(
            state["id"],
            project=plan.project,
            artifact_revision=graph["revision"],
            verification={
                "artifact_revision": graph["revision"],
                "results": stored.get("results", []),
                "confirmations": stored.get("confirmations", []),
            },
            idempotency_key=key,
        )

    async def _synthesize_tickets(self, state: dict[str, Any], key: str, plan: _Plan) -> dict[str, Any]:
        skill = await self._skill("tickets", state, plan)
        return await plan.tickets.synthesize(
            state["id"],
            state["revision"],
            idempotency_key=key,
            project=plan.project,
            skill_path=skill,
            authorized_roots=plan.roots,
            harness=plan.adapters.harness.tickets,
            research_sources=plan.request.research_sources,
        )

    async def _review_tickets(self, state: dict[str, Any], key: str, plan: _Plan) -> dict[str, Any]:
        graph = state["tickets"]
        threshold = state["workflow_policy"]["confidence"]
        approval = plan.request.ticket_reviews.get(graph["revision"])
        if weak(graph.get("verification", {}), threshold):
            researched = await plan.tickets.research_tickets(
                state["id"],
                plan=plan.adapters.harness.research,
                idempotency_key=key,
                authorize_jev=plan.authorize_jev,
                repair=plan.adapters.harness.tickets,
                capture=plan.adapters.capture,
            )
            # The host accepts a residual only after the cycles end, so a person's
            # acceptance of this exact revision is applied once the research stops.
            if (
                approval
                and approval.residual
                and researched.get("research_handoff")
                and graph.get("authorization") != approval.model_dump()
            ):
                return await plan.tickets.review(
                    state["id"],
                    researched["revision"],
                    idempotency_key=f"{key}:accept",
                    artifact_revision=graph["revision"],
                    authorization=approval,
                )
            return researched
        if approval:
            return await plan.tickets.review(
                state["id"],
                state["revision"],
                idempotency_key=key,
                artifact_revision=graph["revision"],
                authorization=approval,
            )
        return await self._delegate("tickets", state, key, plan)

    async def _deliver(self, state: dict[str, Any], key: str, plan: _Plan) -> dict[str, Any]:
        if not plan.publication_authorized:
            gaps = self._withheld(plan, state)
            if any(gap["operation"] == "draft_pr" for gap in gaps):
                raise _Stop(state, *gaps)
        delivered = await plan.implementation.implement_workspace(
            state["id"],
            plan.request.delivery,
            plan.adapters.trackers,
            plan.adapters.implementations,
            plan.adapters.publishers,
            sources=plan.request.sources,
            required=plan.request.sources,
            publication_authorized=plan.publication_authorized,
            idempotency_key=key,
        )
        final = await self.get(state["id"])
        final["delivery_results"] = delivered
        return final

    # Adjudication: one judgment of a gap after three research cycles.

    @staticmethod
    def _gaps(state: dict[str, Any]) -> list[str]:
        threshold = state.get("workflow_policy", {}).get("confidence", 0.8)
        stage = state.get("stage")
        if stage == "spec_review":
            return [
                f"spec:{claim}"
                for claim in weak_claims((state.get("spec") or {}).get("verification", {}), threshold)
            ]
        if stage == "tickets_review":
            return [
                f"tickets:{claim}"
                for claim in weak((state.get("tickets") or {}).get("verification", {}), threshold)
            ]
        question = (state.get("pending") or {}).get("question", {}).get("id")
        return [question] if question else []

    @staticmethod
    def _applicable(state: dict[str, Any], gap: str) -> dict[str, Any] | None:
        """The judgment the host kept for the artifact as it stands, if it recommends a repair."""
        kind = gap.split(":", 1)[0]
        if kind not in {"spec", "tickets"}:
            return None
        return applicable_adjudication(state, gap, (state.get(kind) or {}).get("revision"))

    async def _adjudicate(self, result: dict[str, Any], plan: _Plan) -> dict[str, Any]:
        """Judge the first gap whose cycles ended on admitted evidence, once.

        The host admits the evidence and decides whether the gap may be judged; this
        only asks. A recommendation to revise goes through the usual verification
        again, and a judgment the host already holds for this revision is repaired on
        instead of asked again. Anything else leaves the handoff, and the dev the human
        alternatives.
        """
        if not load_delegation(self.home).active(plan.project, self.url):
            return result
        state = await plan.tickets.get(result["id"])
        if state.get("error"):
            return {**result, "adjudication": state}
        gaps = self._gaps(state)
        resumed = next((item for item in gaps if self._applicable(state, item)), None)
        if resumed:
            return await self._repair(state["id"], resumed, plan)
        spent = {
            row["gap_id"]
            for row in state.get("flow_adjudications", [])
            # An outage or an unfinished reservation is for the host to settle, not a judgment.
            if row["status"] not in {"unavailable", "evaluating"}
        }
        gap = next(
            (item for item in gaps if item not in spent and len(counted(state, item)) >= 3),
            None,
        )
        if gap is None:
            return result
        digest = sha256(f"{plan.request.delivery.delivery_id}:{gap}".encode()).hexdigest()
        judged = await self._call(
            "flow_adjudicate",
            run_id=state["id"],
            expected_revision=state["revision"],
            # The host binds the key to the revision it was asked on, so a later ask needs its own.
            idempotency_key=f"flow:adjudicate:{digest[:32]}:{state['revision']}",
            gap_id=gap,
        )
        if judged.get("error") or judged.get("reconcile"):
            return {**result, "adjudication": judged}
        entry = judged["flow_adjudications"][-1]
        if entry.get("applicable") is not True:
            return {
                **result,
                "flow_adjudications": judged["flow_adjudications"],
                "adjudication": entry,
            }
        return await self._repair(state["id"], gap, plan)

    @staticmethod
    async def _repair(run_id: str, gap: str, plan: _Plan) -> dict[str, Any]:
        """Repair the claim of a judged gap; the key keeps a rerun from repairing twice."""
        digest = sha256(f"{plan.request.delivery.delivery_id}:{gap}".encode()).hexdigest()
        key = f"flow:repair:{digest[:40]}"
        harness = plan.adapters.harness
        if gap.startswith("spec:"):
            return await plan.spec.repair_claim(
                run_id, gap.removeprefix("spec:"), repair=harness.spec, idempotency_key=key
            )
        if gap.startswith("tickets:"):
            return await plan.tickets.repair_claim(
                run_id, gap.removeprefix("tickets:"), repair=harness.tickets, idempotency_key=key
            )
        raise ValueError("Só uma claim de spec ou de tickets tem reparo.")

    # Authority: the grant, never the score, lets a stage run an external effect.

    def _withheld(self, plan: _Plan, state: dict[str, Any]) -> list[dict[str, Any]]:
        """Each operation that still needs a grant and does not have a valid one."""
        grant = load_delegation(self.home)
        spec = state.get("spec") or {}
        graph = state.get("tickets") or {}
        delivery = sorted({row.repository for row in plan.request.delivery.repositories})
        recorded = {
            item["delegation_revision"]
            for item in (
                spec.get("authorization") or {},
                graph.get("authorization") or {},
                *(
                    (row.get("outcome") or {}).get("publication_authorization") or {}
                    for row in (state.get("implementations") or {}).values()
                ),
            )
            if item.get("origin") == "automated" and item.get("delegation_revision")
        }

        def human(authorization: dict[str, Any]) -> bool:
            return (
                authorization.get("origin", "human") == "human"
                and authorization.get("publication_authorized") is True
            )

        needs: dict[Operation, tuple[list[str], bool]] = {
            "spec": (
                [plan.adapters.spec_tracker.repository],
                spec.get("status") == "published" or human(spec.get("authorization") or {}),
            ),
            "tickets": (
                sorted({row["repository"] for row in graph.get("draft", {}).get("tickets", [])})
                or delivery,
                graph.get("status") == "published"
                or human(graph.get("authorization") or {}),
            ),
            "draft_pr": (delivery, plan.publication_authorized),
        }
        gaps = []
        for operation, (repositories, covered) in needs.items():
            if covered or publication_authority(
                project=plan.project,
                host=self.url,
                repositories=repositories,
                operation=operation,
                home=self.home,
            ):
                continue
            if grant.active(plan.project, self.url):
                code = "authorization_scope_exceeded"
            elif recorded:
                code = "authorization_revoked"
            else:
                code = "authorization_absent"
            gaps.append(
                {
                    "stage": "authority",
                    "operation": operation,
                    "repositories": repositories,
                    "code": code,
                }
            )
        return gaps

    # The consolidated report: what stopped the run and what it still holds.

    @staticmethod
    def _gates(state: dict[str, Any]) -> list[dict[str, Any]]:
        """The gate each stored verification of the open review reports.

        A closed review may be a grant that is missing under an open gate, or a gate
        that is closed; the report tells which, since a score authorizes nothing.
        """
        spec = (state.get("spec") or {}).get("verification") or {}
        graph = (state.get("tickets") or {}).get("verification") or {}
        stored = (
            [("spec", spec.get("fidelity")), ("spec", spec.get("coverage"))]
            if state.get("stage") == "spec_review"
            else [
                ("tickets", row)
                for row in (*graph.get("results", []), *graph.get("confirmations", []))
            ]
        )
        views: list[dict[str, Any]] = []
        for artifact, result in stored:
            gate = result.get("gate") if isinstance(result, dict) else None
            view = {
                "artifact": artifact,
                "basis": gate.get("basis") if isinstance(gate, dict) else None,
                "passed": gate.get("passed") if isinstance(gate, dict) else None,
                "reason": gate.get("reason") if isinstance(gate, dict) else None,
            }
            if view not in views:
                views.append(view)
        return views

    @staticmethod
    def _bases(state: dict[str, Any]) -> list[str]:
        found: set[str] = set()
        spec = (state.get("spec") or {}).get("verification") or {}
        results = [spec.get("fidelity"), spec.get("coverage")]
        graph = (state.get("tickets") or {}).get("verification") or {}
        results += [*graph.get("results", []), *graph.get("confirmations", [])]
        for result in results:
            gate = result.get("gate") if isinstance(result, dict) else None
            if isinstance(gate, dict) and isinstance(gate.get("basis"), str):
                found.add(gate["basis"])
        found.update(
            row["basis"]
            for row in (*state.get("research_attempts", []), *state.get("flow_adjudications", []))
            if row.get("basis")
        )
        return sorted(found)

    def _stop(self, plan: _Plan, state: dict[str, Any], *extra: dict[str, Any]) -> dict[str, Any]:
        exceptions = list(extra)
        handoff = state.get("research_handoff")
        error = state.get("error")
        if handoff:
            exceptions.append(self._research_exception(state, handoff))
        # A research that ended on its limit reports that once, with its alternatives.
        if error and not (handoff and error.get("code") == handoff.get("reason")):
            exceptions.append(
                {
                    "stage": state.get("stage"),
                    **error,
                    **(
                        {"gates": self._gates(state)}
                        if error.get("code") == "delegated_review_unavailable"
                        else {}
                    ),
                }
            )
        if state.get("diagnostic"):
            exceptions.append({"stage": state.get("stage"), **state["diagnostic"]})
        if self._operational(state):
            exceptions.append(
                {"stage": state.get("stage"), "code": self._operational(state)}
            )
        delivery = state.get("delivery_results") or {}
        if delivery.get("error"):
            exceptions.append({"stage": "implementation", **delivery["error"]})
        for ticket, outcome in (delivery.get("results") or {}).items():
            if outcome.get("error"):
                exceptions.append({"ticket": ticket, **outcome["error"]})
            else:
                status = (outcome.get("implementations") or {}).get(ticket, {}).get("status")
                if status != "draft":
                    exceptions.append(
                        {"ticket": ticket, "code": status or "delivery_incomplete"}
                    )
        waiting: list[dict[str, Any]] = []
        if "results" in delivery:
            waiting, stranded = self._unattended(state, exceptions, delivery.get("completed", []))
            exceptions.extend(stranded)
            if not exceptions and not self._drafted(state):
                exceptions.append({"stage": "implementation", "code": "no_draft_produced"})
        if state.get("id") and any(row.get("code") in AUTHORITY_CODES for row in exceptions):
            exceptions.extend(self._withheld(plan, state))
        bases = self._bases(state)
        result = {
            **state,
            "exceptions": exceptions,
            # Not failures: a ticket waits for the merge of its blockers, which is a person's.
            **({"awaiting_blockers": waiting} if delivery else {}),
            "auto_advance": False,
            "action": "needs_human" if exceptions else "draft_review",
            # What advanced the run, and that none of it measured accuracy.
            "policy": {
                "gate_basis": bases,
                "calibrated": bool(bases) and set(bases) == {BASIS_MANIFEST},
                "skills": plan.skills,
            },
        }
        result["journal"] = plan.journal.capture(result, plan.request, "stopped")
        return result

    @staticmethod
    def _drafted(state: dict[str, Any]) -> bool:
        return any(
            row.get("status") in {"draft", "ready"}
            for row in (state.get("implementations") or {}).values()
        )

    @staticmethod
    def _unattended(
        state: dict[str, Any], exceptions: list[dict[str, Any]], completed: list[str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The tickets the run neither drafted nor reported: waiting for a merge, or stranded.

        A ticket waits only while a blocker is still open. One that left the frontier for
        another reason (closed as not planned, not actionable) is an exception of its own.
        """
        settled = {
            ticket
            for ticket, row in (state.get("implementations") or {}).items()
            if row.get("status") in {"draft", "ready"}
        } | set(completed)
        reported = {row["ticket"] for row in exceptions if "ticket" in row}
        waiting: list[dict[str, Any]] = []
        stranded: list[dict[str, Any]] = []
        for ticket in (state.get("tickets") or {}).get("draft", {}).get("tickets", []):
            if ticket["id"] in settled | reported:
                continue
            if any(blocker not in completed for blocker in ticket["blocked_by"]):
                waiting.append({"ticket": ticket["id"], "blocked_by": ticket["blocked_by"]})
            else:
                stranded.append({"ticket": ticket["id"], "code": "ticket_not_actionable"})
        return waiting, stranded

    @staticmethod
    def _research_exception(state: dict[str, Any], handoff: dict[str, Any]) -> dict[str, Any]:
        # A run reopened on a judged gap does not ask again: the host's record tells.
        gaps = FlowClient._gaps(state)
        recorded = [
            row for row in state.get("flow_adjudications", []) if row["gap_id"] in gaps
        ]
        adjudication = state.get("adjudication") or (recorded[-1] if recorded else {})
        return {
            "stage": state.get("stage"),
            "code": "research_review_required",
            "reason": handoff.get("reason"),
            "attempts": len(handoff.get("research_attempts", [])),
            "recommendation": adjudication.get("recommendation"),
            "adjudication": adjudication.get("status") or adjudication.get("error"),
            "alternatives": (handoff.get("fallback") or {}).get("alternatives")
            or list(HUMAN_ALTERNATIVES),
            "fallback": handoff.get("fallback"),
        }


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("request", type=Path)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--host", required=True)
    parser.add_argument(
        "--adapter", required=True, help="Adapter instalado, no formato module:factory"
    )
    parser.add_argument("--authorize-jev", action="store_true")
    parser.add_argument("--authorize-drafts", action="store_true")


def load_adapters(spec: str, request: FlowRequest) -> FlowAdapters:
    """The adapters an installed `module:factory` builds for this request."""
    module, separator, factory = spec.partition(":")
    if not (module and separator and factory):
        raise ValueError("Informe o adapter no formato module:factory.")
    try:
        imported = importlib.import_module(module)
    except (ImportError, AttributeError) as error:
        # The cause names a module the adapter imports but the install lacks.
        raise ValueError(f"Não foi possível carregar o adapter {spec}: {error}") from error
    build = getattr(imported, factory, None)
    if build is None:
        raise ValueError(
            f"Não foi possível carregar o adapter {spec}: o módulo não define {factory}."
        )
    adapters = build(request)
    if isinstance(adapters, FlowAdapters):
        return adapters
    raise ValueError("O adapter precisa fornecer FlowAdapters.")


def execute(
    args: argparse.Namespace,
    *,
    home: Path | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    request = FlowRequest.model_validate_json(args.request.read_text())
    adapters = load_adapters(args.adapter, request)
    host = args.host.rstrip("/")
    if not host.endswith(("/mcp", "/sse")):
        host += "/mcp"
    result: dict[str, Any] = asyncio.run(
        FlowClient(
            host,
            mcp_api_key(),
            project=args.project,
            home=home,
            project_id=project_id,
        ).run(
            request,
            adapters,
            authorize_jev=args.authorize_jev,
            publication_authorized=args.authorize_drafts,
        )
    )
    return result
