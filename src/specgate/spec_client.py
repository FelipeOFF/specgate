"""Harness synthesis through the installed skill, then reconciled tracker effects."""

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from specgate import lineage_budget
from specgate.beads_tracker import BeadsSpecTracker
from specgate.claim_equivalence import check_equivalence
from specgate.claim_wording import check_derivation, check_wording
from specgate.client import MIXED, confirmed_result, gate_authority
from specgate.context import (
    ContextPacket,
    SkillSource,
    build_context,
    read_skill,
)
from specgate.decision_lineage import Group
from specgate.delegation import (
    review_authority_valid,
    rewrite_authority,
    rewrite_authority_valid,
)
from specgate.destination import resolve_destination, valid_project_reference
from specgate.grill_client import GrillClient
from specgate.jira_tracker import JiraSpecTracker
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
from specgate.research_restore import restore_research
from specgate.residual import AGENT_ACTION, handoff_route
from specgate.shared.domain.inputs import Item
from specgate.spec_claims import keeps_researched_decision
from specgate.spec_contracts import (
    IssueReference,
    SpecAuthorization,
    SpecDraft,
    publication_body,
    source_revision,
    valid_reference,
)
from specgate.spec_residual import next_cycle, remaining
from specgate.spec_rewrite import (
    APPROVED,
    IMPLEMENTATION_PUBLISHING,
    PUBLISHING_MESSAGE,
    REWRITE_NOT_APPROVED,
    REWRITE_NOT_AUTHORIZED,
    SpecRewrite,
    check_scope,
    publication_claimed,
    replaced_approved,
    rewrite_groups,
)
from specgate.tracker import GitHubTracker, Tracker, TrackerError
from specgate.verification_refs import context_digest, digest, source_refs
from specgate.wording import AGENT_CODES, AgentRefusal

# The coverage evidence holds the rendered spec; the context only points to it.
SPEC_ARTIFACT = "Spec renderizada, no campo evidence do pedido."


def _once(rules: Iterable[str], evidence: Iterable[Mapping[str, Any]]) -> list[str]:
    """Point each rule that an evidence item already holds to that item.

    `build_context` files CONTEXT.md, AGENTS.md and README.md under the rules and
    under the evidence, so sending both would carry the vocabulary twice. The
    evidence keeps the text, the copy Jev reads as evidence (#148); the rule
    becomes a short pointer and the rules stay ready for a decision.
    """
    held: dict[str, str] = {}
    for item in evidence:
        held.setdefault(item["text"], item["id"])
    return [
        f"Regras do projeto: texto integral no item {held[rule]} da evidência do contexto."
        if rule in held
        else rule
        for rule in rules
    ]


def _domain(project: Path, objective: str, research: list[str]) -> ContextPacket:
    """The vocabulary and the research sources, read once from the project."""
    paths = ["CONTEXT.md", *research]
    return build_context(
        project,
        objective,
        paths,
        paths,
        artifact="Síntese da spec",
        alternatives=["revisar", "publicar"],
    )


def _review(
    state: dict[str, Any],
    code: str,
    message: str = "Confira a revisão e reconcilie o tracker antes de continuar.",
) -> dict[str, Any]:
    return {
        **state,
        "action": "needs_human",
        "auto_advance": False,
        "error": {"code": code, "message": message},
    }


def _not_authorized(state: dict[str, Any]) -> dict[str, Any]:
    """The refusal of a rewrite the grant does not cover: nothing was written."""
    return _review(
        state,
        REWRITE_NOT_AUTHORIZED,
        "A delegação vigente não inclui a operação de reescrita de decisões, venceu ou "
        "foi revogada; a pessoa a concede por specgate setup --allow-rewrite.",
    )


