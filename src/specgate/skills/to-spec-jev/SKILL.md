---
name: to-spec-jev
description: Turn a completed grill into a researched, Jev-verified specification and reuse valid delegated publication authority. Use after grill-with-jev resolves the decision frontier.
---

# To Spec with Jev

## Happy path

1. Resume the grill run `grill-with-jev` recorded (`grill_get` with its `run_id`, at
   `grill_complete`): one grill run feeds one spec.
2. Load the installed `to-spec` skill for its template and testing seams only.
3. Research the remaining claims from authorized project sources.
4. Write the draft from this run's active decisions, label each implementation decision
   (`**slug:**`) and test it with `jev_check`.
5. Submit only through the client (`SpecClient.synthesize` or `specgate flow`).
6. Answer a typed refusal or a weak claim only with its `next_actions`.
7. Review the exact revision once, delegated or by a person.
8. Publish only through Specgate (`spec_publication_begin`, `spec_publication_record`).
9. Call `feedback_submit` once and hand off to `to-tickets-jev`.

A spec left in review with no action of yours stops at the dead end below.

## Sources and the grill run

Load the installed `to-spec` skill and use only its template and testing seams: its step
that publishes to the tracker with `ready-for-agent` does not apply here. It comes from
another source, not from the Specgate set, and synthesis needs its `<spec-template>` block,
which this skill does not carry. Without a manifest it is not a public skill: routing keeps
it in review until a person picks that skill and revision (`HumanReview`); without that,
`specgate flow` stops at `skill_load_unconfirmed`.

Record every research source and content revision. Do not use secrets, harness
transcripts, or an artifact as evidence for itself. Without an additional source, keep the
spec in human review as `research_insufficient`. Preserve public decision IDs and do not
invent answers for unresolved business questions.

The spec cites exactly the active decisions of the grill run it is recorded in
(`decision_mismatch` otherwise). A decision of another run, or a detail that never went
through the grill, is outside the sources: Jev finds it `unsupported` or `beyond_sources`.
Take each such detail to `grill_continue` on the same run (it reopens the grill and makes the
spec stale; finish it again) or list it as pending; never open another `grill_start`. Write
each decision atomic and in the proposed mode. A delivery too large for one spec
(`spec_too_large`, above 48 KB) becomes several specs, each with its own grill run.

## Wording and typed refusals

Test each implementation decision you wrote with `jev_check` before sending the draft
(`text` is the decision, `scope` the original scope with the active grill decisions,
`context` the research packet). Label each decision (`**slug:**`) so its id holds when
the draft changes. The id has three forms: the bare `slug` keys `decision_lineage` and is
its `original`; `decision-<slug>` is the target of a `wording_rejected` finding;
`fidelity:decision-<slug>` is the claim of `lineage_actions` and `spec_research_begin`.
Send the draft when every check passed the gate (`action=auto`) with
`next_actions=["judge"]`. A check left in review (`action=review`) clears nothing: do not
reword a decision to draw another result. Send the draft anyway: the client runs the same
check before `jev_verify`, and the host keeps a decision whose check did not pass the gate
as the gap `wording_check_review`. Its actions follow the recorded probabilities: a
defect above 0.80 names yours (`reformulate`, `split` or `new_grill_question`), and the
client already refuses that draft as `wording_rejected` with `gate_passed: false`, answered
as any wording defect; with none above 0.80 the only action is the maintainer's
`accept_wording_residual`; a check with no probabilities needs a new verification.
[The residual acceptance](references/residual-acceptance.md) says where its research ends.
The check is an uncertainty filter with no measured accuracy. Jev only judges: never ask it
to write, rewrite or split a decision.

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

A claim the server derives (`coverage:*`) is not yours: for `review_derivation` in
`derivation_diagnostics`, pass the diagnostic to the maintainer (`derivation_review`); read
[the derived claims](references/derived-claims.md).

## Verification and research

Submit only through the public client: `SpecClient.synthesize` (or `specgate flow`) tests the
wording, runs `jev_verify` and sends the verification it built to `spec_submit`. Never build
`verification` by hand in a direct `spec_submit` call: the host refuses an unknown shape
(`invalid_verification`). `jev_verify` judges fidelity and the coverage of the scope and of
each active grill decision. Every blocking claim needs confidence
and support strictly above the frozen workflow threshold, with a minimum of 0.80; a strong
average cannot compensate for a weak claim. Without a manifest the verdicts advance under the
confidence policy (`gate.basis=confidence_policy`, `calibrated=false`), and the score is an
uncertainty filter, not measured accuracy. The client confirms each `gate.policy_binding`
against the request and context it sent. A result without `gate.basis`, with a binding it did
not confirm, or judged by a different authority than the other results keeps the spec in
review, never an inferred success.

For a weak claim with `research` in its `next_actions`, reuse `research-with-jev`
and `research-filter-jev`. Reserve the attempt through `spec_research_begin` before
collection, bound to the exact spec revision and `fidelity:<id>` or `coverage:<id>`, with
`lineage_actions` (`research` and the reason). Record the hash-addressed filtered
packet through `spec_research_record`; only admitted independent evidence permits
reevaluation. `SpecClient.research_spec` runs this cycle and keeps strong unchanged
verdicts and all earlier results.

