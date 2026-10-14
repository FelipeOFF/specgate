---
name: to-tickets-jev
description: Turn an approved specification into a validated graph of vertical tickets and publish it through project-selected trackers. Use after to-spec-jev approval, including deliveries that span multiple repositories.
---

# To Tickets with Jev

Load the installed `to-tickets` skill and preserve its decomposition guidance. It
comes from another source, not from the Specgate set. Without a manifest it is not a
public skill: routing keeps it in review until a person picks that skill and revision
(`HumanReview`); without that, `specgate flow` stops at `skill_load_unconfirmed`.
Read the approved spec revision, active decisions, domain vocabulary, repository
boundaries, and the project's chosen destinations. Never rewrite or split a decision
of the approved spec from here: only the delegated rewrite of `to-spec-jev` does,
under a delegation that includes `rewrite_decisions`, and it makes the published graph
stale.

Draft vertical behavior slices. Every ticket must:

- own one observable behavior and its acceptance criteria;
- name exactly one `owner/repository`;
- cover explicit spec scope without implementation paths or code;
- list only real ticket IDs in `blocked_by`.

Test each acceptance criterion you wrote with `jev_check` before submitting the graph
(`text` is the criterion `<ticket>:ac-N`, `scope` the approved spec, `context` the
evidence packet that leads with the approved spec). Submit when every check passed the
gate (`action=auto`) with `next_actions=["judge"]`. A check left in review
(`action=review`) clears nothing and finds no defect: do not reword a criterion just to
draw another result. Submit the graph anyway: the host keeps a criterion whose check did
not pass the gate as the gap `wording_check_review` (`<ticket>:ac-N`). A criterion is no
claim: the gap has no `next_actions`, no research and no handoff, and the graph stays in
review for a person. The check is an uncertainty filter with no
measured accuracy. Jev only judges: never ask it to write, rewrite or split a criterion.

A typed refusal (`action=agent_revision`, never `needs_human`) is yours to answer, and
only with an action the finding or the weak claim lists in `next_actions` (`tickets_get`
shows them per weak claim): `reformulate` or `split` for a wording defect
(`wording_rejected`), `new_grill_question` for a subject the approved spec does not
decide, `research` for a clean wording whose claim is `unsupported`,
`accept_contradiction` for a clean claim the evidence confidently contradicts. A wording
defect goes to reformulate or split, never to research. Register the action and the
reason in `lineage_actions` (`{target, action, reason}`, the reason not empty) on the
graph that follows a weak claim; without it the host answers `action_not_registered`,
and an action outside `next_actions` is `action_not_allowed`. A criterion refused with
`wording_rejected`, or fixed after your own test, returns by what it was. One that
replaced no stored criterion was never stored, so it is no original: write it as a new
criterion, with no `criteria_lineage`, and keep the action and the reason in the graph
record under `docs/tickets/`. Declare a reformulated or divided criterion in
`criteria_lineage` (`<ticket>:ac-M` to `{original, kind}`, `reformulation` or `split`; IDs
are positions) only when its original is a criterion of a graph already stored; otherwise
the client answers `invalid_decision_lineage` (`original_unknown`). When you resend a
refused or fixed part of a stored criterion, keep that declaration: as a new criterion the
division escapes the equivalence. Test that lineage
against the original with `jev_equivalence` and send it when the result is `accept`:
`equivalence_rejected` (a part adds, or the parts lose, something the original says; its
`next_actions` is `revise_parts`) asks you to revise the parts. An equivalence left in
review (`action=review`) accepts nothing and finds no defect: do not reword the parts to
draw another result. Send them, and the host keeps the gap `equivalence_review` for a
person. A graph identical to one already rejected is `identical_resubmission`, refused
without counting.

The obligations the server derives from the spec sentences
(`coverage:<line>:<sentence>`) are not yours to reword. A defect there shows in
`derivation_diagnostics` with `review_derivation` as the only action: pass the
diagnostic to the maintainer. `research_tickets` ends with the handoff
`derivation_review`, to `human_review` even with a delegation. Never reformulate, split
or research that claim.

Before review, use `jev_verify` to check complete scope coverage, verticality,
missing blockers, and whether the graph can ship in topological order. Reject
duplicate IDs, duplicate blockers, missing nodes, cycles, uncovered scope, or
horizontal layer tickets. Keep gaps visible and do not publish a rejected graph.

