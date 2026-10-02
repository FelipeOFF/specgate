"""Public harness API for a grill; retries keep the original idempotency key."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mcp.shared.dispatcher import ProgressFnT

from specgate.context import ContextPacket
from specgate.delegation import load_delegation
from specgate.grill_contracts import GrillQuestion, HumanAnswer
from specgate.payload import CODE, PayloadTooLarge
from specgate.research import (
    ResearchRequest,
    ResearchSource,
    collect_research,
    save_research,
)
from specgate.research_filter import (
    CollectedPacket,
    filter_research,
    overflow_report,
    read_packet,
)
from specgate.research_ledger import counted, exhausted, resumable, stop
from specgate.transport import MCPTransportError, call_tool
from specgate.workflow_policy import WorkflowPolicy, resolve_workflow_policy


@dataclass(frozen=True)
class GrillClient:
    url: str
    token: str
    transport: str = "streamable"
    ca_file: Path | None = None
    timeout_seconds: float = 30
    progress_callback: ProgressFnT | None = None
    project_id: str | None = None
    home: Path | None = None
    project: Path | None = None
    skill: str = "grill-with-jev"

    async def _call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        try:
            result = await call_tool(
                self.url,
                self.token,
                tool,
                arguments,
                transport=self.transport,
                ca_file=self.ca_file,
                timeout_seconds=self.timeout_seconds,
                progress_callback=self.progress_callback,
                project_id=self.project_id,
            )
        except PayloadTooLarge as overflow:
            # Nothing was sent: unlike a transport failure, there is nothing to reconcile.
            return overflow.result()
        except MCPTransportError as error:
            failure = {"code": error.code, "message": str(error)}
            if fallback := getattr(error, "fallback", None):
                failure["fallback"] = fallback
            return {
                "action": "needs_human",
                "auto_advance": False,
                "error": failure,
                "reconcile": True,
            }
        if result.is_error or result.structured_content is None:
            raise ValueError("O MCP não retornou um resultado válido do grill.")
        content: dict[str, Any] = result.structured_content
        return content

    async def start(
        self,
        objective: str,
        context: ContextPacket,
        *,
        idempotency_key: str,
        policy: WorkflowPolicy | None = None,
    ) -> dict[str, Any]:
        if policy is None:
            project = self.project or Path.cwd()
            policy = WorkflowPolicy.model_validate(
                resolve_workflow_policy(
                    home=self.home, project=project, skill=self.skill
                )["policy"]
            ).snapshot(project)
        return await self._call(
            "grill_start",
            policy=policy.model_dump(),
            objective=objective,
            context=asdict(context),
            idempotency_key=idempotency_key,
        )

    async def set_policy(
        self,
        run_id: str,
        expected_revision: int,
        *,
        idempotency_key: str,
        policy: WorkflowPolicy,
    ) -> dict[str, Any]:
        """Apply an explicitly authorized snapshot; never rewrite local setup."""
        return await self._call(
            "grill_set_policy",
            run_id=run_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            policy=policy.model_dump(),
        )

    async def get(self, run_id: str) -> dict[str, Any]:
        return await self._call("grill_get", run_id=run_id)

    async def research(
        self,
        run_id: str,
        expected_revision: int,
        *,
        idempotency_key: str,
        filtered: dict[str, Any],
        artifact: dict[str, str],
    ) -> dict[str, Any]:
        return await self._call(
            "grill_research",
            run_id=run_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            filtered=filtered,
            artifact=artifact,
        )

    async def research_begin(
        self,
        run_id: str,
        expected_revision: int,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._call(
            "grill_research_begin",
            run_id=run_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    async def _collect_cycle(
        self,
        request: ResearchRequest,
        previous: list[dict[str, str]],
        *,
        authorize_jev: bool,
        capture: Callable[[ResearchSource], Awaitable[None]] | None,
        known: Sequence[str] = (),
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Collect and filter one cycle; return the filtered packet and its artifact.

        `known` holds texts already cited, so an excerpt of them is not new evidence.
        """
        project = self.project or Path.cwd()
        options = {
            "transport": self.transport,
            "ca_file": self.ca_file,
            "timeout_seconds": self.timeout_seconds,
            "project_id": self.project_id,
            "progress_callback": self.progress_callback,
        }
        collected = await collect_research(
            request,
            project,
            self.url,
            self.token,
            authorize_jev=authorize_jev,
            capture=capture,
            transport_options=options,
        )
        collected_ref = save_research(collected, project, "docs/research")
        filtered = await filter_research(
            CollectedPacket.model_validate(collected),
            [read_packet(project, row["path"])[0] for row in previous],
            self.url,
            self.token,
            authorize_jev=authorize_jev,
            known=known,
            transport_options=options,
        )
        filtered["history"] = {
            "input": collected_ref,
            "previous": previous,
            "selected_ids": [row["id"] for row in filtered["selected"]],
            "rejected_ids": [row["id"] for row in filtered["rejected"]],
        }
        return filtered, save_research(filtered, project, "docs/research/filtered")

    async def _record_cycle(
        self,
        tool: str,
        filtered: dict[str, Any],
        artifact: dict[str, str],
        **arguments: Any,
    ) -> dict[str, Any]:
        """Record a filtered cycle; an overflow is reported, never recorded as a verdict.

        The host keeps the reserved cycle on a report, so the cycle resumes with the
        same key once the dev shrinks the sources.
        """
        recorded = await self._call(
            tool, **arguments, filtered=filtered, artifact=artifact
        )
        overflow = PayloadTooLarge.from_result(recorded)
        if overflow is not None:
            # The packet is too big to send: report the sizes instead of its content.
            report = overflow_report(
                filtered["question"],
                filtered["claim"],
                filtered["selected_topic"],
                "record",
                None,
                overflow,
            )
            report["history"] = {
                **filtered["history"],
                "selected_ids": [],
                "rejected_ids": [],
            }
            recorded = await self._call(
                tool,
                **arguments,
                filtered=report,
                artifact=save_research(
                    report, self.project or Path.cwd(), "docs/research/filtered"
                ),
            )
        elif filtered.get("collection_diagnostic") == CODE:
            found = filtered["overflow"]
            overflow = PayloadTooLarge(found["size"], found["limit"])
        if overflow is None or recorded.get("error"):
            return recorded
        return overflow.review(recorded)

    async def _reserve_research(
        self,
        run_id: str,
        state: dict[str, Any],
        active: dict[str, Any],
        request: ResearchRequest,
        *,
        idempotency_key: str,
        authorize_jev: bool,
    ) -> tuple[dict[str, Any], bool]:
        """Reserve the cycle; the flag says whether this caller owns the external work."""
        question_id = active["question"]["id"]
        diagnostic = (
            "authorization_required"
            if not authorize_jev
            else "claim_required"
            if not request.claim
            else "human_decision_required"
            if active["question"]["requires_authorization"]
            or active["question"]["missing_personal_fact"]
            else "evaluation_unavailable"
            if active["evaluation"].get("origin") != "jev"
            or active["evaluation"].get("action") == "error"
            else "research_budget_exhausted"
            if exhausted(
                state, question_id, state["workflow_policy"].get("research_budget", 12)
            )
            else "research_stopped"
            if stop(state, question_id)
            else None
        )
        if diagnostic:
            return {**state, "diagnostic": {"code": diagnostic}}, False
        reserved = await self.research_begin(
            run_id,
            state["revision"],
            idempotency_key=idempotency_key,
        )
        if reserved.get("reconcile") or reserved.get("error"):
            return reserved, False
        if reserved.get("reservation_acquired"):
            return reserved, True
        current = await self.get(run_id)
        if current.get("reconcile") or current.get("error"):
            return current, False
        pending = current.get("research_pending")
        if pending and pending["cycle_key"] == idempotency_key:
            if pending["phase"] == "evaluating":
                evaluated = await self._evaluate_research(
                    run_id, current, idempotency_key
                )
                return evaluated, False
            unknown = {**current, "diagnostic": {"code": "external_outcome_unknown"}}
            return unknown, False
        return current, False

    async def _evaluate_research(
        self, run_id: str, state: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        return await self.continue_run(
            run_id,
            state["revision"],
            idempotency_key=f"{idempotency_key}:evaluation",
            question=GrillQuestion.model_validate(
                state["research_pending"]["question"]
            ),
        )

    async def research_cycle(
        self,
        run_id: str,
        request: ResearchRequest,
        *,
        idempotency_key: str,
        authorize_jev: bool,
        capture: Callable[[ResearchSource], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Collect, filter and re-evaluate one durable cycle for the active question."""
        state = await self.get(run_id)
        if state.get("reconcile") or state.get("error"):
            return state
        pending = state.get("research_pending")
        if pending is not None:
            if pending["cycle_key"] != idempotency_key:
                return {**state, "diagnostic": {"code": "research_in_progress"}}
            if pending["phase"] == "evaluating":
                return await self._evaluate_research(run_id, state, idempotency_key)
            if not resumable(state, idempotency_key):
                return {**state, "diagnostic": {"code": "external_outcome_unknown"}}
        elif any(
            item.get("cycle_key") == idempotency_key
            for item in state.get("research_attempts", [])
        ):
            return state
        active = state.get("pending")
        if not active or active["question"]["question"] != request.question:
            raise ValueError("A pesquisa deve corresponder à pergunta ativa.")
        if pending is None:
            state, owned = await self._reserve_research(
                run_id,
                state,
                active,
                request,
                idempotency_key=idempotency_key,
                authorize_jev=authorize_jev,
            )
            if not owned:
                return state
        filtered, artifact = await self._collect_cycle(
            request,
            [
                item["collected"]
                for item in counted(state, active["question"]["id"])
                if item.get("collected")
            ],
            authorize_jev=authorize_jev,
            capture=capture,
        )
        recorded = await self._record_cycle(
            "grill_research",
            filtered,
            artifact,
            run_id=run_id,
            expected_revision=state["revision"],
            idempotency_key=idempotency_key,
        )
        if (
            recorded.get("reconcile")
            or recorded.get("error")
            or not recorded.get("research_pending")
        ):
            return recorded
        return await self._evaluate_research(run_id, recorded, idempotency_key)

    async def research_until_resolved(
        self,
        run_id: str,
        requests: list[ResearchRequest],
        *,
        idempotency_key: str,
        authorize_jev: bool,
        capture: Callable[[ResearchSource], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        state = await self.get(run_id)
        active_id = (state.get("pending") or {}).get("question", {}).get("id")
        for index, request in enumerate(requests[:3], start=1):
            if (
                state.get("error")
                or state.get("diagnostic")
                or not state.get("pending")
            ):
                break
            state = await self.research_cycle(
                run_id,
                request,
                idempotency_key=f"{idempotency_key}:{index}",
                authorize_jev=authorize_jev,
                capture=capture,
            )
            if state.get("research_stopped", {}).get(active_id) or state.get(
                "reconcile"
            ):
                break
        return self.research_handoff(state)

    def research_handoff(self, state: dict[str, Any]) -> dict[str, Any]:
        """Persist an unresolved gap for the flow without granting another attempt."""
        pending = state.get("pending")
        if not pending or not pending.get("human_fallback"):
            return state
        question = pending["question"]
        reason = state.get("research_stopped", {}).get(question["id"])
        if reason not in {"evaluation_limit", "research_limit", "research_budget_exhausted", "stagnation"}:
            return state
        project = self.project or Path.cwd()
        grant = load_delegation(self.home)
        delegated = (
            grant.active(project, self.url)
            and not question["requires_authorization"]
            and not question["missing_personal_fact"]
        )
        packet = {
            "schema": 1,
            "run_id": state["id"],
            "run_revision": state["revision"],
            "question_id": question["id"],
            "question": question["question"],
            "policy_hash": state["workflow_policy_hash"],
            "reason": reason,
            "research_attempts": state.get("research_attempts", []),
            "fallback": pending["human_fallback"],
            "route": "bounded_adjudication" if delegated else "human_review",
            "delegation_revision": grant.revision if delegated else None,
            "auto_advance": False,
        }
        artifact = save_research(packet, project, "docs/grills/handoffs")
        return {**state, "research_handoff": {**packet, "artifact": artifact}}

    async def continue_run(
        self,
        run_id: str,
        expected_revision: int,
        *,
        idempotency_key: str,
        question: GrillQuestion | None = None,
        finish: bool = False,
    ) -> dict[str, Any]:
        return await self._call(
            "grill_continue",
            run_id=run_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            question=question.model_dump(mode="json") if question else None,
            finish=finish,
        )

    async def answer(
        self,
        run_id: str,
        expected_revision: int,
        pending_id: str,
        *,
        idempotency_key: str,
        answer: HumanAnswer,
    ) -> dict[str, Any]:
        return await self._call(
            "grill_answer",
            run_id=run_id,
            expected_revision=expected_revision,
            pending_id=pending_id,
            idempotency_key=idempotency_key,
            answer=answer.model_dump(),
        )

    async def summary(self, run_id: str) -> dict[str, Any]:
        return await self._call("grill_summary", run_id=run_id)