Allow at most three additional cycles per claim within the run's shared research
budget. Changing the draft or idempotency key does not reset the budget: the cycles of a
decision count under its id, and a repair that rewrites an unlabelled one is refused
(`decision_id_changed`). Stop on no new evidence, conflicting sources, an unreadable
calibration manifest or an uncertain external result. A host with no manifest researches
too: it admits new evidence by the confidence policy, and an installed manifest by its own
binding. Resume an existing intent before another collection. Persistent gaps go to the
delivery's bounded adjudication or exception report, except a weak claim with no action
(`no_action_available`, as a check left in review with no probabilities leaves it) or with
only the maintainer's (`wording_residual`), the third rejection and the derivation
diagnostic above, which go to a person (not the research limit of a single weak claim,
below). A `payload_too_large` result is a size, not a failure
of Jev: read [the payload limit](references/payload-limit.md).

The spec counts its rejections, and the third (`lineage_exhausted`) calls the person, even
with a delegation: read [the rejection counter](references/rejection-counter.md).

Keep the spec, source hashes, verdict history and testing seams in versionable
`docs/specs/` files and the research packets in `docs/research/`, so another harness can
resume the same revision. A weak claim stays a visible gap; a person may accept only one
still `verified` below the threshold whose cycles ended, never a contradicted or unsupported
one.

## Review

Review the exact artifact once. When the setup delegation and the instructions already
given cover the project, destination and publication, call MCP `spec_review` after fidelity
and coverage pass, with the exact revision, `seams_approved=true`,
`publication_authorized=true`, `origin=automated`, the global `delegation_revision`,
canonical `project`, approved `repositories` and the separate authorization `reference`.
Passing gates only recommend (`execution_authorized` stays `false`): a score never replaces
the human grant to publish for this repository, host, operation and exact revision.
`specgate.delegation.publication_authority` and `verification_allows_review` prepare these
fields; `SpecClient.review_delegated` runs the same review. Never manufacture a human
acceptance or overwrite an existing human rejection. In manual mode, present one
consolidated review; an explicit rejection calls `spec_reject` and returns to the
unresolved scope. A missing grant or persistent technical gap becomes one exception with
the blocked action, the research done, a recommendation and only the alternatives the
person must choose between. Continue independent work.

Never rewrite or split a decision of an approved spec on your own. Only
`SpecClient.rewrite_decisions` does it (`spec_submit` with `rewrite`), and only while a
delegation in force includes the operation `rewrite_decisions`, which the person adds with
`specgate setup --allow-rewrite`; the pull request records it under
`## Reescritas delegadas`. Read [the delegated rewrite](references/delegated-rewrite.md)
before it.

## Dead end

A `spec_review_required` with no blocking gap is no dead end: review the exact revision,
delegated or with the person's approval. When the spec stays in review with a blocking gap
and no `next_actions` of its gaps is yours (`spec_review_required` lists them), or the
destination has no spec publication (`destination_adapter_missing`: only the maintainer
clears it, with an adapter), stop there:

1. Keep the spec unpublished.
2. Report each blocking gap and what clears it, as `review_exits` in `spec_get` and
   `error.blocking` in `spec_review_required` name them: a person's residual acceptance (a
   claim still `verified` below the threshold whose cycles ended, or `accept_wording_residual`
   as `wording:decision-<slug>`), a new revision (`reformulate`, `split`), the grill of this
   run (`new_grill_question`) or a new verification (a check with no probabilities).
3. Offer only the exits inside Specgate: the person's approval with that residual
   ([the residual acceptance](references/residual-acceptance.md)), or the person's rejection
   through `spec_reject`, which reopens the grill.
4. Call `feedback_submit` with `stage=spec` and `outcome=blocked`.

Never say Jev or Specgate is down unless a tool returned a provider or transport error.

## Publication only through Specgate

Only the Specgate client publishes the spec: `SpecClient.publish` (or `specgate flow`)
reserves the intent with `spec_publication_begin`, creates the item through the authorized
tracker adapter, applies `ready-for-agent` itself and records it with
`spec_publication_record`. Never publish, edit or label the spec yourself with `gh`, `glab`,
a tracker API or CLI, and never offer or recommend that, not even when the person asks for a
way out. Before each tracker mutation the client rechecks the live delegation
(`specgate.delegation.review_authority_valid`): a stale or revoked grant retains the action.
Reuse the idempotency key to reconcile an uncertain result; never create a second item for
the same intent and revision.

## Feedback

When this flow ends, done or blocked, call `feedback_submit` once with `stage=spec`, the
`outcome` (`completed` or `blocked`) and an honest review of the tool: what worked, the
difficulties, the error codes you saw and the tools involved. Never include transcripts,
secrets, ids or paths of a harness session, or the person's words verbatim. On a host
without `feedback_submit`, go on without it. A feedback refusal never blocks the flow.
