---
name: grill-with-jev
description: Resolve specification questions from authorized project evidence through the Dev Decision MCP, escalating only business choices, authorizations, and persistent gaps. Use when clarifying scope before writing a spec.
---

# Grill with Jev

Collect the objective, applicable rules, current artifact, explicit alternatives,
and evidence with its source and revision. Exclude secrets, session transcripts,
and material outside the authorized project roots.

Set each evidence revision to the sha256 of that evidence text before the call.
Resolve each gap before you send the question that depends on it: investigate with the
project's tools, and when only the person knows the fact, ask it first as its own question.
Declare a gap, a conflict or an unexamined item that remains; never hide one to reach the
model. A declared item keeps Jev out of the evaluation (`context_incomplete`, with
`origin=policy`, `action=collect` and the failed checks after `insufficient_context:`) and
blocks `finish`, which answers the same `context_incomplete`; while that turn waits, no other
question opens (`pending_review`). Its pending turn carries `agent_action`: the repair is
yours, not a confirmation for the person. Do not research it, and do not answer it with
`grill_answer`, which leaves the gap in the summary. Investigate, then resend the same
question with the repaired packet through `grill_continue` (same `question.id`, keeping any
new evidence the held packet brought): a free recheck, with no research cycle and outside the
four evaluations. The same packet comes back `context_incomplete` (`action=agent_revision`)
and stores nothing. When only the person knows the fact, ask them for it and bring it as new
evidence in that resend. The resend is the first real evaluation and replaces any answer
given to the held turn: if the person already answered, confirm the result with them. This
is a packet repair, not Jev off.

Before a remote call, check existing authorization for this delivery and the
setup delegation. Reuse a valid disclosure grant covering these project sources,
the Specgate host and its provider. A new run alone does not require another
question. A new destination or wider source scope requires a new grant. When it
is absent, prepare the evidence locally and consolidate the exception with other
blocked decisions. Never send secrets or harness transcripts.

## One run per spec

Call `grill_start` once per spec and record the returned `run_id` with the grill record.
Resume it with `grill_get`; never call `grill_start` again to shrink the responses or to
start over. To add a decision after `finish`, send its question through `grill_continue` on
the same run: it reopens the grill and makes the spec stale, so finish again. The spec
cites only the active decisions of the run it is recorded in: a decision of another run,
or a detail that never went through the grill, is outside its sources (`beyond_sources`,
`unsupported`). A delivery too large for one spec (`spec_too_large`, above 48 KB) becomes
several specs, each with its own run.

## Questions

Work through the unresolved frontier one question at a time:

1. Offer stable public option IDs. A recommendation is evidence, not a forced
   answer; accept a different option when the server returns `action=auto`.
2. Test every text you wrote with `jev_check` before sending it: the question
   (`target=question`) and each option (`target=option:<id>`), with the run
   objective and the packet rules as `scope` and the question's evidence packet as
   `context`. Send it when the check passed the gate (`action=auto`) and returned
   `next_actions=["judge"]`. A check that stayed in review (`action=review`: mock, error, a
   probability at or between the cutoffs) clears nothing; do not reword a text only to draw
   another result. Send it and let the host keep the turn (`decision_check_review`). A
   person decides it, unless a real-mode probability of a wording defect is above 0.80: then
   `lineages` in `grill_get` allows `reformulate` or `split`, answered as below for
   `wording_rejected` (`outside_grill` above 0.80 stays with the person). The host runs the
   same check inside `grill_continue`, so this test only saves a refused call. The check is
   an uncertainty filter with no measured accuracy. Jev only judges: never ask it to write,
   rewrite or split a question or an option.
3. Call `grill_start` once, then `grill_continue` for each question. Preserve the returned
   run ID, revision, evaluation origin, context revision, and evidence references.
4. A typed refusal (`action=agent_revision`, never `needs_human`) is yours to answer, and
   only with an action its finding lists in `next_actions` (for a question the grill stored,
   `lineages` in `grill_get` shows the same set); `context_incomplete` lists none: repair
   the packet. For `wording_rejected` those are `reformulate` or `split` (a text that is
   compound, ambiguous, written in the present for a proposed change, or adds detail beyond
   the sources) and `new_grill_question` (a subject the grill did not decide). `research` is
   allowed only for a clean wording whose evaluation came out weak, so a wording defect goes
   to reformulate or split, never to research. You write every reformulation, part and new
   question. Register the action you chose and the reason in `lineage_action` when the host
   takes it, and otherwise in the grill record you keep for `to-spec-jev`.
   - A refused text was never stored, so it is no original, and it returns by what it was. A
     new question comes back as a new one, with no `lineage`, or opens the new grill
     question the finding asks for. A refused part of a reformulation or division you
     declared against a stored question keeps that declaration: send the corrected text with
     the same `lineage` (the stored `original`, `kind`), `lineage_action` and, for a
     division, `siblings`, through the equivalence below. Sent as a new question it comes
     back `needs_human` (`pending_review`), a call to a person for what you solve alone.
   - A question the grill already stored (`pending` or `stale`) is reformulated or split by
     declaring `lineage` on each part: follow [the lineage](references/lineage.md). Submit the
     reformulation or division to `jev_equivalence` against the original before sending it,
     and send the parts when it returns `accept`; `equivalence_rejected` asks you to
     `revise_parts`. Send the parts of an equivalence left in review unchanged: the host keeps
     the turn for a person (`decision_equivalence_review`).
   - Never resend text identical to one already rejected, under any id:
     `identical_resubmission` refuses it without counting an attempt. A missing
     `lineage_action` is `action_not_registered`, and an action outside `next_actions` is
     `action_not_allowed`: choose again inside the set.
