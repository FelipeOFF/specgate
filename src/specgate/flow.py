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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
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
from specgate.flow_revision import (
    REOPEN_LIMIT,
    REVISE,
    REVISION_LIMIT,
    RevisingHarness,
    Revision,
    Stage,
    allowed_of,
    is_refusal,
    own_actions,
    refuse_choice,
    refuse_content,
    refuse_parts,
    refuse_reopening,
    registered,
    report,
    targets,
    without_reopening,
)
from specgate.gate_policy import BASIS_MANIFEST
from specgate.grill_client import GrillClient
from specgate.grill_contracts import GrillQuestion, QuestionAction, QuestionLineage
from specgate.implementation_client import (
    DeliveryManifest,
    ImplementationClient,
    ImplementationHarness,
)
from specgate.implementation_contracts import PullRequestPublisher
from specgate.lineage_budget import NOT_REGISTERED_MESSAGE, LineageAction
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
from specgate.wording import (
    ACTION_NOT_ALLOWED,
    ACTION_NOT_REGISTERED,
    AGENT_CODES,
    AGENT_REVISION,
    AgentRefusal,
)

STEP_LIMIT = 200
Operation = Literal["spec", "tickets", "draft_pr"]
# The human alternatives of a gap the research and the adjudication could not close.
HUMAN_ALTERNATIVES = ("repair_with_evidence", "review_scope", "keep_unpublished")
# Errors that may come from a missing grant; the grant itself tells whether they do.
AUTHORITY_CODES = frozenset(
    {
        "delegated_review_unavailable",
        "dependency_authorization_changed",
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
    """Immutable audit snapshots and a run locator; never an authorization store.

    The locator also keeps the questions the flow replaced (`replaced`), so a run that resumes
    asks what took their place: the host does not store a question it refused, and the harness
    lists the questions it wrote, not the ones the agent reworded since.
    """

    def __init__(self, project: Path, delivery_id: str, host: str) -> None:
        self.project, self.host = project.resolve(), host
        self.relative = f"docs/flows/{delivery_id}"
        self.directory = self.project / self.relative
        if not self.directory.resolve().is_relative_to(self.project):
            raise ValueError("O journal precisa permanecer no projeto.")
        self.locator = self.directory / "run.json"
        self.replaced: dict[str, dict[str, Any]] = {}

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
        self.replaced = dict(stored.get("replaced") or {})
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
                "replaced": self.replaced,
            }
            self._write(locator, artifact["revision"])
        return artifact

    def keep(self) -> None:
        """Write the replaced questions into the locator now, not at the next snapshot."""
        if self.locator.exists():
            stored = json.loads(self.locator.read_text())
            self._write({**stored, "replaced": self.replaced}, "replaced")

    def _write(self, locator: dict[str, Any], name: str) -> None:
        temporary = self.directory / f"{name}.locator"
        temporary.write_text(json.dumps(locator, indent=2) + "\n")
        os.replace(temporary, self.locator)


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
    # The questions as the harness lists them; `questions` is what the flow asks of them.
    listed: list[GrillQuestion]
    authorize_jev: bool
    publication_authorized: bool
    spec: SpecClient
    tickets: TicketClient
    implementation: ImplementationClient
    skills: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The question the flow last sent to the grill, and what the agent chose to do with the
    # refusals and the weak questions it was handed, with why.
    asking: str | None = None
    revisions: list[dict[str, Any]] = field(default_factory=list)
    # The text the harness wrote last for a synthesis the host refused, to register an action on.
    drafted: dict[str, Any] = field(default_factory=dict)
    # The times the agent reopened the grill from each stage in this run.
    reopened: dict[str, int] = field(default_factory=dict)
    # The questions the agent set aside in this run, each with the listing it was set aside from.
    # The journal keeps them once the host takes a question in their place, never before.
    aside: list[tuple[list[GrillQuestion], str]] = field(default_factory=list)

    @property
    def roots(self) -> list[Path]:
        return [Path(value) for value in self.request.authorized_skill_roots]

    @property
    def questions(self) -> list[GrillQuestion]:
        """The questions to ask: the listed ones, each replaced by what took its place."""
        return self.view(self.listed, self.aside)

    def view(
        self, listed: list[GrillQuestion], aside: Sequence[tuple[list[GrillQuestion], str]]
    ) -> list[GrillQuestion]:
        """The questions of a listing, with the replacements kept and the questions set aside."""
        return _substituted(listed, _set_aside(self.journal.replaced, aside))


class _Reasoned:
    """The harness, with the reason the agent gave to research on every request it plans.

    It only plans the research that follows the choice, so it does not revise: a refusal that
    comes up meanwhile ends the research and goes back to the harness that chose it.
    """

    def __init__(self, harness: FlowHarness, reason: str) -> None:
        self._harness, self._reason = harness, reason

    async def questions(self, state: dict[str, Any]) -> list[GrillQuestion]:
        return await self._harness.questions(state)

    async def skill(self, stage: str) -> dict[str, Any]:
        return await self._harness.skill(stage)

    async def spec(self, context: dict[str, Any]) -> SpecDraft:
        return await self._harness.spec(context)

    async def tickets(self, context: dict[str, Any]) -> TicketPlan:
        return await self._harness.tickets(context)

    async def research(self, state: dict[str, Any], claim: str) -> ResearchRequest:
        request = await self._harness.research(state, claim)
        if request.reason.strip():
            return request
        return request.model_copy(update={"reason": self._reason})


def _once(
    revised: Any, fallback: Callable[[dict[str, Any]], Awaitable[Any]]
) -> Callable[[dict[str, Any]], Awaitable[Any]]:
    """A callback that answers with the revised text once; the next call is the harness's own."""
    held = [revised]

    async def answer(context: dict[str, Any]) -> Any:
        if held:
            return held.pop()
        return await fallback(context)

    return answer


def _open_turn(state: dict[str, Any], question_id: str | None) -> dict[str, Any] | None:
    """The latest turn of a question the grill still holds open: pending or stale."""
    turn = next(
        (item for item in reversed(state.get("turns", [])) if item["question"]["id"] == question_id),
        None,
    )
    return turn if turn is not None and turn["status"] in {"pending", "stale"} else None


def _words(question: GrillQuestion) -> str:
    """What the flow remembers of a question it replaced: its content, never its id or lineage.

    A harness that words the question another way, or on other evidence, lists another question.
    """
    content = question.model_dump(mode="json", exclude={"id", "lineage", "lineage_action"})
    return sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _substituted(
    listed: list[GrillQuestion], replaced: Mapping[str, Mapping[str, Any]]
) -> list[GrillQuestion]:
    """The questions the harness listed, with each one the flow replaced swapped for its parts.

    The parts are the ones the flow sent in its place, none when the agent set it aside.
    """
    questions: list[GrillQuestion] = []
    for question in listed:
        held = replaced.get(question.id)
        if held is not None and held["digest"] == _words(question):
            questions.extend(GrillQuestion.model_validate(part) for part in held["parts"])
        else:
            questions.append(question)
    return questions


def _checked(questions: list[GrillQuestion]) -> list[GrillQuestion]:
    """The questions the harness listed, refused when they repeat an id or pass the limit."""
    if len(questions) > 100 or len({q.id for q in questions}) != len(questions):
        raise ValueError("O grill exige perguntas únicas dentro do limite de 100.")
    return questions


def _replacing(
    listed: Sequence[GrillQuestion],
    replaced: Mapping[str, Mapping[str, Any]],
    question_id: str | None,
    parts: Sequence[GrillQuestion],
) -> dict[str, dict[str, Any]] | None:
    """The replacements with `parts` in place of a question of the listing, or of a part the flow
    sent in place of one; None when no listed question and no part has the id. Changes nothing.
    """
    if question_id is None:
        return None
    dumped = [part.model_dump(mode="json") for part in parts]
    updated = {key: {**held, "parts": list(held["parts"])} for key, held in replaced.items()}
    for item in listed:
        held = updated.get(item.id)
        if held is None or held["digest"] != _words(item):
            continue
        ids = [part["id"] for part in held["parts"]]
        if question_id in ids:
            at = ids.index(question_id)
            held["parts"][at : at + 1] = dumped
            return updated
    question = next((item for item in listed if item.id == question_id), None)
    if question is None:
        return None
    updated[question_id] = {"digest": _words(question), "parts": dumped}
    return updated


def _set_aside(
    replaced: Mapping[str, Mapping[str, Any]],
    aside: Sequence[tuple[Sequence[GrillQuestion], str]],
) -> dict[str, dict[str, Any]]:
    """The replacements with each question set aside replaced by nothing, from its own listing."""
    current = {key: dict(held) for key, held in replaced.items()}
    for listed, question_id in aside:
        current = _replacing(listed, current, question_id, []) or current
    return current


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
        questions = _checked(await adapters.harness.questions(state))
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
            # A text the agent wrote was refused: its harness answers, when it can, and the
            # step goes on from the revision, which may end in a gap to adjudicate or in
            # another refusal. Without the capability, the typed result ends the run below.
            result = await self._revise(state, result, plan)
            if (
                result.get("research_handoff", {}).get("route")
                == "bounded_adjudication"
            ):
                result = await self._revise(state, await self._adjudicate(result, plan), plan)
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
            if (reworded := await self._held(state, plan)) is not None:
                return reworded
            prompt = await plan.adapters.harness.research(state, question["id"])
            return await self._refused(
                run_id,
                await self.research_until_resolved(
                    run_id,
                    [prompt],
                    idempotency_key=key,
                    authorize_jev=plan.authorize_jev,
                    capture=plan.adapters.capture,
                ),
            )
        return await self._ask(state, key, plan)

    @staticmethod
    def _unresolved(
        state: dict[str, Any], questions: Sequence[GrillQuestion]
    ) -> GrillQuestion | None:
        """The first question the grill has not decided as the harness words it."""
        resolved = {t["question"]["id"]: t for t in state.get("decisions", [])}
        return next(
            (
                q
                for q in questions
                if q.id not in resolved or not _same_question(q, resolved[q.id])
            ),
            None,
        )

    async def _ask(self, state: dict[str, Any], key: str, plan: _Plan) -> dict[str, Any]:
        """Send the next question the grill has not decided, or finish when none is left."""
        run_id = state["id"]
        if question_to_ask := self._unresolved(state, plan.questions):
            plan.asking = question_to_ask.id
            asked = await self._refused(
                run_id,
                await self.continue_run(
                    run_id,
                    state["revision"],
                    idempotency_key=key,
                    question=question_to_ask,
                ),
            )
            if not asked.get("error"):
                self._settle(plan)
            return asked
        return await self.continue_run(
            run_id, state["revision"], idempotency_key=key, finish=True
        )

    @staticmethod
    def _settle(plan: _Plan) -> None:
        """Keep the questions the agent set aside, now that the host took a question in their place.

        Until then a run that stops keeps none of them: the one that resumes asks them again, and
        the agent is asked again about each.
        """
        if not plan.aside:
            return
        plan.journal.replaced = _set_aside(plan.journal.replaced, plan.aside)
        plan.aside.clear()
        plan.journal.keep()

    async def _refused(self, run_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """A refused wording is the agent's to rewrite, not a person's to decide.

        The result keeps the run with the typed refusal; the loop hands it to the harness
        that revises, or ends the run on it.
        """
        if result.get("action") != AGENT_REVISION:
            return result
        current = await self.get(run_id)
        return result if current.get("error") else {**current, **result}

    # Revision: a refused or weak text is answered by the agent that wrote it, never by a person.

    @staticmethod
    def _stage(state: dict[str, Any]) -> Stage | None:
        """The stage whose text a refusal is about, from the same state `_advance` reads."""
        stage = state["stage"]
        if state.get("pending") or stage == "grill":
            return "grill"
        if state.get("spec", {}).get("status") in {None, "stale"} or stage in {
            "spec_review",
            "spec_ready",
            "spec_publishing",
        }:
            return "spec"
        if state.get("tickets", {}).get("status") in {None, "stale"} or stage in {
            "tickets_review",
            "tickets_ready",
            "tickets_publishing",
            "tickets_publication_review",
        }:
            return "tickets"
        return None

    async def _revise(
        self, state: dict[str, Any], result: dict[str, Any], plan: _Plan
    ) -> dict[str, Any]:
        """Hand the typed refusal of a text back to the harness that wrote it, until one holds.

        Only a harness with the capability is asked. The refusal stands, to end the run as the
        agent's, when the harness cannot revise, when the code allows the agent nothing for it, or
        when the harness is refused `REVISION_LIMIT` times in a row. A revision the host accepts
        is the result of the step, and the loop goes on from it like from any other.
        """
        harness = plan.adapters.harness
        if not isinstance(harness, RevisingHarness):
            return result
        # Where the last revision sent its text, when the run's state cannot say: a question sent to
        # the grill while the spec or the graph waits is refused with nothing stored.
        origin: Stage | None = None
        for _ in range(REVISION_LIMIT):
            if not is_refusal(result):
                return result
            state = await plan.tickets.get(state["id"])
            if state.get("error"):
                raise _Stop(state)
            stage = origin or self._stage(state)
            allowed = allowed_of(result)
            if state.get("pending") or state.get("research_pending"):
                # The host takes no new question while one waits for a person or a cycle is open.
                allowed = without_reopening(allowed)
            if stage is None or not any(allowed.values()):
                return result
            result, origin = await self._revise_once(
                stage, state, result, result["error"]["code"], allowed, plan
            )
        return result

    async def _held(self, state: dict[str, Any], plan: _Plan) -> dict[str, Any] | None:
        """A pending question the code lets the agent reword is reworded, not researched.

        The set comes from the lineage the host reads: a defect found after an admitted research
        cycle leaves the question pending with `reformulate` and `split`. The question stays with
        the person when the set holds nothing the host has an operation for.
        """
        if not isinstance(plan.adapters.harness, RevisingHarness):
            return None
        question_id = state["pending"]["question"]["id"]
        offered = [
            action
            for action in (state.get("lineages", {}).get(question_id) or {}).get("next_actions") or []
            if action in REVISE
        ]
        if not offered:
            return None
        plan.asking = question_id
        reworded, _ = await self._revise_once(
            "grill", state, state, "weak_question", {question_id: offered}, plan
        )
        return reworded

    async def _revise_once(
        self,
        stage: Stage,
        state: dict[str, Any],
        refusal_or_weak: dict[str, Any],
        code: str,
        allowed: dict[str, list[str]],
        plan: _Plan,
    ) -> tuple[dict[str, Any], Stage | None]:
        """Ask for one revision, refuse it locally when its action is not in the set, else send it.

        Returns the host's answer and the stage whose text it answers, when that is not the one
        the state of the run tells.
        """
        harness = plan.adapters.harness
        assert isinstance(harness, RevisingHarness)
        revision = await harness.revise(stage, refusal_or_weak, allowed)
        # The claims an action refusal names are the weak claims of the artifact, as the host keys them.
        named = allowed if code in {ACTION_NOT_REGISTERED, ACTION_NOT_ALLOWED} else {}
        # The text an action refusal answers when the host stored none: the one it was just sent.
        unstored = plan.drafted.get(stage) if named else None
        problem = refuse_choice(revision, allowed) or refuse_content(revision, stage)
        listed: list[GrillQuestion] = []
        if problem is None and revision.action == "new_grill_question":
            # The host accepts each new question, so only the flow can bound the reopenings of a
            # stage: a spec written again may come back with the same decision.
            plan.reopened[stage] = plan.reopened.get(stage, 0) + 1
            if plan.reopened[stage] > REOPEN_LIMIT:
                problem = refuse_reopening(revision)
            else:
                listed, problem = await self._relisted(stage, state, revision, plan)
        elif problem is None and stage == "grill":
            problem = self._refuse_parts(state, revision, plan)
        plan.revisions.append(report(stage, revision, code, rejected=problem is not None))
        plan.journal.capture({**state, "revisions": plan.revisions}, plan.request, "revision")
        if problem is not None:
            # The host is never called and nothing of the choice is kept: it is outside the set,
            # brings no text, repeats an id, reopens the stage after the limit or has no new question.
            raise _Stop({**state, **problem.refusal()})
        # The host stores nothing it refuses, so a text sent again under the key of a refused one
        # is a new request; the revision of the run still binds the key to the state it answers.
        key = f"flow:{plan.request.delivery.delivery_id}:{state['revision']}:revision"
        if revision.action == "research":
            if unstored is not None:
                # The host wanted the action registered on the text it was sent, or refused the one
                # it declared: it is the same text, with the action the agent chose now.
                return (
                    await self._rewrite(
                        stage, state, revision, key, plan, drafted=unstored, named=named
                    ),
                    stage,
                )
            return await self._researching(state, revision, plan), None
        if revision.action == "new_grill_question":
            return await self._reopen(stage, state, listed, key, plan), "grill"
        if stage == "grill":
            return await self._reword(state, revision, key, plan), "grill"
        # A contradiction is corrected in the text the refusal answers; a revision brings its own.
        held = unstored if revision.action == "accept_contradiction" else None
        return (
            await self._rewrite(stage, state, revision, key, plan, drafted=held, named=named),
            stage,
        )

    async def _researching(
        self, state: dict[str, Any], revision: Revision, plan: _Plan
    ) -> dict[str, Any]:
        """Research through the loop the stage already has, with the reason the agent gave.

        The loop researches the stored artifact, or writes a stale one again: a refusal it ends on
        answers none of the texts the host refused before, so the flow forgets them.
        """
        plan.drafted.clear()
        adapters = plan.adapters
        plan.adapters = replace(adapters, harness=_Reasoned(adapters.harness, revision.reason))
        try:
            return await self._advance(state, plan)
        finally:
            plan.adapters = adapters

    @staticmethod
    def _setting_aside(stage: Stage, plan: _Plan) -> list[tuple[list[GrillQuestion], str]]:
        """The question a new one replaces: the one of the grill the code placed outside it."""
        return [(plan.listed, plan.asking)] if stage == "grill" and plan.asking else []

    async def _relisted(
        self, stage: Stage, state: dict[str, Any], revision: Revision, plan: _Plan
    ) -> tuple[list[GrillQuestion], AgentRefusal | None]:
        """The questions the harness lists now, and the refusal of a new question it does not list.

        The question the code placed outside the grill is not the new one, whatever the harness
        still lists. It is left out only of the view this reads: nothing is kept here.
        """
        questions = _checked(await plan.adapters.harness.questions(state))
        aside = [*plan.aside, *self._setting_aside(stage, plan)]
        if self._unresolved(state, plan.view(questions, aside)) is not None:
            return questions, None
        return questions, AgentRefusal(
            ACTION_NOT_REGISTERED,
            NOT_REGISTERED_MESSAGE,
            [
                {
                    "target": revision.target or "*",
                    "required": "new_grill_question",
                    "problem": "no_new_question",
                }
            ],
        )

    async def _reopen(
        self, stage: Stage, state: dict[str, Any], listed: list[GrillQuestion], key: str, plan: _Plan
    ) -> dict[str, Any]:
        """Go back to the grill with a question the harness lists now; the spec waits for it.

        A question of the grill the code placed outside it is set aside in this run, and kept aside
        once the host takes a question in its place.
        """
        plan.aside.extend(self._setting_aside(stage, plan))
        plan.listed = listed
        return await self._ask(state, key, plan)

    @staticmethod
    def _rewording(state: dict[str, Any], plan: _Plan) -> str | None:
        """The question a revision of the grill answers: the one sent last, or the one pending."""
        return plan.asking or (state.get("pending") or {}).get("question", {}).get("id")

    @staticmethod
    def _refuse_parts(
        state: dict[str, Any], revision: Revision, plan: _Plan
    ) -> AgentRefusal | None:
        """The refusal of parts of a question the grill stored nowhere that repeat an id.

        Each part is asked on its own, so no part may have the id of another part, of a question
        the grill holds or of one still to ask, save the id of the question it replaces. Nor the id
        of one set aside in this run, which the flow still removes by its id. The parts of a
        question the grill holds open declare it as their original, and the host checks them.
        """
        question_id = FlowClient._rewording(state, plan)
        if not revision.parts or _open_turn(state, question_id) is not None:
            return None
        taken = (
            {question.id for question in plan.questions}
            | {turn["question"]["id"] for turn in state.get("turns", [])}
            | {aside for _, aside in plan.aside}
        )
        taken.discard(question_id)
        return refuse_parts(revision, taken)

    async def _reword(
        self, state: dict[str, Any], revision: Revision, key: str, plan: _Plan
    ) -> dict[str, Any]:
        """Send the reformulated question, or the parts of the division, the agent wrote.

        A question the grill holds open is the original of a lineage, and the parts declare it with
        the action and the reason, on the context the turn stored. A refused question was stored
        nowhere, so it is no original: its parts are new questions, each asked on its own.
        """
        question_id = self._rewording(state, plan)
        turn = _open_turn(state, question_id)
        parts = revision.parts
        if turn is None:
            self._replace(plan, question_id, parts)
            return await self._ask(state, key, plan)
        kind: Literal["reformulation", "split"] = (
            "reformulation" if revision.action == "reformulate" else "split"
        )
        action: Literal["reformulate", "split"] = (
            "reformulate" if revision.action == "reformulate" else "split"
        )
        stored = GrillQuestion.model_validate(turn["question"])
        declared = [
            part.model_copy(
                update={
                    "lineage": QuestionLineage(original=str(question_id), kind=kind),
                    "lineage_action": QuestionAction(action=action, reason=revision.reason),
                    # A pending question was rejected on this context: the evidence that is new
                    # goes through the research cycle, not through a rewording.
                    **({"context": stored.context} if turn["status"] == "pending" else {}),
                }
            )
            for part in parts
        ]
        sent = await self._refused(
            state["id"],
            await self.continue_run(
                state["id"],
                state["revision"],
                idempotency_key=key,
                question=declared[0],
                siblings=declared[1:],
            ),
        )
        if not is_refusal(sent):
            self._replace(plan, question_id, declared)
        return sent

    @staticmethod
    def _replace(plan: _Plan, question_id: str | None, parts: list[GrillQuestion]) -> None:
        """Remember the parts that took the place of a question, or that it was set aside (none).

        The harness goes on listing the question as it worded it, and a run that resumes lists it
        again: the journal keeps what the flow asks in its place, for as long as the words are those.
        """
        replaced = _replacing(plan.listed, plan.journal.replaced, question_id, parts)
        if replaced is None:
            return
        plan.journal.replaced = replaced
        plan.journal.keep()

    async def _rewrite(
        self,
        stage: Stage,
        state: dict[str, Any],
        revision: Revision,
        key: str,
        plan: _Plan,
        drafted: Any = None,
        named: Mapping[str, Sequence[str]] | None = None,
    ) -> dict[str, Any]:
        """Submit the revised spec or graph, with the action and the reason it registers.

        The revision goes through the verification like any other: the checks of the text, the
        equivalence of a division with the original it declares, and the judgment. A research
        cycle the refusal left evaluating takes the revision as its repair, so the cycle closes
        on it; otherwise it is a revision of its own. A confident contradiction the agent accepts
        is corrected through the repair callback of the stage, with no cycle of research.

        `drafted` is the text the host refused for want of an action, or for an action outside the
        set: the same text goes again with the action the agent chose now, and the actions it carried
        were the ones the host refused. A contradiction the agent accepts is corrected in that text,
        and in the stored one only when the refusal answers it: once the grill is reopened, the
        stored one is stale, written from answers the grill no longer holds.
        """
        harness = plan.adapters.harness
        spec = stage == "spec"
        name = "spec" if spec else "tickets"
        client = plan.spec if spec else plan.tickets
        fresh = await client.get(state["id"])
        if fresh.get("error"):
            raise _Stop(fresh)
        stored = fresh.get(name) or {}
        # A stale artifact reads with no weak claims; the refusal that came for it names them.
        weak = stored.get("next_actions") or named or {}
        cycle = bool(fresh.get("research_pending"))
        repair = harness.spec if spec else harness.tickets
        again = drafted is not None
        if drafted is None:
            drafted = revision.spec if spec else revision.tickets
        if revision.action == "accept_contradiction":
            claims = targets(weak, revision)
            answered = (
                {**stored, "draft": drafted.model_dump(mode="json")} if again else fresh[name]
            )
            drafted = await repair(
                {
                    "spec" if spec else "graph": answered,
                    "claim_id": claims[0] if claims else revision.target or "",
                    "contradiction": {"claims": claims, "reason": revision.reason},
                    "directive": (
                        "A verificação confirmou a contradição: aceite-a e corrija apenas a claim "
                        "para o que a evidência sustenta, sem pesquisar; "
                        + (
                            "preserve decisões e testing seams."
                            if spec
                            else "preserve obrigações, repos donos e dependências reais."
                        )
                    ),
                }
            )
        # The cycle registered its action when it opened; any other revision registers its own.
        # The actions the draft carries are the agent's only when it declared them in this
        # revision: the repair of a contradiction returns the stored text with the ones registered
        # for the revision before, and a text sent again carries what the host refused.
        carried = (
            []
            if again or revision.action == "accept_contradiction"
            else own_actions(
                drafted.lineage_actions,
                [
                    LineageAction.model_validate(item)
                    for item in (stored.get("draft") or {}).get("lineage_actions") or []
                ],
            )
        )
        draft = (
            drafted
            if cycle
            else drafted.model_copy(
                update={"lineage_actions": registered(carried, weak, revision)}
            )
        )
        callback = self._remembered(stage, plan, _once(draft, repair))
        if cycle:
            research = plan.spec.research_spec if spec else plan.tickets.research_tickets
            result = await research(
                state["id"],
                plan=harness.research,
                idempotency_key=key,
                authorize_jev=plan.authorize_jev,
                repair=callback,
                capture=plan.adapters.capture,
            )
        else:
            result = await client.synthesize(
                state["id"],
                fresh["revision"],
                idempotency_key=key,
                project=plan.project,
                skill_path=await self._skill(stage, state, plan),
                authorized_roots=plan.roots,
                harness=callback,
                research_sources=plan.request.research_sources,
            )
        if not is_refusal(result):
            plan.drafted.pop(stage, None)
        return result

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

    @staticmethod
    def _remembered(
        stage: Stage, plan: _Plan, write: Callable[[dict[str, Any]], Awaitable[Any]]
    ) -> Callable[[dict[str, Any]], Awaitable[Any]]:
        """The synthesis callback, keeping the text it wrote until the host takes it."""

        async def remember(context: dict[str, Any]) -> Any:
            plan.drafted[stage] = drafted = await write(context)
            return drafted

        return remember

    async def _synthesize_spec(self, state: dict[str, Any], key: str, plan: _Plan) -> dict[str, Any]:
        skill = await self._skill("spec", state, plan)
        result = await plan.spec.synthesize(
            state["id"],
            state["revision"],
            idempotency_key=key,
            project=plan.project,
            skill_path=skill,
            authorized_roots=plan.roots,
            harness=self._remembered("spec", plan, plan.adapters.harness.spec),
            research_sources=plan.request.research_sources,
        )
        if not is_refusal(result):
            plan.drafted.pop("spec", None)
        return result

    async def _review_spec(self, state: dict[str, Any], key: str, plan: _Plan) -> dict[str, Any]:
        spec = state["spec"]
        threshold = state["workflow_policy"]["confidence"]
        approval = plan.request.spec_reviews.get(spec["revision"])
        if weak_claims(spec.get("verification", {}), threshold):
            researched = await plan.spec.research_spec(
                state["id"],
                plan=plan.adapters.harness.research,
                idempotency_key=key,
                authorize_jev=plan.authorize_jev,
                repair=plan.adapters.harness.spec,
                capture=plan.adapters.capture,
            )
            # The host accepts a residual only after the cycles end, so a person's
            # acceptance of this exact revision is applied once the research stops.
            if (
                approval
                and approval.residual
                and researched.get("research_handoff")
                and (researched.get("spec") or {}).get("revision") == spec["revision"]
                and spec.get("authorization") != approval.model_dump()
            ):
                return await plan.spec.review(
                    state["id"],
                    researched["revision"],
                    idempotency_key=f"{key}:accept",
                    artifact_revision=spec["revision"],
                    authorization=approval,
                )
            return researched
        if approval:
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
        result = await plan.tickets.synthesize(
            state["id"],
            state["revision"],
            idempotency_key=key,
            project=plan.project,
            skill_path=skill,
            authorized_roots=plan.roots,
            harness=self._remembered("tickets", plan, plan.adapters.harness.tickets),
            research_sources=plan.request.research_sources,
        )
        if not is_refusal(result):
            plan.drafted.pop("tickets", None)
        return result

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
        # The agent's own refusal is not an escalation: only its text stopped the run, and no
        # other exception, a person's or the flow's, rides on it.
        stopped = {item.get("code") for item in exceptions}
        refused = state.get("action") == AGENT_REVISION and bool(stopped) and stopped <= AGENT_CODES
        result = {
            **state,
            "exceptions": exceptions,
            # What the agent chose to do with each refusal and weak question, and why.
            **({"revisions": plan.revisions} if plan.revisions else {}),
            # Not failures: a ticket without a verified draft of each blocker waits for the
            # merge, which is a person's.
            **({"awaiting_blockers": waiting} if delivery else {}),
            "auto_advance": False,
            "action": AGENT_REVISION
            if refused
            else "needs_human"
            if exceptions
            else "draft_review",
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

        A ticket waits only while a blocker is still open and has no verified draft to
        stack on. One that left the frontier for another reason (closed as not planned,
        not actionable) is an exception of its own.
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
