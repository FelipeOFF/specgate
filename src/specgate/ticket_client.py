"""Harness decomposition and reconciled publication of a ticket graph."""

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, replace
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Any

from specgate.beads_tickets import BeadsTicketTracker
from specgate.client import confirmed_result
from specgate.context import (
    ContextPacket,
    Evidence,
    SkillSource,
    build_context,
    read_skill,
)
from specgate.destination import DestinationError, ProjectTracker, resolve_destination
from specgate.grill_client import GrillClient
from specgate.grill_contracts import GrillError
from specgate.jira_tickets import JiraTicketTracker
from specgate.payload import PayloadTooLarge
from specgate.research import (
    ResearchRequest,
    ResearchSource,
    save_research,
)
from specgate.research_ledger import (
    OPERATIONAL,
    applicable_adjudication,
    counted,
    resumable,
)
from specgate.research_restore import restore_refs
from specgate.spec_contracts import IssueReference
from specgate.ticket_claims import (
    confirmation_rows,
    expected_claims,
    near,
    planning_evidence,
    weak,
)
from specgate.ticket_contracts import (
    TicketAuthorization,
    TicketObservation,
    TicketPlan,
    TicketRelations,
    require_native_relations,
    ticket_body,
    ticket_marker,
)
from specgate.ticket_residual import ENDED, next_cycle, remaining
from specgate.ticket_reuse import (
    change,
    history_of,
    inputs,
    lineage,
    memories,
    reentry,
)
from specgate.tracker import GitHubTracker, TicketTracker, TrackerError
from specgate.verification_refs import context_digest, digest, source_refs

VERIFY_BATCH_SIZE = 20
# The graph travels once, in the evidence; the context only points to it.
GRAPH_ARTIFACT = "Grafo de tickets proposto, no campo graph da evidência."


def _vocabulary(
    project: Path, state: dict[str, Any], research: list[str]
) -> ContextPacket:
    """The vocabulary and the research sources, read once from the project."""
    paths = ["CONTEXT.md", *research]
    return build_context(
        project,
        state["objective"],
        paths,
        paths,
        artifact=state["spec"]["body"],
        alternatives=["revisar decomposição", "publicar tickets"],
    )


def _review(state: dict[str, Any], code: str) -> dict[str, Any]:
    return {
        **state,
        "action": "needs_human",
        "auto_advance": False,
        "error": {
            "code": code,
            "message": "Confira a decomposição e reconcilie o tracker antes de continuar.",
        },
    }


