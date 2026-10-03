---
name: grill-with-jev
description: Resolve specification questions from authorized project evidence through the Dev Decision MCP, escalating only business choices, authorizations, and persistent gaps. Use when clarifying scope before writing a spec.
---

# Grill with Jev

Collect the objective, applicable rules, current artifact, explicit alternatives,
and evidence with its source and revision. Exclude secrets, session transcripts,
and material outside the authorized project roots.

Set each evidence revision to the sha256 of that evidence text before the call.
Leave gaps, conflicts, and unexamined items empty. A complete packet reaches the
model.

Before a remote call, check existing authorization for this delivery and the
setup delegation. Reuse a valid disclosure grant covering these project sources,
the Specgate host and its provider. A new run alone does not require another
question. A new destination or wider source scope requires a new grant. When it
is absent, prepare the evidence locally and consolidate the exception with other
blocked decisions. Never send secrets or harness transcripts.

When the reason starts with `insufficient_context:` and names the failed checks,
repair those checks and retry the same question. That list is a packet repair.
It does not mean Jev is off. Do not resend the same kind of revision.

Work through the unresolved frontier one question at a time:

1. Offer stable public option IDs. A recommendation is evidence, not a forced
   answer; accept a different option when the server returns `action=auto`.
2. Call `grill_start` once, then `grill_continue` for each question. Preserve
   the returned run ID, revision, evaluation origin, context revision, and
   evidence references.
3. If the active question remains weak, use the run's frozen policy and its
   remaining `research_budget`. Build a `research-with-jev` request for that
   exact question and claim, with a broad topic catalog and authorized source
   snapshots. Choose one stable cycle key and call `grill_research_begin` with
   the current run revision **before** any collection or filtering. Only a
   response with `reservation_acquired=true` owns the external work. A replay
   (`reservation_acquired=false`) must call `grill_get` to reconcile; seeing
   `research_pending` alone does not grant ownership. If its phase is
   `collecting`, report `external_outcome_unknown` and require explicit
   recovery without recollecting, unless the cycle's latest attempt has
   `reason=payload_too_large`: that overflow report means nothing was judged, so
   the same cycle key collects again after the sources shrink. If its phase is
   `evaluating`, continue with its exact registered question. If the pending
   cycle belongs to another key, leave it in review and do not collect.

   After acquiring the reservation, save the collected packet under
   `docs/research`, then run `research-filter-jev` with every earlier packet
   collected for this question
   as `--previous`. Send the saved filtered JSON and its `{path, revision}` to
   `grill_research` with the **same cycle key** and the revision returned by
   `grill_research_begin`. A filtered packet with
   `collection_diagnostic=payload_too_large` is that overflow report (sizes and
   ids, no evidence): send it the same way and stop in review, showing the code,
   the size and the limit. If it returns `research_pending` in phase
   `evaluating`, call `grill_continue` with that exact
   `research_pending.question`, current run revision and a
   stable evaluation key. Preserve each operation's key on transport retries;
   use `grill_get` to reconcile an uncertain response, following the persisted
   phase and never repeating external work from an ambiguous `collecting`
   phase. A new cycle must add a new evidence revision. Do not paraphrase the
   same source to manufacture novelty. On a later question, copy each evidence
   item already stored with the same id, source, revision, and
   text. A new fact is a new id. Another excerpt of a file already cited does
   not replace the earlier item, and it does not reopen an answered question.
   Ask a resolved question again only when that question id is in
   `stale_questions`.
4. Stop after the initial evaluation plus at most three research cycles, within
   the run's total budget. Stop earlier for no material novelty, contradiction,
   missing authorization, an unreadable calibration manifest, unavailable service,
   or a closed evidence gate. Operational diagnostics do not trigger more research.
   A host with no manifest researches too: it admits a new source whose Screen and
   Verify pass the confidence policy (`gate.basis=confidence_policy`,
   `calibrated=false`) with the `gate.policy_binding` checked against the collected
   text, question and claim, and an installed manifest admits by its own binding.
   Each cycle shows its `basis`.
   With an active delegation, hand unresolved research to the delivery's bounded
   adjudication. Without that capability, preserve the precise blocker.
5. Reuse delegated business policy and existing authority. A fact or choice only
   the human can supply remains an exception. Continue independent work and ask
   once with a recommendation and the minimum options. Preserve human restrictions.

Treat `action=auto` with `auto_advance` as the selected answer when the server
returns it for a real decision and names its basis: `gate.basis=confidence_policy`
with `calibrated=false` (no manifest needed), or `validated_manifest` with
`calibrated=true`. The policy does this for one winning option strictly above 0.80.
The score is an uncertainty filter, not measured accuracy, and `auto_advance` only
recommends: `execution_authorized` stays `false`. `origin=automated` is that server
decision. A turn without `gate.basis`, or whose binding no longer holds, comes back
`stale` and returns to review, never to an inferred success. Mock, recorded,
abstaining, and error results stay in review. Confidence at or below 0.80 remains
unresolved.

When a pending turn includes `review_text`, show that sentence before asking
the human to confirm. It names the option Jev selected and the gate reason.
A closed gate still has an answer. Do not describe it as a missing response.
When research is exhausted or stagnant, call `GrillClient.research_handoff`
with the current state, project and host. It writes a versioned artifact under
`docs/grills/handoffs`. An active setup delegation routes it to T09 bounded
adjudication; that handoff grants no extra evaluation and resolves no question.
In manual mode show `human_fallback`: alternatives, sources, recorded confidence
and favorable excerpts. A human choice uses `grill_answer` with `origin=human`.
Operational diagnostics stay blocked; do not adjudicate them.

After an explicit human answer, call `grill_answer` with the current run ID,
revision, `pending.id` (not the question ID), and a new idempotency key for that
intent. Put the chosen option ID in `answer.selected_option` and any human
explanation in `answer.text`. Send `authorization_granted` only when the human
explicitly grants or denies authorization. In Codex Default mode, ask in chat
when the plan-only `request_user_input` tool is unavailable.

Confirm the returned turn is `resolved`, has `origin=human`, and contains the
answer before advancing. A revision increment alone is not success. If the
turn remains pending, call `grill_get` to reconcile and report the inconsistency;
do not loop over new keys or force `finish=true`. On a transport failure, retry
the identical request with its original key or reconcile with `grill_get`.

Do not interrupt the user for each automated decision. At the end, call
`grill_summary` and show a compact list containing the question, selected option,
origin, evidence sources, and remaining gaps. Keep the complete server record
available for `to-spec-jev`.

Finish only when every active question is resolved, required authorizations were
granted, and no gap or stale decision remains.