Start from the existing approved spec; ticket research does not require a new
spec synthesis. Derive atomic coverage claims from every obligation and limit in
that revision, with the exact source excerpt. Add separate verticality and
blocker claims for each ticket and a delivery-order claim. Judge proposed planning,
not completed implementation. Keep every claim, rejected ones included. Each
blocking claim needs `verified`, confidence and support strictly above the greater
of 0.80 and the frozen workflow threshold. An average cannot clear a weak claim,
with one fixed, symmetric exception: a claim whose confidence or support is within
0.05 of that cutoff, above or below, is evaluated once more by resending the
identical provider request (same claims in the same order, evidence and context),
and the mean of the two decides. Keep only the rows of the claims inside the band
as second evaluations; a claim farther away keeps its first evaluation alone, and
no claim is evaluated a third time. The host recomputes the decision from the
stored rows, requires both evaluations of a claim to carry the same
`request_revision`, and records both evaluations, the mean and the rule under the graph's
`verification.confirmation`.

Keep a verdict between two verifications only while the claim text, the evidence
(the approved spec and the whole graph, research sources included) and the context
are exactly those the earlier verification judged. A strong claim keeps its
verdict. A weak claim keeps its weak verdict and returns to `jev_verify` only
with new content in the claim, the evidence or the context, or through the symmetric
confirmation when the host cannot use its pair; it is never sent alone again.
Every claim receives the whole graph, so editing a ticket judges all of them again.
The host derives `verification.reuse` from the stored rows (`reused`, `retained`, `evaluated` with
its reason, `held`), ignores a client-provided one, and holds the graph with
`verification_reuse_mismatch` when a row judged on another input is carried onto
this one or a weak claim is judged again with nothing changed. It compares each
claim with the latest stored verification that judged it, so leaving a claim out clears
neither its verdict nor its hold. The return through the
symmetric confirmation is allowed once per input, only for a near claim without a
usable pair, as a new first evaluation with its own second one, and never after a
contradicted verdict.

Without a manifest the verification runs under the confidence policy
(`gate.basis=confidence_policy`, `calibrated=false`), and the score is an
uncertainty filter, not measured accuracy. The client confirms each evaluation's
`gate.policy_binding` against the request it sent. A result without `gate.basis`,
with a binding it did not confirm, or judged by a different authority than the other
results keeps the graph in review, never an inferred success.

For a weak claim with `research` in its `next_actions`, run `research-with-jev` and
`research-filter-jev`, repair the graph using admitted evidence, then verify the
affected planning again. Reserve through `tickets_research_begin`, with
`lineage_actions` (`research` and the reason), and persist through
`tickets_research_record`; bind both to the current graph revision. Resume an evaluating
reservation before starting another collection. The public package ships
`TicketClient.research_tickets`, which orchestrates this cycle; a harness that calls the
MCP tools directly preserves the same journal.

Allow at most three attempts per claim within the shared frozen research budget.
Stop on repeated evidence or no useful new evidence. Keep the graph unpublished
and preserve sources, hashes, scores, alternatives and remaining gaps. Under a
valid delegation, send persistent decision gaps to bounded adjudication, except a weak
claim with no action (`no_action_available`), the third rejection and the derivation
diagnostic below, which go to a person (not the research limit of a single weak claim,
below), and route operational failures to delivery exceptions. Otherwise consolidate human review.

The graph has one counter. The initial evaluation does not count; each later evaluation
with new content that ends with a weak claim is a rejection. Call the person at the
third rejection (`lineage_exhausted`): every weak claim goes to `human_review` even with
a delegation, except the research limit of a single weak claim, which bounded
adjudication still judges. With several weak claims the person may be called before the
third rejection of one of them; that deviation is accepted. The person also enters when
the global research budget ends (`research_budget_exhausted`), after the bounded adjudication under
a delegation.

An explicit continuation may grant one to three extra attempts for a named claim
through `tickets_research_extend`, with the exact graph revision, human authority
reference and new source text with its SHA-256. Never infer that grant from a score,
erase prior attempts, reduce the threshold or change global setup to continue.

A `payload_too_large` result (`error.code`, with `size` and `limit` in bytes) means the
request passed the host's 64 KB limit (or its 256 KB HTTP body), not that Jev or the
provider failed. Stop in review and show the code, the size and the limit; never retry
it unchanged, raise the limit or spend another research cycle on it. Shrink the request
(fewer or shorter sources) and resume the same idempotency key: the reservation resumes
without a new one. After admission it stays in evaluation; before it, the client has
reported the size (sizes and ids, never content) and the host keeps the reservation in
`collecting` for that key to collect again. Never collect again from a bare `collecting`
reservation: without that report the outcome is unknown.