5. If the active question remains weak with a clean wording (`research` is in its
   `next_actions`), run one research cycle under the run's frozen policy and its remaining
   `research_budget`: call `grill_research_begin` with the current run revision and
   `lineage_action` (`research` and the reason) **before** any collection or filtering, and
   follow [the research cycle](references/research-cycle.md) for the reservation, the filter
   and the evaluation. A new cycle must add a new evidence revision. Do not paraphrase the
   same source to manufacture novelty. On a later question, copy each evidence item already
   stored with the same id, source, revision, and text. A new fact is a new id. Another
   excerpt of a file already cited does not replace the earlier item, and it does not reopen
   an answered question. Ask a resolved question again only when that question id is in
   `stale_questions`, or to bring a held turn the fact it lacked (above).
6. Stop after the initial evaluation plus at most three research cycles, within the run's
   total budget, or earlier with the route
   [the research cycle](references/research-cycle.md) gives. A check that did not pass the
   gate (`decision_check_review`, `decision_equivalence_review`) and the third rejection of
   a lineage (`lineage_exhausted`) stop it too: a person decides, or the agent reformulates
   or splits when the lineage allows it, so collect nothing.
7. Reuse delegated business policy and existing authority. A fact or choice only the human
   can supply remains an exception. Continue independent work and ask once with a
   recommendation and the minimum options. Preserve human restrictions. The lineage count
   calls the person only at the third rejection of a question's lineage, counted per
   question and per declared reformulation or division (the initial evaluation does not
   count, and the lineage count alone calls no one before the third rejection). The other
   stops call the person as before: the end of the run's global research budget
   (`research_budget_exhausted`), `evaluation_limit`, `research_limit` and stagnation (after
   the bounded adjudication when a delegation is in force, at once as `human_fallback`
   without one), and a check that did not pass the gate and gives you no action.

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
When research is exhausted or stagnant, hand it off as
[the research cycle](references/research-cycle.md) says; the handoff grants no extra
evaluation and resolves no question.

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

## Codes

| Code | What to do |
| --- | --- |
| `context_incomplete` | The policy held the turn and Jev was not called: repair the packet and resend the same question (a free recheck); no other question opens meanwhile. |
| `research_cycle_required` | Jev judged the pending question: reserve a cycle with `grill_research_begin` before another evaluation. |
| `evaluation_unavailable` | Only a real provider error (`action=error`): show it; never read a policy stop as one. |
| `context_mismatch` | The packet's `objective` must be the run's objective. |
| `new_evidence_required` | Another evaluation needs material evidence not yet examined. |
| `wording_rejected` | Act only within the finding's `next_actions`: `reformulate`, `split` or `new_grill_question`. |
| `invalid_verification` | Submit the spec through the client; never build `verification` by hand. |
| `spec_review_required` | Gaps with no action of yours: the dead end of `to-spec-jev`; with no gap, review it. |
| `spec_too_large` | Split the delivery into several specs, one run each. |
| `decision_mismatch` | The spec must cite exactly the active decisions of its run. |

## Dead end

A turn that waits for the person (a check or an equivalence that did not pass the gate and
gives you no action, `lineage_exhausted`, a research stop) is a pause, not a dead end: ask once and send the
answer through `grill_answer`. The grill is at a dead end only when `finish` stays refused
(`incomplete_grill` names what blocks) and you have nothing left to send:

1. Keep the grill open: never force `finish=true` or open another run.
2. Report each item that blocks and what it needs: a fact, an authorization, an answer.
3. Offer only the exits inside Specgate: the person's answer through `grill_answer`, or the
   fact that lets the same question return.
4. End with `feedback_submit` with `stage=grill` and `outcome=blocked` (below).

Never say Jev or Specgate is down unless a tool returned a provider or transport error.

## Feedback

When the grill ends, finished or at a dead end, call `feedback_submit` once with
`stage=grill`, the `outcome` (`completed` or `blocked`) and an honest review of the tool:
what worked, the difficulties, the error codes you saw and the tools involved. A pause for
the person's answer records none. Never include transcripts, secrets, ids or paths of a
harness session, or the person's words verbatim. On a host without `feedback_submit`, go on
without it. A feedback refusal never blocks the flow.