class SpecClient(GrillClient):
    async def get(self, run_id: str) -> dict[str, Any]:
        return await self._call("spec_get", run_id=run_id)

    async def submit(
        self,
        run_id: str,
        expected_revision: int,
        *,
        idempotency_key: str,
        draft: SpecDraft,
        skill_revision: str,
        vocabulary_revision: str | None = None,
        authorization: SpecAuthorization | None = None,
        research_refs: list[dict[str, str]] | None = None,
        verification: dict[str, Any] | None = None,
        rewrite: SpecRewrite | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            "spec_submit",
            run_id=run_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            draft=draft.model_dump(),
            skill_revision=skill_revision,
            vocabulary_revision=vocabulary_revision,
            authorization=authorization.model_dump() if authorization else None,
            research_refs=research_refs or [],
            verification=verification or {},
            # Sent only by a rewrite, so every other request keeps the shape it had.
            **({"rewrite": rewrite.model_dump()} if rewrite else {}),
        )

    async def review(
        self,
        run_id: str,
        expected_revision: int,
        *,
        idempotency_key: str,
        artifact_revision: str,
        authorization: SpecAuthorization,
    ) -> dict[str, Any]:
        state = await self._call(
            "spec_review",
            run_id=run_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            artifact_revision=artifact_revision,
            authorization=authorization.model_dump(),
        )
        return self.save_spec(state, self.project) if self.project else state

    async def review_delegated(
        self, run_id: str, *, project: Path, repository: str, idempotency_key: str
    ) -> dict[str, Any]:
        from specgate.delegation import (
            publication_authority,
            verification_allows_review,
        )

        state = await self.get(run_id)
        if "error" in state:
            return state
        artifact = state.get("spec", {})
        verification = artifact.get("verification", {})
        results = [verification.get(name, {}) for name in ("fidelity", "coverage")]
        stored = verification.get("context_revisions", {})
        authority = publication_authority(
            project=project,
            host=self.url,
            repositories=[repository],
            operation="spec",
            home=self.home,
        )
        if (
            not authority
            or artifact.get("status") not in {"review", "ready", "published"}
            # Decisions of an approved revision were replaced outside the rewrite operation.
            or replaced_approved(state)
            or artifact.get("gaps")
            or not verification_allows_review(
                results,
                state.get("workflow_policy", {}).get("confidence", 0.8),
                contexts=[stored.get("fidelity"), stored.get("coverage")],
            )
        ):
            return _review(state, "delegated_review_unavailable")
        return await self.review(
            run_id,
            state["revision"],
            idempotency_key=idempotency_key,
            artifact_revision=artifact["revision"],
            authorization=SpecAuthorization(
                seams_approved=True, publication_authorized=True, **authority
            ),
        )

    async def reject(
        self,
        run_id: str,
        expected_revision: int,
        *,
        idempotency_key: str,
        artifact_revision: str,
        reason: str,
    ) -> dict[str, Any]:
        return await self._call(
            "spec_reject",
            run_id=run_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            artifact_revision=artifact_revision,
            reason=reason,
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
        harness: Callable[[dict[str, Any]], Awaitable[SpecDraft]],
        research_sources: list[str] | None = None,
        authorization: SpecAuthorization | None = None,
        rewrite: bool = False,
    ) -> dict[str, Any]:
        """Synthesize the spec; with `rewrite`, rewrite decisions of the approved one.

        A rewrite needs the standing grant of the operation, read before the harness is
        asked for anything and checked again right before the revision is written.
        """
        state = await self.get(run_id)
        if "error" in state:
            return state
        stages = {"grill_complete", "spec_review", "spec_ready", "spec_published"}
        if rewrite:
            # The tickets of the approved spec may already exist: they go stale with the rewrite.
            stages = {
                "spec_ready",
                "spec_published",
                "tickets_review",
                "tickets_ready",
                "tickets_published",
            }
        if state["revision"] != expected_revision or state["stage"] not in stages:
            return _review(state, REWRITE_NOT_APPROVED if rewrite else "grill_incomplete")
        authority: dict[str, Any] | None = None
        if rewrite:
            status = (state.get("spec") or {}).get("status")
            if status not in APPROVED:
                return _review(state, REWRITE_NOT_APPROVED)
            # A published spec names the repository whose scope the grant must cover.
            repository = (state.get("publication") or {}).get("repository")
            authority = rewrite_authority(
                project=project,
                host=self.url,
                repositories=[repository] if status == "published" and repository else [],
                home=self.home,
            )
            if authority is None:
                return _not_authorized(state)
            # A worker that claimed its draft pull request opens it without reading the run.
            if publication_claimed(state):
                return _review(state, IMPLEMENTATION_PUBLISHING, PUBLISHING_MESSAGE)
        source = SkillSource(
            "skills/to-spec",
            "",
            str(skill_path.resolve()),
            str(skill_path.absolute()),
            "",
            (),
        )
        instructions = read_skill(source, authorized_roots)
        source = replace(source, revision=sha256(instructions.encode()).hexdigest())
        if (
            "<spec-template>" not in instructions
            or "</spec-template>" not in instructions
        ):
            raise ValueError("A skill instalada não contém o template de to-spec.")

        def research() -> ContextPacket:
            return _domain(project, state["objective"], research_sources or [])

        domain = research()
        existing_content = {item["revision"] for item in state["context"]["evidence"]}
        if (
            not research_sources
            or domain.gaps
            or not any(
                item.revision not in existing_content for item in domain.evidence[1:]
            )
        ):
            return _review(state, "research_insufficient")
        auth = (authorization or SpecAuthorization()).model_copy(deep=True)
        summary = await self._call("grill_summary", run_id=run_id)
        draft = await harness(
            {
                "instructions": instructions,
                "vocabulary": domain.evidence[0].text,
                "research": [asdict(item) for item in domain.evidence[1:]],
                "research_revision": domain.revision,
                "grill": summary,
                "authorization": auth.model_dump(),
                "directive": "Sintetize com to-spec, sem inventar decisões pendentes. Reutilize aprovações explícitas de seams e publicação; não repita perguntas já respondidas.",
            }
        )
        try:
            read_skill(source, authorized_roots)
        except (OSError, ValueError):
            return _review(state, "skill_changed")
        if research().revision != domain.revision:
            return _review(state, "research_changed")
        divisions: list[Group] | None = None
        if authority is not None:
            # A rewrite the host would refuse for what it changes spends nothing on the Jev.
            # Its lineage is read against the approved revision alone, as the host reads it.
            try:
                divisions = rewrite_groups(state["spec"], draft.model_dump())
                check_scope(state["spec"], draft.model_dump(), divisions)
            except AgentRefusal as rejected:
                return {**state, **rejected.refusal()}
        try:
            verification = await self._verify_draft(
                state, draft, summary, asdict(domain), divisions=divisions
            )
        except PayloadTooLarge as overflow:
            return overflow.review(state)
        except AgentRefusal as rejected:
            return {**state, **rejected.refusal()}
        if verification is None:
            return _review(state, "verification_failed")
        if research().revision != domain.revision:
            return _review(state, "research_changed")
        try:
            read_skill(source, authorized_roots)
        except (OSError, ValueError):
            return _review(state, "skill_changed")
        # The harness and the Jev took their time: the grant is read once more, right before
        # the call that writes. A grant that expired, was revoked or lost the operation
        # writes no revision.
        if authority is not None and not rewrite_authority_valid(
            authority, host=self.url, home=self.home
        ):
            return _not_authorized(state)
        submitted = await self.submit(
            run_id,
            expected_revision,
            idempotency_key=idempotency_key,
            draft=draft,
            skill_revision=source.revision,
            vocabulary_revision=domain.revision,
            verification=verification,
            research_refs=[
                {key: item[key] for key in ("id", "source", "revision")}
                for item in verification["sources"]
            ],
            rewrite=SpecRewrite.model_validate(authority) if authority else None,
        )
        return self.save_spec(submitted, project)

    async def rewrite_decisions(
        self,
        run_id: str,
        expected_revision: int,
        *,
        idempotency_key: str,
        project: Path,
        skill_path: Path,
        authorized_roots: list[Path],
        harness: Callable[[dict[str, Any]], Awaitable[SpecDraft]],
        research_sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """Rewrite or divide decisions of the approved spec under the grant of the operation."""
        return await self.synthesize(
            run_id,
            expected_revision,
            idempotency_key=idempotency_key,
            project=project,
            skill_path=skill_path,
            authorized_roots=authorized_roots,
            harness=harness,
            research_sources=research_sources,
            rewrite=True,
        )

    @staticmethod
    def _fidelity_evidence(
        state: dict[str, Any], decisions: list[dict[str, Any]], context: dict[str, Any]
    ) -> str:
        """Objective, scope, rules and decision texts; research stays in the context.

        The scope is left out when it is the objective, the case of the flow.
        """
        carried = {*context["rules"], *(item["text"] for item in context["evidence"])}
        rows = []
        for turn in decisions:
            answer = turn["answer"]
            row = {
                "id": turn["id"],
                "question": turn["question"]["question"],
                "option": next(
                    (
                        option["text"]
                        for option in turn["question"]["options"]
                        if option["id"] == answer.get("selected_option")
                    ),
                    None,
                ),
                "answer": answer.get("text"),
            }
            rows.append({key: value for key, value in row.items() if value})
        scope = state["context"]["artifact"]
        return json.dumps(
            {
                "objective": state["objective"],
                # The flow opens its grill with the objective as the scope: the same
                # text under a second key is dropped.
                **({"scope": scope} if scope != state["objective"] else {}),
                "rules": [
                    rule for rule in state["context"]["rules"] if rule not in carried
                ],
                "decisions": rows,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _restore_research(
        state: dict[str, Any], project: Path
    ) -> tuple[dict[str, Any], list[dict[str, str]]] | None:
        """The vocabulary packet and the admitted sources the stored refs name."""
        spec = state["spec"]
        restored = restore_research(
            state,
            project,
            spec["verification"].get("sources", []),
            spec.get("vocabulary_revision"),
            lambda paths: _domain(project, state["objective"], paths),
        )
        return None if restored is None else (asdict(restored[0]), restored[1])

    async def _verify_draft(
        self,
        state: dict[str, Any],
        draft: SpecDraft,
        summary: dict[str, Any],
        context: dict[str, Any],
        added: list[dict[str, Any]] | None = None,
        divisions: list[Group] | None = None,
    ) -> dict[str, Any] | None:
        """Verify with every text once per request; store refs and hashes.

        The vocabulary and each research text travel once, as context evidence; the
        rules only point to the vocabulary item. The rendered spec travels once, as
        the coverage evidence; the coverage artifact only points to it.

        `context` holds the vocabulary and the project sources. `added` holds the
        sources a research cycle admitted: only the fidelity context carries them.
        `divisions` are the groups the equivalence check covers; a rewrite hands the ones
        it resolved against the approved revision, and every other request reads them from
        the revisions stored before the draft.
        """
        from specgate.spec_claims import (
            derived_targets,
            earlier_specs,
            expected_claims,
            spec_groups,
            strong,
            wording_targets,
        )

        groups = expected_claims(
            draft.model_dump(), state["objective"], summary["decisions"]
        )
        try:
            factual = [Item.model_validate(row) for row in groups["fidelity"]]
            coverage = [Item.model_validate(row) for row in groups["coverage"]]
        except ValidationError:
            # jev_verify rejects oversized claims; truncating one would change what Jev judges.
            return None
        added = added or []
        rendered = draft.render()
        held = [*context["evidence"], *added]
        contexts = {
            "fidelity": {
                **context,
                "rules": _once(context["rules"], held),
                "evidence": held,
            },
            "coverage": {
                **context,
                "rules": _once(context["rules"], context["evidence"]),
                "artifact": SPEC_ARTIFACT,
            },
        }
        evidence = {
            "fidelity": self._fidelity_evidence(state, summary["decisions"], context),
            "coverage": rendered,
        }
        stored = state.get("spec") or {}
        prior = stored.get("verification", {})
        context_revisions = {kind: context_digest(value) for kind, value in contexts.items()}
        evidence_revisions = {kind: digest(value) for kind, value in evidence.items()}
        # The content of an evaluation that already came out weak is refused before any call, as
        # the host refuses it: judging it again would only draw another score. A person who acted
        # since the counter last took a revision in starts the count over, and what it had
        # rejected with it.
        content = lineage_budget.content_digest(
            draft.model_dump(), evidence_revisions, context_revisions
        )
        acted = (
            bool(stored)
            and lineage_budget.approved_by_person(stored.get("authorization"), "seams_approved")
        ) or lineage_budget.answered_since(state, lineage_budget.SPEC)
        if not acted and lineage_budget.rejected(state, lineage_budget.SPEC, content):
            raise lineage_budget.refuse_identical(lineage_budget.SPEC, content)
        # A decision the agent divided or reformulated is checked against the original it
        # replaces before anything else: a part that adds or loses content refuses the draft
        # here, and an unresolvable lineage is refused before the Jev is called.
        if divisions is None:
            divisions = spec_groups(draft.model_dump(), earlier_specs(state))
        equivalence = (
            await check_equivalence(
                self._call, divisions, contexts["fidelity"], prior.get("equivalence")
            )
            if divisions
            else None
        )
        # The decisions the agent wrote are checked before any claim is judged: a defect
        # refuses the draft here, with no evaluation spent and no research opened.
        targets, scope = wording_targets(groups)
        wording = await check_wording(
            self._call, targets, scope, contexts["fidelity"], prior.get("wording")
        )
        # The claims the host derived are checked in the same pass, after the texts above: a
        # refusal comes first and spends no call on them. A defect here is a diagnostic for
        # the maintainer, never a refusal, and the check is not required.
        wording = await check_derivation(
            self._call,
            derived_targets(groups),
            wording,
            scope,
            contexts["fidelity"],
            prior.get("wording"),
        )
        verification: dict[str, Any] = {
            "wording": wording,
            **({"equivalence": equivalence} if equivalence else {}),
            "claims": {},
            "sources": source_refs([*context["evidence"][1:], *added]),
            "context_revisions": context_revisions,
            "evidence_revisions": evidence_revisions,
        }
        threshold = state.get("workflow_policy", {}).get("confidence", 0.8)
        for kind, claims in (("fidelity", factual), ("coverage", coverage)):
            previous = prior.get(kind, {})
            old_claims = {
                row["id"]: row["text"] for row in prior.get("claims", {}).get(kind, [])
            }
            old_rows = {row["id"]: row for row in previous.get("verdicts", [])}
            unchanged = (
                state.get("spec", {}).get("source_fingerprint")
                == source_revision(state)
                and prior.get("context_revisions", {}).get(kind)
                == verification["context_revisions"][kind]
                and prior.get("evidence_revisions", {}).get(kind)
                == verification["evidence_revisions"][kind]
            )
            cached = {
                claim.id: old_rows[claim.id]
                for claim in claims
                if unchanged
                and old_claims.get(claim.id) == claim.text
                and claim.id in old_rows
                and strong(old_rows[claim.id], threshold)
            }
            pending = [claim.model_dump() for claim in claims if claim.id not in cached]
            if pending:
                arguments = {
                    "claims": pending,
                    "evidence": evidence[kind],
                    "context": contexts[kind],
                }
                result = await self._call("jev_verify", **arguments)
                if overflow := PayloadTooLarge.from_result(result):
                    raise overflow
                rows = result.get("verdicts", [])
                if (
                    result.get("error")
                    or len(rows) != len(pending)
                    or {row.get("id") for row in rows} != {row["id"] for row in pending}
                ):
                    return None
                # What is stored advances only if this request was confirmed.
                result = confirmed_result(result, "jev_verify", arguments)
                by_id = {**cached, **{row["id"]: row for row in rows}}
                combined = {**result, "verdicts": [by_id[claim.id] for claim in claims]}
                # Kept rows count for the gate only under the authority that judged them.
                before = gate_authority(previous)
                if cached and (before is None or before != gate_authority(result)):
                    gate = result.get("gate")
                    combined.update(
                        auto_advance=False,
                        gate={
                            **(gate if isinstance(gate, dict) else {}),
                            "passed": False,
                            "reason": MIXED,
                        },
                    )
                verification[kind] = combined
            else:
                # Every claim kept its strong verdict; a claim that left the spec takes its
                # row with it, so the rows are exactly those of the claims.
                verification[kind] = {
                    **previous,
                    "verdicts": [old_rows[claim.id] for claim in claims],
                }
            verification["claims"][kind] = [claim.model_dump() for claim in claims]
        return verification

    @staticmethod
    def save_spec(state: dict[str, Any], project: Path) -> dict[str, Any]:
        from specgate.research import save_research

        if state.get("error") or "spec" not in state:
            return state
        artifact = save_research(
            {
                "schema": 1,
                "run_id": state["id"],
                "run_revision": state["revision"],
                "spec": state["spec"],
                "workflow_policy": state.get("workflow_policy", {}),
                "research_attempts": state.get("research_attempts", []),
            },
            project,
            "docs/specs",
        )
        return {**state, "local_spec": artifact}

    async def research_claim(
        self,
        run_id: str,
        claim_id: str,
        request: ResearchRequest,
        *,
        idempotency_key: str,
        authorize_jev: bool,
        repair: Callable[[dict[str, Any]], Awaitable[SpecDraft]] | None = None,
        capture: Callable[[ResearchSource], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Reserve, collect, filter and verify one claim without repeating effects."""
        state = await self.get(run_id)
        if state.get("error"):
            return state
        if not authorize_jev:
            return _review(state, "authorization_required")
        project = self.project or Path.cwd()
        spec = state.get("spec", {})
        pending = state.get("research_pending")
        if pending:
            if (
                pending["cycle_key"] != idempotency_key
                or pending["question_id"] != f"spec:{claim_id}"
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
            return self.save_spec(state, project)
        known: list[str] = []
        if not pending or pending["phase"] == "collecting":
            kind, identifier = claim_id.split(":", 1)
            claim = next(
                (
                    row
                    for row in spec.get("verification", {})
                    .get("claims", {})
                    .get(kind, [])
                    if row["id"] == identifier
                ),
                None,
            )
            if (
                claim is None
                or request.question != claim["text"]
                or request.claim != claim["text"]
            ):
                raise ValueError("A pesquisa deve corresponder à claim atual da spec.")
            # A changed source is known now; reserving would spend a cycle on it.
            restored = self._restore_research(state, project)
            if restored is None:
                return _review(state, "research_changed")
            known = [
                *(item["text"] for item in restored[0]["evidence"][1:]),
                *(row["text"] for row in restored[1]),
            ]
        if not pending:
            reserved = await self._call(
                "spec_research_begin",
                run_id=run_id,
                expected_revision=state["revision"],
                idempotency_key=idempotency_key,
                artifact_revision=spec["revision"],
                claim_id=claim_id,
                # The agent chooses research and registers why before the cycle opens.
                lineage_actions=[lineage_budget.research_action(claim_id, request.reason)],
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
                    for row in counted(state, f"spec:{claim_id}")
                    if row.get("collected")
                ],
                authorize_jev=True,
                capture=capture,
                known=known,
            )
            state = await self._record_cycle(
                "spec_research_record",
                filtered,
                artifact,
                run_id=run_id,
                expected_revision=state["revision"],
                idempotency_key=idempotency_key,
            )
            if state.get("error") or not state.get("research_pending"):
                return self.save_spec(state, project)
        spec = state["spec"]
        additions = state["spec_research"]["evidence"]
        restored = self._restore_research(state, project)
        if restored is None:
            return _review(state, "research_changed")
        context, earlier = restored
        draft = SpecDraft.model_validate(spec["draft"])
        if repair:
            draft = await repair(
                {
                    "spec": spec,
                    "claim_id": claim_id,
                    "research": additions,
                    "directive": "Corrija apenas claims alteradas pela evidência; preserve decisões e testing seams.",
                }
            )
            try:
                keeps_researched_decision(spec["draft"], draft.model_dump(), claim_id)
            except AgentRefusal as rejected:
                return {**state, **rejected.refusal()}
        summary = await self._call("grill_summary", run_id=run_id)
        if summary.get("error"):
            return summary
        try:
            verification = await self._verify_draft(
                state,
                draft,
                summary,
                context,
                [
                    *earlier,
                    *[
                        {key: row[key] for key in ("id", "source", "revision", "text")}
                        for row in additions
                    ],
                ],
            )
        except PayloadTooLarge as overflow:
            return overflow.review(state)
        except AgentRefusal as rejected:
            return {**state, **rejected.refusal()}
        if verification is None:
            return _review(state, "verification_failed")
        submitted = await self.submit(
            run_id,
            state["revision"],
            idempotency_key=f"{idempotency_key}:evaluation",
            draft=draft,
            skill_revision=spec["skill_revision"],
            vocabulary_revision=spec["vocabulary_revision"],
            verification=verification,
            research_refs=verification["sources"],
        )
        return self.save_spec(submitted, project)

    async def repair_claim(
        self,
        run_id: str,
        claim_id: str,
        *,
        repair: Callable[[dict[str, Any]], Awaitable[SpecDraft]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Repair one claim on the evidence its adjudication judged, then verify it.

        The research the cycles admitted stays in the verification context, so the
        repair is judged on what the adjudication read and on nothing else.
        """
        state = await self.get(run_id)
        if state.get("error"):
            return state
        project = self.project or Path.cwd()
        spec = state.get("spec", {})
        entry = applicable_adjudication(
            state, f"spec:{claim_id}", spec.get("revision")
        )
        if entry is None:
            return _review(state, "adjudication_unavailable")
        restored = self._restore_research(state, project)
        if restored is None:
            return _review(state, "research_changed")
        context, earlier = restored
        judged = {ref["revision"] for ref in entry["evidence_refs"]}
        draft = await repair(
            {
                "spec": spec,
                "claim_id": claim_id,
                "research": [item for item in earlier if item["revision"] in judged],
                "adjudication": entry,
                "directive": "Corrija apenas a claim julgada, com as evidências que a adjudicação examinou; preserve decisões e testing seams.",
            }
        )
        try:
            keeps_researched_decision(spec["draft"], draft.model_dump(), claim_id)
        except AgentRefusal as rejected:
            return {**state, **rejected.refusal()}
        summary = await self._call("grill_summary", run_id=run_id)
        if summary.get("error"):
            return summary
        try:
            verification = await self._verify_draft(state, draft, summary, context, earlier)
        except PayloadTooLarge as overflow:
            return overflow.review(state)
        except AgentRefusal as rejected:
            return {**state, **rejected.refusal()}
        if verification is None:
            return _review(state, "verification_failed")
        submitted = await self.submit(
            run_id,
            state["revision"],
            idempotency_key=idempotency_key,
            draft=draft,
            skill_revision=spec["skill_revision"],
            vocabulary_revision=spec["vocabulary_revision"],
            verification=verification,
            research_refs=verification["sources"],
        )
        return self.save_spec(submitted, project)

    async def research_spec(
        self,
        run_id: str,
        *,
        plan: Callable[[dict[str, Any], str], Awaitable[ResearchRequest]],
        idempotency_key: str,
        authorize_jev: bool,
        repair: Callable[[dict[str, Any]], Awaitable[SpecDraft]] | None = None,
        capture: Callable[[ResearchSource], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        from specgate.delegation import load_delegation
        from specgate.spec_claims import weak_claims

        state = await self.get(run_id)
        while not state.get("error"):
            weak = weak_claims(
                state.get("spec", {}).get("verification", {}),
                state.get("workflow_policy", {}).get("confidence", 0.8),
            )
            if not weak:
                return self.save_spec(state, self.project or Path.cwd())
            pending = state.get("research_pending")
            if pending:
                claim_id, reason = pending["question_id"].removeprefix("spec:"), None
            else:
                if "next_actions" not in state.get("spec", {}):
                    # What a write returns carries no derived actions: read them.
                    state = await self.get(run_id)
                    if state.get("error"):
                        break
                allowed = state.get("spec", {}).get("next_actions", {})
                claim_id, reason = next_cycle(state, weak, allowed)
                if reason == AGENT_ACTION:
                    # What is left is the agent's own to change, not a research to run.
                    refused = lineage_budget.not_researchable(claim_id, allowed.get(claim_id, []))
                    return {**state, **refused.refusal()}
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
            operational = state.get("research_stopped", {}).get(f"spec:{claim_id}")
            if operational in OPERATIONAL:
                state = _review(state, operational)
        if state.get("error", {}).get("code") in AGENT_CODES:
            # The agent revises its own text; there is nothing to hand to a person.
            return state
        project = self.project or Path.cwd()
        grant = load_delegation(self.home)
        handoff = {
            "schema": 1,
            "run_id": run_id,
            "spec": state.get("spec"),
            "reason": state.get("error", {}).get("code"),
            "research_attempts": state.get("research_attempts", []),
            "route": handoff_route(
                state.get("error", {}).get("code"), grant.active(project, self.url)
            ),
            "delegation_revision": grant.revision
            if grant.active(project, self.url)
            else None,
            "auto_advance": False,
        }
        artifact = save_research(handoff, project, "docs/specs/handoffs")
        return {**state, "research_handoff": {**handoff, "artifact": artifact}}

    @staticmethod
    def _matches(state: dict[str, Any], issue: IssueReference) -> bool:
        return (
            issue.repository.casefold() == state["publication"]["repository"].casefold()
            and (
                valid_reference(issue)
                if state["publication"].get("destination", "github") == "github"
                else (
                    valid_project_reference(issue, state["publication"]["destination"])
                    and issue.project == state["publication"]["tracker_project"]
                    and issue.remote_revision is not None
                    and (
                        (
                            state["publication"]["destination"] == "jira"
                            and issue.url
                            == f"{state['publication']['tracker_site']}/browse/{issue.external_id}"
                        )
                        or (
                            state["publication"]["destination"] == "beads"
                            and issue.url is None
                            and issue.external_id
                            == state["publication"]["tracker_external_id"]
                        )
                    )
                )
            )
            and issue.title == state["spec"]["title"]
            and issue.body == publication_body(state)
        )

    @staticmethod
    async def _fetch(tracker: Tracker, issue: dict[str, Any]) -> IssueReference:
        reference = IssueReference.model_validate(issue)
        if reference.comment_id is not None:
            return await tracker.get_version(
                reference.legacy_number, reference.comment_id
            )
        return await tracker.get(reference.lookup_id)

    async def publish(
        self, run_id: str, tracker: Tracker, *, idempotency_key: str
    ) -> dict[str, Any]:
        state = await self.get(run_id)
        if "error" in state:
            return state
        chosen = resolve_destination(None, None, policy=state.get("workflow_policy"))
        if getattr(tracker, "kind", "github") != chosen:
            return _review(state, "destination_adapter_missing")
        capabilities = await self._call(
            "tracker_capabilities", destination=getattr(tracker, "kind", "github")
        )
        if "error" in capabilities:
            return _review(state, capabilities["error"]["code"])
        if (
            capabilities.get("reference_contract") != 2
            or capabilities.get("destination") != chosen
            or capabilities.get("spec_versions") is not True
            or capabilities.get("publish") is not True
            or capabilities.get("reconcile") is not True
        ):
            return _review(state, "destination_adapter_missing")
        artifact = state.get("spec")
        if not artifact or artifact["status"] not in {"ready", "published"}:
            return _review(state, "spec_review_required")
        # The raw gaps stay on record; the ones the maintainer accepted do not block.
        if remaining(artifact.get("gaps", []), artifact.get("residual_acceptance")):
            return _review(state, "spec_review_required")

        def authorized() -> bool:
            return review_authority_valid(
                artifact.get("authorization", {}),
                host=self.url,
                repository=tracker.repository,
                operation="spec",
                home=self.home,
            )

        if not authorized():
            return _review(state, "publication_authorization_changed")
        if chosen == "jira" and not isinstance(tracker, JiraSpecTracker):
            return _review(state, "destination_adapter_missing")
        if chosen == "beads" and not isinstance(tracker, BeadsSpecTracker):
            return _review(state, "destination_adapter_missing")
        if isinstance(tracker, (JiraSpecTracker, BeadsSpecTracker)):
            try:
                local_capabilities = await tracker.capabilities()
                if not all(
                    local_capabilities.get(name) is True
                    for name in ("publish", "reconcile", "spec_versions")
                ):
                    return _review(state, "destination_capabilities_missing")
            except (TrackerError, OSError, ValueError):
                return _review(state, "destination_capabilities_missing")
        if isinstance(tracker, (GitHubTracker, JiraSpecTracker, BeadsSpecTracker)):
            tracker = tracker.with_authority(authorized)
        intent = state.get("publication")
        if intent and intent["repository"].casefold() != tracker.repository.casefold():
            return _review(state, "repository_mismatch")
        if intent and (
            intent.get("destination", "github") != chosen
            or intent.get("tracker_project") != getattr(tracker, "project", None)
            or intent.get("tracker_site") != getattr(tracker, "site", None)
            or intent.get("tracker_instance") != getattr(tracker, "instance", None)
        ):
            return _review(state, "tracker_project_mismatch")
        claimed = False
        if not intent or intent["artifact_revision"] != artifact["revision"]:
            state = await self._call(
                "spec_publication_begin",
                run_id=run_id,
                expected_revision=state["revision"],
                idempotency_key=idempotency_key,
                repository=tracker.repository,
                **(
                    {
                        "tracker_project": tracker.project,
                        "tracker_instance": tracker.instance,
                        "tracker_external_id": tracker.planned_id(
                            publication_body(state)
                        ),
                    }
                    if isinstance(tracker, BeadsSpecTracker)
                    else {"tracker_project": tracker.project, "tracker_site": tracker.site}
                    if isinstance(tracker, JiraSpecTracker)
                    else {}
                ),
            )
            if "error" in state:
                return state
            claimed = state.get("publication_claimed", False)
            intent = state["publication"]
        try:
            previous = intent["previous_issue"]
            if intent["status"] == "published":
                matches = [await self._fetch(tracker, intent["issue"])]
            elif previous is not None:
                if isinstance(tracker, (JiraSpecTracker, BeadsSpecTracker)):
                    current = await tracker.get(
                        IssueReference.model_validate(previous).lookup_id
                    )
                    matches = [current] if intent["marker"] in current.body else []
                else:
                    matches = await tracker.find_versions(
                        previous["number"], intent["marker"]
                    )
            else:
                matches = await tracker.find(intent["marker"])
            if len(matches) > 1:
                return _review(state, "ambiguous_publication")
            issue = matches[0] if matches else None
            if claimed and issue is None:
                if not authorized():
                    return _review(state, "publication_authorization_changed")
                if previous is not None:
                    if isinstance(tracker, (JiraSpecTracker, BeadsSpecTracker)):
                        issue = await tracker.update(
                            IssueReference.model_validate(previous),
                            artifact["title"],
                            publication_body(state),
                        )
                    else:
                        issue = await tracker.append_version(
                            previous["number"],
                            artifact["title"],
                            publication_body(state),
                        )
                else:
                    issue = await tracker.create(
                        artifact["title"], publication_body(state)
                    )
            # Missing marker after an unknown outcome is not proof of absence.
            if issue is None:
                return _review(state, "publication_unknown")
            issue = await self._fetch(tracker, issue.model_dump())
            recorded_issue = (
                IssueReference.model_validate(intent["issue"])
                if intent.get("issue")
                else None
            )
            if not self._matches(state, issue) or (
                recorded_issue is not None
                and (
                    recorded_issue.identity_key != issue.identity_key
                    or recorded_issue.id != issue.id
                    or recorded_issue.comment_id != issue.comment_id
                )
            ):
                return _review(state, "tracker_content_changed")
            if "ready-for-agent" not in issue.labels:
                if intent["status"] == "published":
                    return _review(state, "tracker_content_changed")
                if not authorized():
                    return _review(state, "publication_authorization_changed")
                await tracker.ensure_label(issue.lookup_id)
                issue = await self._fetch(tracker, issue.model_dump())
            if not self._matches(state, issue) or "ready-for-agent" not in issue.labels:
                return _review(state, "publication_unverified")
            if intent["status"] == "published" and artifact["status"] == "published":
                return state
            record_key = sha256(
                f"{idempotency_key}:record:{intent['id']}".encode()
            ).hexdigest()
            recorded = await self._call(
                "spec_publication_record",
                run_id=run_id,
                expected_revision=state["revision"],
                idempotency_key=record_key,
                intent_id=intent["id"],
                issue=issue.model_dump(),
            )
            return self.save_spec(recorded, self.project) if self.project else recorded
        except (TrackerError, OSError, ValueError):
            return _review(state, "publication_unknown")