When the automatic cycles of a claim have ended (its attempts, the shared budget
or stagnation, with or without a calibration manifest; never an operational stop, an
invalid or non-applicable manifest, a contradicting source or a cycle still reserved), the maintainer may
accept a graph whose remaining gaps are
verified claims below the threshold. `research_tickets` moves on to the next weak
claim whose cycles have not ended, so a graph with several residual claims
exhausts each one before the acceptance.
The `tickets_review` authorization carries `residual`, a list of `claim_id`,
`verdict` and `reason`. The host decides from the stored rows and the research
ledger, not from the list: each claim must be a current gap, verified on every
evaluation, already decided by the symmetric confirmation when it is near the
cutoff, and out of cycles. It refuses, and stores nothing, for a contradicted or
unsupported claim, a claim a research source contradicted at any attempt (even if
an extension erased the stop), a structural gap, a duplicate or a delegated
origin; only a human origin accepts. Review and `tickets_publication_begin` accept the graph when
every remaining gap is accepted (`verification_gaps` keeps the raw gaps), recompute
the acceptance on every read, and record under `residual_acceptance` the exact
graph revision, each claim's evaluations, mean, verdict, reason and how its cycles
ended; each publication entry keeps that record. A new graph revision starts with an empty list.
Before the publication starts, an authorized continuation (`tickets_research_extend`) reopens the
cycles and ends the acceptance: review again without the residual to resume the research. An
acceptance recorded when a host with no manifest counted the cycles as ended also ends there. Once the
publication starts the acceptance holds, whatever the host's gates become. Never lower the threshold
or accept a residual on a delegation.

Save graph revisions and readable ticket previews under `docs/tickets/` in the
project. Include acceptance criteria, owner, blockers, verification history and
research references; save research under `docs/research/`. A material graph change
requires a fresh revision-bound review under the existing valid authority.

Cross-repository blockers remain edges in the delivery graph. When combined
behavior needs independent validation, add one `integration: true` ticket that
depends on completed behavior from at least two repositories. Do not combine
implementation for multiple repositories in one ticket.

GitHub, GitLab, Jira, Beads, local documents, and a free-form workflow are
project choices handled by the authorized harness adapter. Read
`.specgate/tracker.json` (`{"destination":"github"}`). A missing file keeps
GitHub. GitLab, Jira, and Beads publish only through an injected adapter.
Local documents and the free-form workflow do not use tracker credentials.
The MCP stores the graph, origins, revisions, and public references; it does
not require tracker credentials or silently choose a destination.

Review the exact graph once. Reuse the current setup delegation or explicit
publication authority already given for this delivery. With all gates satisfied,
call MCP `tickets_review` with the graph revision and authorization containing
`decomposition_approved=true`, `publication_authorized=true`, `origin=automated`,
global `delegation_revision`, canonical `project`, approved `repositories` and
the separate authorization `reference`. The public
`specgate.delegation.publication_authority` and `verification_allows_review`
helpers prepare authority and check results; bind verification to this graph's
revision before calling. `TicketClient.review_delegated` runs the same review
through the public client. Passing gates only recommend (`execution_authorized`
stays `false`): a score never replaces the human grant to publish for this
repository, host, operation and graph revision. Without that grant, retain the
publication in the consolidated exception report. Preserve
reviewed relation capabilities. A rejected or stale graph stays unpublished.

Only missing authority or unresolved decisions need human input. Consolidate
those exceptions with research, a recommendation and the minimum alternatives;
continue independent tickets. In manual mode, present the graph for human review.

On GitHub, leave `native_dependencies` enabled. Publication adds each
`blocked_by` link and reads the dependencies back; the ticket counts as
published only when those links are present.  Tell the person the edges are
those GitHub blocked-by links.

Keep the graph revision inside the approval tool call.

Publish with a stable idempotency key. Reconcile an uncertain response before
each retry. Before each tracker mutation, use the public
`specgate.delegation.review_authority_valid` helper to recheck the reviewed
delegation against its current revision, expiry, project, host and destination.
A stale or revoked grant retains the action. Never treat review as permanent
authority. Reject ambiguous duplicate references; never create a second item
for the same ticket revision. If the project changes destination, preserve the
previous graph and its public references in history before publishing the new
revision.
