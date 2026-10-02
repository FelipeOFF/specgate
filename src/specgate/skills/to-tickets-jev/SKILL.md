---
name: to-tickets-jev
description: Turn an approved specification into a validated graph of vertical tickets and publish it through project-selected trackers. Use after to-spec-jev approval, including deliveries that span multiple repositories.
---

# To Tickets with Jev

Load the installed `to-tickets` skill and preserve its decomposition guidance.
Read the approved spec revision, active decisions, domain vocabulary, repository
boundaries, and the project's chosen destinations.

Draft vertical behavior slices. Every ticket must:

- own one observable behavior and its acceptance criteria;
- name exactly one `owner/repository`;
- cover explicit spec scope without implementation paths or code;
- list only real ticket IDs in `blocked_by`.

Before review, use `jev_verify` to check complete scope coverage, verticality,
missing blockers, and whether the graph can ship in topological order. Reject
duplicate IDs, duplicate blockers, missing nodes, cycles, uncovered scope, or
horizontal layer tickets. Keep gaps visible and do not publish a rejected graph.

Start from the existing approved spec; ticket research does not require a new
spec synthesis. Derive atomic coverage claims from every obligation and limit in
that revision, with the exact source excerpt. Add separate verticality and
blocker claims for each ticket and a delivery-order claim. Judge proposed planning,
not completed implementation. Keep every claim, including rejected ones. Each
blocking claim needs `verified`, confidence and support strictly above the greater
of 0.80 and the frozen workflow threshold. An average cannot clear a weak claim,
with one fixed, symmetric exception: a claim whose confidence or support is within
0.05 of that cutoff, above or below, is evaluated once more by resending the
identical provider request (same claims in the same order, evidence and context),
and the mean of the two decides. Keep only the rows of the claims inside the band
as second evaluations; a claim farther away keeps its first evaluation alone, and
no claim is evaluated a third time. The host recomputes the decision from the
stored rows, requires both evaluations of a claim to carry the same
`request_revision`, and records both evaluations, the mean and the rule under the
graph's `verification.confirmation`; it ignores any client-provided mean.

Keep a verdict between two verifications only while the claim text, the evidence
(the approved spec and the whole graph, research sources included) and the context
are exactly those the earlier verification judged. A strong claim keeps its
verdict. A weak claim keeps its weak verdict and is never sent to `jev_verify`
alone again: it returns only with new content in the claim, the evidence or the
context, or through the symmetric confirmation when the host cannot use its pair.
Every claim receives the whole graph, so editing a ticket changes the evidence of
every claim and all of them are judged again. The host derives the record under
`verification.reuse` from the stored rows (`reused`, `retained`, `evaluated` with
its reason, `held`), ignores a client-provided one, and holds the graph with
`verification_reuse_mismatch` when a row judged on another input is carried onto
this one or a weak claim is judged again with nothing changed. It compares each
claim with the latest stored verification that judged it, so a submission that
leaves a claim out does not clear its verdict or its hold. The return through the
symmetric confirmation is allowed once per input, only for a near claim without a
usable pair, as a new first evaluation with its own second one, and never after a
contradicted verdict.

For a weak claim, run `research-with-jev` and `research-filter-jev`, repair the graph
using admitted evidence, then verify the affected planning again. Reserve through
`tickets_research_begin` and persist through `tickets_research_record`; bind both
to the current graph revision. Resume an evaluating reservation before starting
another collection. The private `TicketClient.research_tickets` orchestrates this
cycle. Public harnesses call the MCP tools and preserve the same journal.

Allow at most three attempts per claim within the shared frozen research budget.
Stop on repeated evidence or no useful new evidence. Keep the graph unpublished
and preserve sources, hashes, scores, alternatives and remaining gaps. Under a
valid delegation, send persistent decision gaps to bounded adjudication and route
operational failures to delivery exceptions. Otherwise consolidate human review.
An explicit continuation may grant one to three extra attempts for a named claim
through `tickets_research_extend`, with the exact graph revision, human authority
reference and new source text with its SHA-256. Never infer that grant from a score,
erase prior attempts, reduce the threshold or change global setup to continue.

A `payload_too_large` result (`error.code`, with `size` and `limit` in bytes) means the
request passed the host's 64 KB limit (or its 256 KB HTTP body), not that Jev or the
provider failed. Stop in review and show the code, the size and the limit; never retry
it unchanged, raise the limit or spend another research cycle on it. Shrink the request
(fewer or shorter sources) and resume the same idempotency key: a reservation already
made resumes without a new one. After admission it stays in evaluation; before it, the
client has already reported the size to the host (sizes and ids, never content), which
keeps the reservation in `collecting` for that same key to collect again. Never collect
again from a bare `collecting` reservation: without that report it is an unknown
outcome.

When the automatic cycles of a claim have ended (its attempts, the shared budget
or stagnation; the research runs with or without a calibration manifest, so an absent
manifest ends nothing; never an operational stop, an invalid or non-applicable
manifest, a contradicting source or a cycle still reserved), the maintainer may
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
research references; save research under `docs/research/`. Reuse stored verdicts
only while claim text, complete evidence and context remain identical. A material graph
change requires a fresh revision-bound review under the existing valid authority.

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
through the public client. Preserve
reviewed relation capabilities. A rejected or stale graph stays unpublished.

Only missing authority or unresolved decisions need human input. Consolidate
those exceptions with research, a recommendation and the minimum alternatives;
continue independent tickets. In manual mode, present the graph for human review.

On GitHub, leave `native_dependencies` enabled. Publication adds each
`blocked_by` link and reads the dependencies back; the ticket counts as
published only when those links are present. Tell the person the edges are
those GitHub blocked-by links. The issue body may repeat the same edges as a
reading list.

Keep the graph revision inside the approval tool call. Preserve the graph locally
so the developer can inspect it without a blocking question.

Publish with a stable idempotency key. Reconcile an uncertain response before
each retry. Before each tracker mutation, use the public
`specgate.delegation.review_authority_valid` helper to recheck the reviewed
delegation against its current revision, expiry, project, host and destination.
A stale or revoked grant retains the action. Never treat review as permanent
authority. Reject ambiguous duplicate references and never create a second item
for the same ticket revision. If the project changes destination, preserve the
previous graph and its public references in history before publishing the new
revision.
