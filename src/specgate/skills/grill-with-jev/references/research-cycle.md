# Research cycle of a grill question

A turn the policy held (`context_incomplete`) needs no cycle: repair the packet and resend
the same question (a free recheck). A cycle is for a question Jev judged weak with a clean
wording, `research` in its `next_actions`.

Build a `research-with-jev` request for that exact question and claim, with a broad topic
catalog and authorized source snapshots. Choose one stable cycle key and call
`grill_research_begin` with the current run revision and `lineage_action` (`research` and
the reason) **before** any collection or filtering. Only a response with
`reservation_acquired=true` owns the external work. A replay
(`reservation_acquired=false`) must call `grill_get` to reconcile; seeing
`research_pending` alone does not grant ownership. If its phase is `collecting`, report
`external_outcome_unknown` and require explicit recovery without recollecting, unless the
cycle's latest attempt has `reason=payload_too_large`: that overflow report means nothing
was judged, so the same cycle key collects again after the sources shrink. If its phase is
`evaluating`, continue with its exact registered question. If the pending cycle belongs to
another key, leave it in review and do not collect.

After acquiring the reservation, save the collected packet under `docs/research`, then run
`research-filter-jev` with every earlier packet collected for this question as
`--previous`. Send the saved filtered JSON and its `{path, revision}` to `grill_research`
with the **same cycle key** and the revision returned by `grill_research_begin`. A
filtered packet with `collection_diagnostic=payload_too_large` is that overflow report
(sizes and ids, no evidence): send it the same way and stop in review, showing the code,
the size and the limit. If it returns `research_pending` in phase `evaluating`, call
`grill_continue` with that exact `research_pending.question`, current run revision and a
stable evaluation key. Preserve each operation's key on transport retries; use `grill_get`
to reconcile an uncertain response, following the persisted phase and never repeating
external work from an ambiguous `collecting` phase.

A host with no manifest researches too: it admits a new source whose Screen and Verify
pass the confidence policy (`gate.basis=confidence_policy`, `calibrated=false`) with the
`gate.policy_binding` checked against the collected text, question and claim, and an
installed manifest admits by its own binding. Each cycle shows its `basis`.

## Stops

Stop earlier than three cycles for no material novelty, contradiction, missing
authorization, an unreadable calibration manifest, unavailable service, or a closed
evidence gate. Operational diagnostics do not trigger more research. With an active
delegation, hand unresolved research to the delivery's bounded adjudication, except a
lineage at its third rejection (`lineage_exhausted`), which goes to `human_review` even with
a delegation. The research limit of a question (`research_limit`) is judged before its
lineage count, so the adjudication still takes that stop. Without that capability, preserve
the precise blocker.

## Handoff and manual mode

When research is exhausted or stagnant, call `GrillClient.research_handoff` with the current
state, project and host. It writes a versioned artifact under `docs/grills/handoffs`. An
active setup delegation routes it to T09 bounded adjudication, except the third rejection of
a lineage (`lineage_exhausted`), which stays with the person; that handoff grants no extra
evaluation and resolves no question. In manual mode show `human_fallback`: alternatives,
sources, recorded confidence and favorable excerpts. A human choice uses `grill_answer` with
`origin=human`. Operational diagnostics stay blocked; do not adjudicate them.