class TicketClient(GrillClient):
    async def get(self, run_id: str) -> dict[str, Any]:
        return await self._call("tickets_get", run_id=run_id)

    async def submit(
        self,
        run_id: str,
        expected_revision: int,
        *,
        idempotency_key: str,
        draft: TicketPlan,
        skill_revision: str,
        vocabulary_revision: str,
        verification: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            "tickets_submit",
            run_id=run_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            draft=draft.model_dump(),
            skill_revision=skill_revision,
            vocabulary_revision=vocabulary_revision,
            verification=verification or {},
        )

    async def review(
        self,
        run_id: str,
        expected_revision: int,
        *,
        idempotency_key: str,
        artifact_revision: str,
        authorization: TicketAuthorization,
        relations: TicketRelations | None = None,
    ) -> dict[str, Any]:
        state = await self._call(
            "tickets_review",
            run_id=run_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            artifact_revision=artifact_revision,
            authorization=authorization.model_dump(),
            relations=(relations or TicketRelations()).model_dump(),
        )

        return self.save_graph(state, self.project) if self.project else state

    async def review_delegated(
        self,
        run_id: str,
        *,
        project: Path,
        artifact_revision: str,
        verification: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        from specgate.delegation import (
            publication_authority,
            verification_allows_review,
        )

        state = await self.get(run_id)
        if "error" in state:
            return state
        graph = state.get("tickets", {})
        judged = [
            *verification.get("results", []),
            *verification.get("confirmations", []),
        ]
        authority = publication_authority(
            project=project,
            host=self.url,
            repositories=[
                t["repository"] for t in graph.get("draft", {}).get("tickets", [])
            ],
            operation="tickets",
            home=self.home,
        )
        if (
            not authority
            or graph.get("status") not in {"review", "ready", "published"}
            or graph.get("revision") != artifact_revision
            or verification.get("artifact_revision") != artifact_revision
            or graph.get("draft", {}).get("gaps")
            or graph.get("verification_gaps", ["verification_missing"])
            or not verification_allows_review(
                # Both evaluations of a confirmed claim must pass the gate.
                judged,
                state.get("workflow_policy", {}).get("confidence", 0.8),
                contexts=[graph.get("verification", {}).get("context_revision")]
                * len(judged),
            )
        ):
            return _review(state, "delegated_review_unavailable")
        return await self.review(
            run_id,
            state["revision"],
            idempotency_key=idempotency_key,
            artifact_revision=artifact_revision,
            authorization=TicketAuthorization(
                decomposition_approved=True, publication_authorized=True, **authority
            ),
        )

    async def synthesize(
        self,
        run_id: str,
        expected_revision: int,
        *,
        idempotency_key: str,
        project: Path,
        skill_path: Path,
        authorized_roots: list[Path],
        harness: Callable[[dict[str, Any]], Awaitable[TicketPlan]],
        research_sources: list[str] | None = None,
    ) -> dict[str, Any]:
        state = await self.get(run_id)
        if "error" in state:
            return state
        if state["revision"] != expected_revision or state["stage"] not in {
            "spec_published",
            "tickets_review",
            "tickets_ready",
            "tickets_published",
        }:
            return _review(state, "spec_unpublished")
        source = SkillSource(
            "skills/to-tickets",
            "",
            str(skill_path.resolve()),
            str(skill_path.absolute()),
            "",
            (),
        )
        instructions = read_skill(source, authorized_roots)
        source = replace(source, revision=sha256(instructions.encode()).hexdigest())

        def vocabulary() -> ContextPacket:
            return _vocabulary(project, state, research_sources or [])

        domain = vocabulary()
        if domain.gaps:
            return _review(state, "vocabulary_missing")
        draft = await harness(
            {
                "instructions": instructions,
                "vocabulary": domain.evidence[0].text,
                "spec": state["spec"],
                "research": [asdict(item) for item in domain.evidence[1:]],
                "directive": "Proponha tracer bullets verticais e bloqueadores reais. Publique somente após revisão autorizada da revisão exata; reutilize delegação válida.",
            }
        )
        try:
            read_skill(source, authorized_roots)
        except (OSError, ValueError):
            return _review(state, "skill_changed")
        if vocabulary().revision != domain.revision:
            return _review(state, "vocabulary_changed")
        try:
            verification = await self._verify_plan(
                state, draft, [asdict(item) for item in domain.evidence[1:]]
            )
        except PayloadTooLarge as overflow:
            return overflow.review(state)
        if verification is None:
            return _review(state, "verification_failed")
        if vocabulary().revision != domain.revision:
            return _review(state, "research_changed")
        submitted = await self.submit(
            run_id,
            expected_revision,
            idempotency_key=idempotency_key,
            draft=draft,
            skill_revision=source.revision,
            vocabulary_revision=domain.revision,
            verification=verification,
        )
        return self.save_graph(submitted, project)

    async def _verify_plan(
        self,
        state: dict[str, Any],
        draft: TicketPlan,
        sources: list[dict[str, Any]],
        rejudge: frozenset[str] = frozenset(),
    ) -> dict[str, Any] | None:
        draft = draft.model_copy(
            update={
                "tickets": [
                    ticket.model_copy(
                        update={
                            "repository": ticket.repository
                            or state["publication"]["issue"]["repository"]
                        }
                    )
                    for ticket in draft.tickets
                ]
            }
        )
        draft.order()
        claims = expected_claims(state["spec"]["body"], draft.model_dump())
        refs = source_refs(sources)
        # Spec, graph and refs only; each research text travels once, in the context.
        evidence = planning_evidence(state["spec"], draft.model_dump(), refs)
        original = state.get("context", {})
        context = asdict(
            ContextPacket(
                objective=state["objective"],
                rules=tuple(original.get("rules", [])),
                artifact=GRAPH_ARTIFACT,
                # The spec leads the context: without it Jev's confidence fell (#124).
                evidence=(
                    Evidence(
                        "approved-spec",
                        "approved-spec",
                        sha256(state["spec"]["body"].encode()).hexdigest(),
                        state["spec"]["body"],
                    ),
                    *(
                        Evidence(
                            item["id"], item["source"], item["revision"], item["text"]
                        )
                        for item in sources
                    ),
                ),
                alternatives=(
                    "repair_with_evidence",
                    "review_graph",
                    "keep_unpublished",
                ),
                gaps=(*original.get("gaps", []), *draft.gaps),
                conflicts=tuple(original.get("conflicts", [])),
                unexamined=tuple(original.get("unexamined", [])),
            )
        )
        context = json.loads(json.dumps(context))
        prior = state.get("tickets", {}).get("verification", {})
        threshold = state.get("workflow_policy", {}).get("confidence", 0.8)
        # The latest stored judgment of each claim, even if the last verification
        # left the claim out.
        judged = memories(lineage(prior, history_of(state)))
        evidence_revision, context_revision = digest(evidence), context_digest(context)
        after = inputs(
            {
                "claims": claims,
                "evidence_revision": evidence_revision,
                "context_revision": context_revision,
            }
        )
        results = []
        confirmations = []
        pending = []
        for claim in claims:
            past = judged.get(claim["id"])
            if (
                claim["id"] not in rejudge
                and past
                and change(past.inputs, after, claim["id"]) is None
            ):
                old, second = past.judged.result, past.judged.pair
                first = past.judged.first
                # Scoped reuse: while the claim, the evidence and the context are the
                # ones judged, the verdict stands, weak or strong. A weak claim is
                # never judged alone again; it returns with new content or through
                # the symmetric confirmation.
                # A pair the host would reject is not a confirmation to keep.
                kept = (
                    second
                    if second
                    and near(first, threshold)
                    and confirmation_rows({"results": [old], "confirmations": [second]})
                    is not None
                    else None
                )
                if kept or not near(first, threshold) or not reentry(past, threshold):
                    results.append(old)
                    if kept:
                        confirmations.append(kept)
                    continue
            pending.append(claim)

        async def evaluate(batch: list[dict[str, str]]) -> dict[str, Any] | None:
            arguments = {"claims": batch, "evidence": evidence, "context": context}
            result: dict[str, Any] = await self._call("jev_verify", **arguments)
            if overflow := PayloadTooLarge.from_result(result):
                raise overflow
            rows = result.get("verdicts", [])
            if (
                result.get("error")
                or len(rows) != len(batch)
                or {row.get("id") for row in rows} != {row["id"] for row in batch}
            ):
                return None
            # What is stored advances only if this request was confirmed.
            return confirmed_result(result, "jev_verify", arguments)

        evaluated = []
        for offset in range(0, len(pending), VERIFY_BATCH_SIZE):
            batch = pending[offset : offset + VERIFY_BATCH_SIZE]
            result = await evaluate(batch)
            if result is None:
                return None
            evaluated.append((batch, result))
        results.extend(result for _, result in evaluated)
        # Symmetric confirmation: a batch holding a claim near the threshold is
        # sent again as the identical request, so both evaluations see the same
        # input. Only the rows of the claims near the threshold are kept.
        for batch, first in evaluated:
            close = [row["id"] for row in first["verdicts"] if near(row, threshold)]
            if not close:
                continue
            result = await evaluate(batch)
            if result is None:
                return None
            rows = [row for row in result["verdicts"] if row["id"] in close]
            confirmations.append(
                {
                    **result,
                    "verdicts": rows,
                    "summary": {
                        name: sum(row["verdict"] == name for row in rows)
                        for name in result.get("summary", {})
                    },
                }
            )
        return {
            "claims": claims,
            "results": results,
            "confirmations": confirmations,
            "sources": refs,
            "evidence_revision": evidence_revision,
            "context_revision": context_revision,
        }

    @staticmethod
    def save_graph(state: dict[str, Any], project: Path) -> dict[str, Any]:
        if state.get("error") or "tickets" not in state:
            return state
        graph = state["tickets"]
        artifact = save_research(
            {
                "schema": 1,
                "run_id": state["id"],
                "run_revision": state["revision"],
                "graph": graph,
                "spec": state["spec"],
                "previews": [
                    {
                        "id": ticket["id"],
                        "title": ticket["title"],
                        "behavior": ticket["behavior"],
                        "acceptance_criteria": ticket["acceptance_criteria"],
                        "repository": ticket["repository"],
                        "blocked_by": ticket["blocked_by"],
                    }
                    for ticket in graph["draft"]["tickets"]
                ],
                "alternatives": [
                    "repair_with_evidence",
                    "review_scope",
                    "keep_unpublished",
                ],
                "research_attempts": state.get("research_attempts", []),
                "continuations": state.get("ticket_research_continuations", []),
            },
            project,
            "docs/tickets",
        )
        return {**state, "local_graph": artifact}

    @staticmethod
    def _restore_research(
        state: dict[str, Any], project: Path
    ) -> list[dict[str, str]] | None:
        """The sources the stored refs name, with their texts.

        The stored verification keeps revisions only: project files are read again
        and admitted research comes from the journal. None means a source changed
        or is gone, and the verdicts that the refs name no longer describe what
        Jev would read.
        """
        return restore_refs(
            state, project, state["tickets"]["verification"].get("sources", [])
        )

    async def _rejudge(
        self,
        run_id: str,
        state: dict[str, Any],
        claim_id: str,
        sources: list[dict[str, str]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Verify one claim again and store the graph with the new verdict.

        The host never lets a weak claim be judged again without a change; the
        authority that judges it is that change.
        """
        graph = state["tickets"]
        draft = TicketPlan.model_validate(graph["draft"])
        try:
            verification = await self._verify_plan(
                state, draft, sources, frozenset({claim_id})
            )
        except PayloadTooLarge as overflow:
            return overflow.review(state)
        if verification is None:
            return _review(state, "verification_failed")
        return await self.submit(
            run_id,
            state["revision"],
            idempotency_key=f"{idempotency_key}:rejudge",
            draft=draft,
            skill_revision=graph["skill_revision"],
            vocabulary_revision=graph["vocabulary_revision"],
            verification=verification,
        )

    async def research_claim(
        self,
        run_id: str,
        claim_id: str,
        request: ResearchRequest,
        *,
        idempotency_key: str,
        authorize_jev: bool,
        repair: Callable[[dict[str, Any]], Awaitable[TicketPlan]] | None = None,
        capture: Callable[[ResearchSource], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Reserve, collect, filter and verify one claim without repeating effects."""
        state = await self.get(run_id)
        if state.get("error"):
            return state
        if not authorize_jev:
            return _review(state, "authorization_required")
        project = self.project or Path.cwd()
        graph = state.get("tickets", {})
        pending = state.get("research_pending")
        if pending:
            if (
                pending["cycle_key"] != idempotency_key
                or pending["question_id"] != f"tickets:{claim_id}"
            ):
                return _review(state, "research_in_progress")
            if pending["phase"] != "evaluating" and not resumable(
                state, idempotency_key
            ):
                return _review(state, "external_outcome_unknown")
        elif any(
            row["cycle_key"] == idempotency_key
            for row in state.get("research_attempts", [])
        ):
            return self.save_graph(state, project)
        known: list[str] = []
        if not pending or pending["phase"] == "collecting":
            claim = next(
                (
                    row
                    for row in graph.get("verification", {}).get("claims", [])
                    if row["id"] == claim_id
                ),
                None,
            )
            if (
                claim is None
                or request.question != claim["text"]
                or request.claim != claim["text"]
            ):
                raise ValueError("A pesquisa deve corresponder à claim atual do grafo.")
            # A changed source is known now; reserving would spend a cycle on it.
            restored = self._restore_research(state, project)
            if restored is None:
                return _review(state, "research_changed")
            known = [item["text"] for item in restored]
        if not pending:
            reserved = await self._call(
                "tickets_research_begin",
                run_id=run_id,
                expected_revision=state["revision"],
                idempotency_key=idempotency_key,
                artifact_revision=graph["revision"],
                claim_id=claim_id,
            )
            if reserved.get("error", {}).get("code") == "verification_gate_mismatch":
                # Another authority judged the weak verdict: the claim is verified
                # again under this host, once, before a cycle is spent on it.
                assert restored is not None  # read above, as nothing was pending
                state = await self._rejudge(
                    run_id, state, claim_id, restored, idempotency_key
                )
                graph = state.get("tickets", {})
                if state.get("error") or claim_id not in weak(
                    graph["verification"],
                    state.get("workflow_policy", {}).get("confidence", 0.8),
                ):
                    return self.save_graph(state, project)
                reserved = await self._call(
                    "tickets_research_begin",
                    run_id=run_id,
                    expected_revision=state["revision"],
                    idempotency_key=idempotency_key,
                    artifact_revision=graph["revision"],
                    claim_id=claim_id,
                )
            if reserved.get("error"):
                return {**state, **reserved}
            if not reserved.get("reservation_acquired"):
                return _review(await self.get(run_id), "external_outcome_unknown")
            state = reserved
        if state["research_pending"]["phase"] == "collecting":
            filtered, artifact = await self._collect_cycle(
                request,
                [
                    row["collected"]
                    for row in counted(state, f"tickets:{claim_id}")
                    if row.get("collected")
                ],
                authorize_jev=True,
                capture=capture,
                known=known,
            )
            state = await self._record_cycle(
                "tickets_research_record",
                filtered,
                artifact,
                run_id=run_id,
                expected_revision=state["revision"],
                idempotency_key=idempotency_key,
            )
            if state.get("error") or not state.get("research_pending"):
                return self.save_graph(state, project)
        graph = state["tickets"]
        additions = state["ticket_research"]["evidence"]
        earlier = self._restore_research(state, project)
        if earlier is None:
            return _review(state, "research_changed")
        draft = TicketPlan.model_validate(graph["draft"])
        if repair:
            # The texts are read back for the harness only; the stored graph keeps refs.
            cited = {
                **graph,
                "verification": {**graph["verification"], "sources": earlier},
            }
            draft = await repair(
                {
                    "graph": cited,
                    "claim_id": claim_id,
                    "research": additions,
                    "directive": "Corrija as lacunas demonstradas pela evidência; preserve obrigações, repos donos e dependências reais.",
                }
            )
        sources = [
            *earlier,
            *[
                {key: row[key] for key in ("id", "source", "revision", "text")}
                for row in additions
            ],
        ]
        try:
            verification = await self._verify_plan(state, draft, sources)
        except PayloadTooLarge as overflow:
            return overflow.review(state)
        if verification is None:
            return _review(state, "verification_failed")
        submitted = await self.submit(
            run_id,
            state["revision"],
            idempotency_key=f"{idempotency_key}:evaluation",
            draft=draft,
            skill_revision=graph["skill_revision"],
            vocabulary_revision=graph["vocabulary_revision"],
            verification=verification,
        )
        return self.save_graph(submitted, project)

    async def repair_claim(
        self,
        run_id: str,
        claim_id: str,
        *,
        repair: Callable[[dict[str, Any]], Awaitable[TicketPlan]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Repair one claim on the evidence its adjudication judged, then verify it."""
        state = await self.get(run_id)
        if state.get("error"):
            return state
        project = self.project or Path.cwd()
        graph = state.get("tickets", {})
        entry = applicable_adjudication(
            state, f"tickets:{claim_id}", graph.get("revision")
        )
        if entry is None:
            return _review(state, "adjudication_unavailable")
        earlier = self._restore_research(state, project)
        if earlier is None:
            return _review(state, "research_changed")
        judged = {ref["revision"] for ref in entry["evidence_refs"]}
        # The texts are read back for the harness only; the stored graph keeps refs.
        draft = await repair(
            {
                "graph": {
                    **graph,
                    "verification": {**graph["verification"], "sources": earlier},
                },
                "claim_id": claim_id,
                "research": [item for item in earlier if item["revision"] in judged],
                "adjudication": entry,
                "directive": "Corrija apenas a lacuna julgada, com as evidências que a adjudicação examinou; preserve obrigações, repos donos e dependências reais.",
            }
        )
        try:
            verification = await self._verify_plan(state, draft, earlier)
        except PayloadTooLarge as overflow:
            return overflow.review(state)
        if verification is None:
            return _review(state, "verification_failed")
        submitted = await self.submit(
            run_id,
            state["revision"],
            idempotency_key=idempotency_key,
            draft=draft,
            skill_revision=graph["skill_revision"],
            vocabulary_revision=graph["vocabulary_revision"],
            verification=verification,
        )
        return self.save_graph(submitted, project)

    async def research_tickets(
        self,
        run_id: str,
        *,
        plan: Callable[[dict[str, Any], str], Awaitable[ResearchRequest]],
        idempotency_key: str,
        authorize_jev: bool,
        repair: Callable[[dict[str, Any]], Awaitable[TicketPlan]] | None = None,
        capture: Callable[[ResearchSource], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        from specgate.delegation import load_delegation

        state = await self.get(run_id)
        while not state.get("error"):
            weak_ids = weak(
                state.get("tickets", {}).get("verification", {}),
                state.get("workflow_policy", {}).get("confidence", 0.8),
            )
            if not weak_ids:
                return self.save_graph(state, self.project or Path.cwd())
            pending = state.get("research_pending")
            if pending:
                claim_id, reason = pending["question_id"].removeprefix("tickets:"), None
            else:
                claim_id, reason = next_cycle(state, weak_ids)
            if reason:
                state = _review(state, reason)
                break
            key = (
                pending["cycle_key"]
                if pending
                else f"{idempotency_key}:{len(state.get('research_attempts', []))}"
            )
            request = await plan(state, claim_id)
            state = await self.research_claim(
                run_id,
                claim_id,
                request,
                idempotency_key=key,
                authorize_jev=authorize_jev,
                repair=repair,
                capture=capture,
            )
            # The error of this cycle is the result of the research; another cycle
            # here would only draw another score.
            operational = state.get("research_stopped", {}).get(f"tickets:{claim_id}")
            if operational in OPERATIONAL:
                state = _review(state, operational)
        project = self.project or Path.cwd()
        grant = load_delegation(self.home)
        handoff = {
            "schema": 1,
            "run_id": run_id,
            "graph": state.get("tickets"),
            "continuations": state.get("ticket_research_continuations", []),
            "reason": state.get("error", {}).get("code"),
            "research_attempts": state.get("research_attempts", []),
            "route": (
                "bounded_adjudication"
                if state.get("error", {}).get("code") in ENDED
                else "delivery_exception"
            )
            if grant.active(project, self.url)
            else "human_review",
            "delegation_revision": grant.revision
            if grant.active(project, self.url)
            else None,
            "auto_advance": False,
        }
        artifact = save_research(handoff, project, "docs/tickets/handoffs")
        return {**state, "research_handoff": {**handoff, "artifact": artifact}}

    @staticmethod
    def _matches(state: dict[str, Any], ticket_id: str, issue: IssueReference) -> bool:
        graph = state["tickets"]
        ticket = next(
            item for item in graph["draft"]["tickets"] if item["id"] == ticket_id
        )
        return (
            issue.repository.casefold()
            == (ticket.get("repository") or graph["parent"]["repository"]).casefold()
            and issue.title == ticket["title"]
            and issue.body == ticket_body(state, ticket_id)
        )

    @staticmethod
    def _tracker(
        repository: str,
        trackers: TicketTracker | Mapping[str, TicketTracker],
    ) -> TicketTracker:
        if not isinstance(trackers, Mapping):
            if trackers.repository.casefold() != repository.casefold():
                raise ValueError("The tracker does not match the ticket repository.")
            return trackers
        matches = [
            tracker
            for name, tracker in trackers.items()
            if name.casefold() == repository.casefold()
            and tracker.repository.casefold() == repository.casefold()
        ]
        if len(matches) != 1:
            raise ValueError("Exactly one tracker is required per repository.")
        return matches[0]

    def _bind(
        self,
        repository: str,
        trackers: TicketTracker | Mapping[str, TicketTracker],
        destination: str,
        project: Path | None,
    ) -> TicketTracker:
        try:
            selected: TicketTracker | None = self._tracker(repository, trackers)
        except ValueError:
            selected = None
        if selected is not None and getattr(selected, "kind", "github") == destination:
            return selected
        if destination in {"local", "freeform"} and project is not None:
            return ProjectTracker(project, destination, repository)
        if selected is None:
            raise DestinationError("repository_mismatch")
        raise DestinationError("destination_adapter_missing")

    async def _record_review(
        self, state: dict[str, Any], ticket_id: str, issue: IssueReference, key: str
    ) -> dict[str, Any]:
        entry = state["tickets"]["publications"][ticket_id]
        if entry["status"] == "review":
            return _review(state, "tracker_content_changed")
        recorded = await self._call(
            "tickets_publication_record",
            run_id=state["id"],
            expected_revision=state["revision"],
            idempotency_key=sha256(f"{key}:review:{entry['id']}:{state['revision']}".encode()).hexdigest(),
            ticket_id=ticket_id,
            intent_id=entry["id"],
            issue=entry["issue"] or issue.model_dump(),
            review_required=True,
        )
        return recorded if "error" in recorded else _review(recorded, "tracker_content_changed")

    async def publish(
        self,
        run_id: str,
        tracker: TicketTracker | Mapping[str, TicketTracker],
        *,
        idempotency_key: str,
        project: Path | None = None,
        destination: str | None = None,
    ) -> dict[str, Any]:
        state = await self.get(run_id)
        if "error" in state:
            return state
        try:
            chosen = resolve_destination(
                project, destination, policy=state.get("workflow_policy")
            )
        except DestinationError as error:
            return _review(state, error.code)
        capabilities = await self._call("tracker_capabilities", destination=chosen)
        if "error" in capabilities:
            return _review(state, capabilities["error"]["code"])
        if (
            capabilities.get("reference_contract") != 2
            or capabilities.get("destination") != chosen
            or capabilities.get("publish") is not True
            or capabilities.get("reconcile") is not True
            or capabilities.get("ticket_graph") is not True
        ):
            return _review(state, "destination_adapter_missing")
        graph = state.get("tickets")
        if not graph or graph["status"] not in {"ready", "publishing", "published", "publication_review"}:
            return _review(state, "tickets_review_required")
        # The raw gaps stay on record; the ones the maintainer accepted do not block.
        if remaining(
            graph.get("verification_gaps", ["verification_missing"]),
            graph.get("residual_acceptance"),
        ):
            return _review(state, "tickets_review_required")
        from specgate.delegation import review_authority_valid

        def authorized(repository: str) -> bool:
            assert graph is not None
            return review_authority_valid(
                graph.get("authorization", {}),
                host=self.url,
                repository=repository,
                operation="tickets",
                home=self.home,
            )

        if not all(authorized(t["repository"]) for t in graph["draft"]["tickets"]):
            return _review(state, "publication_authorization_changed")
        try:
            require_native_relations(chosen, graph["relations"])
        except GrillError as error:
            return _review(state, error.code)
        if chosen == "github" and any(
            graph["relations"][name] and capabilities.get(name) is not True
            for name in ("native_sub_issues", "native_dependencies")
        ):
            return _review(state, "destination_capabilities_missing")
        try:
            bound = {
                ticket["id"]: self._bind(
                    ticket.get("repository") or graph["parent"]["repository"],
                    tracker,
                    chosen,
                    project,
                )
                for ticket in graph["draft"]["tickets"]
            }
            parent_tracker = (
                self._bind(graph["parent"]["repository"], tracker, chosen, project)
                if chosen == "github" and graph["relations"]["native_sub_issues"]
                else None
            )
            for key, adapter in bound.items():
                if isinstance(
                    adapter, (GitHubTracker, JiraTicketTracker, BeadsTicketTracker)
                ):
                    bound[key] = adapter.with_authority(
                        partial(authorized, adapter.repository)
                    )
            if isinstance(parent_tracker, GitHubTracker):
                parent_repository = parent_tracker.repository
                parent_tracker = parent_tracker.with_authority(
                    lambda: authorized(parent_repository)
                )
            if chosen == "jira":
                parent = IssueReference.model_validate(state["publication"]["issue"])
                for adapter in bound.values():
                    if not isinstance(adapter, JiraTicketTracker):
                        return _review(state, "destination_capabilities_missing")
                    adapter.validate_parent(parent)
                    await adapter.capabilities()
                    if any(
                        entry.get("adapter_scope") != adapter.publication_scope
                        for entry in graph["publications"].values()
                    ):
                        return _review(state, "tracker_scope_changed")
            if chosen == "beads":
                parent = IssueReference.model_validate(state["publication"]["issue"])
                for adapter in bound.values():
                    if not isinstance(adapter, BeadsTicketTracker):
                        return _review(state, "destination_capabilities_missing")
                    adapter.validate_parent(parent)
                    if (
                        adapter.spec.identity_key != parent.identity_key
                        or adapter.instance != state["publication"].get("tracker_instance")
                    ):
                        return _review(state, "tracker_scope_changed")
                    await adapter.capabilities()
                    if any(
                        {
                            key: value
                            for key, value in entry.get("adapter_scope", {}).items()
                            if key != "external_id"
                        }
                        != adapter.base_scope
                        for entry in graph["publications"].values()
                    ):
                        return _review(state, "tracker_scope_changed")
        except DestinationError as error:
            return _review(state, error.code)
        except TrackerError:
            return _review(state, "destination_capabilities_missing")
        recorded = graph.get("destination") or "github"
        if recorded != chosen:
            state = await self._call(
                "tickets_destination_change",
                run_id=run_id,
                expected_revision=state["revision"],
                idempotency_key=sha256(
                    f"{idempotency_key}:destination:{chosen}".encode()
                ).hexdigest(),
                destination=chosen,
            )
            if "error" in state:
                return state
            graph = state["tickets"]
        try:
            for ticket_id in graph["order"]:
                state = await self.get(run_id)
                graph = state["tickets"]
                ticket = next(
                    item
                    for item in graph["draft"]["tickets"]
                    if item["id"] == ticket_id
                )
                ticket_tracker = bound[ticket_id]
                repository = ticket_tracker.repository
                entry = graph["publications"].get(ticket_id)
                published = bool(entry and entry["status"] == "published")
                read_only = bool(entry and entry["status"] in {"published", "review"})
                key = sha256(
                    f"{idempotency_key}:begin:{ticket_id}".encode()
                ).hexdigest()
                if not entry:
                    if not authorized(repository):
                        return _review(state, "publication_authorization_changed")
                    adapter_scope = (
                        ticket_tracker.publication_scope
                        if isinstance(ticket_tracker, JiraTicketTracker)
                        else ticket_tracker.publication_scope(ticket_body(state, ticket_id))
                        if isinstance(ticket_tracker, BeadsTicketTracker)
                        else None
                    )
                    state = await self._call(
                        "tickets_publication_begin",
                        run_id=run_id,
                        expected_revision=state["revision"],
                        idempotency_key=key,
                        ticket_id=ticket_id,
                        **({"adapter_scope": adapter_scope} if adapter_scope else {}),
                    )
                    if "error" in state:
                        return state
                    graph = state["tickets"]
                    entry = graph["publications"][ticket_id]
                recorded_issue = (
                    IssueReference.model_validate(entry["issue"])
                    if entry.get("issue")
                    else None
                )
                previous_issue = (
                    IssueReference.model_validate(entry["previous_issue"])
                    if entry.get("previous_issue") else None
                )
                issue: IssueReference | None
                if recorded_issue is not None:
                    issue = await ticket_tracker.get(recorded_issue.lookup_id)
                else:
                    marker = ticket_marker(state, ticket_id)
                    matches = await ticket_tracker.find(marker)
                    if len(matches) > 1:
                        return _review(state, "ambiguous_publication")
                    issue = matches[0] if matches else None
                    if issue is None and state.get("publication_claimed"):
                        if not authorized(repository):
                            return _review(state, "publication_authorization_changed")
                        if previous_issue is not None:
                            update = getattr(ticket_tracker, "update", None)
                            if update is None:
                                return _review(state, "destination_capabilities_missing")
                            issue = await update(
                                previous_issue, ticket["title"], ticket_body(state, ticket_id)
                            )
                        else:
                            issue = await ticket_tracker.create(
                                ticket["title"], ticket_body(state, ticket_id)
                            )
                    if issue is None:
                        return _review(state, "publication_unknown")
                    issue = await ticket_tracker.get(issue.lookup_id)
                if not self._matches(state, ticket_id, issue) or (
                    previous_issue is not None
                    and (previous_issue.identity_key != issue.identity_key
                         or previous_issue.id != issue.id)
                ) or (
                    recorded_issue is not None
                    and (
                        recorded_issue.identity_key != issue.identity_key
                        or (chosen == "github" and recorded_issue.id != issue.id)
                    )
                ):
                    return await self._record_review(state, ticket_id, issue, idempotency_key)
                if "ready-for-agent" not in issue.labels:
                    if read_only:
                        return await self._record_review(state, ticket_id, issue, idempotency_key)
                    if not authorized(repository):
                        return _review(state, "publication_authorization_changed")
                    issue = await ticket_tracker.ensure_label(issue.lookup_id)
                if (
                    not self._matches(state, ticket_id, issue)
                    or "ready-for-agent" not in issue.labels
                ):
                    return _review(state, "publication_unverified")

                parent_linked = False
                if parent_tracker is not None:
                    children = await parent_tracker.list_sub_issues(
                        graph["parent"]["number"]
                    )
                    if issue.id not in {child.id for child in children}:
                        if read_only:
                            return await self._record_review(state, ticket_id, issue, idempotency_key)
                        if not authorized(parent_tracker.repository):
                            return _review(state, "publication_authorization_changed")
                        await parent_tracker.add_sub_issue(
                            graph["parent"]["number"], issue.legacy_id
                        )
                        children = await parent_tracker.list_sub_issues(
                            graph["parent"]["number"]
                        )
                    parent_linked = issue.id in {child.id for child in children}

                ticket = next(
                    item
                    for item in graph["draft"]["tickets"]
                    if item["id"] == ticket_id
                )
                blockers_linked: list[str] = []
                if chosen == "github" and graph["relations"]["native_dependencies"]:
                    blockers = await ticket_tracker.list_blockers(issue.legacy_number)
                    blocker_ids = {blocker.id for blocker in blockers}
                    expected_blockers = {
                        graph["publications"][key]["issue"]["id"]
                        for key in ticket["blocked_by"]
                    }
                    removed = blocker_ids - expected_blockers
                    if removed and not read_only and previous_issue is not None:
                        remove = getattr(ticket_tracker, "remove_blocker", None)
                        if remove is None or not removed <= set(entry.get("previous_blockers", [])):
                            return await self._record_review(state, ticket_id, issue, idempotency_key)
                        for blocker_id in removed:
                            if not authorized(repository):
                                return _review(state, "publication_authorization_changed")
                            await remove(issue.legacy_number, blocker_id)
                        blockers = await ticket_tracker.list_blockers(issue.legacy_number)
                        blocker_ids = {blocker.id for blocker in blockers}
                    if blocker_ids - expected_blockers or (read_only and blocker_ids != expected_blockers):
                        return await self._record_review(state, ticket_id, issue, idempotency_key)
                    for blocker_key in ticket["blocked_by"]:
                        blocker = graph["publications"][blocker_key]["issue"]
                        if blocker["id"] not in blocker_ids:
                            if not authorized(repository):
                                return _review(
                                    state, "publication_authorization_changed"
                                )
                            await ticket_tracker.add_blocker(
                                issue.legacy_number, blocker["id"]
                            )
                    if not read_only:
                        blockers = await ticket_tracker.list_blockers(issue.legacy_number)
                        blocker_ids = {blocker.id for blocker in blockers}
                    if blocker_ids - expected_blockers:
                        return await self._record_review(state, ticket_id, issue, idempotency_key)
                    blockers_linked = [
                        blocker_key
                        for blocker_key in ticket["blocked_by"]
                        if graph["publications"][blocker_key]["issue"]["id"]
                        in blocker_ids
                    ]

                if isinstance(ticket_tracker, (JiraTicketTracker, BeadsTicketTracker)):
                    related = await ticket_tracker.reconcile_relations(
                        issue, IssueReference.model_validate(state["publication"]["issue"]),
                        [IssueReference.model_validate(graph["publications"][key]["issue"]) for key in ticket["blocked_by"]],
                        read_only=read_only, previous_blockers=entry.get("previous_blockers", []),
                    )
                    if not related:
                        return await self._record_review(state, ticket_id, issue, idempotency_key)
                    parent_linked = True
                    blockers_linked = ticket["blocked_by"]

                current = await ticket_tracker.get(issue.lookup_id)
                if (
                    not self._matches(state, ticket_id, current)
                    or "ready-for-agent" not in current.labels
                    or current.identity_key != issue.identity_key
                    or (chosen == "github" and current.id != issue.id)
                ):
                    return await self._record_review(state, ticket_id, issue, idempotency_key)
                issue = current
                if published:
                    continue

                review_suffix = (
                    f":review:{entry['review_history'][-1]['revision']}"
                    if entry.get("review_history") else ""
                )
                record_key = sha256(
                    f"{idempotency_key}:record:{entry['id']}{review_suffix}".encode()
                ).hexdigest()
                state = await self._call(
                    "tickets_publication_record",
                    run_id=run_id,
                    expected_revision=state["revision"],
                    idempotency_key=record_key,
                    ticket_id=ticket_id,
                    intent_id=entry["id"],
                    issue=issue.model_dump(),
                    parent_linked=parent_linked,
                    blockers_linked=blockers_linked,
                )
                if "error" in state:
                    return state
            output_project = project or self.project
            return self.save_graph(state, output_project) if output_project else state
        except (TrackerError, OSError, ValueError):
            return _review(state, "publication_unknown")

    async def frontier(
        self,
        run_id: str,
        tracker: TicketTracker | Mapping[str, TicketTracker],
    ) -> dict[str, Any]:
        state = await self.get(run_id)
        if "error" in state:
            return state
        graph = state.get("tickets")
        if not graph or graph["status"] not in {"published", "publication_review"}:
            return _review(state, "tickets_unpublished")
        try:
            observations = []
            for ticket_id in graph["order"]:
                issue = graph["publications"].get(ticket_id, {}).get("issue")
                if issue is None:
                    continue
                ticket_tracker = self._tracker(issue["repository"], tracker)
                if issue.get("tracker") == "jira" and (
                    not isinstance(ticket_tracker, JiraTicketTracker)
                    or ticket_tracker.publication_scope != graph["publications"][ticket_id].get("adapter_scope")
                ):
                    return _review(state, "tracker_scope_changed")
                if issue.get("tracker") == "beads" and (
                    not isinstance(ticket_tracker, BeadsTicketTracker)
                    or ticket_tracker.publication_scope(issue["body"]) != graph["publications"][ticket_id].get("adapter_scope")
                ):
                    return _review(state, "tracker_scope_changed")
                current = await ticket_tracker.issue_state(
                    IssueReference.model_validate(issue).lookup_id
                )
                observations.append(
                    TicketObservation(
                        ticket_id=ticket_id,
                        issue_id=current.id,
                        number=current.number,
                        external_id=current.external_id,
                        state=current.state,
                        state_reason=current.state_reason,
                        actionable=current.actionable,
                    )
                )
            return await self._call(
                "tickets_frontier",
                run_id=run_id,
                expected_revision=state["revision"],
                observations=[item.model_dump(exclude={"actionable"} if item.actionable else set()) for item in observations],
            )
        except (TrackerError, OSError, ValueError):
            return _review(state, "tracker_unavailable")
