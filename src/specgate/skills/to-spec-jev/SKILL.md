---
name: to-spec-jev
description: Turn a completed grill into a researched, Jev-verified specification and reuse valid delegated publication authority. Use after grill-with-jev resolves the decision frontier.
---

# To Spec with Jev

Start from the completed grill record. Load the installed `to-spec` skill and
preserve its template and testing-seam guidance. It comes from another source, not
from the Specgate set, and synthesis needs its `<spec-template>` block, which this
skill does not carry. Without a manifest it is not a public skill: routing keeps it
in review until a person picks that skill and revision (`HumanReview`); without
that, `specgate flow` stops at `skill_load_unconfirmed`.

Before synthesis, research the remaining technical and domain claims from
authorized project sources. Record every source and content revision. Do not use
secrets, harness transcripts, or an artifact as evidence for itself. If no
additional source is available, keep the spec in human review as
`research_insufficient`.

Synthesize the spec from the original scope, active decisions, domain vocabulary,
and research packet. Preserve public decision IDs and do not invent answers for
unresolved business questions.

Test each implementation decision you wrote with `jev_check` before sending the draft
(`text` is the decision, `scope` the original scope with the active grill decisions,
`context` the research packet). Label each decision (`**slug:**`) so its id holds when
the draft changes. The id has three forms: the bare `slug` keys `decision_lineage` and is
its `original`; `decision-<slug>` is the target of a `wording_rejected` finding;
`fidelity:decision-<slug>` is the claim of `lineage_actions` and `spec_research_begin`.
Send the draft when every check passed the gate (`action=auto`) with
`next_actions=["judge"]`. A check left in review (`action=review`) clears nothing and
finds no defect: do not reword a decision just to draw another result. Send the draft
anyway: the client runs the same check before `jev_verify`, and the host keeps a decision
whose check did not pass the gate as the gap `wording_check_review`, with no
`next_actions`, for a person. When the `fidelity:decision-<slug>` claim of that decision
is also weak, the research ends in `no_action_available` (to `human_review` even with a
delegation); when it is strong, the gap stays on the spec, the research ends with no
handoff and the spec stays in review for a person. The check is an uncertainty filter
with no measured accuracy. Jev only judges: never ask it to write, rewrite or split a
decision.

A typed refusal (`action=agent_revision`, never `needs_human`) is yours to answer, and
only with an action the finding or the weak claim lists in `next_actions` (`spec_get`
shows them per weak claim): `reformulate` or `split` for a wording defect
(`wording_rejected`), `new_grill_question` for a subject the grill did not decide (go
back to `grill-with-jev`), `research` for a clean wording whose claim is `unsupported`,
`accept_contradiction` for a clean claim the evidence confidently contradicts. A wording
defect goes to reformulate or split, never to research. Register the action and the
reason in `lineage_actions` (`{target, action, reason}`, the reason not empty) on the
revision that follows a weak claim; without it the host answers `action_not_registered`,
and an action outside `next_actions` is `action_not_allowed`. A decision refused with
`wording_rejected`, or fixed after your own test, returns in one of two ways. If it
replaced no stored decision, it was never stored, so it is no original: write the
corrected text as a new decision, with no `decision_lineage`, and keep the action and the
reason in the spec record under `docs/specs/`. If it was a part or a reformulation of a
decision already stored (the repair of a weak claim, or the delegated rewrite below), keep
the same `decision_lineage` with the stored decision as `original` and resend only the
corrected text of that part. Sent as a new decision, a rewrite is refused
(`rewrite_out_of_scope`), so is a repair that renames the decision under research
(`decision_id_changed`), and a division escapes the equivalence. Declare a reformulated or
divided decision in `decision_lineage` (`{original, kind}` for each part, `reformulation`
or `split`) only when its original is a decision of a revision already stored; otherwise
the client answers `invalid_decision_lineage` (`original_unknown`). Test that lineage
against the original with `jev_equivalence` and send it when the result is `accept`:
`equivalence_rejected` (a part adds, or the parts lose, something the original says; its
`next_actions` is `revise_parts`) asks you to revise the parts. An equivalence left in
review (`action=review`) accepts nothing and finds no defect: do not reword the parts to
draw another result. Send them: the host keeps them as the gap `equivalence_review` for a
person, and a delegated rewrite is refused (`rewrite_equivalence_unverified`). A draft
identical to a revision already rejected is `identical_resubmission`, refused without
counting.

The claims the server derives (`coverage:scope`, `coverage:<turn>`) are not yours to
reword. A defect there shows in `derivation_diagnostics` with `review_derivation` as the
only action: pass the diagnostic to the maintainer. The research loop ends with the
handoff `derivation_review`, to `human_review` even with a delegation. Never
reformulate, split or research that claim.

Use `jev_verify` to check the problem, solution, and implementation decisions
against the original scope, active grill decisions, and research. Preserve each
verdict and its evidence revision. Check coverage of the original scope and every
active grill decision separately. Every blocking claim needs confidence and
support strictly above the frozen workflow threshold, with a minimum of 0.80.
A strong average cannot compensate for a weak claim. Without a manifest the
verdicts advance under the confidence policy (`gate.basis=confidence_policy`,
`calibrated=false`), and the score is an uncertainty filter, not measured accuracy.
The client confirms each `gate.policy_binding` against the request and context it
sent. A result without `gate.basis`, with a binding it did not confirm, or judged by
a different authority than the other results keeps the spec in review, never an
inferred success.

