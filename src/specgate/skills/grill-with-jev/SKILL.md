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

When the reason starts with `insufficient_context:` and names the failed checks,
repair those checks and retry the same question. That list is a packet repair.
It does not mean Jev is off. Do not resend the same kind of revision.

Work through the unresolved frontier one question at a time:

1. Offer stable public option IDs. A recommendation is evidence, not a forced
   answer; accept a different option when the server returns `action=auto`.
2. Call `grill_start` once, then `grill_continue` for each question. Preserve
   the returned run ID, revision, evaluation origin, context revision, and
   evidence references.
3. If an evaluation requests more context, collect material evidence and retry
   the same question ID. A retry must add a new evidence revision. Do not
   paraphrase the same source to manufacture novelty. On a later question, copy
   each evidence item already stored with the same id, source, revision, and
   text. A new fact is a new id. Another excerpt of a file already cited does
   not replace the earlier item, and it does not reopen an answered question.
   Ask a resolved question again only when that question id is in
   `stale_questions`.
4. Stop after three evaluations of one question. A stalled retry or unresolved
   third evaluation stays in human review.
5. Send business policy choices, authorizations, personal facts, ambiguous
   options, and persistent gaps to the human. Never weaken a declared human
   restriction to obtain an automatic choice.

Treat `action=auto` with `auto_advance` as the selected answer when the server
returns it for a real decision. The confidence policy does this for one winning
option strictly above 0.80, with or without a calibration manifest.
`origin=automated` is that server decision. Mock, recorded, abstaining, and
error results stay in review. Confidence at or below 0.80 stays with the human.

When a pending turn includes `review_text`, show that sentence before asking
the human to confirm. It names the option Jev selected and the gate reason.
A closed gate still has an answer. Do not describe it as a missing response.

Do not interrupt the user for each automated decision. At the end, call
`grill_summary` and show a compact list containing the question, selected option,
origin, evidence sources, and remaining gaps. Keep the complete server record
available for `to-spec-jev`.

Finish only when every active question is resolved, required authorizations were
granted, and no gap or stale decision remains.
