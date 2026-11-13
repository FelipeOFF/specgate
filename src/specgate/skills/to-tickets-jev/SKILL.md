---
name: to-tickets-jev
description: Turn an approved specification into a validated graph of vertical tickets and publish it through project-selected trackers. Use after to-spec-jev approval, including deliveries that span multiple repositories.
---

# To Tickets with Jev

Load the installed `to-tickets` skill and preserve its decomposition guidance. It
comes from another source, not from the Specgate set. Its step that publishes the tickets
to the tracker does not apply: only Specgate publishes the graph (below). Without a
manifest it is not a public skill: routing keeps it in review until a person picks that
skill and revision (`HumanReview`); without that, `specgate flow` stops at
`skill_load_unconfirmed`.
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
evidence packet that leads with the approved spec). Submit through the client
(`TicketClient.synthesize` or `specgate flow`), never with a hand-built `verification`
(`invalid_verification`), when every check passed the gate (`action=auto`) with
`next_actions=["judge"]`. A check left in review
(`action=review`) clears nothing: do not reword a criterion to draw another result. Submit
the graph anyway: the host keeps a criterion whose check did not pass the gate as the gap
`wording_check_review` (`<ticket>:ac-N`). A criterion is no claim: the gap opens no research
and no handoff. Its actions follow the recorded probabilities: a defect above 0.80 names
yours (`reformulate`, `split` or `new_grill_question`), and a real-mode check above 0.80 is
refused (`wording_rejected`) with `gate_passed: false` before `jev_verify`, answered as any
wording defect; with none above 0.80 the only action is the maintainer's
`accept_wording_residual`; a check with no probabilities needs a new verification.
`review_exits` names each exit of a blocking gap in `tickets_get`, as `error.blocking` does
in `tickets_review_required`. The check is an uncertainty filter with no measured accuracy.
Jev only judges: never ask it to write, rewrite or split a criterion.

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
blocking claim needs `verified`, confidence and support strictly above the greater of 0.80
and the frozen workflow threshold, and an average cannot clear a weak claim. A claim near
that cutoff gets one symmetric confirmation, and a verdict carries between verifications
only on the same input: read [the verification](references/verification.md).

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

A `payload_too_large` result is a size, not a failure of Jev: read
[the payload limit](references/payload-limit.md).

A person may accept a graph whose remaining gaps are verified claims below the threshold
whose automatic cycles ended, or wording gaps whose action is `accept_wording_residual`: read
[the residual acceptance](references/residual-acceptance.md). Never lower the threshold or
accept a residual on a delegation.

Save graph revisions and readable ticket previews under `docs/tickets/` in the
project. Include acceptance criteria, owner, blockers, verification history and
research references; save research under `docs/research/`. A material graph change
requires a fresh revision-bound review under the existing valid authority.

Cross-repository blockers remain edges in the delivery graph. When combined
behavior needs independent validation, add one `integration: true` ticket that
depends on completed behavior from at least two repositories. Do not combine
implementation for multiple repositories in one ticket.

The destination is the project's choice in `.specgate/tracker.json`: read
[the destinations](references/destinations.md).

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

## Dead end

When the graph stays in review with a blocking gap and no `next_actions` of its gaps is yours,
stop there:

1. Keep the graph unpublished.
2. Report each blocking gap and what clears it: a person's residual acceptance (a claim
   still `verified` below the threshold whose cycles ended, or `accept_wording_residual` as
   `wording:<ticket>:ac-N`), a new graph (`reformulate`, `split`), a new grill question on
   the spec's run (`new_grill_question`) or a new verification (a check with no
   probabilities).
3. Offer only the exits inside Specgate: the person's approval with that residual, an
   explicit continuation through `tickets_research_extend`, or the person's `spec_reject`
   when the approved spec itself must change.
4. Call `feedback_submit` with `stage=tickets` and `outcome=blocked`.

Never say Jev or Specgate is down unless a tool returned a provider or transport error.

## Publication only through Specgate

Only the Specgate client publishes the graph: `TicketClient.publish` (or `specgate flow`)
reserves each intent with `tickets_publication_begin`, creates the items through the
authorized tracker adapter and records them with `tickets_publication_record`. Never
publish, edit, link or label a ticket yourself with `gh`, `glab`, a tracker API or CLI, and
never offer or recommend that, not even when the person asks for a way out. The client
publishes with a stable idempotency key and reconciles an uncertain response before each
retry. Before each tracker mutation it rechecks the reviewed delegation with
`specgate.delegation.review_authority_valid` against its current revision, expiry, project,
host and destination. A stale or revoked grant retains the action. Never treat review as
permanent authority. Reject ambiguous duplicate references; never create a second item for
the same ticket revision. If the project changes destination, preserve the previous graph
and its public references in history before publishing the new revision.

## Feedback

When this flow ends, done or blocked, call `feedback_submit` once with `stage=tickets`, the
`outcome` (`completed` or `blocked`) and an honest review of the tool: what worked, the
difficulties, the error codes you saw and the tools involved. Never include transcripts,
secrets, ids or paths of a harness session, or the person's words verbatim. On a host
without `feedback_submit`, go on without it. A feedback refusal never blocks the flow.