For a weak claim with `research` in its `next_actions`, reuse `research-with-jev`
and `research-filter-jev`. Reserve the attempt through `spec_research_begin` before
collection, bound to the exact spec revision and `fidelity:<id>` or `coverage:<id>`, with
`lineage_actions` (`research` and the reason). Record the hash-addressed filtered
packet through `spec_research_record`; only admitted independent evidence permits
reevaluation. The public package ships `SpecClient.research_spec`, which takes a
research planner and an optional repair callback. Reevaluate weak or materially
changed claims, retaining strong unchanged verdicts and all earlier results.

Allow at most three additional cycles per claim within the run's shared research
budget. Changing the draft or idempotency key does not reset the budget: the cycles of a
decision count under its id, so label each implementation decision (`**slug:**`), because a repair
that rewrites an unlabelled one is refused (`decision_id_changed`). Stop on
no new evidence, conflicting sources, an unreadable calibration manifest or an
uncertain external result. A host with no manifest researches too: it admits new
evidence by the confidence policy, and an installed manifest by its own binding.
Resume an existing intent before attempting another collection.
Persistent gaps go to the delivery's bounded adjudication or exception report,
except a weak claim with no action (`no_action_available`, as a check left in review
leaves it), the third rejection and the derivation diagnostic below, which go to a person (not the research limit of a single
weak claim, below); include alternatives, sources, scores and reasons without asking
repeatedly.

The spec has one counter. The initial evaluation does not count; each later evaluation
with new content that ends with a weak claim is a rejection. Call the person at the
third rejection (`lineage_exhausted`): every weak claim goes to `human_review` even with
a delegation, except the research limit of a single weak claim, which bounded
adjudication still judges. With several weak claims the person may be called before the
third rejection of one of them; that deviation is accepted. The person also enters when
the global research budget ends (`research_budget_exhausted`), after the bounded adjudication under
a delegation.

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

Keep the spec, source hashes, verdict history and testing seams in versionable
`docs/specs/` files inside the project. Collection and filter packets stay in
`docs/research/`. These records allow another harness to resume the same revision.
Contradicted, unsupported or below-threshold claims remain visible gaps and keep
publication in review, unless a person accepts the residual (below).

Review the exact artifact once. Read the current setup delegation and explicit
instructions already given for this delivery. When they cover the project,
destination and publication, call MCP `spec_review` after fidelity and coverage
gates pass. Send the exact artifact revision and authorization with
`seams_approved=true`, `publication_authorized=true`, `origin=automated`, the
global `delegation_revision`, canonical `project`, approved `repositories` and
the separate authorization `reference`. Passing gates only recommend
(`execution_authorized` stays `false`): a score never replaces the human grant to
publish for this repository, host, operation and exact revision. The public client
exposes `specgate.delegation.publication_authority` and `verification_allows_review`
to prepare these fields and check gates; `SpecClient.review_delegated` runs the same
review through the public client.
Never manufacture a human acceptance or overwrite an existing human rejection.

Never rewrite or split a decision of an approved spec on your own. Only
`SpecClient.rewrite_decisions` does it (`spec_submit` with `rewrite`), and only while a
delegation in force includes the operation `rewrite_decisions`, which no publication,
score or delegated approval grants: the person adds it with
`specgate setup --allow-rewrite`. The client reads that grant with
`specgate.delegation.rewrite_authority` before asking for any text and again right
before writing (`rewrite_not_authorized` when it is expired, revoked, edited or lacks
the operation). The host writes only if each original's equivalence passed the gate and
only the implementation decisions and their `decision_lineage` changed
(`equivalence_rejected`, `rewrite_equivalence_unverified`, `rewrite_out_of_scope`). The
rewrite approves nothing: the revision returns to review, the ticket graph becomes
stale, and the pull request records the rewrite with the original decision
(`## Reescritas delegadas`). Without the grant the defective decision goes to the
person as an exception; an ordinary synthesis over an approved spec is no way around
it.

A missing grant or persistent technical gap becomes one exception in the delivery
report. Include the blocked action, research already done, recommendation and
only the alternatives the human must choose between. Continue independent work.
In manual mode, present a consolidated review; an explicit rejection calls
`spec_reject` and returns to the unresolved scope. Do not publish a rejected spec.

The consolidated review may also record a human residual. When the automatic cycles
of a claim have ended (its attempts, the shared budget or stagnation; never an
operational stop, a contradicting source or a cycle still reserved),
the maintainer may accept a spec whose remaining gaps are claims still `verified`
below the threshold. `SpecClient.research_spec` moves on to the next weak claim
whose cycles have not ended, so a spec with several residual claims exhausts each
one before the acceptance. The `spec_review` authorization carries `residual`, a list
of `claim_id` (`fidelity:<id>` or `coverage:<id>`), `verdict` and `reason`. The host
decides from the stored rows and the research ledger, not from the list: each claim
must be a current gap, verified, and out of cycles. It refuses, and stores nothing,
for a contradicted or unsupported claim, a claim a research source contradicted, a
structural gap, a duplicate or a delegated origin; only a human origin accepts, so
`SpecClient.review_delegated` never does. The threshold and the support criterion do
not change: `gaps` keeps listing the claim, and the spec is `ready` only when an
acceptance that still holds covers every gap. The host records under
`residual_acceptance` the exact spec revision, each claim's evaluation, verdict,
reason and how its cycles ended; the publication intent keeps that record. A new spec
revision starts with an empty list, and a research cycle that reopens before the
publication starts ends the acceptance. Once the publication starts it holds.

Publication uses only the tracker and credentials already authorized in the
harness. Reuse the same idempotency key while reconciling an uncertain result.
Immediately before each tracker mutation, recheck the live global delegation,
its revision, expiry, project, host and destination with
`specgate.delegation.review_authority_valid`. A stale or revoked grant retains
the action. An old review never authorizes a different destination.
Never create a second artifact or tracker item for the same intent and revision.
